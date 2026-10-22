import type pg from "pg";
import { mapRuntimeJobRow, type RuntimeJobRow } from "../db/rows.js";
import { badRequest, internalError } from "../errors/controller-error.js";
import type { AdmissionAction, WorkloadRuntimePolicy } from "../types/policy.js";
import type { ExecutionShape, QueueClass, RuntimeJob, SubmissionMode } from "../types/runtime.js";
import { getRuntimeArtifact } from "./artifacts.js";
import { validateQmdEmbeddingInputArtifact, validateQmdEmbeddingSubmissionContract } from "./qmd-embedding-contract.js";

export interface SubmitRequest {
  producerSystem: string;
  producerRef: string;
  resultOwner: string;
  resultRoute: string;
  workloadType: string;
  queueClass: QueueClass;
  executionProfile: string;
  executionShape: ExecutionShape;
  requestedSubmissionMode?: SubmissionMode;
  priority?: number;
  inputItemCount?: number | null;
  inputBytes?: number | null;
  inputJson?: Record<string, unknown> | null;
  inputArtifactId?: string | null;
  outputContractJson?: Record<string, unknown> | null;
  runnableAt?: string | null;
  deadlineAt?: string | null;
  idempotencyKey: string;
  effectKey?: string | null;
  maxAttempts?: number;
}

export interface SubmissionValidationResult {
  ok: boolean;
  errors: string[];
  submissionMode: SubmissionMode;
}

export interface AdmissionPlan {
  admissionAction: AdmissionAction;
  submissionMode: SubmissionMode;
  maxWaitSec: number | null;
  deferUntil: string | null;
}

export type PersistedSubmissionResult = {
  action: "created" | "existing";
  admissionAction: "admit_now";
  submissionMode: SubmissionMode;
  job: RuntimeJob;
};

export type AdmitAndSubmitResult =
  | PersistedSubmissionResult
  | {
      action: "deferred";
      admissionAction: "defer";
      submissionMode: SubmissionMode;
      maxWaitSec: number | null;
      deferUntil: string | null;
    }
  | {
      action: "rejected";
      admissionAction: "reject_retry_later";
      submissionMode: SubmissionMode;
    };

function parseIsoOrNull(value: string | null | undefined, fieldName: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    throw badRequest("invalid_submission_request", `${fieldName} must be an ISO 8601 datetime`, { fieldName });
  }
  return parsed.toISOString();
}

export function resolveSubmissionMode(
  request: SubmitRequest,
  policy: WorkloadRuntimePolicy,
): SubmissionMode {
  return request.requestedSubmissionMode ?? policy.submissionMode;
}

export function validateSubmissionRequest(
  request: SubmitRequest,
  policy: WorkloadRuntimePolicy,
): SubmissionValidationResult {
  const errors: string[] = [];
  const submissionMode = resolveSubmissionMode(request, policy);

  if (submissionMode === "explicit_batch") {
    if (!request.inputItemCount || request.inputItemCount <= 0) {
      errors.push("explicit_batch submission requires inputItemCount > 0");
    }
    if (policy.maxItems != null && request.inputItemCount != null && request.inputItemCount > policy.maxItems) {
      errors.push(`inputItemCount exceeds maxItems=${policy.maxItems}`);
    }
    if (policy.maxBytes != null && request.inputBytes != null && request.inputBytes > policy.maxBytes) {
      errors.push(`inputBytes exceeds maxBytes=${policy.maxBytes}`);
    }
  }

  if (!request.idempotencyKey.trim()) {
    errors.push("idempotencyKey is required");
  }

  if (!request.producerSystem.trim()) errors.push("producerSystem is required");
  if (!request.producerRef.trim()) errors.push("producerRef is required");
  if (!request.resultOwner.trim()) errors.push("resultOwner is required");
  if (!request.resultRoute.trim()) errors.push("resultRoute is required");
  if (!request.workloadType.trim()) errors.push("workloadType is required");
  if (!request.executionProfile.trim()) errors.push("executionProfile is required");

  if (request.workloadType === "qmd_embedding") {
    errors.push(...validateQmdEmbeddingSubmissionContract(request));
  }

  return {
    ok: errors.length === 0,
    errors,
    submissionMode,
  };
}

export function planAdmission(
  request: SubmitRequest,
  policy: WorkloadRuntimePolicy,
  now = new Date(),
): AdmissionPlan {
  const submissionMode = resolveSubmissionMode(request, policy);
  return {
    admissionAction: policy.admissionAction,
    submissionMode,
    maxWaitSec: policy.maxWaitSec,
    deferUntil: policy.admissionAction === "defer" && policy.maxWaitSec != null
      ? new Date(now.getTime() + policy.maxWaitSec * 1000).toISOString()
      : null,
  };
}

