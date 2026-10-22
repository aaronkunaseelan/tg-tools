import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeJobResultRow, mapRuntimeJobRow, type RuntimeJobAttemptRow, type RuntimeJobResultRow, type RuntimeJobRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { conflict, notFound } from "../errors/controller-error.js";
import type { RuntimeJob, RuntimeJobAttempt, RuntimeJobResult } from "../types/runtime.js";
import { markWorkerDrainedIfIdle } from "./workers.js";

export interface RetryDecisionInput {
  attemptCount: number;
  maxAttempts: number;
  deadlineAt: Date | null;
}

export interface LeaseReconcileOptions {
  limit?: number;
  baseBackoffSec?: number;
  maxBackoffSec?: number;
  now?: Date;
}

export interface DeadlineReconcileOptions {
  limit?: number;
  now?: Date;
}

export interface CancelRequest {
  jobId: string;
  reason?: string | null;
  now?: Date;
}

export interface LeaseReconcileItem {
  action: "retried" | "dead";
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
  result: RuntimeJobResult | null;
}

export interface DeadlineReconcileItem {
  action: "deadline_dead";
  job: RuntimeJob;
  result: RuntimeJobResult;
}

export interface CancelResult {
  job: RuntimeJob;
  result: RuntimeJobResult;
}

export function attemptsRemaining(input: RetryDecisionInput): number {
  return Math.max(0, input.maxAttempts - input.attemptCount);
}

export function shouldRetryExpiredLease(input: RetryDecisionInput, now = new Date()): boolean {
  if (attemptsRemaining(input) <= 0) return false;
  if (input.deadlineAt && input.deadlineAt.getTime() <= now.getTime()) return false;
  return true;
}

export function canCancelBeforeStart(status: RuntimeJob["status"]): boolean {
  return status === "pending" || status === "retryable";
}

export function computeBackoffSeconds(attemptCount: number, baseBackoffSec = 60, maxBackoffSec = 3600): number {
  const multiplier = Math.max(0, attemptCount - 1);
  return Math.min(maxBackoffSec, baseBackoffSec * Math.pow(2, multiplier));
}

