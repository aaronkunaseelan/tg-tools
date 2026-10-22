import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeWorkerRow, type RuntimeJobAttemptRow, type RuntimeWorkerRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest, notFound } from "../errors/controller-error.js";
import { QUEUE_CLASSES, WORKER_STATES, WORKER_VENUES, type QueueClass, type RuntimeJobAttempt, type RuntimeWorker, type WorkerState, type WorkerVenue } from "../types/runtime.js";

export interface RegisterWorkerRequest {
  workerId: string;
  workerVenue?: WorkerVenue;
  supportedExecutionProfiles: readonly string[];
  workloadTypes?: readonly string[];
  producerSystems?: readonly string[];
  allowedQueueClasses?: readonly QueueClass[];
  queuePreference?: readonly QueueClass[];
  capacity?: number;
  metadataJson?: Record<string, unknown> | null;
  now?: Date;
}

export interface ClaimWorkerTouchRequest {
  workerId: string;
  workerVenue?: WorkerVenue;
  supportedExecutionProfiles?: readonly string[];
  workloadTypes?: readonly string[];
  producerSystems?: readonly string[];
  allowedQueueClasses?: readonly QueueClass[];
  queuePreference?: readonly QueueClass[];
  capacity?: number;
  metadataJson?: Record<string, unknown> | null;
  now?: Date;
}

export interface WorkerHeartbeatRequest {
  workerId: string;
  metadataJson?: Record<string, unknown> | null;
  now?: Date;
}

export const DEFAULT_WORKER_HEARTBEAT_TTL_SEC = 120;

export interface WorkerAvailabilityOptions {
  now?: Date;
  heartbeatTtlSec?: number;
}

export interface RuntimeWorkerAvailability extends RuntimeWorker {
  heartbeatAgeSec: number;
  heartbeatFresh: boolean;
  activeLeaseCount: number;
  availableCapacity: number;
  available: boolean;
}

export interface WorkerQuery extends WorkerAvailabilityOptions {
  state?: WorkerState;
  venue?: WorkerVenue;
  limit?: number;
}

export interface WorkerListResult {
  items: RuntimeWorkerAvailability[];
  count: number;
}

export interface WorkerControl {
  action: "continue" | "drain";
  reason: string | null;
  deadlineAt: string | null;
}

export interface WorkerTouchResult {
  worker: RuntimeWorker;
  control: WorkerControl;
}

export interface DrainWorkerRequest {
  workerId: string;
  reason?: string | null;
  gracePeriodSec?: number;
  requestedBy?: string | null;
  now?: Date;
}

export interface DrainWorkerResult {
  worker: RuntimeWorker;
  activeAttempts: RuntimeJobAttempt[];
}

export interface ResumeWorkerRequest {
  workerId: string;
  now?: Date;
}

const VALID_QUEUE_CLASSES = new Set<string>(QUEUE_CLASSES);
const VALID_WORKER_VENUES = new Set<string>(WORKER_VENUES);
const VALID_WORKER_STATES = new Set<string>(WORKER_STATES);

function normalizeHeartbeatTtlSec(value: number | undefined): number {
  const ttl = value ?? DEFAULT_WORKER_HEARTBEAT_TTL_SEC;
  if (!Number.isFinite(ttl) || ttl <= 0) {
    throw new Error(`worker heartbeat TTL must be a positive number: ${ttl}`);
  }
  return ttl;
}

export function getWorkerHeartbeatAgeSec(lastHeartbeatAt: string, now = new Date()): number {
  return Math.max(0, Math.floor((now.getTime() - new Date(lastHeartbeatAt).getTime()) / 1000));
}

export function isWorkerHeartbeatFresh(
  worker: Pick<RuntimeWorker, "lastHeartbeatAt">,
  options: WorkerAvailabilityOptions = {},
): boolean {
  const now = options.now ?? new Date();
  const ttlSec = normalizeHeartbeatTtlSec(options.heartbeatTtlSec);
  return now.getTime() - new Date(worker.lastHeartbeatAt).getTime() <= ttlSec * 1000;
}

