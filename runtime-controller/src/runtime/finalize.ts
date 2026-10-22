import type pg from "pg";
import { mapRuntimeJobAttemptRow, mapRuntimeJobResultRow, mapRuntimeJobRow, type RuntimeJobAttemptRow, type RuntimeJobResultRow, type RuntimeJobRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest, conflict, internalError, notFound } from "../errors/controller-error.js";
import { getRuntimeArtifact } from "./artifacts.js";
import { validateQmdEmbeddingFinalizeContract, validateQmdEmbeddingOutputArtifact } from "./qmd-embedding-contract.js";
import type { RuntimeJob, RuntimeJobAttempt, RuntimeJobResult, ResultKind } from "../types/runtime.js";
import { markWorkerDrainedIfIdle } from "./workers.js";
import { lockedAuthorityTime, requireCurrentAttempt } from "./lease-authority.js";

export type FinalizeOutcome = "succeeded" | "retryable_error" | "terminal_error";

export interface FinalizeRequest {
  jobId: string;
  attemptId: string;
  leaseToken: string;
  outcome: FinalizeOutcome;
  resultJson?: Record<string, unknown> | null;
  outputArtifactId?: string | null;
  errorClass?: string | null;
  errorText?: string | null;
  telemetryJson?: Record<string, unknown> | null;
  providerReceiptJson?: Record<string, unknown> | null;
  probeSummaryJson?: Record<string, unknown> | null;
  attemptOutputArtifactId?: string | null;
  backoffSec?: number;
  now?: Date;
}

export interface FinalizeResult {
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
  result: RuntimeJobResult | null;
}

export function jobStatusFromFinalizeOutcome(outcome: FinalizeOutcome): RuntimeJob["status"] {
  switch (outcome) {
    case "succeeded":
      return "succeeded";
    case "retryable_error":
      return "retryable";
    case "terminal_error":
      return "dead";
  }
}

export function createsTerminalResult(outcome: FinalizeOutcome): boolean {
  return outcome === "succeeded" || outcome === "terminal_error";
}

export function resultKindFromTerminalJobStatus(status: RuntimeJob["status"]): ResultKind {
  if (status === "succeeded" || status === "dead" || status === "cancelled") {
    return status;
  }
  throw internalError("invalid_result_kind", `non-terminal job status has no result kind: ${status}`, { status });
}

function backoffToRunnableAt(now: Date, backoffSec: number): string {
  return new Date(now.getTime() + backoffSec * 1000).toISOString();
}

function retryWouldExceedAttempts(job: RuntimeJob): boolean {
  return job.attemptCount >= job.maxAttempts;
}

function retryWouldMissDeadline(job: RuntimeJob, now: Date): boolean {
  if (!job.deadlineAt) return false;
  return new Date(job.deadlineAt).getTime() <= now.getTime();
}

