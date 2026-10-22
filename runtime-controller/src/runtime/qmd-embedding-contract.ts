import type { FinalizeRequest } from "./finalize.js";
import type { SubmitRequest } from "./submit.js";
import type { QueueClass, RuntimeArtifact, RuntimeJob } from "../types/runtime.js";

export const QMD_EMBEDDING_PRODUCER_SYSTEM = "qmd";
export const QMD_EMBEDDING_WORKLOAD_TYPE = "qmd_embedding";
export const QMD_EMBEDDING_RESULT_OWNER = "qmd";
export const QMD_EMBEDDING_RESULT_ROUTE = "qmd.embedding.import.v1";
export const QMD_EMBEDDING_EXECUTION_PROFILE = "embed_batch";
export const QMD_EMBEDDING_EXECUTION_SHAPE = "remote_async_artifact";
export const QMD_EMBEDDING_INPUT_ARTIFACT_KIND = "qmd_embedding_input_manifest_json";
export const QMD_EMBEDDING_INPUT_SCHEMA_REF = "qmd.embedding.input-manifest.v1";
export const QMD_EMBEDDING_OUTPUT_ARTIFACT_KIND = "qmd_embedding_output_manifest_json";
export const QMD_EMBEDDING_OUTPUT_SCHEMA_REF = "qmd.embedding.output-manifest.v1";
const REQUIRED_OUTPUT_ARTIFACTS = [
  "qmd_embedding_output_manifest_json",
  "qmd_embedding_vectors_parquet",
] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asTrimmedString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function queueClassForPriorityClass(priorityClass: string): QueueClass | null {
  switch (priorityClass) {
    case "interactive_freshness":
      return "foreground";
    case "hot_backfill":
    case "cold_backfill":
      return "background";
    case "repair_rebuild":
      return "ops";
    default:
      return null;
  }
}

export function validateQmdEmbeddingSubmissionContract(request: SubmitRequest): string[] {
  const errors: string[] = [];

  if (request.producerSystem !== QMD_EMBEDDING_PRODUCER_SYSTEM) {
    errors.push(`qmd_embedding requires producerSystem=${QMD_EMBEDDING_PRODUCER_SYSTEM}`);
  }
  if (request.resultOwner !== QMD_EMBEDDING_RESULT_OWNER) {
    errors.push(`qmd_embedding requires resultOwner=${QMD_EMBEDDING_RESULT_OWNER}`);
  }
  if (request.resultRoute !== QMD_EMBEDDING_RESULT_ROUTE) {
    errors.push(`qmd_embedding requires resultRoute=${QMD_EMBEDDING_RESULT_ROUTE}`);
  }
  if (request.executionProfile !== QMD_EMBEDDING_EXECUTION_PROFILE) {
    errors.push(`qmd_embedding requires executionProfile=${QMD_EMBEDDING_EXECUTION_PROFILE}`);
  }
  if (request.executionShape !== QMD_EMBEDDING_EXECUTION_SHAPE) {
    errors.push(`qmd_embedding requires executionShape=${QMD_EMBEDDING_EXECUTION_SHAPE}`);
  }
  if (!asTrimmedString(request.inputArtifactId)) {
    errors.push("qmd_embedding requires inputArtifactId");
  }

  const inputJson = asRecord(request.inputJson);
  if (!inputJson) {
    errors.push("qmd_embedding requires inputJson");
  } else {
    const priorityClass = asTrimmedString(inputJson.priorityClass);
    const model = asTrimmedString(inputJson.model);
    const artifactBundleVersion = asTrimmedString(inputJson.artifactBundleVersion);
    if (!priorityClass) {
      errors.push("qmd_embedding inputJson.priorityClass is required");
    } else {
      const expectedQueueClass = queueClassForPriorityClass(priorityClass);
      if (!expectedQueueClass) {
        errors.push(`qmd_embedding inputJson.priorityClass is invalid: ${priorityClass}`);
      } else if (request.queueClass !== expectedQueueClass) {
        errors.push(`qmd_embedding priorityClass=${priorityClass} requires queueClass=${expectedQueueClass}`);
      }
    }
    if (!model) {
      errors.push("qmd_embedding inputJson.model is required");
    }
    if (!artifactBundleVersion) {
      errors.push("qmd_embedding inputJson.artifactBundleVersion is required");
    }

    const uniqueInputCount = inputJson.uniqueInputCount;
    if (typeof uniqueInputCount === "number" && request.inputItemCount != null && uniqueInputCount !== request.inputItemCount) {
      errors.push("qmd_embedding inputJson.uniqueInputCount must equal inputItemCount");
    }
  }

  const outputContractJson = asRecord(request.outputContractJson);
  if (!outputContractJson) {
    errors.push("qmd_embedding requires outputContractJson");
  } else {
    const route = asTrimmedString(outputContractJson.resultRoute);
    if (route !== QMD_EMBEDDING_RESULT_ROUTE) {
      errors.push(`qmd_embedding outputContractJson.resultRoute must equal ${QMD_EMBEDDING_RESULT_ROUTE}`);
    }
    const requiredArtifacts = new Set(asStringArray(outputContractJson.requiredArtifacts));
    for (const requiredArtifact of REQUIRED_OUTPUT_ARTIFACTS) {
      if (!requiredArtifacts.has(requiredArtifact)) {
        errors.push(`qmd_embedding outputContractJson.requiredArtifacts must include ${requiredArtifact}`);
      }
    }
  }

  return errors;
}

