import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeJobResultRow, mapRuntimeJobRow, type RuntimeJobAttemptRow, type RuntimeJobResultRow, type RuntimeJobRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { conflict, notFound } from "../errors/controller-error.js";
import type { RuntimeJob, RuntimeJobAttempt, RuntimeJobResult } from "../types/runtime.js";
import { markWorkerDrainedIfIdle } from "./workers.js";
import { lockedAuthorityTime, requireCurrentAttempt } from "./lease-authority.js";

export interface AbandonRequest {
  jobId: string;
  attemptId: string;
  leaseToken: string;
  reason?: string | null;
  errorClass?: string | null;
  errorText?: string | null;
  backoffSec?: number;
  now?: Date;
}

export interface AbandonResult {
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
  result: RuntimeJobResult | null;
}

function backoffToRunnableAt(now: Date, backoffSec: number): string {
  return new Date(now.getTime() + backoffSec * 1000).toISOString();
}

export function abandonWouldRetry(job: Pick<RuntimeJob, "attemptCount" | "maxAttempts" | "deadlineAt">, now = new Date()): boolean {
  if (job.attemptCount >= job.maxAttempts) {
    return false;
  }
  if (job.deadlineAt && new Date(job.deadlineAt).getTime() <= now.getTime()) {
    return false;
  }
  return true;
}

async function lockAbandonRows(client: pg.PoolClient, request: AbandonRequest): Promise<{
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}> {
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

  const attemptResult = await client.query<RuntimeJobAttemptRow>(
    `SELECT *
       FROM runtime.job_attempts
      WHERE job_id = $1::uuid
        AND attempt_id = $2::uuid
      FOR UPDATE`,
    [request.jobId, request.attemptId],
  );
  if (attemptResult.rowCount !== 1) {
    throw notFound("attempt_not_found", `attempt not found: ${request.attemptId}`, {
      jobId: request.jobId,
      attemptId: request.attemptId,
    });
  }

  const job = mapRuntimeJobRow(jobResult.rows[0]!);
  const attempt = mapRuntimeJobAttemptRow(attemptResult.rows[0]!);

  if (job.status !== "leased") {
    throw conflict("invalid_job_state", `job ${job.jobId} is not leased`, {
      jobId: job.jobId,
      status: job.status,
    });
  }
  if (attempt.status !== "leased" && attempt.status !== "running") {
    throw conflict("invalid_attempt_state", `attempt ${attempt.attemptId} is not active`, {
      attemptId: attempt.attemptId,
      status: attempt.status,
    });
  }
  if (job.leaseToken !== request.leaseToken || attempt.leaseToken !== request.leaseToken) {
    throw conflict("stale_lease_token", `stale lease token for job ${job.jobId} attempt ${attempt.attemptId}`, {
      jobId: job.jobId,
      attemptId: attempt.attemptId,
    });
  }

  return { job, attempt };
}

export async function abandonAttempt(
  db: pg.Pool | pg.PoolClient,
  request: AbandonRequest,
): Promise<AbandonResult> {
  return withTransaction(db, async (client) => {
    const { job, attempt: activeAttempt } = await lockAbandonRows(client, request);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentAttempt(job, activeAttempt, now);
    const errorClass = request.errorClass?.trim() || "abandoned";
    const errorText = request.errorText?.trim() || request.reason?.trim() || "Attempt abandoned before fenced finalize";

    const abandonedAttemptResult = await client.query<RuntimeJobAttemptRow>(
      `UPDATE runtime.job_attempts
          SET status = 'abandoned',
              completed_at = $4::timestamptz,
              error_class = $5,
              error_text = $6
        WHERE job_id = $1::uuid
          AND attempt_id = $2::uuid
          AND lease_token = $3::uuid
        RETURNING *`,
      [request.jobId, request.attemptId, request.leaseToken, now.toISOString(), errorClass, errorText],
    );

    const attempt = mapRuntimeJobAttemptRow(abandonedAttemptResult.rows[0]!);

    if (abandonWouldRetry(job, now)) {
      const retriedJobResult = await client.query<RuntimeJobRow>(
        `UPDATE runtime.jobs
            SET status = 'retryable',
                runnable_at = $3::timestamptz,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_class = $4,
                last_error_text = $5
          WHERE job_id = $1::uuid
            AND lease_token = $2::uuid
            AND status = 'leased'
          RETURNING *`,
        [
          request.jobId,
          request.leaseToken,
          backoffToRunnableAt(now, request.backoffSec ?? 30),
          errorClass,
          errorText,
        ],
      );
      await markWorkerDrainedIfIdle(client, attempt.workerId);
      return {
        job: mapRuntimeJobRow(retriedJobResult.rows[0]!),
        attempt,
        result: null,
      };
    }

    const insertedResult = await client.query<RuntimeJobResultRow>(
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
       VALUES ($1::uuid, $2::uuid, $3, $4, 'dead', $5, $6, $7::timestamptz)
       RETURNING *`,
      [
        request.jobId,
        request.attemptId,
        job.resultOwner,
        job.resultRoute,
        errorClass,
        errorText,
        now.toISOString(),
      ],
    );
    const result = mapRuntimeJobResultRow(insertedResult.rows[0]!);

    const deadJobResult = await client.query<RuntimeJobRow>(
      `UPDATE runtime.jobs
          SET status = 'dead',
              lease_owner = NULL,
              lease_token = NULL,
              lease_expires_at = NULL,
              terminal_result_id = $3::uuid,
              last_error_class = $4,
              last_error_text = $5
        WHERE job_id = $1::uuid
          AND lease_token = $2::uuid
          AND status = 'leased'
        RETURNING *`,
      [request.jobId, request.leaseToken, result.resultId, errorClass, errorText],
    );

    await markWorkerDrainedIfIdle(client, attempt.workerId);

    return {
      job: mapRuntimeJobRow(deadJobResult.rows[0]!),
      attempt,
      result,
    };
  });
}

export const ABANDON_BUNDLE_NOTES = [
  "lock leased job and active attempt with matching lease_token",
  "mark runtime.job_attempts row abandoned",
  "clear leased job state and either requeue with backoff or dead-letter based on attempts/deadline",
  "mark draining workers as drained once no active attempts remain",
].join("; ");
