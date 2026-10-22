import type pg from "pg";
import { mapRuntimeArtifactRow, type RuntimeArtifactRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest, conflict, internalError } from "../errors/controller-error.js";
import type { RetentionClass, RuntimeArtifact } from "../types/runtime.js";

export interface RegisterArtifactRequest {
  artifactKind: string;
  storageUri: string;
  contentHash: string;
  byteSize: number;
  mimeType?: string | null;
  schemaRef?: string | null;
  retentionClass: RetentionClass;
  producerSystem: string;
  createdByJobId?: string | null;
  createdByAttemptId?: string | null;
  metadataJson?: Record<string, unknown> | null;
}

export interface RegisterArtifactResult {
  action: "created" | "existing";
  artifact: RuntimeArtifact;
}

function validateByteSize(value: number): boolean {
  return Number.isInteger(value) && value >= 0;
}

function normalizeOptionalText(value: string | null | undefined): string | null {
  if (value == null) return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function validateRegisterArtifactRequest(request: RegisterArtifactRequest): string[] {
  const errors: string[] = [];

  if (!request.artifactKind.trim()) errors.push("artifactKind is required");
  if (!request.storageUri.trim()) errors.push("storageUri is required");
  if (!request.contentHash.trim()) errors.push("contentHash is required");
  if (!request.producerSystem.trim()) errors.push("producerSystem is required");
  if (!validateByteSize(request.byteSize)) errors.push("byteSize must be a non-negative integer");

  return errors;
}

function assertArtifactCompatible(existing: RuntimeArtifact, request: RegisterArtifactRequest): void {
  const immutableMismatches: string[] = [];

  if (existing.artifactKind !== request.artifactKind) immutableMismatches.push("artifactKind");
  if (existing.contentHash !== request.contentHash) immutableMismatches.push("contentHash");
  if (existing.byteSize !== request.byteSize) immutableMismatches.push("byteSize");
  if ((existing.mimeType ?? null) !== normalizeOptionalText(request.mimeType)) immutableMismatches.push("mimeType");
  if ((existing.schemaRef ?? null) !== normalizeOptionalText(request.schemaRef)) immutableMismatches.push("schemaRef");
  if (existing.retentionClass !== request.retentionClass) immutableMismatches.push("retentionClass");
  if (existing.producerSystem !== request.producerSystem) immutableMismatches.push("producerSystem");

  if (immutableMismatches.length > 0) {
    throw conflict(
      "artifact_conflict",
      `artifact already exists for storageUri=${request.storageUri} with different immutable fields: ${immutableMismatches.join(", ")}`,
      {
        storageUri: request.storageUri,
        immutableMismatches,
      },
    );
  }
}

export async function registerArtifact(
  db: pg.Pool | pg.PoolClient,
  request: RegisterArtifactRequest,
): Promise<RegisterArtifactResult> {
  const errors = validateRegisterArtifactRequest(request);
  if (errors.length > 0) {
    throw badRequest("invalid_artifact_request", `invalid artifact registration request: ${errors.join("; ")}`, { errors });
  }

  return withTransaction(db, async (client) => {
    const inserted = await client.query<RuntimeArtifactRow>(
      `INSERT INTO runtime.artifacts (
          artifact_kind,
          storage_uri,
          content_hash,
          byte_size,
          mime_type,
          schema_ref,
          retention_class,
          producer_system,
          created_by_job_id,
          created_by_attempt_id,
          metadata_json
       )
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, $10::uuid, $11::jsonb)
       ON CONFLICT (storage_uri) DO NOTHING
       RETURNING *`,
      [
        request.artifactKind,
        request.storageUri,
        request.contentHash,
        request.byteSize,
        normalizeOptionalText(request.mimeType),
        normalizeOptionalText(request.schemaRef),
        request.retentionClass,
        request.producerSystem,
        request.createdByJobId ?? null,
        request.createdByAttemptId ?? null,
        JSON.stringify(request.metadataJson ?? {}),
      ],
    );

    if (inserted.rowCount === 1) {
      return {
        action: "created",
        artifact: mapRuntimeArtifactRow(inserted.rows[0]!),
      };
    }

    const existingResult = await client.query<RuntimeArtifactRow>(
      `SELECT *
         FROM runtime.artifacts
        WHERE storage_uri = $1
        LIMIT 1`,
      [request.storageUri],
    );

    if (existingResult.rowCount !== 1) {
      throw internalError(
        "existing_artifact_lookup_failed",
        `failed to read existing artifact for storageUri=${request.storageUri}`,
        { storageUri: request.storageUri },
      );
    }

    const existing = mapRuntimeArtifactRow(existingResult.rows[0]!);
    assertArtifactCompatible(existing, request);

    return {
      action: "existing",
      artifact: existing,
    };
  });
}

export async function getRuntimeArtifact(
  db: pg.Pool | pg.PoolClient,
  artifactId: string,
): Promise<RuntimeArtifact | null> {
  const result = await db.query<RuntimeArtifactRow>(
    `SELECT *
       FROM runtime.artifacts
      WHERE artifact_id = $1::uuid`,
    [artifactId],
  );

  if (result.rowCount !== 1) {
    return null;
  }

  return mapRuntimeArtifactRow(result.rows[0]!);
}