async function fetchExistingJob(db: pg.Pool | pg.PoolClient, request: SubmitRequest): Promise<RuntimeJob> {
  const existing = await db.query<RuntimeJobRow>(
    `SELECT *
       FROM runtime.jobs
      WHERE producer_system = $1
        AND idempotency_key = $2
      LIMIT 1`,
    [request.producerSystem, request.idempotencyKey],
  );

  if (existing.rowCount !== 1) {
    throw internalError(
      "existing_job_lookup_failed",
      `failed to read existing job for ${request.producerSystem}:${request.idempotencyKey}`,
      {
        producerSystem: request.producerSystem,
        idempotencyKey: request.idempotencyKey,
      },
    );
  }

  return mapRuntimeJobRow(existing.rows[0]!);
}

export async function submitRuntimeJob(
  db: pg.Pool | pg.PoolClient,
  request: SubmitRequest,
  submissionMode: SubmissionMode,
): Promise<PersistedSubmissionResult> {
  const runnableAt = parseIsoOrNull(request.runnableAt, "runnableAt");
  const deadlineAt = parseIsoOrNull(request.deadlineAt, "deadlineAt");
  const priority = request.priority ?? 50;
  const maxAttempts = request.maxAttempts ?? 3;

  const inserted = await db.query<RuntimeJobRow>(
    `INSERT INTO runtime.jobs (
        producer_system,
        producer_ref,
        result_owner,
        result_route,
        workload_type,
        queue_class,
        execution_profile,
        execution_shape,
        submission_mode,
        priority,
        input_item_count,
        status,
        runnable_at,
        deadline_at,
        idempotency_key,
        effect_key,
        attempt_count,
        max_attempts,
        input_json,
        input_artifact_id,
        output_contract_json
      )
      VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, 'pending', COALESCE($12::timestamptz, NOW()), $13::timestamptz,
        $14, $15, 0, $16, $17::jsonb, $18::uuid, $19::jsonb
      )
      ON CONFLICT (producer_system, idempotency_key) DO NOTHING
      RETURNING *`,
    [
      request.producerSystem,
      request.producerRef,
      request.resultOwner,
      request.resultRoute,
      request.workloadType,
      request.queueClass,
      request.executionProfile,
      request.executionShape,
      submissionMode,
      priority,
      request.inputItemCount ?? null,
      runnableAt,
      deadlineAt,
      request.idempotencyKey,
      request.effectKey ?? null,
      maxAttempts,
      request.inputJson ? JSON.stringify(request.inputJson) : null,
      request.inputArtifactId ?? null,
      request.outputContractJson ? JSON.stringify(request.outputContractJson) : null,
    ],
  );

  if (inserted.rowCount === 1) {
    return {
      action: "created",
      admissionAction: "admit_now",
      submissionMode,
      job: mapRuntimeJobRow(inserted.rows[0]!),
    };
  }

  return {
    action: "existing",
    admissionAction: "admit_now",
    submissionMode,
    job: await fetchExistingJob(db, request),
  };
}

export async function admitAndSubmitRuntimeJob(
  db: pg.Pool | pg.PoolClient,
  request: SubmitRequest,
  policy: WorkloadRuntimePolicy,
  now = new Date(),
): Promise<AdmitAndSubmitResult> {
  const validation = validateSubmissionRequest(request, policy);
  if (!validation.ok) {
    throw badRequest("invalid_submission_request", `invalid submission request: ${validation.errors.join("; ")}`, {
      workloadType: request.workloadType,
    });
  }

  if (request.workloadType === "qmd_embedding") {
    const inputArtifact = await getRuntimeArtifact(db, request.inputArtifactId ?? "");
    const artifactErrors = validateQmdEmbeddingInputArtifact(inputArtifact, request);
    if (artifactErrors.length > 0) {
      throw badRequest("invalid_submission_request", `invalid submission request: ${artifactErrors.join("; ")}`, {
        workloadType: request.workloadType,
        inputArtifactId: request.inputArtifactId ?? null,
      });
    }
  }

  const plan = planAdmission(request, policy, now);
  if (plan.admissionAction === "defer") {
    return {
      action: "deferred",
      admissionAction: plan.admissionAction,
      submissionMode: plan.submissionMode,
      maxWaitSec: plan.maxWaitSec,
      deferUntil: plan.deferUntil,
    };
  }

  if (plan.admissionAction === "reject_retry_later") {
    return {
      action: "rejected",
      admissionAction: plan.admissionAction,
      submissionMode: plan.submissionMode,
    };
  }

  return submitRuntimeJob(db, request, plan.submissionMode);
}