export function describeWorkerAvailability(
  worker: RuntimeWorker,
  activeLeaseCount: number,
  options: WorkerAvailabilityOptions = {},
): RuntimeWorkerAvailability {
  const heartbeatAgeSec = getWorkerHeartbeatAgeSec(worker.lastHeartbeatAt, options.now);
  const heartbeatFresh = isWorkerHeartbeatFresh(worker, options);
  const availableCapacity = heartbeatFresh && worker.state === "active"
    ? Math.max(0, worker.capacity - activeLeaseCount)
    : 0;
  return {
    ...worker,
    heartbeatAgeSec,
    heartbeatFresh,
    activeLeaseCount,
    availableCapacity,
    available: availableCapacity > 0,
  };
}

function trimOrNull(value: string | undefined | null): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function normalizeStringList(
  values: readonly string[] | undefined,
  fieldName: string,
  options: { allowEmpty?: boolean } = {},
): string[] {
  if (!values) return [];
  const normalized = values.map((value) => value.trim()).filter(Boolean);
  if (normalized.length === 0 && !options.allowEmpty) {
    throw badRequest("invalid_worker_request", `${fieldName} must not be empty`, { fieldName });
  }
  return Array.from(new Set(normalized));
}

function normalizeQueueClassList(
  values: readonly string[] | undefined,
  fieldName: string,
): QueueClass[] {
  const normalized = normalizeStringList(values, fieldName);
  for (const value of normalized) {
    if (!VALID_QUEUE_CLASSES.has(value)) {
      throw badRequest("invalid_worker_request", `${fieldName} contains invalid queue class: ${value}`, {
        fieldName,
        value,
      });
    }
  }
  return normalized as QueueClass[];
}

function normalizeCapacity(value: number | undefined, fieldName = "capacity"): number {
  if (value == null) return 1;
  if (!Number.isInteger(value) || value < 0) {
    throw badRequest("invalid_worker_request", `${fieldName} must be an integer >= 0`, { fieldName, value });
  }
  return value;
}

function normalizeMetadataJson(value: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (value == null) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    throw badRequest("invalid_worker_request", "metadataJson must be a JSON object", { fieldName: "metadataJson" });
  }
  return value;
}

export function normalizeWorkerVenue(value: string | undefined): WorkerVenue {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return "generic";
  if (!VALID_WORKER_VENUES.has(normalized)) {
    throw badRequest("invalid_worker_request", "workerVenue must be one of generic, oci, modal", { value });
  }
  return normalized as WorkerVenue;
}

export function normalizeWorkerState(value: string | undefined, fieldName = "state"): WorkerState {
  const normalized = value?.trim().toLowerCase();
  if (!normalized || !VALID_WORKER_STATES.has(normalized)) {
    throw badRequest("invalid_worker_request", `${fieldName} must be one of active, draining, drained`, {
      fieldName,
      value,
    });
  }
  return normalized as WorkerState;
}

function deriveDrainReason(reason: string | null, requestedBy: string | null): string {
  if (reason && requestedBy) {
    return `${reason} (requestedBy=${requestedBy})`;
  }
  if (reason) return reason;
  if (requestedBy) return `drain_requested (requestedBy=${requestedBy})`;
  return "drain_requested";
}

function deriveWorkerControl(worker: RuntimeWorker): WorkerControl {
  return worker.state === "active"
    ? {
        action: "continue",
        reason: null,
        deadlineAt: null,
      }
    : {
        action: "drain",
        reason: worker.drainReason,
        deadlineAt: worker.drainDeadlineAt,
      };
}

async function loadWorkerRow(
  client: pg.PoolClient,
  workerId: string,
  options: { forUpdate?: boolean } = {},
): Promise<RuntimeWorkerRow | null> {
  const result = await client.query<RuntimeWorkerRow>(
    `SELECT *
       FROM runtime.workers
      WHERE worker_id = $1
      ${options.forUpdate ? "FOR UPDATE" : ""}`,
    [workerId],
  );
  return result.rows[0] ?? null;
}

async function loadWorkerOrThrow(
  client: pg.PoolClient,
  workerId: string,
  options: { forUpdate?: boolean } = {},
): Promise<RuntimeWorkerRow> {
  const row = await loadWorkerRow(client, workerId, options);
  if (!row) {
    throw notFound("worker_not_found", `worker not found: ${workerId}`, { workerId });
  }
  return row;
}