async function lockFinalizeRows(client: pg.PoolClient, request: FinalizeRequest): Promise<{
  job: RuntimeJob;
  attempt: RuntimeJobAttempt;
}> {
  const jobResult = await client.query<RuntimeJobRow>(
    `SELECT * FROM runtime.jobs WHERE job_id = $1::uuid FOR UPDATE`,
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

export async function finalizeAttempt(
  db: pg.Pool | pg.PoolClient,
  request: FinalizeRequest,
): Promise<FinalizeResult> {
  return withTransaction(db, async (client) => {
    const { job, attempt: activeAttempt } = await lockFinalizeRows(client, request);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentAttempt(job, activeAttempt, now);

    const contractErrors = validateQmdEmbeddingFinalizeContract(job, request);
    if (contractErrors.length > 0) {
      throw badRequest("invalid_finalize_request", `invalid finalize request: ${contractErrors.join("; ")}`, {
        jobId: request.jobId,
        attemptId: request.attemptId,
      });
    }

    if (job.workloadType === "qmd_embedding" && request.outcome === "succeeded") {
      const outputArtifact = await getRuntimeArtifact(client, request.outputArtifactId ?? "");
      const artifactErrors = validateQmdEmbeddingOutputArtifact(outputArtifact, job, request);
      if (artifactErrors.length > 0) {
        throw badRequest("invalid_finalize_request", `invalid finalize request: ${artifactErrors.join("; ")}`, {
          jobId: request.jobId,
          attemptId: request.attemptId,
          outputArtifactId: request.outputArtifactId ?? null,
        });
      }
    }

    const attemptStatus = request.outcome === "succeeded"
      ? "succeeded"
      : request.outcome === "retryable_error"
        ? "retryable_error"
        : "terminal_error";

    const attemptResult = await client.query<RuntimeJobAttemptRow>(
      `UPDATE runtime.job_attempts
          SET status = $4,
              completed_at = $5::timestamptz,
              error_class = $6,
              error_text = $7,
              telemetry_json = $8::jsonb,
              provider_receipt_json = $9::jsonb,
              probe_summary_json = $10::jsonb,
              attempt_output_artifact_id = $11::uuid
        WHERE job_id = $1::uuid
          AND attempt_id = $2::uuid
          AND lease_token = $3::uuid
        RETURNING *`,
      [
        request.jobId,
        request.attemptId,
        request.leaseToken,
        attemptStatus,
        now.toISOString(),
        request.errorClass ?? null,
        request.errorText ?? null,
        request.telemetryJson ? JSON.stringify(request.telemetryJson) : null,
        request.providerReceiptJson ? JSON.stringify(request.providerReceiptJson) : null,
        request.probeSummaryJson ? JSON.stringify(request.probeSummaryJson) : null,
        request.attemptOutputArtifactId ?? null,
      ],
    );

    const attempt = mapRuntimeJobAttemptRow(attemptResult.rows[0]!);

    if (request.outcome === "retryable_error") {
      const mustDeadLetter = retryWouldExceedAttempts(job) || retryWouldMissDeadline(job, now);
      if (!mustDeadLetter) {
        const jobResult = await client.query<RuntimeJobRow>(
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
            backoffToRunnableAt(now, request.backoffSec ?? 60),
            request.errorClass ?? null,
            request.errorText ?? null,
          ],
        );

        await markWorkerDrainedIfIdle(client, attempt.workerId);
        return {
          job: mapRuntimeJobRow(jobResult.rows[0]!),
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
            result_json,
            output_artifact_id,
            error_class,
            error_text,
            created_at
         )
         VALUES ($1::uuid, $2::uuid, $3, $4, 'dead', $5::jsonb, $6::uuid, $7, $8, $9::timestamptz)
         RETURNING *`,
        [
          request.jobId,
          request.attemptId,
          job.resultOwner,
          job.resultRoute,
          request.resultJson ? JSON.stringify(request.resultJson) : null,
          request.outputArtifactId ?? null,
          request.errorClass ?? null,
          request.errorText ?? null,
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
        [
          request.jobId,
          request.leaseToken,
          result.resultId,
          request.errorClass ?? null,
          request.errorText ?? null,
        ],
      );

      await markWorkerDrainedIfIdle(client, attempt.workerId);
      return {
        job: mapRuntimeJobRow(deadJobResult.rows[0]!),
        attempt,
        result,
      };
    }

    const terminalStatus = jobStatusFromFinalizeOutcome(request.outcome);
    const resultKind = resultKindFromTerminalJobStatus(terminalStatus);

    const insertedResult = await client.query<RuntimeJobResultRow>(
      `INSERT INTO runtime.job_results (
          job_id,
          attempt_id,
          result_owner,
          result_route,
          result_kind,
          result_json,
          output_artifact_id,
          error_class,
          error_text,
          created_at
       )
       VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7::uuid, $8, $9, $10::timestamptz)
       RETURNING *`,
      [
        request.jobId,
        request.attemptId,
        job.resultOwner,
        job.resultRoute,
        resultKind,
        request.resultJson ? JSON.stringify(request.resultJson) : null,
        request.outputArtifactId ?? null,
        request.errorClass ?? null,
        request.errorText ?? null,
        now.toISOString(),
      ],
    );

    const result = mapRuntimeJobResultRow(insertedResult.rows[0]!);

    const updatedJob = await client.query<RuntimeJobRow>(
      `UPDATE runtime.jobs
          SET status = $3,
              lease_owner = NULL,
              lease_token = NULL,
              lease_expires_at = NULL,
              terminal_result_id = $4::uuid,
              last_error_class = $5,
              last_error_text = $6
        WHERE job_id = $1::uuid
          AND lease_token = $2::uuid
          AND status = 'leased'
        RETURNING *`,
      [
        request.jobId,
        request.leaseToken,
        terminalStatus,
        result.resultId,
        request.outcome === "succeeded" ? null : request.errorClass ?? null,
        request.outcome === "succeeded" ? null : request.errorText ?? null,
      ],
    );

    await markWorkerDrainedIfIdle(client, attempt.workerId);
    return {
      job: mapRuntimeJobRow(updatedJob.rows[0]!),
      attempt,
      result,
    };
  });
}
