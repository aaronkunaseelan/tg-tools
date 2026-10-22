import { randomUUID } from "node:crypto";
import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeJobRow, type RuntimeJobAttemptRow, type RuntimeJobRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest } from "../errors/controller-error.js";
import { evaluatePlacement, type EvaluatePlacementOptions } from "../placement/index.js";
import type { PlacementDecision, PlacementWorkerSnapshot } from "../placement/types.js";
import { QUEUE_CLASSES, type QueueClass, type RuntimeJob, type RuntimeJobAttempt, type RuntimeWorker, type WorkerVenue } from "../types/runtime.js";
import { countActiveAttemptsForWorker, describeWorkerAvailability, markWorkerClaimed, normalizeWorkerVenue, touchWorkerFromClaim } from "./workers.js";

export interface ClaimableJobView {
  status: RuntimeJob["status"];
  runnableAt: Date;
  deadlineAt: Date | null;
  executionProfile: string;
  queueClass: QueueClass;
  producerSystem: string;
}

export interface ClaimRequest {
  workerId: string;
  workerVenue?: WorkerVenue;
  supportedExecutionProfiles?: readonly string[];
  workloadTypes?: readonly string[];
  producerSystems?: readonly string[];
  allowedQueueClasses?: readonly QueueClass[];
  queuePreference?: readonly QueueClass[];
  workerCapacity?: number;
  workerMetadataJson?: Record<string, unknown> | null;
  leaseDurationSec?: number;
  maxCandidateRows?: number;
  softCapsByProducer?: Record<string, number>;
  now?: Date;
}

export interface ClaimRuntimeJobOptions extends EvaluatePlacementOptions {
  workerHeartbeatTtlSec?: number;
}

export interface ClaimedJob {
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}

export function isClaimableJob(job: ClaimableJobView, now = new Date()): boolean {
  if (job.status !== "pending" && job.status !== "retryable") return false;
  if (job.runnableAt.getTime() > now.getTime()) return false;
  if (job.deadlineAt && job.deadlineAt.getTime() <= now.getTime()) return false;
  return true;
}

export function supportsExecutionProfile(jobExecutionProfile: string, workerExecutionProfiles: readonly string[]): boolean {
  return workerExecutionProfiles.includes(jobExecutionProfile);
}

export function passesSoftInFlightCap(inFlightCount: number, targetCap: number): boolean {
  return inFlightCount < targetCap;
}

const DEFAULT_QUEUE_PREFERENCE: readonly QueueClass[] = ["ops", "foreground", "background"] as const;
const VALID_QUEUE_CLASSES = new Set<string>(QUEUE_CLASSES);

export { normalizeWorkerVenue };

export function normalizeTextFilters(values: readonly string[] | undefined, fieldName: string): string[] | null {
  if (!values) return null;
  const normalized = values.map((value) => value.trim()).filter(Boolean);
  if (normalized.length === 0) {
    throw badRequest("invalid_claim_filter", `${fieldName} must not be empty when provided`, { fieldName });
  }
  return Array.from(new Set(normalized));
}

export function normalizeQueueClassFilters(
  values: readonly string[] | undefined,
  fieldName: string,
): QueueClass[] | null {
  const normalized = normalizeTextFilters(values, fieldName);
  if (!normalized) return null;
  for (const value of normalized) {
    if (!VALID_QUEUE_CLASSES.has(value)) {
      throw badRequest("invalid_claim_filter", `${fieldName} contains invalid queue class: ${value}`, {
        fieldName,
        value,
      });
    }
  }
  return normalized as QueueClass[];
}

function queueWeight(queueClass: QueueClass, preference: readonly QueueClass[]): number {
  const idx = preference.indexOf(queueClass);
  return idx === -1 ? preference.length : idx;
}