async function upsertWorkerFromRegistration(
  client: pg.PoolClient,
  request: RegisterWorkerRequest,
): Promise<RuntimeWorker> {
  const workerId = trimOrNull(request.workerId);
  if (!workerId) {
    throw badRequest("invalid_worker_request", "workerId is required");
  }

  const supportedExecutionProfiles = normalizeStringList(
    request.supportedExecutionProfiles,
    "supportedExecutionProfiles",
    { allowEmpty: false },
  );
  if (supportedExecutionProfiles.length === 0) {
    throw badRequest("invalid_worker_request", "supportedExecutionProfiles must not be empty");
  }

  const now = request.now ?? new Date();
  const result = await client.query<RuntimeWorkerRow>(
    `INSERT INTO runtime.workers (
        worker_id,
        venue,
        supported_execution_profiles,
        supported_workload_types,
        allowed_producer_systems,
        allowed_queue_classes,
        queue_preference,
        capacity,
        metadata_json,
        last_heartbeat_at
     ) VALUES (
        $1,
        $2,
        $3::text[],
        $4::text[],
        $5::text[],
        $6::text[],
        $7::text[],
        $8,
        $9::jsonb,
        $10::timestamptz
     )
     ON CONFLICT (worker_id) DO UPDATE
        SET venue = EXCLUDED.venue,
            supported_execution_profiles = EXCLUDED.supported_execution_profiles,
            supported_workload_types = EXCLUDED.supported_workload_types,
            allowed_producer_systems = EXCLUDED.allowed_producer_systems,
            allowed_queue_classes = EXCLUDED.allowed_queue_classes,
            queue_preference = EXCLUDED.queue_preference,
            capacity = EXCLUDED.capacity,
            metadata_json = runtime.workers.metadata_json || EXCLUDED.metadata_json,
            last_heartbeat_at = EXCLUDED.last_heartbeat_at
     RETURNING *`,
    [
      workerId,
      normalizeWorkerVenue(request.workerVenue),
      supportedExecutionProfiles,
      normalizeStringList(request.workloadTypes, "workloadTypes"),
      normalizeStringList(request.producerSystems, "producerSystems"),
      normalizeQueueClassList(request.allowedQueueClasses, "allowedQueueClasses"),
      normalizeQueueClassList(request.queuePreference, "queuePreference"),
      normalizeCapacity(request.capacity),
      JSON.stringify(normalizeMetadataJson(request.metadataJson)),
      now.toISOString(),
    ],
  );
  return mapRuntimeWorkerRow(result.rows[0]!);
}

async function touchExistingWorkerForClaim(client: pg.PoolClient, request: ClaimWorkerTouchRequest): Promise<RuntimeWorker> {
  const workerId = trimOrNull(request.workerId);
  if (!workerId) {
    throw badRequest("invalid_claim_request", "workerId is required");
  }

  const workerRow = await loadWorkerRow(client, workerId, { forUpdate: true });
  if (!workerRow) {
    throw badRequest(
      "invalid_claim_request",
      `supportedExecutionProfiles must not be empty for unknown worker ${workerId}`,
      { workerId },
    );
  }

  const now = request.now ?? new Date();
  const metadataJson = normalizeMetadataJson(request.metadataJson);
  const result = await client.query<RuntimeWorkerRow>(
    `UPDATE runtime.workers
        SET last_heartbeat_at = $2::timestamptz,
            metadata_json = metadata_json || $3::jsonb
      WHERE worker_id = $1
      RETURNING *`,
    [workerId, now.toISOString(), JSON.stringify(metadataJson)],
  );
  return mapRuntimeWorkerRow(result.rows[0]!);
}

export async function touchWorkerFromClaim(
  db: pg.Pool | pg.PoolClient,
  request: ClaimWorkerTouchRequest,
): Promise<RuntimeWorker> {
  return withTransaction(db, async (client) => {
    if (request.supportedExecutionProfiles && request.supportedExecutionProfiles.length > 0) {
      return upsertWorkerFromRegistration(client, {
        workerId: request.workerId,
        workerVenue: request.workerVenue,
        supportedExecutionProfiles: request.supportedExecutionProfiles,
        workloadTypes: request.workloadTypes,
        producerSystems: request.producerSystems,
        allowedQueueClasses: request.allowedQueueClasses,
        queuePreference: request.queuePreference,
        capacity: request.capacity,
        metadataJson: request.metadataJson,
        now: request.now,
      });
    }
    return touchExistingWorkerForClaim(client, request);
  });
}