export function validateQmdEmbeddingInputArtifact(
  artifact: RuntimeArtifact | null,
  request: SubmitRequest,
): string[] {
  if (request.workloadType !== QMD_EMBEDDING_WORKLOAD_TYPE) {
    return [];
  }

  const errors: string[] = [];
  if (!artifact) {
    errors.push(`qmd_embedding inputArtifactId not found: ${request.inputArtifactId}`);
    return errors;
  }

  if (artifact.artifactKind !== QMD_EMBEDDING_INPUT_ARTIFACT_KIND) {
    errors.push(`qmd_embedding input artifact must have artifactKind=${QMD_EMBEDDING_INPUT_ARTIFACT_KIND}`);
  }
  if (artifact.schemaRef !== QMD_EMBEDDING_INPUT_SCHEMA_REF) {
    errors.push(`qmd_embedding input artifact must have schemaRef=${QMD_EMBEDDING_INPUT_SCHEMA_REF}`);
  }
  if (artifact.producerSystem !== QMD_EMBEDDING_PRODUCER_SYSTEM) {
    errors.push(`qmd_embedding input artifact must have producerSystem=${QMD_EMBEDDING_PRODUCER_SYSTEM}`);
  }

  return errors;
}

export function validateQmdEmbeddingOutputArtifact(
  artifact: RuntimeArtifact | null,
  job: RuntimeJob,
  request: FinalizeRequest,
): string[] {
  if (job.workloadType !== QMD_EMBEDDING_WORKLOAD_TYPE || request.outcome !== "succeeded") {
    return [];
  }

  const errors: string[] = [];
  if (!artifact) {
    errors.push(`qmd_embedding outputArtifactId not found: ${request.outputArtifactId}`);
    return errors;
  }

  if (artifact.artifactKind !== QMD_EMBEDDING_OUTPUT_ARTIFACT_KIND) {
    errors.push(`qmd_embedding output artifact must have artifactKind=${QMD_EMBEDDING_OUTPUT_ARTIFACT_KIND}`);
  }
  if (artifact.schemaRef !== QMD_EMBEDDING_OUTPUT_SCHEMA_REF) {
    errors.push(`qmd_embedding output artifact must have schemaRef=${QMD_EMBEDDING_OUTPUT_SCHEMA_REF}`);
  }
  if (artifact.createdByJobId != null && artifact.createdByJobId !== job.jobId) {
    errors.push("qmd_embedding output artifact createdByJobId must match jobId when present");
  }
  if (artifact.createdByAttemptId != null && artifact.createdByAttemptId !== request.attemptId) {
    errors.push("qmd_embedding output artifact createdByAttemptId must match attemptId when present");
  }

  return errors;
}

export function validateQmdEmbeddingFinalizeContract(job: RuntimeJob, request: FinalizeRequest): string[] {
  if (job.workloadType !== QMD_EMBEDDING_WORKLOAD_TYPE || request.outcome !== "succeeded") {
    return [];
  }

  const errors: string[] = [];
  if (!asTrimmedString(request.outputArtifactId)) {
    errors.push("qmd_embedding success finalize requires outputArtifactId");
  }

  const resultJson = asRecord(request.resultJson);
  if (!resultJson) {
    errors.push("qmd_embedding success finalize requires resultJson");
    return errors;
  }

  if (asTrimmedString(resultJson.status) !== "artifact_ready") {
    errors.push("qmd_embedding success finalize requires resultJson.status=artifact_ready");
  }
  if (!asTrimmedString(resultJson.runId)) {
    errors.push("qmd_embedding success finalize requires resultJson.runId");
  }

  const inputModel = asRecord(job.inputJson)?.model;
  const resultModel = resultJson.model;
  if (typeof inputModel === "string" && typeof resultModel === "string" && inputModel !== resultModel) {
    errors.push("qmd_embedding success finalize resultJson.model must match job inputJson.model");
  }

  return errors;
}
