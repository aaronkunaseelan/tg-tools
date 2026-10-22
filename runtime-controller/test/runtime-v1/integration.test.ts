import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import { claimRuntimeJob } from "../../src/runtime/claim.js";
import { abandonAttempt } from "../../src/runtime/abandon.js";
import { finalizeAttempt } from "../../src/runtime/finalize.js";
import { heartbeatAttempt, markAttemptRunning } from "../../src/runtime/heartbeat.js";
import { cancelJobBeforeStart, reconcileExpiredDeadlines, reconcileExpiredLeases } from "../../src/runtime/reconcile.js";
import { claimResultsForOwner, consumeResult, listReadyResultsForOwner } from "../../src/runtime/results.js";
import { getRuntimeArtifact, registerArtifact } from "../../src/runtime/artifacts.js";
import { getRuntimeHealth } from "../../src/runtime/health.js";
import { getRuntimeJob, listRuntimeJobs } from "../../src/runtime/jobs.js";
import { admitAndSubmitRuntimeJob } from "../../src/runtime/submit.js";
import { drainWorker, getWorker, heartbeatWorker, listWorkers, registerWorker, resumeWorker } from "../../src/runtime/workers.js";

function testDatabaseName(): string {
  return `runtime_controller_test_${Date.now()}_${randomUUID().replace(/-/g, "")}`;
}

async function registerQmdInputArtifact(pool: Pool, suffix: string): Promise<string> {
  const artifact = await registerArtifact(pool, {
    artifactKind: "qmd_embedding_input_manifest_json",
    storageUri: `b2://qmd-artifacts/test/${suffix}/input/manifest.json`,
    contentHash: `sha256:${suffix}`,
    byteSize: 1024,
    mimeType: "application/json",
    schemaRef: "qmd.embedding.input-manifest.v1",
    retentionClass: "durable",
    producerSystem: "qmd",
    metadataJson: {
      workloadType: "qmd_embedding",
      suffix,
    },
  });
  return artifact.artifact.artifactId;
}

