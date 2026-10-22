import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { prepareQmdEmbeddingReferenceOutput } from "../../src/worker-adapters/qmd-embedding-reference.js";
import type { RuntimeJob, RuntimeJobAttempt } from "../../src/types/runtime.js";

const tempDirs: string[] = [];

afterEach(async () => {
  await Promise.all(tempDirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

function makeJob(): RuntimeJob {
  const now = new Date().toISOString();
  return {
    jobId: "job-qmd-1",
    producerSystem: "qmd",
    producerRef: "qmd-embed:2026-04-12T141300Z:shard-0003",
    resultOwner: "qmd",
    resultRoute: "qmd.embedding.import.v1",
    workloadType: "qmd_embedding",
    queueClass: "foreground",
    executionProfile: "embed_batch",
    executionShape: "remote_async_artifact",
    submissionMode: "explicit_batch",
    priority: 50,
    inputItemCount: 12,
    status: "leased",
    runnableAt: now,
    deadlineAt: null,
    idempotencyKey: "qmd-embedding:test",
    effectKey: null,
    attemptCount: 1,
    maxAttempts: 3,
    leaseOwner: "worker-a",
    leaseToken: "lease-1",
    leaseExpiresAt: now,
    inputJson: {
      priorityClass: "interactive_freshness",
      model: "BAAI/bge-base-en-v1.5",
      uniqueInputCount: 12,
      artifactBundleVersion: "qmd-embedding-bundle-v1",
      selectionPolicyVersion: "qmd-hot-embed-v1",
      inputManifestSha256: "sha256:test-input",
      modelRevision: "bge-base-en-v1.5-onnx-2026-04-01",
      dimensions: 768,
    },
    inputArtifactId: "11111111-1111-4111-8111-111111111111",
    outputContractJson: null,
    terminalResultId: null,
    lastErrorClass: null,
    lastErrorText: null,
    createdAt: now,
    updatedAt: now,
  };
}

function makeAttempt(): RuntimeJobAttempt {
  const now = new Date().toISOString();
  return {
    attemptId: "attempt-qmd-1",
    jobId: "job-qmd-1",
    attemptNo: 1,
    workerId: "worker-a",
    executionProfile: "embed_batch",
    leaseToken: "lease-1",
    leaseExpiresAt: now,
    status: "running",
    startedAt: now,
    heartbeatAt: now,
    completedAt: null,
    errorClass: null,
    errorText: null,
    telemetryJson: null,
    providerReceiptJson: null,
    probeSummaryJson: null,
    claimJson: null,
    attemptOutputArtifactId: null,
    createdAt: now,
    updatedAt: now,
  };
}

describe("qmd embedding reference output", () => {
  it("writes a reference output manifest and returns artifact-ready result metadata", async () => {
    const dir = await mkdtemp(join(tmpdir(), "runtime-controller-qmd-ref-"));
    tempDirs.push(dir);

    const prepared = await prepareQmdEmbeddingReferenceOutput(
      makeJob(),
      makeAttempt(),
      "worker-a",
      dir,
      new Date("2026-04-12T14:18:02Z"),
    );

    expect(prepared.resultJson.status).toBe("artifact_ready");
    expect(prepared.resultJson.runId).toBe("2026-04-12T141300Z-shard-0003");
    expect(prepared.artifactRequest.artifactKind).toBe("qmd_embedding_output_manifest_json");
    expect(prepared.artifactRequest.storageUri.startsWith("file://")).toBe(true);

    const outputPath = prepared.artifactRequest.storageUri.slice("file://".length);
    const manifestText = await readFile(outputPath, "utf8");
    expect(manifestText).toContain("reference_stub");
    expect(manifestText).toContain("qmd_embedding_reference_worker");
  });
});