export async function countActiveAttemptsForWorker(
  db: pg.Pool | pg.PoolClient,
  workerId: string,
): Promise<number> {
  const result = await db.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.job_attempts
      WHERE worker_id = $1
        AND status IN ('leased', 'running')`,
    [workerId],
  );
  return Number(result.rows[0]?.count ?? 0);
}

export async function listActiveAttemptsForWorker(
  db: pg.Pool | pg.PoolClient,
  workerId: string,
): Promise<RuntimeJobAttempt[]> {
  const result = await db.query<RuntimeJobAttemptRow>(
    `SELECT *
       FROM runtime.job_attempts
      WHERE worker_id = $1
        AND status IN ('leased', 'running')
      ORDER BY lease_expires_at ASC, started_at ASC, attempt_id ASC`,
    [workerId],
  );
  return result.rows.map(mapRuntimeJobAttemptRow);
}

export async function markWorkerClaimed(
  db: pg.Pool | pg.PoolClient,
  workerId: string,
  now = new Date(),
): Promise<RuntimeWorker | null> {
  const result = await db.query<RuntimeWorkerRow>(
    `UPDATE runtime.workers
        SET last_claim_at = $2::timestamptz
      WHERE worker_id = $1
      RETURNING *`,
    [workerId, now.toISOString()],
  );
  if (result.rowCount !== 1) {
    return null;
  }
  return mapRuntimeWorkerRow(result.rows[0]!);
}

export async function markWorkerDrainedIfIdle(
  db: pg.Pool | pg.PoolClient,
  workerId: string,
): Promise<RuntimeWorker | null> {
  return withTransaction(db, async (client) => {
    const workerRow = await loadWorkerRow(client, workerId, { forUpdate: true });
    if (!workerRow) {
      return null;
    }

    if (workerRow.state !== "draining") {
      return mapRuntimeWorkerRow(workerRow);
    }

    const activeCount = await countActiveAttemptsForWorker(client, workerId);
    if (activeCount > 0) {
      return mapRuntimeWorkerRow(workerRow);
    }

    const updated = await client.query<RuntimeWorkerRow>(
      `UPDATE runtime.workers
          SET state = 'drained'
        WHERE worker_id = $1
        RETURNING *`,
      [workerId],
    );
    return mapRuntimeWorkerRow(updated.rows[0]!);
  });
}

export async function registerWorker(
  db: pg.Pool | pg.PoolClient,
  request: RegisterWorkerRequest,
): Promise<WorkerTouchResult> {
  return withTransaction(db, async (client) => {
    const worker = await upsertWorkerFromRegistration(client, request);
    const stabilized = await markWorkerDrainedIfIdle(client, worker.workerId) ?? worker;
    return {
      worker: stabilized,
      control: deriveWorkerControl(stabilized),
    };
  });
}

export async function heartbeatWorker(
  db: pg.Pool | pg.PoolClient,
  request: WorkerHeartbeatRequest,
): Promise<WorkerTouchResult> {
  return withTransaction(db, async (client) => {
    const workerId = trimOrNull(request.workerId);
    if (!workerId) {
      throw badRequest("invalid_worker_request", "workerId is required");
    }
    await loadWorkerOrThrow(client, workerId, { forUpdate: true });
    const result = await client.query<RuntimeWorkerRow>(
      `UPDATE runtime.workers
          SET last_heartbeat_at = $2::timestamptz,
              metadata_json = metadata_json || $3::jsonb
        WHERE worker_id = $1
        RETURNING *`,
      [
        workerId,
        (request.now ?? new Date()).toISOString(),
        JSON.stringify(normalizeMetadataJson(request.metadataJson)),
      ],
    );
    const worker = mapRuntimeWorkerRow(result.rows[0]!);
    const stabilized = await markWorkerDrainedIfIdle(client, worker.workerId) ?? worker;
    return {
      worker: stabilized,
      control: deriveWorkerControl(stabilized),
    };
  });
}

export async function getWorker(
  db: pg.Pool | pg.PoolClient,
  workerId: string,
  options: WorkerAvailabilityOptions = {},
): Promise<RuntimeWorkerAvailability | null> {
  const result = await db.query<RuntimeWorkerRow & { active_lease_count: string }>(
    `SELECT w.*, COUNT(a.attempt_id)::text AS active_lease_count
       FROM runtime.workers w
       LEFT JOIN runtime.job_attempts a
         ON a.worker_id = w.worker_id
        AND a.status IN ('leased', 'running')
      WHERE w.worker_id = $1
      GROUP BY w.worker_id
      LIMIT 1`,
    [workerId],
  );
  if (result.rowCount !== 1) {
    return null;
  }
  const row = result.rows[0]!;
  return describeWorkerAvailability(mapRuntimeWorkerRow(row), Number(row.active_lease_count), options);
}

export async function listWorkers(
  db: pg.Pool | pg.PoolClient,
  query: WorkerQuery,
): Promise<WorkerListResult> {
  const clauses: string[] = [];
  const params: unknown[] = [];

  if (query.state) {
    params.push(normalizeWorkerState(query.state));
    clauses.push(`w.state = $${params.length}`);
  }
  if (query.venue) {
    params.push(normalizeWorkerVenue(query.venue));
    clauses.push(`w.venue = $${params.length}`);
  }

  const whereSql = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
  const limit = Math.max(1, Math.min(query.limit ?? 100, 500));
  const limitParam = `$${params.length + 1}`;

  const itemsResult = await db.query<RuntimeWorkerRow & { active_lease_count: string }>(
    `SELECT w.*, COUNT(a.attempt_id)::text AS active_lease_count
       FROM runtime.workers w
       LEFT JOIN runtime.job_attempts a
         ON a.worker_id = w.worker_id
        AND a.status IN ('leased', 'running')
       ${whereSql}
      GROUP BY w.worker_id
      ORDER BY w.updated_at DESC, w.worker_id ASC
      LIMIT ${limitParam}`,
    [...params, limit],
  );

  const countResult = await db.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.workers w
       ${whereSql}`,
    params,
  );

  return {
    items: itemsResult.rows.map((row) => describeWorkerAvailability(
      mapRuntimeWorkerRow(row),
      Number(row.active_lease_count),
      query,
    )),
    count: Number(countResult.rows[0]?.count ?? 0),
  };
}