export async function reconcileExpiredLeases(
  db: pg.Pool | pg.PoolClient,
  options: LeaseReconcileOptions = {},
): Promise<LeaseReconcileItem[]> {
  const limit = options.limit ?? 100;
  const items: LeaseReconcileItem[] = [];

  for (let i = 0; i < limit; i += 1) {
    const item = await withTransaction(db, async (client) => {
      const now = options.now ?? new Date();
      const locked = await client.query<{ job_id: string; attempt_id: string }>(
        `SELECT j.job_id, a.attempt_id
           FROM runtime.jobs j
           JOIN runtime.job_attempts a
             ON a.job_id = j.job_id
            AND a.lease_token = j.lease_token
            AND a.status IN ('leased', 'running')
          WHERE j.status = 'leased'
            AND j.lease_expires_at < $1::timestamptz
          ORDER BY j.lease_expires_at ASC, j.job_id ASC
          LIMIT 1
          FOR UPDATE OF j, a SKIP LOCKED`,
        [now.toISOString()],
      );

      if (locked.rowCount !== 1) {
        return null;
      }

      const { job_id: jobId, attempt_id: attemptId } = locked.rows[0]!;
      const jobResult = await client.query<RuntimeJobRow>(`SELECT * FROM runtime.jobs WHERE job_id = $1::uuid`, [jobId]);
      const attemptResult = await client.query<RuntimeJobAttemptRow>(`SELECT * FROM runtime.job_attempts WHERE attempt_id = $1::uuid`, [attemptId]);
      const job = mapRuntimeJobRow(jobResult.rows[0]!);
      const attempt = mapRuntimeJobAttemptRow(attemptResult.rows[0]!);

      const expiredAttemptResult = await client.query<RuntimeJobAttemptRow>(
        `UPDATE runtime.job_attempts
            SET status = 'expired',
                completed_at = $3::timestamptz,
                error_class = COALESCE(error_class, 'lease_expired'),
                error_text = COALESCE(error_text, 'Lease expired before fenced finalize')
          WHERE attempt_id = $1::uuid
            AND job_id = $2::uuid
          RETURNING *`,
        [attempt.attemptId, job.jobId, now.toISOString()],
      );
      const expiredAttempt = mapRuntimeJobAttemptRow(expiredAttemptResult.rows[0]!);

      if (shouldRetryExpiredLease({
        attemptCount: job.attemptCount,
        maxAttempts: job.maxAttempts,
        deadlineAt: job.deadlineAt ? new Date(job.deadlineAt) : null,
      }, now)) {
        const backoffSec = computeBackoffSeconds(job.attemptCount, options.baseBackoffSec ?? 60, options.maxBackoffSec ?? 3600);
        const retriedJobResult = await client.query<RuntimeJobRow>(
          `UPDATE runtime.jobs
              SET status = 'retryable',
                  runnable_at = $2::timestamptz,
                  lease_owner = NULL,
                  lease_token = NULL,
                  lease_expires_at = NULL,
                  last_error_class = 'lease_expired',
                  last_error_text = 'Lease expired before fenced finalize'
            WHERE job_id = $1::uuid
            RETURNING *`,
          [job.jobId, new Date(now.getTime() + backoffSec * 1000).toISOString()],
        );

        await markWorkerDrainedIfIdle(client, expiredAttempt.workerId);
        return {
          action: "retried" as const,
          job: mapRuntimeJobRow(retriedJobResult.rows[0]!),
          attempt: expiredAttempt,
          result: null,
        };
      }

      const resultInsert = await client.query<RuntimeJobResultRow>(
        `INSERT INTO runtime.job_results (
            job_id,
            attempt_id,
            result_owner,
            result_route,
            result_kind,
            error_class,
            error_text,
            created_at
         )
         VALUES ($1::uuid, $2::uuid, $3, $4, 'dead', 'lease_expired', 'Lease expired before fenced finalize', $5::timestamptz)
         RETURNING *`,
        [job.jobId, attempt.attemptId, job.resultOwner, job.resultRoute, now.toISOString()],
      );
      const result = mapRuntimeJobResultRow(resultInsert.rows[0]!);

      const deadJobResult = await client.query<RuntimeJobRow>(
        `UPDATE runtime.jobs
            SET status = 'dead',
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                terminal_result_id = $2::uuid,
                last_error_class = 'lease_expired',
                last_error_text = 'Lease expired before fenced finalize'
          WHERE job_id = $1::uuid
          RETURNING *`,
        [job.jobId, result.resultId],
      );

      await markWorkerDrainedIfIdle(client, expiredAttempt.workerId);
      return {
        action: "dead" as const,
        job: mapRuntimeJobRow(deadJobResult.rows[0]!),
        attempt: expiredAttempt,
        result,
      };
    });

    if (!item) break;
    items.push(item);
  }

  return items;
}

