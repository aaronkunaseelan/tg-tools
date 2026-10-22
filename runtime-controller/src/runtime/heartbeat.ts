import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeJobRow, type RuntimeJobAttemptRow, type RuntimeJobRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { conflict, notFound } from "../errors/controller-error.js";
import type { RuntimeJob, RuntimeJobAttempt } from "../types/runtime.js";
import { lockedAuthorityTime, requireCurrentAttempt } from "./lease-authority.js";

export interface AttemptLeaseRef {
  jobId: string;
  attemptId: string;
  leaseToken: string;
}

export interface LeaseHeartbeatRequest extends AttemptLeaseRef {
  leaseDurationSec?: number;
  now?: Date;
}

export interface RunningRequest extends AttemptLeaseRef {
  now?: Date;
}

export interface ActiveLeaseView {
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}

export function leaseIsExpired(leaseExpiresAt: Date, now = new Date()): boolean {
  return leaseExpiresAt.getTime() <= now.getTime();
}

export function extendLease(currentLeaseExpiresAt: Date, extensionMs: number): Date {
  return new Date(currentLeaseExpiresAt.getTime() + extensionMs);
}

export function canHeartbeat(expectedLeaseToken: string, actualLeaseToken: string | null): boolean {
  return Boolean(actualLeaseToken) && actualLeaseToken === expectedLeaseToken;
}

async function lockActiveLease(client: pg.PoolClient, ref: AttemptLeaseRef): Promise<ActiveLeaseView> {
  const jobResult = await client.query<RuntimeJobRow>(
    `SELECT *
       FROM runtime.jobs
      WHERE job_id = $1::uuid
      FOR UPDATE`,
    [ref.jobId],
  );
  if (jobResult.rowCount !== 1) {
    throw notFound("job_not_found", `job not found: ${ref.jobId}`, { jobId: ref.jobId });
  }

  const attemptResult = await client.query<RuntimeJobAttemptRow>(
    `SELECT *
       FROM runtime.job_attempts
      WHERE job_id = $1::uuid
        AND attempt_id = $2::uuid
      FOR UPDATE`,
    [ref.jobId, ref.attemptId],
  );
  if (attemptResult.rowCount !== 1) {
    throw notFound("attempt_not_found", `attempt not found: ${ref.attemptId}`, {
      jobId: ref.jobId,
      attemptId: ref.attemptId,
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
  if (!canHeartbeat(ref.leaseToken, job.leaseToken) || !canHeartbeat(ref.leaseToken, attempt.leaseToken)) {
    throw conflict("stale_lease_token", `stale lease token for job ${job.jobId} attempt ${attempt.attemptId}`, {
      jobId: job.jobId,
      attemptId: attempt.attemptId,
    });
  }

  return { job, attempt };
}

export async function markAttemptRunning(
  db: pg.Pool | pg.PoolClient,
  request: RunningRequest,
): Promise<ActiveLeaseView> {
  return withTransaction(db, async (client) => {
    const active = await lockActiveLease(client, request);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentAttempt(active.job, active.attempt, now);
    if (active.attempt.status === "running") {
      return active;
    }

    const attemptResult = await client.query<RuntimeJobAttemptRow>(
      `UPDATE runtime.job_attempts
          SET status = 'running',
              heartbeat_at = $4::timestamptz
        WHERE job_id = $1::uuid
          AND attempt_id = $2::uuid
          AND lease_token = $3::uuid
          AND status = 'leased'
        RETURNING *`,
      [request.jobId, request.attemptId, request.leaseToken, now.toISOString()],
    );

    return {
      job: active.job,
      attempt: mapRuntimeJobAttemptRow(attemptResult.rows[0]!),
    };
  });
}

export async function heartbeatAttempt(
  db: pg.Pool | pg.PoolClient,
  request: LeaseHeartbeatRequest,
): Promise<ActiveLeaseView> {
  return withTransaction(db, async (client) => {
    const active = await lockActiveLease(client, request);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentAttempt(active.job, active.attempt, now);
    const leaseExpiresAt = new Date(now.getTime() + (request.leaseDurationSec ?? 300) * 1000).toISOString();

    const jobResult = await client.query<RuntimeJobRow>(
      `UPDATE runtime.jobs
          SET lease_expires_at = $3::timestamptz
        WHERE job_id = $1::uuid
          AND status = 'leased'
          AND lease_token = $2::uuid
        RETURNING *`,
      [request.jobId, request.leaseToken, leaseExpiresAt],
    );

    const attemptResult = await client.query<RuntimeJobAttemptRow>(
      `UPDATE runtime.job_attempts
          SET heartbeat_at = $4::timestamptz,
              lease_expires_at = $5::timestamptz
        WHERE job_id = $1::uuid
          AND attempt_id = $2::uuid
          AND lease_token = $3::uuid
          AND status IN ('leased', 'running')
        RETURNING *`,
      [request.jobId, request.attemptId, request.leaseToken, now.toISOString(), leaseExpiresAt],
    );

    if (jobResult.rowCount !== 1 || attemptResult.rowCount !== 1) {
      throw conflict("lost_lease", `lost lease for job ${request.jobId} attempt ${request.attemptId}`, {
        jobId: request.jobId,
        attemptId: request.attemptId,
      });
    }

    return {
      job: mapRuntimeJobRow(jobResult.rows[0]!),
      attempt: mapRuntimeJobAttemptRow(attemptResult.rows[0]!),
    };
  });
}
