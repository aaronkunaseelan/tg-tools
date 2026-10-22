import { describe, expect, it } from "vitest";
import { executeJobViaAdapter } from "../../src/worker-adapters/index.js";
import type { RuntimeJob } from "../../src/types/runtime.js";

function makeJob(overrides: Partial<RuntimeJob>): RuntimeJob {
  return {
    jobId: "job-1",
    producerSystem: "test",
    producerRef: "ref-1",
    resultOwner: "owner",
    resultRoute: "owner.route",
    workloadType: "scorer",
    queueClass: "foreground",
    executionProfile: "cpu_small",
    executionShape: "local_bounded",
    submissionMode: "singleton",
    priority: 50,
    inputItemCount: null,
    status: "pending",
    runnableAt: new Date().toISOString(),
    deadlineAt: null,
    idempotencyKey: "idempotency-1", // gitleaks:allow (synthetic test key, not a credential)
    effectKey: null,
    attemptCount: 0,
    maxAttempts: 3,
    leaseOwner: null,
    leaseToken: null,
    leaseExpiresAt: null,
    inputJson: null,
    inputArtifactId: null,
    outputContractJson: null,
    terminalResultId: null,
    lastErrorClass: null,
    lastErrorText: null,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  };
}

describe("worker adapters", () => {
  it("handles scorer jobs", () => {
    const outcome = executeJobViaAdapter(makeJob({
      workloadType: "scorer",
      inputJson: { text: "please score this candidate" },
    }));
    expect(outcome.adapter).toBe("scorer");
    expect(outcome.outcome).toBe("succeeded");
    expect(outcome.resultJson?.label).toBeTypeOf("string");
  });

  it("handles drafter jobs", () => {
    const outcome = executeJobViaAdapter(makeJob({
      workloadType: "drafter",
      inputJson: { recipient: "Sam", topic: "the roadmap", bullets: ["Ship v1", "Review metrics"] },
    }));
    expect(outcome.adapter).toBe("drafter");
    expect(String(outcome.resultJson?.draftText)).toContain("Hi Sam");
  });

  it("handles transcription jobs", () => {
    const outcome = executeJobViaAdapter(makeJob({
      workloadType: "transcription",
      inputJson: { segments: [{ text: "hello", endSec: 1 }, { text: "world", endSec: 2 }], language: "en" },
    }));
    expect(outcome.adapter).toBe("transcription");
    expect(outcome.resultJson?.transcriptText).toBe("hello world");
    expect(outcome.resultJson?.durationSec).toBe(2);
  });

  it("handles embeddings jobs", () => {
    const outcome = executeJobViaAdapter(makeJob({
      workloadType: "embeddings",
      inputJson: { dimensions: 4, items: [{ id: "a", text: "alpha" }, { id: "b", text: "beta" }] },
    }));
    expect(outcome.adapter).toBe("embeddings");
    expect(outcome.resultJson?.itemCount).toBe(2);
    expect(Array.isArray((outcome.resultJson?.items as Array<unknown>)[0])).toBe(false);
  });

  it("honors simulated retryable errors", () => {
    const outcome = executeJobViaAdapter(makeJob({
      workloadType: "scorer",
      inputJson: { simulate: { outcome: "retryable_error", errorClass: "temp_error" } },
    }));
    expect(outcome.adapter).toBe("simulate");
    expect(outcome.outcome).toBe("retryable_error");
    expect(outcome.errorClass).toBe("temp_error");
  });
});