async function countInFlightForProducer(
  client: pg.PoolClient,
  producerSystem: string,
  executionProfile: string,
  queueClass: QueueClass,
): Promise<number> {
  const result = await client.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.jobs
      WHERE producer_system = $1
        AND execution_profile = $2
        AND queue_class = $3
        AND status = 'leased'`,
    [producerSystem, executionProfile, queueClass],
  );
  return Number(result.rows[0]?.count ?? 0);
}

function effectiveStringFilter(
  requestValues: readonly string[] | undefined,
  storedValues: readonly string[],
  fieldName: string,
): string[] | null {
  return normalizeTextFilters(requestValues ?? (storedValues.length > 0 ? storedValues : undefined), fieldName);
}

function effectiveQueueFilter(
  requestValues: readonly string[] | undefined,
  storedValues: readonly string[],
  fieldName: string,
): QueueClass[] | null {
  return normalizeQueueClassFilters(requestValues ?? (storedValues.length > 0 ? storedValues : undefined), fieldName);
}

function buildWorkerSnapshot(worker: RuntimeWorker, activeLeaseCount: number): PlacementWorkerSnapshot {
  return {
    workerId: worker.workerId,
    venue: worker.venue,
    state: worker.state,
    capacity: worker.capacity,
    activeLeaseCount,
  };
}

function buildClaimMetadata(
  worker: RuntimeWorker,
  activeLeaseCount: number,
  candidateLimit: number,
  queuePreference: readonly QueueClass[],
  placement: PlacementDecision,
): Record<string, unknown> {
  return {
    worker: {
      workerId: worker.workerId,
      venue: worker.venue,
      state: worker.state,
      capacity: worker.capacity,
      activeLeaseCount,
    },
    selection: {
      queuePreference,
      candidateLimit,
    },
    placement: {
      policy: placement.policy,
      allowed: placement.allowed,
      reason: placement.reason,
      scoreAdjust: placement.scoreAdjust,
      details: placement.details ?? {},
    },
  };
}

export async function claimRuntimeJob(
  db: pg.Pool | pg.PoolClient,
  request: ClaimRequest,
  options: ClaimRuntimeJobOptions = {},
): Promise<ClaimedJob | null> {
  if (!request.workerId.trim()) {
    throw badRequest("invalid_claim_request", "workerId is required");
  }

  return withTransaction(db, async (client) => {
    const now = request.now ?? new Date();
    const worker = await touchWorkerFromClaim(client, {
      workerId: request.workerId,
      workerVenue: request.workerVenue,
      supportedExecutionProfiles: request.supportedExecutionProfiles,
      workloadTypes: request.workloadTypes,
      producerSystems: request.producerSystems,
      allowedQueueClasses: request.allowedQueueClasses,
      queuePreference: request.queuePreference,
      capacity: request.workerCapacity,
      metadataJson: request.workerMetadataJson,
      now,
    });

    const activeLeaseCount = await countActiveAttemptsForWorker(client, worker.workerId);
    const workerAvailability = describeWorkerAvailability(worker, activeLeaseCount, {
      now,
      heartbeatTtlSec: options.workerHeartbeatTtlSec,
    });
    if (!workerAvailability.available) {
      return null;
    }

    const supportedExecutionProfiles = normalizeTextFilters(
      request.supportedExecutionProfiles ?? (worker.supportedExecutionProfiles.length > 0 ? worker.supportedExecutionProfiles : undefined),
      "supportedExecutionProfiles",
    );
    if (!supportedExecutionProfiles || supportedExecutionProfiles.length === 0) {
      throw badRequest("invalid_claim_request", "supportedExecutionProfiles must not be empty");
    }

    const queuePreference = effectiveQueueFilter(request.queuePreference, worker.queuePreference, "queuePreference") ?? DEFAULT_QUEUE_PREFERENCE;
    const workloadTypes = request.workloadTypes === undefined
      ? effectiveStringFilter(undefined, worker.supportedWorkloadTypes, "workloadTypes")
      : normalizeTextFilters(request.workloadTypes, "workloadTypes");
    const producerSystems = request.producerSystems === undefined
      ? effectiveStringFilter(undefined, worker.allowedProducerSystems, "producerSystems")
      : normalizeTextFilters(request.producerSystems, "producerSystems");
    const allowedQueueClasses = request.allowedQueueClasses === undefined
      ? effectiveQueueFilter(undefined, worker.allowedQueueClasses, "allowedQueueClasses")
      : normalizeQueueClassFilters(request.allowedQueueClasses, "allowedQueueClasses");
    const candidateLimit = request.maxCandidateRows ?? 25;

    const whereClauses = [
      `status IN ('pending', 'retryable')`,
      `runnable_at <= $1::timestamptz`,
      `(deadline_at IS NULL OR deadline_at > $1::timestamptz)`,
      `execution_profile = ANY($2::text[])`,
    ];
    const params: unknown[] = [now.toISOString(), supportedExecutionProfiles];

    if (workloadTypes) {
      params.push(workloadTypes);
      whereClauses.push(`workload_type = ANY($${params.length}::text[])`);
    }

    if (producerSystems) {
      params.push(producerSystems);
      whereClauses.push(`producer_system = ANY($${params.length}::text[])`);
    }

    if (allowedQueueClasses) {
      params.push(allowedQueueClasses);
      whereClauses.push(`queue_class = ANY($${params.length}::text[])`);
    }

    params.push(candidateLimit);
    const limitParam = `$${params.length}`;

    const candidatesResult = await client.query<RuntimeJobRow>(
      `SELECT *
         FROM runtime.jobs
        WHERE ${whereClauses.join("\n          AND ")}
        ORDER BY priority DESC, runnable_at ASC, created_at ASC, job_id ASC
        LIMIT ${limitParam}
        FOR UPDATE SKIP LOCKED`,
      params,
    );

    const candidates = candidatesResult.rows.map(mapRuntimeJobRow);
    const workerSnapshot = buildWorkerSnapshot(worker, activeLeaseCount);

    const eligible: Array<{ job: RuntimeJob; placement: PlacementDecision }> = [];
    for (const candidate of candidates) {
      const producerCap = request.softCapsByProducer?.[candidate.producerSystem];
      if (producerCap != null) {
        const inFlightCount = await countInFlightForProducer(
          client,
          candidate.producerSystem,
          candidate.executionProfile,
          candidate.queueClass,
        );
        if (!passesSoftInFlightCap(inFlightCount, producerCap)) {
          continue;
        }
      }

      const placement = await evaluatePlacement(candidate, workerSnapshot, options);
      if (!placement.allowed) {
        continue;
      }
      eligible.push({ job: candidate, placement });
    }

    eligible.sort((left, right) => {
      const queueCmp = queueWeight(left.job.queueClass, queuePreference) - queueWeight(right.job.queueClass, queuePreference);
      if (queueCmp !== 0) return queueCmp;
      const placementCmp = right.placement.scoreAdjust - left.placement.scoreAdjust;
      if (placementCmp !== 0) return placementCmp;
      if (left.job.priority !== right.job.priority) return right.job.priority - left.job.priority;
      return left.job.runnableAt.localeCompare(right.job.runnableAt);
    });

    const selected = eligible[0];
    if (!selected) {
      return null;
    }

    const leaseToken = randomUUID();
    const leaseExpiresAt = new Date(now.getTime() + (request.leaseDurationSec ?? 300) * 1000).toISOString();

    const updatedJobResult = await client.query<RuntimeJobRow>(
      `UPDATE runtime.jobs
          SET status = 'leased',
              attempt_count = attempt_count + 1,
              lease_owner = $2,
              lease_token = $3::uuid,
              lease_expires_at = $4::timestamptz
        WHERE job_id = $1::uuid
        RETURNING *`,
      [selected.job.jobId, worker.workerId, leaseToken, leaseExpiresAt],
    );

    const updatedJob = mapRuntimeJobRow(updatedJobResult.rows[0]!);
    const claimJson = buildClaimMetadata(worker, activeLeaseCount, candidateLimit, queuePreference, selected.placement);

    const insertedAttemptResult = await client.query<RuntimeJobAttemptRow>(
      `INSERT INTO runtime.job_attempts (
          job_id,
          attempt_no,
          worker_id,
          execution_profile,
          lease_token,
          lease_expires_at,
          status,
          started_at,
          heartbeat_at,
          claim_json
       )
       VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6::timestamptz, 'leased', $7::timestamptz, $7::timestamptz, $8::jsonb)
       RETURNING *`,
      [
        updatedJob.jobId,
        updatedJob.attemptCount,
        worker.workerId,
        updatedJob.executionProfile,
        leaseToken,
        leaseExpiresAt,
        now.toISOString(),
        JSON.stringify(claimJson),
      ],
    );

    await markWorkerClaimed(client, worker.workerId, now);

    return {
      job: updatedJob,
      attempt: mapRuntimeJobAttemptRow(insertedAttemptResult.rows[0]!),
    };
  });
}

export const CLAIM_BUNDLE_NOTES = [
  "touch worker membership and gate claims by worker state/capacity",
  "select one claimable job FOR UPDATE SKIP LOCKED",
  "verify status, runnable_at, deadline_at, execution_profile compatibility, queue_class compatibility, placement, and optional claim filters",
  "increment attempt_count and mint a fresh lease_token",
  "update runtime.jobs to leased",
  "insert runtime.job_attempts row in leased state with claim_json metadata",
].join("; ");
