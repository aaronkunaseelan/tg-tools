import type { DatabaseConfig } from "../db/client.js";
import { localPeerDatabaseConfig } from "../db/client.js";
import { QUEUE_CLASSES, type QueueClass } from "../types/runtime.js";
import { DEFAULT_WORKER_HEARTBEAT_TTL_SEC } from "../runtime/workers.js";
import { defaultQmdEmbeddingBridgeScriptPath, type QmdEmbeddingAdapterMode } from "../worker-adapters/qmd-embedding-bridge.js";

function parseIntEnv(value: string | undefined, defaultValue: number): number {
  if (!value?.trim()) return defaultValue;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`invalid integer env value: ${value}`);
  }
  return parsed;
}

function parseOptionalIntEnv(value: string | undefined): number | undefined {
  if (!value?.trim()) return undefined;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`invalid integer env value: ${value}`);
  }
  return parsed;
}

export interface ServerConfig {
  bindHost: string;
  port: number;
  bearerToken: string | null;
  policyFile: string | null;
  workerHeartbeatTtlSec: number;
  database: DatabaseConfig;
}

export interface ReconcilerConfig {
  intervalSec: number;
  leaseLimit: number;
  deadlineLimit: number;
  deliveryLimit: number;
  baseBackoffSec: number;
  maxBackoffSec: number;
  policyFile: string | null;
  database: DatabaseConfig;
}

export interface WorkerConfig {
  apiUrl: string;
  bearerToken: string | null;
  workerId: string;
  workerVenue: "generic" | "oci" | "modal";
  capacity: number;
  supportedExecutionProfiles: string[];
  supportedWorkloadTypes: string[];
  allowedProducerSystems: string[];
  allowedQueueClasses: QueueClass[];
  queuePreference: QueueClass[];
  pollIntervalMs: number;
  leaseDurationSec: number;
  heartbeatIntervalSec: number;
  qmdEmbedAdapterMode: QmdEmbeddingAdapterMode;
  qmdRepoRoot: string;
  qmdPythonBin: string;
  qmdBridgeScriptPath: string;
}

export function databaseConfigFromEnv(defaultDatabase = process.env.PGDATABASE ?? "runtime_controller"): DatabaseConfig {
  const connectionString = process.env.RUNTIME_CONTROLLER_DATABASE_URL?.trim() || process.env.DATABASE_URL?.trim() || undefined;
  if (connectionString) {
    return {
      connectionString,
      applicationName: process.env.RUNTIME_CONTROLLER_APP_NAME ?? "runtime-controller",
      max: parseOptionalIntEnv(process.env.RUNTIME_CONTROLLER_DB_MAX) ?? 10,
    };
  }

  const base = localPeerDatabaseConfig(process.env.RUNTIME_CONTROLLER_DATABASE_NAME ?? defaultDatabase);
  return {
    ...base,
    applicationName: process.env.RUNTIME_CONTROLLER_APP_NAME ?? "runtime-controller",
    max: parseOptionalIntEnv(process.env.RUNTIME_CONTROLLER_DB_MAX) ?? 10,
  };
}

export function loadServerConfigFromEnv(): ServerConfig {
  return {
    bindHost: process.env.RUNTIME_CONTROLLER_BIND_HOST?.trim() || "127.0.0.1",
    port: parseIntEnv(process.env.RUNTIME_CONTROLLER_PORT, 8787),
    bearerToken: process.env.RUNTIME_CONTROLLER_BEARER_TOKEN?.trim() || null,
    policyFile: process.env.RUNTIME_CONTROLLER_POLICY_FILE?.trim() || null,
    workerHeartbeatTtlSec: parseIntEnv(
      process.env.RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC,
      DEFAULT_WORKER_HEARTBEAT_TTL_SEC,
    ),
    database: databaseConfigFromEnv(process.env.RUNTIME_CONTROLLER_DATABASE_NAME ?? "runtime_controller"),
  };
}

export function loadReconcilerConfigFromEnv(): ReconcilerConfig {
  return {
    intervalSec: parseIntEnv(process.env.RUNTIME_CONTROLLER_RECONCILE_INTERVAL_SEC, 30),
    leaseLimit: parseIntEnv(process.env.RUNTIME_CONTROLLER_RECONCILE_LEASE_LIMIT, 100),
    deadlineLimit: parseIntEnv(process.env.RUNTIME_CONTROLLER_RECONCILE_DEADLINE_LIMIT, 100),
    deliveryLimit: parseIntEnv(process.env.RUNTIME_CONTROLLER_RECONCILE_DELIVERY_LIMIT, 100),
    baseBackoffSec: parseIntEnv(process.env.RUNTIME_CONTROLLER_BASE_BACKOFF_SEC, 60),
    maxBackoffSec: parseIntEnv(process.env.RUNTIME_CONTROLLER_MAX_BACKOFF_SEC, 3600),
    policyFile: process.env.RUNTIME_CONTROLLER_POLICY_FILE?.trim() || null,
    database: databaseConfigFromEnv(process.env.RUNTIME_CONTROLLER_DATABASE_NAME ?? "runtime_controller"),
  };
}

