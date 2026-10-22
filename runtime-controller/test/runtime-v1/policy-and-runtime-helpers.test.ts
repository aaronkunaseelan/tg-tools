import { describe, expect, it } from "vitest";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import {
  isClaimableJob,
  normalizeQueueClassFilters,
  normalizeTextFilters,
  passesSoftInFlightCap,
} from "../../src/runtime/claim.js";
import { abandonWouldRetry } from "../../src/runtime/abandon.js";
import { createsTerminalResult, jobStatusFromFinalizeOutcome } from "../../src/runtime/finalize.js";
import { canCancelBeforeStart, shouldRetryExpiredLease } from "../../src/runtime/reconcile.js";
import { deriveResultAckState } from "../../src/runtime/results.js";
import { validateSubmissionRequest } from "../../src/runtime/submit.js";
import { describeWorkerAvailability } from "../../src/runtime/workers.js";

describe("policy and runtime helpers", () => {
  it("resolves the embeddings policy", () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "embeddings");
    expect(policy).not.toBeNull();
    expect(policy?.submissionMode).toBe("explicit_batch");
    expect(policy?.admissionAction).toBe("defer");
  });

  it("resolves the qmd embedding policy", () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "qmd_embedding");
    expect(policy).not.toBeNull();
    expect(policy?.submissionMode).toBe("explicit_batch");
    expect(policy?.admissionAction).toBe("admit_now");
    expect(policy?.maxItems).toBe(4096);
  });

  it("validates explicit batch inputs", () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "embeddings");
    expect(policy).not.toBeNull();

    const invalid = validateSubmissionRequest({
      producerSystem: "embed",
      producerRef: "doc:1",
      resultOwner: "embed-owner",
      resultRoute: "embed.route",
      workloadType: "embeddings",
      queueClass: "background",
      executionProfile: "gpu_batch",
      executionShape: "remote_async_artifact",
      idempotencyKey: "embed:1",
    }, policy!);

    expect(invalid.ok).toBe(false);
    expect(invalid.errors[0]).toMatch(/explicit_batch/);

    const valid = validateSubmissionRequest({
      producerSystem: "embed",
      producerRef: "doc:1",
      resultOwner: "embed-owner",
      resultRoute: "embed.route",
      workloadType: "embeddings",
      queueClass: "background",
      executionProfile: "gpu_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 32,
      inputBytes: 1024,
      idempotencyKey: "embed:1",
    }, policy!);

    expect(valid.ok).toBe(true);
    expect(valid.submissionMode).toBe("explicit_batch");
  });

  it("enforces the qmd embedding submit contract", () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "qmd_embedding");
    expect(policy).not.toBeNull();

    const invalid = validateSubmissionRequest({
      producerSystem: "qmd",
      producerRef: "qmd-embed:run-1:shard-1",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "foreground",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 8,
      inputJson: {
        priorityClass: "interactive_freshness",
        model: "BAAI/bge-base-en-v1.5",
        uniqueInputCount: 9,
      },
      idempotencyKey: "qmd-embed:run-1:shard-1",
    }, policy!);

    expect(invalid.ok).toBe(false);
    expect(invalid.errors).toContain("qmd_embedding requires inputArtifactId");
    expect(invalid.errors).toContain("qmd_embedding requires outputContractJson");
    expect(invalid.errors).toContain("qmd_embedding inputJson.artifactBundleVersion is required");
    expect(invalid.errors).toContain("qmd_embedding inputJson.uniqueInputCount must equal inputItemCount");

    const valid = validateSubmissionRequest({
      producerSystem: "qmd",
      producerRef: "qmd-embed:run-1:shard-1",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "foreground",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 8,
      inputArtifactId: "11111111-1111-4111-8111-111111111111",
      inputJson: {
        priorityClass: "interactive_freshness",
        model: "BAAI/bge-base-en-v1.5",
        uniqueInputCount: 8,
        artifactBundleVersion: "qmd-embedding-bundle-v1",
      },
      outputContractJson: {
        resultRoute: "qmd.embedding.import.v1",
        requiredArtifacts: [
          "qmd_embedding_output_manifest_json",
          "qmd_embedding_vectors_parquet",
        ],
      },
      idempotencyKey: "qmd-embed:run-1:shard-1",
    }, policy!);

    expect(valid.ok).toBe(true);
  });

  it("detects claimable work correctly", () => {
    const now = new Date("2026-04-11T12:00:00Z");
    expect(isClaimableJob({
      status: "pending",
      runnableAt: new Date("2026-04-11T11:59:00Z"),
      deadlineAt: new Date("2026-04-11T12:30:00Z"),
      executionProfile: "cpu_small",
      queueClass: "foreground",
      producerSystem: "scorer",
    }, now)).toBe(true);

    expect(isClaimableJob({
      status: "retryable",
      runnableAt: new Date("2026-04-11T12:01:00Z"),
      deadlineAt: null,
      executionProfile: "cpu_small",
      queueClass: "foreground",
      producerSystem: "scorer",
    }, now)).toBe(false);
  });

  it("keeps finalize and reconcile rules aligned", () => {
    expect(jobStatusFromFinalizeOutcome("succeeded")).toBe("succeeded");
    expect(jobStatusFromFinalizeOutcome("retryable_error")).toBe("retryable");
    expect(createsTerminalResult("terminal_error")).toBe(true);
    expect(createsTerminalResult("retryable_error")).toBe(false);
    expect(shouldRetryExpiredLease({ attemptCount: 1, maxAttempts: 3, deadlineAt: null })).toBe(true);
    expect(shouldRetryExpiredLease({ attemptCount: 3, maxAttempts: 3, deadlineAt: null })).toBe(false);
    expect(abandonWouldRetry({ attemptCount: 1, maxAttempts: 3, deadlineAt: null })).toBe(true);
    expect(abandonWouldRetry({ attemptCount: 3, maxAttempts: 3, deadlineAt: null })).toBe(false);
    expect(canCancelBeforeStart("pending")).toBe(true);
    expect(canCancelBeforeStart("leased")).toBe(false);
  });

  it("derives result acknowledgement state and soft cap behavior", () => {
    expect(deriveResultAckState({ claimedAt: null, consumedAt: null })).toBe("ready");
    expect(deriveResultAckState({ claimedAt: "2026-04-11T12:00:00Z", consumedAt: null })).toBe("claimed");
    expect(deriveResultAckState({ claimedAt: "2026-04-11T12:00:00Z", consumedAt: "2026-04-11T12:01:00Z" })).toBe("consumed");
    expect(passesSoftInFlightCap(1, 2)).toBe(true);
    expect(passesSoftInFlightCap(2, 2)).toBe(false);
  });

  it("derives fresh and stale worker availability from controlled timestamps", () => {
    const now = new Date("2026-04-11T12:02:00Z");
    const worker = {
      workerId: "worker-heartbeat",
      venue: "generic" as const,
      state: "active" as const,
      supportedExecutionProfiles: ["cpu_small"],
      supportedWorkloadTypes: [],
      allowedProducerSystems: [],
      allowedQueueClasses: [],
      queuePreference: [],
      capacity: 2,
      metadataJson: {},
      lastHeartbeatAt: "2026-04-11T12:01:00Z",
      lastClaimAt: null,
      drainRequestedAt: null,
      drainDeadlineAt: null,
      drainReason: null,
      createdAt: "2026-04-11T12:00:00Z",
      updatedAt: "2026-04-11T12:01:00Z",
    };

    expect(describeWorkerAvailability(worker, 1, { now, heartbeatTtlSec: 120 })).toMatchObject({
      heartbeatAgeSec: 60,
      heartbeatFresh: true,
      availableCapacity: 1,
      available: true,
    });
    expect(describeWorkerAvailability({ ...worker, lastHeartbeatAt: "2026-04-11T11:59:59Z" }, 1, {
      now,
      heartbeatTtlSec: 120,
    })).toMatchObject({
      heartbeatAgeSec: 121,
      heartbeatFresh: false,
      availableCapacity: 0,
      available: false,
    });
  });

  it("normalizes optional claim filters", () => {
    expect(normalizeTextFilters(undefined, "workloadTypes")).toBeNull();
    expect(normalizeTextFilters([" scorer ", "scorer", "transcription"], "workloadTypes")).toEqual([
      "scorer",
      "transcription",
    ]);
    expect(() => normalizeTextFilters(["   "], "workloadTypes")).toThrow(/workloadTypes/);
  });

  it("normalizes optional queue-class filters", () => {
    expect(normalizeQueueClassFilters(undefined, "allowedQueueClasses")).toBeNull();
    expect(normalizeQueueClassFilters([" foreground ", "foreground", "background"], "allowedQueueClasses")).toEqual([
      "foreground",
      "background",
    ]);
    expect(() => normalizeQueueClassFilters(["interactive"], "allowedQueueClasses")).toThrow(/allowedQueueClasses/);
  });
});
