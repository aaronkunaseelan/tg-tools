import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { RuntimeJob, RuntimeJobAttempt } from "../types/runtime.js";
import type { WorkerAdapterOutcome } from "./index.js";

export type QmdEmbeddingAdapterMode = "auto" | "reference_stub" | "qmd_bridge";
export type ResolvedQmdEmbeddingAdapterMode = Exclude<QmdEmbeddingAdapterMode, "auto">;

export interface QmdEmbeddingBridgeConfig {
  adapterMode: QmdEmbeddingAdapterMode;
  pythonBin: string;
  qmdRepoRoot: string;
  bridgeScriptPath: string;
}

export interface QmdEmbeddingBridgeRequest {
  apiUrl: string;
  bearerToken: string | null;
  workerId: string;
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}

export interface QmdEmbeddingBridgeOutcome extends WorkerAdapterOutcome {
  outputArtifactId?: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function defaultQmdEmbeddingBridgeScriptPath(): string {
  const moduleDir = dirname(fileURLToPath(import.meta.url));
  return resolve(moduleDir, "../../../python/qmd_embed_bridge.py");
}

export function resolveQmdEmbeddingAdapterMode(config: QmdEmbeddingBridgeConfig): ResolvedQmdEmbeddingAdapterMode {
  if (config.adapterMode === "reference_stub" || config.adapterMode === "qmd_bridge") {
    return config.adapterMode;
  }
  return existsSync(config.pythonBin) && existsSync(config.qmdRepoRoot) && existsSync(config.bridgeScriptPath)
    ? "qmd_bridge"
    : "reference_stub";
}

export async function executeQmdEmbeddingViaBridge(
  config: QmdEmbeddingBridgeConfig,
  request: QmdEmbeddingBridgeRequest,
): Promise<QmdEmbeddingBridgeOutcome> {
  const payload = JSON.stringify({
    apiUrl: request.apiUrl,
    bearerToken: request.bearerToken,
    workerId: request.workerId,
    qmdRepoRoot: config.qmdRepoRoot,
    claim: {
      job: request.job,
      attempt: request.attempt,
    },
  });

  const output = await new Promise<string>((resolvePromise, rejectPromise) => {
    const child = spawn(config.pythonBin, [config.bridgeScriptPath], {
      cwd: config.qmdRepoRoot,
      env: {
        ...process.env,
        RUNTIME_CONTROLLER_QMD_REPO_ROOT: config.qmdRepoRoot,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });

    child.on("error", (error) => {
      rejectPromise(error);
    });

    child.on("close", (code) => {
      if (code !== 0) {
        rejectPromise(new Error(`qmd embedding bridge exited ${code}: ${stderr.trim() || stdout.trim()}`));
        return;
      }
      resolvePromise(stdout);
    });

    child.stdin.end(payload);
  });

  let parsed: unknown;
  try {
    parsed = JSON.parse(output);
  } catch (error) {
    throw new Error(`qmd embedding bridge returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
  }

  if (!isRecord(parsed) || typeof parsed.outcome !== "string") {
    throw new Error(`qmd embedding bridge returned unexpected payload: ${JSON.stringify(parsed)}`);
  }

  return {
    adapter: typeof parsed.adapter === "string" && parsed.adapter.trim().length > 0
      ? parsed.adapter
      : "qmd_embedding_fastembed",
    outcome: parsed.outcome === "succeeded" || parsed.outcome === "retryable_error" || parsed.outcome === "terminal_error"
      ? parsed.outcome
      : "retryable_error",
    resultJson: isRecord(parsed.resultJson) ? parsed.resultJson : null,
    outputArtifactId: typeof parsed.outputArtifactId === "string" && parsed.outputArtifactId.trim().length > 0
      ? parsed.outputArtifactId
      : null,
    errorClass: typeof parsed.errorClass === "string" ? parsed.errorClass : null,
    errorText: typeof parsed.errorText === "string" ? parsed.errorText : null,
    backoffSec: typeof parsed.backoffSec === "number" && Number.isFinite(parsed.backoffSec)
      ? parsed.backoffSec
      : undefined,
  };
}