function parseCsvList(value: string | undefined, defaultValues: string[]): string[] {
  if (!value?.trim()) return defaultValues;
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function parseOptionalCsvList(value: string | undefined): string[] {
  if (!value?.trim()) return [];
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

const VALID_QUEUE_CLASSES = new Set<string>(QUEUE_CLASSES);


function parseQmdEmbeddingAdapterMode(value: string | undefined): QmdEmbeddingAdapterMode {
  const normalized = value?.trim();
  if (!normalized) return "auto";
  if (normalized === "auto" || normalized === "reference_stub" || normalized === "qmd_bridge") {
    return normalized;
  }
  throw new Error(`invalid qmd embedding adapter mode: ${normalized}`);
}

function parseOptionalQueueClassList(value: string | undefined, envName: string): QueueClass[] {
  const values = parseOptionalCsvList(value);
  for (const queueClass of values) {
    if (!VALID_QUEUE_CLASSES.has(queueClass)) {
      throw new Error(`invalid queue class in ${envName}: ${queueClass}`);
    }
  }
  return values as QueueClass[];
}

function parseWorkerVenue(value: string | undefined): "generic" | "oci" | "modal" {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return "generic";
  if (normalized === "generic" || normalized === "oci" || normalized === "modal") {
    return normalized;
  }
  throw new Error(`invalid worker venue: ${value}`);
}

export function loadWorkerConfigFromEnv(): WorkerConfig {
  const qmdRepoRoot = process.env.RUNTIME_CONTROLLER_QMD_REPO_ROOT?.trim();
  if (!qmdRepoRoot) throw new Error("RUNTIME_CONTROLLER_QMD_REPO_ROOT is required");
  return {
    apiUrl: process.env.RUNTIME_CONTROLLER_API_URL?.trim() || `http://${process.env.RUNTIME_CONTROLLER_BIND_HOST?.trim() || "127.0.0.1"}:${parseIntEnv(process.env.RUNTIME_CONTROLLER_PORT, 8787)}`,
    bearerToken: process.env.RUNTIME_CONTROLLER_BEARER_TOKEN?.trim() || null,
    workerId: process.env.RUNTIME_CONTROLLER_WORKER_ID?.trim() || `${process.env.HOSTNAME ?? "runtime-worker"}-${process.pid}`,
    workerVenue: parseWorkerVenue(process.env.RUNTIME_CONTROLLER_WORKER_VENUE),
    capacity: parseIntEnv(process.env.RUNTIME_CONTROLLER_WORKER_CAPACITY, 1),
    supportedExecutionProfiles: parseCsvList(
      process.env.RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES,
      ["cpu_small", "cpu_batch", "cpu_media", "local_model", "gpu_batch", "remote_api"],
    ),
    supportedWorkloadTypes: parseOptionalCsvList(
      process.env.RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES,
    ),
    allowedProducerSystems: parseOptionalCsvList(
      process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS,
    ),
    allowedQueueClasses: parseOptionalQueueClassList(
      process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES,
      "RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES",
    ),
    queuePreference: parseOptionalQueueClassList(
      process.env.RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE,
      "RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE",
    ),
    pollIntervalMs: parseIntEnv(process.env.RUNTIME_CONTROLLER_WORKER_POLL_INTERVAL_MS, 2000),
    leaseDurationSec: parseIntEnv(process.env.RUNTIME_CONTROLLER_WORKER_LEASE_DURATION_SEC, 120),
    heartbeatIntervalSec: parseIntEnv(process.env.RUNTIME_CONTROLLER_WORKER_HEARTBEAT_INTERVAL_SEC, 30),
    qmdEmbedAdapterMode: parseQmdEmbeddingAdapterMode(process.env.RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER),
    qmdRepoRoot,
    qmdPythonBin: process.env.RUNTIME_CONTROLLER_QMD_PYTHON?.trim() || `${qmdRepoRoot}/.venv/bin/python`,
    qmdBridgeScriptPath: process.env.RUNTIME_CONTROLLER_QMD_EMBED_BRIDGE_SCRIPT?.trim() || defaultQmdEmbeddingBridgeScriptPath(),
  };
}
