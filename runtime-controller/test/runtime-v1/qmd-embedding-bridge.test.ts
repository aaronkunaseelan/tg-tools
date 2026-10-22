import { spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  executeQmdEmbeddingViaBridge,
  resolveQmdEmbeddingAdapterMode,
} from "../../src/worker-adapters/qmd-embedding-bridge.js";
import type { RuntimeJob, RuntimeJobAttempt } from "../../src/types/runtime.js";

const tempDirs: string[] = [];

afterEach(async () => {
  await Promise.all(tempDirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

function makeJob(): RuntimeJob {
  const now = new Date().toISOString();
  return {
    jobId: "job-qmd-bridge-1",
    producerSystem: "qmd",
    producerRef: "qmd-embed:2026-04-13T210000Z:shard-0001",
    resultOwner: "qmd",
    resultRoute: "qmd.embedding.import.v1",
    workloadType: "qmd_embedding",
    queueClass: "foreground",
    executionProfile: "embed_batch",
    executionShape: "remote_async_artifact",
    submissionMode: "explicit_batch",
    priority: 50,
    inputItemCount: 3,
    status: "leased",
    runnableAt: now,
    deadlineAt: null,
    idempotencyKey: "qmd-embedding:test-bridge",
    effectKey: null,
    attemptCount: 1,
    maxAttempts: 3,
    leaseOwner: "worker-qmd-bridge",
    leaseToken: "lease-qmd-bridge",
    leaseExpiresAt: now,
    inputJson: {
      priorityClass: "interactive_freshness",
      model: "BAAI/bge-base-en-v1.5",
      uniqueInputCount: 3,
      artifactBundleVersion: "qmd-embedding-bundle-v1",
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
    attemptId: "attempt-qmd-bridge-1",
    jobId: "job-qmd-bridge-1",
    attemptNo: 1,
    workerId: "worker-qmd-bridge",
    executionProfile: "embed_batch",
    leaseToken: "lease-qmd-bridge",
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

describe("qmd embedding bridge", () => {
  it("fails closed when the Python bridge has no QMD checkout config", () => {
    const env = { ...process.env };
    delete env.RUNTIME_CONTROLLER_QMD_REPO_ROOT;
    const result = spawnSync("python3", ["python/qmd_embed_bridge.py"], {
      input: "{}", encoding: "utf8", env,
    });
    expect(result.status).toBe(0); // bridge reports structured errors on stdout
    expect(JSON.parse(result.stdout)).toMatchObject({
      outcome: "retryable_error", errorClass: "ValueError",
      errorText: "RUNTIME_CONTROLLER_QMD_REPO_ROOT is required",
    });
  });

  it("resolves auto mode to qmd_bridge only when the external bridge inputs exist", async () => {
    const dir = await mkdtemp(join(tmpdir(), "runtime-controller-qmd-bridge-"));
    tempDirs.push(dir);
    const scriptPath = join(dir, "bridge.cjs");
    await writeFile(scriptPath, "process.stdout.write('{}')\n", "utf8");

    expect(resolveQmdEmbeddingAdapterMode({
      adapterMode: "auto",
      pythonBin: process.execPath,
      qmdRepoRoot: dir,
      bridgeScriptPath: scriptPath,
    })).toBe("qmd_bridge");

    expect(resolveQmdEmbeddingAdapterMode({
      adapterMode: "auto",
      pythonBin: process.execPath,
      qmdRepoRoot: join(dir, "missing-repo"),
      bridgeScriptPath: scriptPath,
    })).toBe("reference_stub");
  });

  it("executes the bridge command and parses outputArtifactId/resultJson", async () => {
    const dir = await mkdtemp(join(tmpdir(), "runtime-controller-qmd-bridge-run-"));
    tempDirs.push(dir);
    const scriptPath = join(dir, "bridge.cjs");
    await writeFile(scriptPath, [
      "let input = '';",
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data', (chunk) => { input += chunk; });",
      "process.stdin.on('end', () => {",
      "  const payload = JSON.parse(input);",
      "  process.stdout.write(JSON.stringify({",
      "    adapter: 'qmd_embedding_fastembed',",
      "    outcome: 'succeeded',",
      "    outputArtifactId: 'artifact-qmd-1',",
      "    resultJson: { runId: payload.claim.job.producerRef, ok: true }",
      "  }));",
      "});",
    ].join("\n"), "utf8");

    const outcome = await executeQmdEmbeddingViaBridge(
      {
        adapterMode: "qmd_bridge",
        pythonBin: process.execPath,
        qmdRepoRoot: dir,
        bridgeScriptPath: scriptPath,
      },
      {
        apiUrl: "http://127.0.0.1:8787",
        bearerToken: null,
        workerId: "worker-qmd-bridge",
        job: makeJob(),
        attempt: makeAttempt(),
      },
    );

    expect(outcome.adapter).toBe("qmd_embedding_fastembed");
    expect(outcome.outcome).toBe("succeeded");
    expect(outcome.outputArtifactId).toBe("artifact-qmd-1");
    expect(outcome.resultJson).toMatchObject({
      ok: true,
      runId: "qmd-embed:2026-04-13T210000Z:shard-0001",
    });
  });
});