export async function drainWorker(
  db: pg.Pool | pg.PoolClient,
  request: DrainWorkerRequest,
): Promise<DrainWorkerResult> {
  return withTransaction(db, async (client) => {
    const workerId = trimOrNull(request.workerId);
    if (!workerId) {
      throw badRequest("invalid_worker_request", "workerId is required");
    }

    await loadWorkerOrThrow(client, workerId, { forUpdate: true });

    const now = request.now ?? new Date();
    const requestedBy = trimOrNull(request.requestedBy);
    const drainReason = deriveDrainReason(trimOrNull(request.reason), requestedBy);
    const gracePeriodSec = request.gracePeriodSec == null ? 300 : normalizeCapacity(request.gracePeriodSec, "gracePeriodSec");
    const drainDeadlineAt = new Date(now.getTime() + gracePeriodSec * 1000).toISOString();

    const updated = await client.query<RuntimeWorkerRow>(
      `UPDATE runtime.workers
          SET state = 'draining',
              drain_requested_at = $2::timestamptz,
              drain_deadline_at = $3::timestamptz,
              drain_reason = $4,
              metadata_json = metadata_json || $5::jsonb
        WHERE worker_id = $1
        RETURNING *`,
      [
        workerId,
        now.toISOString(),
        drainDeadlineAt,
        drainReason,
        JSON.stringify(requestedBy ? { lastDrainRequestedBy: requestedBy } : {}),
      ],
    );

    const activeAttempts = await listActiveAttemptsForWorker(client, workerId);
    const stabilized = activeAttempts.length === 0
      ? await markWorkerDrainedIfIdle(client, workerId) ?? mapRuntimeWorkerRow(updated.rows[0]!)
      : mapRuntimeWorkerRow(updated.rows[0]!);

    return {
      worker: stabilized,
      activeAttempts,
    };
  });
}

export async function resumeWorker(
  db: pg.Pool | pg.PoolClient,
  request: ResumeWorkerRequest,
): Promise<RuntimeWorker> {
  return withTransaction(db, async (client) => {
    const workerId = trimOrNull(request.workerId);
    if (!workerId) {
      throw badRequest("invalid_worker_request", "workerId is required");
    }
    await loadWorkerOrThrow(client, workerId, { forUpdate: true });
    const result = await client.query<RuntimeWorkerRow>(
      `UPDATE runtime.workers
          SET state = 'active',
              drain_requested_at = NULL,
              drain_deadline_at = NULL,
              drain_reason = NULL,
              last_heartbeat_at = COALESCE(last_heartbeat_at, $2::timestamptz)
        WHERE worker_id = $1
        RETURNING *`,
      [workerId, (request.now ?? new Date()).toISOString()],
    );
    return mapRuntimeWorkerRow(result.rows[0]!);
  });
}
