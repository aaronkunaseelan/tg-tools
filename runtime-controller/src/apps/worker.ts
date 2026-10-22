import { setTimeout as sleep } from "node:timers/promises";
import { loadWorkerConfigFromEnv, type WorkerConfig } from "../config/env.js";
import type { RuntimeArtifact, RuntimeJob, RuntimeJobAttempt } from "../types/runtime.js";
import { executeJobViaAdapter, type WorkerAdapterOutcome } from "../worker-adapters/index.js";
import {
  executeQmdEmbeddingViaBridge,
  resolveQmdEmbeddingAdapterMode,
} from "../worker-adapters/qmd-embedding-bridge.js";
import { prepareQmdEmbeddingReferenceOutput } from "../worker-adapters/qmd-embedding-reference.js";
import type { WorkerTouchResult } from "../runtime/workers.js";

interface ClaimResponse {
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}

interface RegisterArtifactResponse {
  action: "created" | "existing";
  artifact: RuntimeArtifact;
}

interface WorkerControlState {
  acceptingClaims: boolean;
}

function logEvent(event: string, details: Record<string, unknown>): void {
  console.log(JSON.stringify({ ts: new Date().toISOString(), event, ...details }));
}

async function apiRequest<T>(
  apiUrl: string,
  bearerToken: string | null,
  method: string,
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(`${apiUrl}${path}`, {
    method,
    headers: {
      "content-type": "application/json",
      ...(bearerToken ? { authorization: `Bearer ${bearerToken}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  const parsed = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new Error(`API ${method} ${path} failed: ${response.status} ${JSON.stringify(parsed)}`);
  }
  return parsed as T;
}

function resolvedQmdAdapter(config: WorkerConfig): "reference_stub" | "qmd_bridge" {
  return resolveQmdEmbeddingAdapterMode({
    adapterMode: config.qmdEmbedAdapterMode,
    pythonBin: config.qmdPythonBin,
    qmdRepoRoot: config.qmdRepoRoot,
    bridgeScriptPath: config.qmdBridgeScriptPath,
  });
}

function buildWorkerMetadata(config: WorkerConfig, extras: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    hostname: process.env.HOSTNAME ?? "unknown",
    pid: process.pid,
    version: process.env.RUNTIME_CONTROLLER_VERSION?.trim() || process.env.npm_package_version?.trim() || null,
    qmdEmbedAdapterResolved: resolvedQmdAdapter(config),
    ...extras,
  };
}

async function waitForApiReady(apiUrl: string, pollIntervalMs: number, workerId: string): Promise<void> {
  while (true) {
    try {
      const response = await fetch(`${apiUrl}/healthz`);
      if (response.ok) {
        logEvent("worker_api_ready", { workerId, apiUrl });
        return;
      }
    } catch {
      // retry below
    }
    await sleep(pollIntervalMs);
  }
}

async function registerWorkerWithApi(config: WorkerConfig): Promise<WorkerTouchResult> {
  const body: Record<string, unknown> = {
    workerId: config.workerId,
    workerVenue: config.workerVenue,
    supportedExecutionProfiles: config.supportedExecutionProfiles,
    capacity: config.capacity,
    metadataJson: buildWorkerMetadata(config, {
      phase: "startup",
    }),
  };
  if (config.supportedWorkloadTypes.length > 0) {
    body.workloadTypes = config.supportedWorkloadTypes;
  }
  if (config.allowedProducerSystems.length > 0) {
    body.producerSystems = config.allowedProducerSystems;
  }
  if (config.allowedQueueClasses.length > 0) {
    body.allowedQueueClasses = config.allowedQueueClasses;
  }
  if (config.queuePreference.length > 0) {
    body.queuePreference = config.queuePreference;
  }

  return apiRequest<WorkerTouchResult>(
    config.apiUrl,
    config.bearerToken,
    "POST",
    "/v1/workers/register",
    body,
  );
}

async function heartbeatWorkerWithApi(
  config: WorkerConfig,
  extras: Record<string, unknown> = {},
): Promise<WorkerTouchResult> {
  return apiRequest<WorkerTouchResult>(
    config.apiUrl,
    config.bearerToken,
    "POST",
    "/v1/workers/heartbeat",
    {
      workerId: config.workerId,
      metadataJson: buildWorkerMetadata(config, extras),
    },
  );
}

function applyWorkerControl(controlState: WorkerControlState, result: WorkerTouchResult, context: Record<string, unknown>): void {
  if (result.control.action !== "drain") {
    return;
  }
  if (controlState.acceptingClaims) {
    logEvent("worker_drain_requested", {
      workerId: result.worker.workerId,
      reason: result.control.reason,
      deadlineAt: result.control.deadlineAt,
      ...context,
    });
  }
  controlState.acceptingClaims = false;
}

async function prepareQmdEmbeddingOutcome(config: WorkerConfig, claim: ClaimResponse): Promise<WorkerAdapterOutcome & { outputArtifactId?: string | null }> {
  if (resolvedQmdAdapter(config) === "qmd_bridge") {
    try {
      const outcome = await executeQmdEmbeddingViaBridge(
        {
          adapterMode: "qmd_bridge",
          pythonBin: config.qmdPythonBin,
          qmdRepoRoot: config.qmdRepoRoot,
          bridgeScriptPath: config.qmdBridgeScriptPath,
        },
        {
          apiUrl: config.apiUrl,
          bearerToken: config.bearerToken,
          workerId: config.workerId,
          job: claim.job,
          attempt: claim.attempt,
        },
      );
      if (outcome.outcome === "succeeded" && !outcome.outputArtifactId) {
        return {
          adapter: outcome.adapter,
          outcome: "retryable_error",
          errorClass: "QmdEmbeddingBridgeError",
          errorText: "qmd embedding bridge succeeded without outputArtifactId",
          backoffSec: 60,
        };
      }
      return outcome;
    } catch (error) {
      return {
        adapter: "qmd_embedding_fastembed",
        outcome: "retryable_error",
        errorClass: error instanceof Error ? error.name : "QmdEmbeddingBridgeError",
        errorText: error instanceof Error ? error.message : String(error),
        backoffSec: 60,
      };
    }
  }

  const prepared = await prepareQmdEmbeddingReferenceOutput(
    claim.job,
    claim.attempt,
    claim.attempt.workerId,
  );
  const registered = await apiRequest<RegisterArtifactResponse>(
    config.apiUrl,
    config.bearerToken,
    "POST",
    "/v1/artifacts/register",
    prepared.artifactRequest as unknown as Record<string, unknown>,
  );
  return {
    adapter: "qmd_embedding_reference",
    outcome: "succeeded",
    resultJson: prepared.resultJson,
    outputArtifactId: registered.artifact.artifactId,
  };
}

async function processClaim(config: WorkerConfig, claim: ClaimResponse, controlState: WorkerControlState): Promise<void> {
  await apiRequest(config.apiUrl, config.bearerToken, "POST", "/v1/running", {
    jobId: claim.job.jobId,
    attemptId: claim.attempt.attemptId,
    leaseToken: claim.attempt.leaseToken,
  });

  const heartbeat = setInterval(() => {
    void apiRequest(config.apiUrl, config.bearerToken, "POST", "/v1/heartbeat", {
      jobId: claim.job.jobId,
      attemptId: claim.attempt.attemptId,
      leaseToken: claim.attempt.leaseToken,
      leaseDurationSec: config.heartbeatIntervalSec * 2,
    }).catch((error) => {
      logEvent("worker_heartbeat_error", {
        jobId: claim.job.jobId,
        attemptId: claim.attempt.attemptId,
        message: error instanceof Error ? error.message : String(error),
      });
    });

    void heartbeatWorkerWithApi(config, {
      phase: "running",
      currentJobId: claim.job.jobId,
      currentAttemptId: claim.attempt.attemptId,
    }).then((result) => {
      applyWorkerControl(controlState, result, {
        phase: "running",
        jobId: claim.job.jobId,
        attemptId: claim.attempt.attemptId,
      });
    }).catch((error) => {
      logEvent("worker_membership_heartbeat_error", {
        workerId: config.workerId,
        jobId: claim.job.jobId,
        attemptId: claim.attempt.attemptId,
        message: error instanceof Error ? error.message : String(error),
      });
    });
  }, config.heartbeatIntervalSec * 1000);

  try {
    const outcome: WorkerAdapterOutcome & { outputArtifactId?: string | null } = claim.job.workloadType === "qmd_embedding"
      ? await prepareQmdEmbeddingOutcome(config, claim)
      : executeJobViaAdapter(claim.job);
    const finalizeBody: Record<string, unknown> = {
      jobId: claim.job.jobId,
      attemptId: claim.attempt.attemptId,
      leaseToken: claim.attempt.leaseToken,
    };

    if (claim.job.workloadType === "qmd_embedding" && outcome.outputArtifactId) {
      finalizeBody.outputArtifactId = outcome.outputArtifactId;
      finalizeBody.attemptOutputArtifactId = outcome.outputArtifactId;
    }

    if (outcome.sleepMs && outcome.sleepMs > 0) {
      await sleep(outcome.sleepMs);
    }

    finalizeBody.outcome = outcome.outcome;
    if (outcome.resultJson) finalizeBody.resultJson = outcome.resultJson;
    if (outcome.errorClass) finalizeBody.errorClass = outcome.errorClass;
    if (outcome.errorText) finalizeBody.errorText = outcome.errorText;
    if (typeof outcome.backoffSec === "number" && Number.isFinite(outcome.backoffSec)) {
      finalizeBody.backoffSec = outcome.backoffSec;
    }

    const finalized = await apiRequest<Record<string, unknown>>(config.apiUrl, config.bearerToken, "POST", "/v1/finalize", finalizeBody);
    logEvent("worker_finalize_complete", {
      jobId: claim.job.jobId,
      attemptId: claim.attempt.attemptId,
      adapter: outcome.adapter,
      outcome: outcome.outcome,
      finalizedJobStatus: (finalized.job as Record<string, unknown> | undefined)?.status ?? null,
      draining: !controlState.acceptingClaims,
    });
  } finally {
    clearInterval(heartbeat);
  }
}

async function main(): Promise<void> {
  const config = loadWorkerConfigFromEnv();
  const controlState: WorkerControlState = { acceptingClaims: true };
  let running = true;

  const stop = (signal: string) => {
    logEvent("worker_stopping", { signal, workerId: config.workerId });
    running = false;
    controlState.acceptingClaims = false;
  };
  process.on("SIGINT", () => stop("SIGINT"));
  process.on("SIGTERM", () => stop("SIGTERM"));

  logEvent("worker_started", {
    workerId: config.workerId,
    apiUrl: config.apiUrl,
    workerVenue: config.workerVenue,
    workerCapacity: config.capacity,
    supportedExecutionProfiles: config.supportedExecutionProfiles,
    supportedWorkloadTypes: config.supportedWorkloadTypes,
    allowedProducerSystems: config.allowedProducerSystems,
    allowedQueueClasses: config.allowedQueueClasses,
    queuePreference: config.queuePreference,
    pollIntervalMs: config.pollIntervalMs,
    leaseDurationSec: config.leaseDurationSec,
    heartbeatIntervalSec: config.heartbeatIntervalSec,
    qmdEmbedAdapterMode: config.qmdEmbedAdapterMode,
    qmdEmbedAdapterResolved: resolvedQmdAdapter(config),
    qmdRepoRoot: config.qmdRepoRoot,
    qmdPythonBin: config.qmdPythonBin,
    qmdBridgeScriptPath: config.qmdBridgeScriptPath,
  });

  await waitForApiReady(config.apiUrl, config.pollIntervalMs, config.workerId);

  const registration = await registerWorkerWithApi(config);
  applyWorkerControl(controlState, registration, { phase: "startup" });
  if (!controlState.acceptingClaims) {
    logEvent("worker_exit_drained", { workerId: config.workerId, phase: "startup" });
    return;
  }

  while (running) {
    try {
      const workerHeartbeat = await heartbeatWorkerWithApi(config, { phase: "idle" });
      applyWorkerControl(controlState, workerHeartbeat, { phase: "idle" });
      if (!controlState.acceptingClaims) {
        logEvent("worker_exit_drained", { workerId: config.workerId, phase: "idle" });
        break;
      }

      const claimBody: Record<string, unknown> = {
        workerId: config.workerId,
        workerVenue: config.workerVenue,
        supportedExecutionProfiles: config.supportedExecutionProfiles,
        leaseDurationSec: config.leaseDurationSec,
        workerCapacity: config.capacity,
        workerMetadataJson: buildWorkerMetadata(config, { phase: "claim" }),
      };
      if (config.supportedWorkloadTypes.length > 0) {
        claimBody.workloadTypes = config.supportedWorkloadTypes;
      }
      if (config.allowedProducerSystems.length > 0) {
        claimBody.producerSystems = config.allowedProducerSystems;
      }
      if (config.allowedQueueClasses.length > 0) {
        claimBody.allowedQueueClasses = config.allowedQueueClasses;
      }
      if (config.queuePreference.length > 0) {
        claimBody.queuePreference = config.queuePreference;
      }

      const claim = await apiRequest<ClaimResponse | null>(config.apiUrl, config.bearerToken, "POST", "/v1/claim", claimBody);

      if (!claim) {
        await sleep(config.pollIntervalMs);
        continue;
      }

      logEvent("worker_claimed_job", {
        workerId: config.workerId,
        jobId: claim.job.jobId,
        attemptId: claim.attempt.attemptId,
        workloadType: claim.job.workloadType,
        executionProfile: claim.job.executionProfile,
        adapter: claim.job.workloadType === "qmd_embedding"
          ? resolvedQmdAdapter(config) === "qmd_bridge"
            ? "qmd_embedding_fastembed"
            : "qmd_embedding_reference"
          : executeJobViaAdapter(claim.job).adapter,
      });

      await processClaim(config, claim, controlState);

      const postAttemptHeartbeat = await heartbeatWorkerWithApi(config, {
        phase: "post_attempt",
        lastCompletedJobId: claim.job.jobId,
        lastCompletedAttemptId: claim.attempt.attemptId,
      });
      applyWorkerControl(controlState, postAttemptHeartbeat, { phase: "post_attempt" });
      if (!controlState.acceptingClaims) {
        logEvent("worker_exit_drained", { workerId: config.workerId, phase: "post_attempt" });
        break;
      }
    } catch (error) {
      logEvent("worker_loop_error", {
        workerId: config.workerId,
        message: error instanceof Error ? error.message : String(error),
      });
      await sleep(config.pollIntervalMs);
    }
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
