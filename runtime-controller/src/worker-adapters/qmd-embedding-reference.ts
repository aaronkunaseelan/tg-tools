import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { RuntimeJob, RuntimeJobAttempt } from "../types/runtime.js";
import type { RegisterArtifactRequest } from "../runtime/artifacts.js";

export interface PreparedQmdEmbeddingReferenceOutput {
  resultJson: Record<string, unknown>;
  artifactRequest: RegisterArtifactRequest;
}

function sha256Hex(input: string | Buffer): string {
  return createHash("sha256").update(input).digest("hex");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function deriveRunId(job: RuntimeJob): string {
  const input = asRecord(job.inputJson);
  if (typeof input?.runId === "string" && input.runId.trim().length > 0) {
    return input.runId;
  }
  if (job.producerRef.startsWith("qmd-embed:")) {
    return job.producerRef.slice("qmd-embed:".length).replaceAll(":", "-");
  }
  return `${job.producerSystem}-${job.producerRef}`;
}

export async function prepareQmdEmbeddingReferenceOutput(
  job: RuntimeJob,
  attempt: RuntimeJobAttempt,
  workerId: string,
  baseDir = "/tmp/runtime-controller-reference-worker-artifacts",
  now = new Date(),
): Promise<PreparedQmdEmbeddingReferenceOutput> {
  const input = asRecord(job.inputJson) ?? {};
  const runId = deriveRunId(job);
  const model = asString(input.model, "unknown-model");
  const artifactBundleVersion = asString(input.artifactBundleVersion, "qmd-embedding-bundle-v1");
  const selectionPolicyVersion = asString(input.selectionPolicyVersion, "unknown-selection-policy");
  const inputManifestSha256 = asString(input.inputManifestSha256, "unknown-input-manifest-sha256");
  const modelRevision = asString(input.modelRevision, "unknown-model-revision");
  const vectorRowCount = Math.max(0, Math.trunc(asNumber(input.uniqueInputCount, job.inputItemCount ?? 0)));
  const dims = Math.max(0, Math.trunc(asNumber(input.dimensions, 0)));

  const outputDir = join(baseDir, job.jobId, attempt.attemptId);
  await mkdir(outputDir, { recursive: true });
  const outputManifestPath = join(outputDir, "output-manifest.json");

  const manifestWithoutHash = {
    schema_version: 1,
    artifact_bundle_version: artifactBundleVersion,
    run_id: runId,
    created_at: now.toISOString(),
    producer_system: "qmd_embedding_reference_worker",
    input_manifest_artifact_id: job.inputArtifactId,
    model,
    model_revision: modelRevision,
    input_manifest_sha256: inputManifestSha256,
    output_manifest_sha256: "",
    runtime_job_id: job.jobId,
    attempt_id: attempt.attemptId,
    worker_id: workerId,
    execution_summary: {
      requested_unique_inputs: job.inputItemCount ?? vectorRowCount,
      embedded_unique_inputs: vectorRowCount,
      cache_hits: 0,
      failures: 0,
      dims,
    },
    artifacts: [],
    status: "reference_stub",
    reference_stub: true,
  };

  const outputManifestSha256 = sha256Hex(JSON.stringify(manifestWithoutHash));
  const manifest = {
    ...manifestWithoutHash,
    output_manifest_sha256: outputManifestSha256,
  };
  const manifestText = `${JSON.stringify(manifest, null, 2)}\n`;
  await writeFile(outputManifestPath, manifestText, "utf8");

  return {
    resultJson: {
      schemaVersion: 1,
      status: "artifact_ready",
      runId,
      model,
      inputManifestSha256,
      artifactBundleVersion,
      selectionPolicyVersion,
      vectorRowCount,
      failureRowCount: 0,
      referenceStub: true,
    },
    artifactRequest: {
      artifactKind: "qmd_embedding_output_manifest_json",
      storageUri: `file://${outputManifestPath}`,
      contentHash: `sha256:${sha256Hex(manifestText)}`,
      byteSize: Buffer.byteLength(manifestText),
      mimeType: "application/json",
      schemaRef: "qmd.embedding.output-manifest.v1",
      retentionClass: "durable",
      producerSystem: "qmd_embedding_worker",
      createdByJobId: job.jobId,
      createdByAttemptId: attempt.attemptId,
      metadataJson: {
        workloadType: job.workloadType,
        resultRoute: job.resultRoute,
        runId,
        referenceStub: true,
      },
    },
  };
}