export async function reconcileExpiredDeadlines(
  db: pg.Pool | pg.PoolClient,
  options: DeadlineReconcileOptions = {},
): Promise<DeadlineReconcileItem[]> {
  const limit = options.limit ?? 100;
  const items: DeadlineReconcileItem[] = [];

  for (let i = 0; i < limit; i += 1) {
    const item = await withTransaction(db, async (client) => {
      const now = options.now ?? new Date();
      const locked = await client.query<{ job_id: string }>(
        `SELECT job_id
           FROM runtime.jobs
          WHERE status IN ('pending', 'retryable')
            AND deadline_at IS NOT NULL
            AND deadline_at <= $1::timestamptz
          ORDER BY deadline_at ASC, job_id ASC
          LIMIT 1
          FOR UPDATE SKIP LOCKED`,
        [now.toISOString()],
      );

      if (locked.rowCount !== 1) {
        return null;
      }

      const jobResult = await client.query<RuntimeJobRow>(
        `SELECT * FROM runtime.jobs WHERE job_id = $1::uuid`,
        [locked.rows[0]!.job_id],
      );
      const job = mapRuntimeJobRow(jobResult.rows[0]!);

      const resultInsert = await client.query<RuntimeJobResultRow>(
        `INSERT INTO runtime.job_results (
            job_id,
            attempt_id,
            result_owner,
            result_route,
            result_kind,
            error_class,
            error_text,
            created_at
         )
         VALUES ($1::uuid, NULL, $2, $3, 'dead', 'deadline_expired', 'Deadline expired before claim', $4::timestamptz)
         RETURNING *`,
        [job.jobId, job.resultOwner, job.resultRoute, now.toISOString()],
      );
      const result = mapRuntimeJobResultRow(resultInsert.rows[0]!);

      const deadJobResult = await client.query<RuntimeJobRow>(
        `UPDATE runtime.jobs
            SET status = 'dead',
                terminal_result_id = $2::uuid,
                last_error_class = 'deadline_expired',
                last_error_text = 'Deadline expired before claim'
          WHERE job_id = $1::uuid
          RETURNING *`,
        [job.jobId, result.resultId],
      );

      return {
        action: "deadline_dead" as const,
        job: mapRuntimeJobRow(deadJobResult.rows[0]!),
        result,
      };
    });

    if (!item) break;
    items.push(item);
  }

  return items;
}

export async function cancelJobBeforeStart(
  db: pg.Pool | pg.PoolClient,
  request: CancelRequest,
): Promise<CancelResult> {
  return withTransaction(db, async (client) => {
    const now = request.now ?? new Date();
    const jobResult = await client.query<RuntimeJobRow>(
      `SELECT *
         FROM runtime.jobs
        WHERE job_id = $1::uuid
        FOR UPDATE`,
      [request.jobId],
    );
    if (jobResult.rowCount !== 1) {
      throw notFound("job_not_found", `job not found: ${request.jobId}`, { jobId: request.jobId });
    }

    const job = mapRuntimeJobRow(jobResult.rows[0]!);
    if (!canCancelBeforeStart(job.status)) {
      throw conflict("invalid_job_state", `job ${job.jobId} cannot be cancelled from status ${job.status}`, {
        jobId: job.jobId,
        status: job.status,
      });
    }

    const reason = request.reason?.trim() || "Cancelled before start";

    const resultInsert = await client.query<RuntimeJobResultRow>(
      `INSERT INTO runtime.job_results (
          job_id,
          attempt_id,
          result_owner,
          result_route,
          result_kind,
          error_class,
          error_text,
          created_at
       )
       VALUES ($1::uuid, NULL, $2, $3, 'cancelled', 'cancelled', $4, $5::timestamptz)
       RETURNING *`,
      [job.jobId, job.resultOwner, job.resultRoute, reason, now.toISOString()],
    );
    const result = mapRuntimeJobResultRow(resultInsert.rows[0]!);

    const cancelledJobResult = await client.query<RuntimeJobRow>(
      `UPDATE runtime.jobs
          SET status = 'cancelled',
              terminal_result_id = $2::uuid,
              last_error_class = 'cancelled',
              last_error_text = $3
        WHERE job_id = $1::uuid
        RETURNING *`,
      [job.jobId, result.resultId, reason],
    );

    return {
      job: mapRuntimeJobRow(cancelledJobResult.rows[0]!),
      result,
    };
  });
}

export const RECONCILE_BUNDLE_NOTES = {
  leaseExpiryRecovery: "mark attempt expired, then retry or dead-letter based on attempts_remaining and deadline_at",
  deadlineExpiryBeforeClaim: "terminalize pending/retryable job with attempt_id = null",
  preStartCancellation: "cancel pending/retryable job with attempt_id = null",
} as const;