describe.sequential("runtime-controller integration", () => {
  let adminPool: Pool;
  let pool: Pool;
  let dbName: string;

  beforeAll(async () => {
    dbName = testDatabaseName();
    const config = testDatabaseConfig();
    adminPool = new Pool({ ...config, database: "postgres" });
    await adminPool.query(`CREATE DATABASE ${dbName}`);
    pool = new Pool({ ...config, database: dbName });
    await applyMigrations(pool);
  }, 20_000);

  afterAll(async () => {
    await pool?.end();
    await adminPool?.query(`DROP DATABASE IF EXISTS ${dbName}`);
    await adminPool?.end();
  }, 20_000);

  it("runs a successful submit/claim/run/heartbeat/finalize/result-consume flow", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer");
    expect(policy).not.toBeNull();

    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "item-1",
      resultOwner: "owner-a",
      resultRoute: "owner-a.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      inputJson: { itemId: 1 },
      outputContractJson: { kind: "score" },
      idempotencyKey: "score:item-1",
    }, policy!);

    expect(submitted.action).toBe("created");
    if (submitted.action !== "created") throw new Error("expected created submission");

    const claimed = await claimRuntimeJob(pool, {
      workerId: "worker-a",
      supportedExecutionProfiles: ["cpu_small"],
      leaseDurationSec: 120,
    });

    expect(claimed).not.toBeNull();
    if (!claimed) throw new Error("expected a claimed job");
    expect(claimed.job.status).toBe("leased");

    const running = await markAttemptRunning(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
    });
    expect(running.attempt.status).toBe("running");

    const heartbeated = await heartbeatAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      leaseDurationSec: 300,
    });
    expect(heartbeated.job.leaseExpiresAt).not.toBeNull();

    const finalized = await finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "succeeded",
      resultJson: { score: 0.99 },
    });

    expect(finalized.job.status).toBe("succeeded");
    expect(finalized.result?.resultKind).toBe("succeeded");

    const ready = await listReadyResultsForOwner(pool, { resultOwner: "owner-a" });
    expect(ready).toHaveLength(1);
    expect(ready[0]?.claimedAt).toBeNull();

    const jobLookup = await getRuntimeJob(pool, claimed.job.jobId);
    expect(jobLookup?.jobId).toBe(claimed.job.jobId);
    expect(jobLookup?.inputJson?.itemId).toBe(1);

    const listed = await listRuntimeJobs(pool, {
      producerSystem: "scorer",
      producerRefPrefix: "item-",
      status: ["succeeded"],
      limit: 10,
    });
    expect(listed.count).toBeGreaterThanOrEqual(1);
    expect(listed.items.some((job) => job.jobId === claimed.job.jobId)).toBe(true);

    const claimedResults = await claimResultsForOwner(pool, { resultOwner: "owner-a" });
    expect(claimedResults).toHaveLength(1);
    expect(claimedResults[0]?.claimedAt).not.toBeNull();

    const consumed = await consumeResult(pool, {
      resultId: claimedResults[0]!.resultId,
      resultOwner: "owner-a",
    });
    expect(consumed?.consumedAt).not.toBeNull();
  }, 20_000);

  it("returns defer without creating a job when policy says defer", async () => {
    const before = await pool.query<{ count: string }>(
      `SELECT COUNT(*)::text AS count FROM runtime.jobs WHERE producer_system = 'embed' AND idempotency_key = 'embed:batch-1'`,
    );

    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "embeddings");
    expect(policy).not.toBeNull();

    const result = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "embed",
      producerRef: "batch-1",
      resultOwner: "embed-owner",
      resultRoute: "embed.route",
      workloadType: "embeddings",
      queueClass: "background",
      executionProfile: "gpu_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 32,
      inputBytes: 1024,
      idempotencyKey: "embed:batch-1",
    }, policy!);

    expect(result.action).toBe("deferred");

    const after = await pool.query<{ count: string }>(
      `SELECT COUNT(*)::text AS count FROM runtime.jobs WHERE producer_system = 'embed' AND idempotency_key = 'embed:batch-1'`,
    );

    expect(Number(before.rows[0]!.count)).toBe(0);
    expect(Number(after.rows[0]!.count)).toBe(0);
  }, 20_000);

  it("supports workloadType and producerSystem claim filters", async () => {
    const scorerPolicy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;
    const transcriptionPolicy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "transcription")!;

    const scorerSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "claim-filter-scorer",
      resultOwner: "owner-filter-a",
      resultRoute: "owner-filter-a.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "claim-filter-scorer",
    }, scorerPolicy);
    if (scorerSubmit.action !== "created") throw new Error("expected scorer submission");

    const transcriptionSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "transcriber",
      producerRef: "claim-filter-transcription",
      resultOwner: "owner-filter-b",
      resultRoute: "owner-filter-b.route",
      workloadType: "transcription",
      queueClass: "background",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "claim-filter-transcription",
    }, {
      ...transcriptionPolicy,
      queueClass: "background",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
    });
    if (transcriptionSubmit.action !== "created") throw new Error("expected transcription submission");

    const scorerClaim = await claimRuntimeJob(pool, {
      workerId: "worker-filter-scorer",
      supportedExecutionProfiles: ["cpu_small"],
      workloadTypes: ["scorer"],
      leaseDurationSec: 120,
    });
    expect(scorerClaim?.job.workloadType).toBe("scorer");
    expect(scorerClaim?.job.jobId).toBe(scorerSubmit.job.jobId);

    const producerFenceClaim = await claimRuntimeJob(pool, {
      workerId: "worker-filter-transcriber",
      supportedExecutionProfiles: ["cpu_small"],
      workloadTypes: ["transcription"],
      producerSystems: ["transcriber"],
      leaseDurationSec: 120,
    });
    expect(producerFenceClaim?.job.workloadType).toBe("transcription");
    expect(producerFenceClaim?.job.producerSystem).toBe("transcriber");
    expect(producerFenceClaim?.job.jobId).toBe(transcriptionSubmit.job.jobId);
  }, 20_000);

  it("supports queue-class claim filters for qmd embedding work", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "qmd_embedding");
    expect(policy).not.toBeNull();

    const foregroundInputArtifactId = await registerQmdInputArtifact(pool, "queue-filter-foreground");
    const foregroundSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "qmd",
      producerRef: "qmd-embed:queue-filter:foreground",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "foreground",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 32,
      inputArtifactId: foregroundInputArtifactId,
      inputJson: {
        priorityClass: "interactive_freshness",
        model: "BAAI/bge-base-en-v1.5",
        uniqueInputCount: 32,
        artifactBundleVersion: "qmd-embedding-bundle-v1",
      },
      outputContractJson: {
        resultRoute: "qmd.embedding.import.v1",
        requiredArtifacts: [
          "qmd_embedding_output_manifest_json",
          "qmd_embedding_vectors_parquet",
        ],
      },
      idempotencyKey: "qmd-embedding:queue-filter:foreground",
    }, policy!);
    if (foregroundSubmit.action !== "created") throw new Error("expected foreground qmd embedding submission");

    const backgroundInputArtifactId = await registerQmdInputArtifact(pool, "queue-filter-background");
    const backgroundSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "qmd",
      producerRef: "qmd-embed:queue-filter:background",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "background",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 64,
      inputArtifactId: backgroundInputArtifactId,
      inputJson: {
        priorityClass: "hot_backfill",
        model: "BAAI/bge-base-en-v1.5",
        uniqueInputCount: 64,
        artifactBundleVersion: "qmd-embedding-bundle-v1",
      },
      outputContractJson: {
        resultRoute: "qmd.embedding.import.v1",
        requiredArtifacts: [
          "qmd_embedding_output_manifest_json",
          "qmd_embedding_vectors_parquet",
        ],
      },
      idempotencyKey: "qmd-embedding:queue-filter:background",
    }, policy!);
    if (backgroundSubmit.action !== "created") throw new Error("expected background qmd embedding submission");

    const backgroundOnlyClaim = await claimRuntimeJob(pool, {
      workerId: "worker-filter-background",
      supportedExecutionProfiles: ["embed_batch"],
      workloadTypes: ["qmd_embedding"],
      allowedQueueClasses: ["background"],
      leaseDurationSec: 120,
    });

    expect(backgroundOnlyClaim?.job.workloadType).toBe("qmd_embedding");
    expect(backgroundOnlyClaim?.job.queueClass).toBe("background");
    expect(backgroundOnlyClaim?.job.jobId).toBe(backgroundSubmit.job.jobId);
    expect(backgroundOnlyClaim?.job.jobId).not.toBe(foregroundSubmit.job.jobId);
  }, 20_000);

  it("registers artifacts idempotently and can read them back", async () => {
    const registered = await registerArtifact(pool, {
      artifactKind: "qmd_embedding_input_manifest_json",
      storageUri: "b2://qmd-artifacts/test/idempotent/input/manifest.json",
      contentHash: "sha256:idempotent",
      byteSize: 2048,
      mimeType: "application/json",
      schemaRef: "qmd.embedding.input-manifest.v1",
      retentionClass: "durable",
      producerSystem: "qmd",
      metadataJson: { kind: "test" },
    });
    expect(registered.action).toBe("created");

    const existing = await registerArtifact(pool, {
      artifactKind: "qmd_embedding_input_manifest_json",
      storageUri: "b2://qmd-artifacts/test/idempotent/input/manifest.json",
      contentHash: "sha256:idempotent",
      byteSize: 2048,
      mimeType: "application/json",
      schemaRef: "qmd.embedding.input-manifest.v1",
      retentionClass: "durable",
      producerSystem: "qmd",
      metadataJson: { kind: "test" },
    });
    expect(existing.action).toBe("existing");
    expect(existing.artifact.artifactId).toBe(registered.artifact.artifactId);

    const lookedUp = await getRuntimeArtifact(pool, registered.artifact.artifactId);
    expect(lookedUp?.artifactId).toBe(registered.artifact.artifactId);
  }, 20_000);

  it("requires qmd embedding input artifacts to match the expected manifest contract", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "qmd_embedding");
    expect(policy).not.toBeNull();

    const wrongArtifact = await registerArtifact(pool, {
      artifactKind: "qmd_embedding_vectors_parquet",
      storageUri: "b2://qmd-artifacts/test/wrong-kind/input/vectors.parquet",
      contentHash: "sha256:wrong-kind-input",
      byteSize: 4096,
      mimeType: "application/x-parquet",
      schemaRef: "qmd.embedding.vectors.v1",
      retentionClass: "durable",
      producerSystem: "qmd",
      metadataJson: { kind: "wrong-input" },
    });

    await expect(admitAndSubmitRuntimeJob(pool, {
      producerSystem: "qmd",
      producerRef: "qmd-embed:wrong-input-artifact:foreground",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "foreground",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 8,
      inputArtifactId: wrongArtifact.artifact.artifactId,
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
      idempotencyKey: "qmd-embedding:wrong-input-artifact:foreground",
    }, policy!)).rejects.toThrow(/input artifact must have artifactKind=qmd_embedding_input_manifest_json/);
  }, 20_000);

  it("requires artifact-ready finalize semantics for qmd embedding success", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "qmd_embedding");
    expect(policy).not.toBeNull();

    const inputArtifactId = await registerQmdInputArtifact(pool, "finalize-success");
    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "qmd",
      producerRef: "qmd-embed:finalize-success:foreground",
      resultOwner: "qmd",
      resultRoute: "qmd.embedding.import.v1",
      workloadType: "qmd_embedding",
      queueClass: "foreground",
      executionProfile: "embed_batch",
      executionShape: "remote_async_artifact",
      inputItemCount: 16,
      inputArtifactId,
      inputJson: {
        priorityClass: "interactive_freshness",
        model: "BAAI/bge-base-en-v1.5",
        uniqueInputCount: 16,
        artifactBundleVersion: "qmd-embedding-bundle-v1",
      },
      outputContractJson: {
        resultRoute: "qmd.embedding.import.v1",
        requiredArtifacts: [
          "qmd_embedding_output_manifest_json",
          "qmd_embedding_vectors_parquet",
        ],
      },
      idempotencyKey: "qmd-embedding:finalize-success:foreground",
    }, policy!);
    if (submitted.action !== "created") throw new Error("expected qmd embedding submission");

    const claimed = await claimRuntimeJob(pool, {
      workerId: "worker-qmd-finalize",
      supportedExecutionProfiles: ["embed_batch"],
      workloadTypes: ["qmd_embedding"],
      leaseDurationSec: 120,
    });
    if (!claimed) throw new Error("expected claimed qmd embedding job");

    await expect(finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "succeeded",
      resultJson: {
        status: "artifact_ready",
        runId: "finalize-success-run",
        model: "BAAI/bge-base-en-v1.5",
      },
    })).rejects.toThrow(/outputArtifactId/);

    const wrongOutputArtifact = await registerArtifact(pool, {
      artifactKind: "qmd_embedding_vectors_parquet",
      storageUri: "b2://qmd-artifacts/test/finalize-success/output/vectors.parquet",
      contentHash: "sha256:finalize-success-wrong-output",
      byteSize: 8192,
      mimeType: "application/x-parquet",
      schemaRef: "qmd.embedding.vectors.v1",
      retentionClass: "durable",
      producerSystem: "qmd_embedding_worker",
      createdByJobId: claimed.job.jobId,
      createdByAttemptId: claimed.attempt.attemptId,
      metadataJson: { kind: "wrong-output" },
    });

    await expect(finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "succeeded",
      outputArtifactId: wrongOutputArtifact.artifact.artifactId,
      attemptOutputArtifactId: wrongOutputArtifact.artifact.artifactId,
      resultJson: {
        status: "artifact_ready",
        runId: "finalize-success-run",
        model: "BAAI/bge-base-en-v1.5",
      },
    })).rejects.toThrow(/output artifact must have artifactKind=qmd_embedding_output_manifest_json/);

    const outputArtifact = await registerArtifact(pool, {
      artifactKind: "qmd_embedding_output_manifest_json",
      storageUri: "b2://qmd-artifacts/test/finalize-success/output/output-manifest.json",
      contentHash: "sha256:finalize-success-output",
      byteSize: 4096,
      mimeType: "application/json",
      schemaRef: "qmd.embedding.output-manifest.v1",
      retentionClass: "durable",
      producerSystem: "qmd_embedding_worker",
      createdByJobId: claimed.job.jobId,
      createdByAttemptId: claimed.attempt.attemptId,
      metadataJson: { kind: "output" },
    });

    const finalized = await finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "succeeded",
      outputArtifactId: outputArtifact.artifact.artifactId,
      attemptOutputArtifactId: outputArtifact.artifact.artifactId,
      resultJson: {
        status: "artifact_ready",
        runId: "finalize-success-run",
        model: "BAAI/bge-base-en-v1.5",
      },
    });

    expect(finalized.job.status).toBe("succeeded");
    expect(finalized.result?.resultKind).toBe("succeeded");
    expect(finalized.result?.outputArtifactId).toBe(outputArtifact.artifact.artifactId);
  }, 20_000);

  it("dead-letters exhausted retryable finalize outcomes", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;

    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "item-finalize-dead",
      resultOwner: "owner-finalize-dead",
      resultRoute: "owner-finalize-dead.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "score:item-finalize-dead",
      maxAttempts: 1,
    }, policy);
    if (submitted.action !== "created") throw new Error("expected created finalize-dead job");

    const claimed = await claimRuntimeJob(pool, {
      workerId: "worker-finalize-dead",
      supportedExecutionProfiles: ["cpu_small"],
      leaseDurationSec: 120,
    });
    if (!claimed) throw new Error("expected claimed finalize-dead job");

    const finalized = await finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "retryable_error",
      errorClass: "temp_error",
      errorText: "temporary but out of attempts",
    });

    expect(finalized.job.status).toBe("dead");
    expect(finalized.attempt.status).toBe("retryable_error");
    expect(finalized.result?.resultKind).toBe("dead");
    expect(finalized.result?.attemptId).toBe(claimed.attempt.attemptId);
  }, 20_000);

  it("reconciles expired leases into retryable or dead terminal jobs", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;

    const retryableSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "item-retry",
      resultOwner: "owner-b",
      resultRoute: "owner-b.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "score:item-retry",
      maxAttempts: 3,
    }, policy);
    if (retryableSubmit.action !== "created") throw new Error("expected created retryable job");

    const retryableClaim = await claimRuntimeJob(pool, {
      workerId: "worker-b",
      supportedExecutionProfiles: ["cpu_small"],
      leaseDurationSec: 60,
    });
    if (!retryableClaim) throw new Error("expected claimed retryable job");

    await pool.query(`UPDATE runtime.jobs SET lease_expires_at = NOW() - interval '5 minutes' WHERE job_id = $1::uuid`, [retryableClaim.job.jobId]);
    await pool.query(`UPDATE runtime.job_attempts SET lease_expires_at = NOW() - interval '5 minutes' WHERE attempt_id = $1::uuid`, [retryableClaim.attempt.attemptId]);

    const retried = await reconcileExpiredLeases(pool, { limit: 10, baseBackoffSec: 5, maxBackoffSec: 5 });
    expect(retried.some((item) => item.job.jobId === retryableClaim.job.jobId && item.action === "retried")).toBe(true);

    const deadSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "item-dead",
      resultOwner: "owner-c",
      resultRoute: "owner-c.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "score:item-dead",
      maxAttempts: 1,
    }, policy);
    if (deadSubmit.action !== "created") throw new Error("expected created dead job");

    const deadClaim = await claimRuntimeJob(pool, {
      workerId: "worker-c",
      supportedExecutionProfiles: ["cpu_small"],
      leaseDurationSec: 60,
    });
    if (!deadClaim) throw new Error("expected claimed dead job");

    await pool.query(`UPDATE runtime.jobs SET lease_expires_at = NOW() - interval '5 minutes' WHERE job_id = $1::uuid`, [deadClaim.job.jobId]);
    await pool.query(`UPDATE runtime.job_attempts SET lease_expires_at = NOW() - interval '5 minutes' WHERE attempt_id = $1::uuid`, [deadClaim.attempt.attemptId]);

    const deadItems = await reconcileExpiredLeases(pool, { limit: 10, baseBackoffSec: 5, maxBackoffSec: 5 });
    const deadItem = deadItems.find((item) => item.job.jobId === deadClaim.job.jobId);
    expect(deadItem?.action).toBe("dead");
    expect(deadItem?.result?.attemptId).toBe(deadClaim.attempt.attemptId);
  }, 20_000);

  it("handles deadline expiry and pre-start cancellation with null attempt_id results", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;

    const deadlineSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "deadline-item",
      resultOwner: "owner-d",
      resultRoute: "owner-d.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "score:deadline-item",
      deadlineAt: "2020-01-01T00:00:00Z",
    }, policy);
    if (deadlineSubmit.action !== "created") throw new Error("expected created deadline job");

    const deadlineItems = await reconcileExpiredDeadlines(pool, { limit: 10 });
    const deadlineItem = deadlineItems.find((item) => item.job.jobId === deadlineSubmit.job.jobId);
    expect(deadlineItem?.result.attemptId).toBeNull();
    expect(deadlineItem?.job.status).toBe("dead");

    const cancelSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer",
      producerRef: "cancel-item",
      resultOwner: "owner-e",
      resultRoute: "owner-e.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "score:cancel-item",
    }, policy);
    if (cancelSubmit.action !== "created") throw new Error("expected created cancel job");

    const cancelled = await cancelJobBeforeStart(pool, {
      jobId: cancelSubmit.job.jobId,
      reason: "user requested cancel",
    });

    expect(cancelled.job.status).toBe("cancelled");
    expect(cancelled.result.attemptId).toBeNull();
    expect(cancelled.result.resultKind).toBe("cancelled");
  }, 20_000);

  it("registers workers and supports heartbeat, drain, and resume flows", async () => {
    const registered = await registerWorker(pool, {
      workerId: "worker-registry-1",
      workerVenue: "oci",
      supportedExecutionProfiles: ["cpu_small"],
      workloadTypes: ["scorer"],
      producerSystems: ["scorer"],
      allowedQueueClasses: ["foreground"],
      queuePreference: ["foreground", "background", "ops"],
      capacity: 2,
      metadataJson: { zone: "oci" },
    });
    expect(registered.control.action).toBe("continue");
    expect(registered.worker.capacity).toBe(2);
    expect(registered.worker.metadataJson.zone).toBe("oci");

    const listed = await listWorkers(pool, { state: "active", venue: "oci" });
    expect(listed.items.some((worker) => worker.workerId === "worker-registry-1")).toBe(true);

    const heartbeated = await heartbeatWorker(pool, {
      workerId: "worker-registry-1",
      metadataJson: { phase: "idle" },
    });
    expect(heartbeated.control.action).toBe("continue");
    expect(heartbeated.worker.metadataJson.phase).toBe("idle");

    const drained = await drainWorker(pool, {
      workerId: "worker-registry-1",
      reason: "maintenance_window",
      requestedBy: "test-suite",
      gracePeriodSec: 60,
    });
    expect(drained.worker.state).toBe("drained");
    expect(drained.activeAttempts).toHaveLength(0);

    const drainHeartbeat = await heartbeatWorker(pool, {
      workerId: "worker-registry-1",
      metadataJson: { phase: "drained" },
    });
    expect(drainHeartbeat.control.action).toBe("drain");
    expect(drainHeartbeat.control.reason).toMatch(/maintenance_window/);

    const resumed = await resumeWorker(pool, { workerId: "worker-registry-1" });
    expect(resumed.state).toBe("active");
  }, 20_000);

  it("reports stale active workers as unavailable and alerts until a fresh matching worker is present", async () => {
    const now = new Date("2030-01-01T01:00:00Z");
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;
    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "heartbeat-health-producer",
      producerRef: "heartbeat-health-item",
      resultOwner: "heartbeat-health-owner",
      resultRoute: "heartbeat-health.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "heartbeat-health-producer:item",
    }, policy);
    if (submitted.action !== "created") throw new Error("expected heartbeat health submission");
    await pool.query(
      `UPDATE runtime.jobs
          SET runnable_at = $2::timestamptz,
              created_at = $3::timestamptz
        WHERE job_id = $1::uuid`,
      [submitted.job.jobId, "2030-01-01T00:50:00Z", "2030-01-01T00:40:00Z"],
    );

    await registerWorker(pool, {
      workerId: "worker-heartbeat-stale",
      supportedExecutionProfiles: ["cpu_small"],
      workloadTypes: ["scorer"],
      producerSystems: ["heartbeat-health-producer"],
      allowedQueueClasses: ["foreground"],
      capacity: 1,
      now: new Date("2030-01-01T00:57:00Z"),
    });

    const listed = await listWorkers(pool, {
      state: "active",
      heartbeatTtlSec: 120,
      now,
    });
    const staleListed = listed.items.find((worker) => worker.workerId === "worker-heartbeat-stale");
    expect(staleListed).toMatchObject({
      state: "active",
      heartbeatAgeSec: 180,
      heartbeatFresh: false,
      available: false,
    });
    const staleGet = await getWorker(pool, "worker-heartbeat-stale", { now, heartbeatTtlSec: 120 });
    expect(staleGet?.heartbeatFresh).toBe(false);

    const stored = await pool.query<{ state: string; last_heartbeat_at: Date }>(
      `SELECT state, last_heartbeat_at FROM runtime.workers WHERE worker_id = $1`,
      ["worker-heartbeat-stale"],
    );
    expect(stored.rows[0]?.state).toBe("active");
    expect(stored.rows[0]?.last_heartbeat_at.toISOString()).toBe("2030-01-01T00:57:00.000Z");

    const degradedHealth = await getRuntimeHealth(pool, { now, workerHeartbeatTtlSec: 120 });
    const degradedQueue = degradedHealth.runtime?.queueCapacity?.find((queue) => (
      queue.producerSystem === "heartbeat-health-producer" && queue.workloadType === "scorer"
    ));
    expect(degradedHealth.ok).toBe(true);
    expect(degradedHealth.runtime?.degraded).toBe(true);
    expect(degradedQueue).toMatchObject({
      runnablePendingJobs: 1,
      freshEligibleWorkerCount: 0,
      freshEligibleWorkerCapacity: 0,
      degraded: true,
    });
    expect(degradedQueue?.oldestPendingJob).toMatchObject({
      jobId: submitted.job.jobId,
      ageSec: 600,
    });
    expect(degradedQueue?.alert).toMatch(/no fresh eligible worker/);

    await registerWorker(pool, {
      workerId: "worker-heartbeat-fresh",
      supportedExecutionProfiles: ["cpu_small"],
      workloadTypes: ["scorer"],
      producerSystems: ["heartbeat-health-producer"],
      allowedQueueClasses: ["foreground"],
      capacity: 2,
      now: new Date("2030-01-01T00:59:30Z"),
    });
    const healthyHealth = await getRuntimeHealth(pool, { now, workerHeartbeatTtlSec: 120 });
    const healthyQueue = healthyHealth.runtime?.queueCapacity?.find((queue) => (
      queue.producerSystem === "heartbeat-health-producer" && queue.workloadType === "scorer"
    ));
    expect(healthyQueue).toMatchObject({
      freshEligibleWorkerCount: 1,
      freshEligibleWorkerCapacity: 2,
      degraded: false,
      alert: null,
    });
  }, 20_000);

  it("writes claim metadata, enforces worker capacity, and marks draining workers drained after finalize", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;

    const firstSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "capacity-scorer",
      producerRef: "capacity-item-1",
      resultOwner: "owner-cap-a",
      resultRoute: "owner-cap-a.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "capacity-scorer:capacity-item-1",
    }, policy);
    if (firstSubmit.action !== "created") throw new Error("expected created first capacity job");

    const secondSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "capacity-scorer",
      producerRef: "capacity-item-2",
      resultOwner: "owner-cap-b",
      resultRoute: "owner-cap-b.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "capacity-scorer:capacity-item-2",
    }, policy);
    if (secondSubmit.action !== "created") throw new Error("expected created second capacity job");

    const claimed = await claimRuntimeJob(pool, {
      workerId: "worker-capacity-1",
      workerVenue: "oci",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      workerMetadataJson: { slot: "a" },
      producerSystems: ["capacity-scorer"],
      leaseDurationSec: 120,
    });
    if (!claimed) throw new Error("expected claimed capacity job");

    expect(claimed.attempt.claimJson?.worker).toMatchObject({
      workerId: "worker-capacity-1",
      venue: "oci",
      capacity: 1,
      activeLeaseCount: 0,
    });
    expect(claimed.attempt.claimJson?.placement).toMatchObject({
      policy: "default",
      allowed: true,
    });

    const blocked = await claimRuntimeJob(pool, {
      workerId: "worker-capacity-1",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      producerSystems: ["capacity-scorer"],
      leaseDurationSec: 120,
    });
    expect(blocked).toBeNull();

    const draining = await drainWorker(pool, {
      workerId: "worker-capacity-1",
      reason: "node_maintenance",
      gracePeriodSec: 300,
    });
    expect(draining.worker.state).toBe("draining");
    expect(draining.activeAttempts).toHaveLength(1);

    const drainedClaim = await claimRuntimeJob(pool, {
      workerId: "worker-capacity-1",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      producerSystems: ["capacity-scorer"],
      leaseDurationSec: 120,
    });
    expect(drainedClaim).toBeNull();

    const finalized = await finalizeAttempt(pool, {
      jobId: claimed.job.jobId,
      attemptId: claimed.attempt.attemptId,
      leaseToken: claimed.attempt.leaseToken,
      outcome: "succeeded",
      resultJson: { ok: true },
    });
    expect(finalized.job.status).toBe("succeeded");

    const workerAfterFinalize = await getWorker(pool, "worker-capacity-1");
    expect(workerAfterFinalize?.state).toBe("drained");

    await resumeWorker(pool, { workerId: "worker-capacity-1" });
    const resumedClaim = await claimRuntimeJob(pool, {
      workerId: "worker-capacity-1",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      producerSystems: ["capacity-scorer"],
      leaseDurationSec: 120,
    });
    expect(resumedClaim?.job.jobId).toBe(secondSubmit.job.jobId);
  }, 20_000);

  it("abandons active attempts into retryable or dead jobs and drains workers when idle", async () => {
    const policy = resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!;

    const retrySubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "abandon-retry-scorer",
      producerRef: "abandon-retry-item",
      resultOwner: "owner-abandon-a",
      resultRoute: "owner-abandon-a.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "abandon-retry-scorer:abandon-retry-item",
      maxAttempts: 3,
    }, policy);
    if (retrySubmit.action !== "created") throw new Error("expected created abandon retry job");

    const retryClaim = await claimRuntimeJob(pool, {
      workerId: "worker-abandon-1",
      workerVenue: "oci",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      producerSystems: ["abandon-retry-scorer"],
      leaseDurationSec: 120,
    });
    if (!retryClaim) throw new Error("expected claimed abandon retry job");

    await markAttemptRunning(pool, {
      jobId: retryClaim.job.jobId,
      attemptId: retryClaim.attempt.attemptId,
      leaseToken: retryClaim.attempt.leaseToken,
    });
    await drainWorker(pool, {
      workerId: "worker-abandon-1",
      reason: "worker_draining",
      gracePeriodSec: 30,
    });

    const abandonedRetry = await abandonAttempt(pool, {
      jobId: retryClaim.job.jobId,
      attemptId: retryClaim.attempt.attemptId,
      leaseToken: retryClaim.attempt.leaseToken,
      reason: "worker_draining",
      backoffSec: 5,
    });
    expect(abandonedRetry.attempt.status).toBe("abandoned");
    expect(abandonedRetry.job.status).toBe("retryable");
    expect(abandonedRetry.result).toBeNull();

    const workerAfterAbandonRetry = await getWorker(pool, "worker-abandon-1");
    expect(workerAfterAbandonRetry?.state).toBe("drained");

    const deadSubmit = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "abandon-dead-scorer",
      producerRef: "abandon-dead-item",
      resultOwner: "owner-abandon-b",
      resultRoute: "owner-abandon-b.route",
      workloadType: "scorer",
      queueClass: "foreground",
      executionProfile: "cpu_small",
      executionShape: "local_bounded",
      idempotencyKey: "abandon-dead-scorer:abandon-dead-item",
      maxAttempts: 1,
    }, policy);
    if (deadSubmit.action !== "created") throw new Error("expected created abandon dead job");

    const deadClaim = await claimRuntimeJob(pool, {
      workerId: "worker-abandon-2",
      workerVenue: "oci",
      supportedExecutionProfiles: ["cpu_small"],
      workerCapacity: 1,
      producerSystems: ["abandon-dead-scorer"],
      leaseDurationSec: 120,
    });
    if (!deadClaim) throw new Error("expected claimed abandon dead job");

    const abandonedDead = await abandonAttempt(pool, {
      jobId: deadClaim.job.jobId,
      attemptId: deadClaim.attempt.attemptId,
      leaseToken: deadClaim.attempt.leaseToken,
      reason: "worker_draining",
    });
    expect(abandonedDead.attempt.status).toBe("abandoned");
    expect(abandonedDead.job.status).toBe("dead");
    expect(abandonedDead.result?.resultKind).toBe("dead");
  }, 20_000);
});
