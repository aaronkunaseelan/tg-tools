import type pg from "pg";
import { mapRuntimeSourceReceiptRow, type RuntimeSourceReceiptRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest, conflict, notFound } from "../errors/controller-error.js";
import { SOURCE_RECEIPT_STATUSES, type RuntimeSourceReceipt, type SourceReceiptStatus } from "../types/runtime.js";

export interface AdmitSourceReceiptRequest {
  sourceSystem: string;
  sourceKey: string;
  producerSystem: string;
  workloadType: string;
  routeId: string;
  routeKind: string;
  channelId?: string | null;
  matchedRuleVersion: string;
  sourceJson?: Record<string, unknown> | null;
  now?: Date;
}

export interface AdmitSourceReceiptResult {
  action: "created" | "existing";
  receipt: RuntimeSourceReceipt;
}

export interface MarkSourceReceiptSubmittedRequest {
  receiptId: string;
  jobId: string;
}

export interface CancelSourceReceiptRequest {
  receiptId: string;
  reason?: string | null;
}

export interface SourceReceiptQuery {
  sourceSystem?: string;
  sourceKey?: string;
  producerSystem?: string;
  workloadType?: string;
  routeKind?: string;
  routeId?: string;
  channelId?: string;
  status?: readonly SourceReceiptStatus[];
  limit?: number;
}

export interface SourceReceiptListResult {
  items: RuntimeSourceReceipt[];
  count: number;
}

function cleanRequiredText(value: string | null | undefined, fieldName: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    throw badRequest("invalid_source_receipt_request", `${fieldName} is required`, { fieldName });
  }
  return trimmed;
}

function cleanOptionalText(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function normalizeRecord(value: Record<string, unknown> | null | undefined, fieldName: string): Record<string, unknown> {
  if (value == null) return {};
  if (typeof value === "object" && !Array.isArray(value)) return value;
  throw badRequest("invalid_source_receipt_request", `${fieldName} must be a JSON object`, { fieldName });
}

const VALID_SOURCE_RECEIPT_STATUSES = new Set<string>(SOURCE_RECEIPT_STATUSES);

function normalizeSourceReceiptStatuses(statuses: readonly SourceReceiptStatus[] | undefined): SourceReceiptStatus[] | null {
  if (!statuses || statuses.length === 0) return null;
  const normalized = statuses.map((status) => status.trim()).filter(Boolean);
  if (normalized.length === 0) {
    throw badRequest("invalid_source_receipt_query", "status must not be empty when provided");
  }
  for (const status of normalized) {
    if (!VALID_SOURCE_RECEIPT_STATUSES.has(status)) {
      throw badRequest("invalid_source_receipt_query", `invalid source receipt status: ${status}`, { status });
    }
  }
  return Array.from(new Set(normalized)) as SourceReceiptStatus[];
}

export async function admitSourceReceipt(
  db: pg.Pool | pg.PoolClient,
  request: AdmitSourceReceiptRequest,
): Promise<AdmitSourceReceiptResult> {
  return withTransaction(db, async (client) => {
    const sourceSystem = cleanRequiredText(request.sourceSystem, "sourceSystem");
    const sourceKey = cleanRequiredText(request.sourceKey, "sourceKey");
    const sourceJson = normalizeRecord(request.sourceJson, "sourceJson");
    const now = request.now ?? new Date();

    const inserted = await client.query<RuntimeSourceReceiptRow>(
      `INSERT INTO runtime.source_receipts (
          source_system,
          source_key,
          producer_system,
          workload_type,
          route_id,
          route_kind,
          channel_id,
          matched_rule_version,
          source_json,
          status,
          created_at
       )
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, 'accepted', $10::timestamptz)
       ON CONFLICT (source_system, source_key) DO NOTHING
       RETURNING *`,
      [
        sourceSystem,
        sourceKey,
        cleanRequiredText(request.producerSystem, "producerSystem"),
        cleanRequiredText(request.workloadType, "workloadType"),
        cleanRequiredText(request.routeId, "routeId"),
        cleanRequiredText(request.routeKind, "routeKind"),
        cleanOptionalText(request.channelId),
        cleanRequiredText(request.matchedRuleVersion, "matchedRuleVersion"),
        JSON.stringify(sourceJson),
        now.toISOString(),
      ],
    );

    if (inserted.rowCount === 1) {
      return {
        action: "created",
        receipt: mapRuntimeSourceReceiptRow(inserted.rows[0]!),
      };
    }

    const existing = await client.query<RuntimeSourceReceiptRow>(
      `SELECT *
         FROM runtime.source_receipts
        WHERE source_system = $1
          AND source_key = $2
        LIMIT 1`,
      [sourceSystem, sourceKey],
    );
    return {
      action: "existing",
      receipt: mapRuntimeSourceReceiptRow(existing.rows[0]!),
    };
  });
}

export async function markSourceReceiptSubmitted(
  db: pg.Pool | pg.PoolClient,
  request: MarkSourceReceiptSubmittedRequest,
): Promise<RuntimeSourceReceipt> {
  return withTransaction(db, async (client) => {
    const result = await client.query<RuntimeSourceReceiptRow>(
      `SELECT *
         FROM runtime.source_receipts
        WHERE receipt_id = $1::uuid
        FOR UPDATE`,
      [request.receiptId],
    );
    if (result.rowCount !== 1) {
      throw notFound("source_receipt_not_found", `source receipt not found: ${request.receiptId}`, {
        receiptId: request.receiptId,
      });
    }

    const receipt = mapRuntimeSourceReceiptRow(result.rows[0]!);
    if (receipt.status === "submitted") {
      if (receipt.jobId === request.jobId) return receipt;
      throw conflict("source_receipt_already_submitted", "source receipt is already linked to another job", {
        receiptId: receipt.receiptId,
        existingJobId: receipt.jobId,
        requestedJobId: request.jobId,
      });
    }
    if (receipt.status !== "accepted") {
      throw conflict("invalid_source_receipt_state", `source receipt ${receipt.receiptId} cannot be submitted from ${receipt.status}`, {
        receiptId: receipt.receiptId,
        status: receipt.status,
      });
    }

    const updated = await client.query<RuntimeSourceReceiptRow>(
      `UPDATE runtime.source_receipts
          SET status = 'submitted',
              job_id = $2::uuid
        WHERE receipt_id = $1::uuid
          AND status = 'accepted'
        RETURNING *`,
      [request.receiptId, request.jobId],
    );
    return mapRuntimeSourceReceiptRow(updated.rows[0]!);
  });
}

export async function cancelSourceReceipt(
  db: pg.Pool | pg.PoolClient,
  request: CancelSourceReceiptRequest,
): Promise<RuntimeSourceReceipt> {
  return withTransaction(db, async (client) => {
    const updated = await client.query<RuntimeSourceReceiptRow>(
      `UPDATE runtime.source_receipts
          SET status = 'cancelled'
        WHERE receipt_id = $1::uuid
          AND status = 'accepted'
        RETURNING *`,
      [request.receiptId],
    );
    if (updated.rowCount !== 1) {
      const existing = await getSourceReceipt(client, request.receiptId);
      if (!existing) {
        throw notFound("source_receipt_not_found", `source receipt not found: ${request.receiptId}`, {
          receiptId: request.receiptId,
        });
      }
      throw conflict("invalid_source_receipt_state", `source receipt ${existing.receiptId} cannot be cancelled from ${existing.status}`, {
        receiptId: existing.receiptId,
        status: existing.status,
      });
    }
    return mapRuntimeSourceReceiptRow(updated.rows[0]!);
  });
}

export async function getSourceReceipt(
  db: pg.Pool | pg.PoolClient,
  receiptId: string,
): Promise<RuntimeSourceReceipt | null> {
  const result = await db.query<RuntimeSourceReceiptRow>(
    `SELECT *
       FROM runtime.source_receipts
      WHERE receipt_id = $1::uuid
      LIMIT 1`,
    [receiptId],
  );
  if (result.rowCount !== 1) return null;
  return mapRuntimeSourceReceiptRow(result.rows[0]!);
}

export async function listSourceReceipts(
  db: pg.Pool | pg.PoolClient,
  query: SourceReceiptQuery = {},
): Promise<SourceReceiptListResult> {
  const clauses: string[] = [];
  const params: unknown[] = [];

  const fields: Array<[keyof SourceReceiptQuery, string]> = [
    ["sourceSystem", "source_system"],
    ["sourceKey", "source_key"],
    ["producerSystem", "producer_system"],
    ["workloadType", "workload_type"],
    ["routeKind", "route_kind"],
    ["routeId", "route_id"],
    ["channelId", "channel_id"],
  ];
  for (const [queryKey, columnName] of fields) {
    const value = cleanOptionalText(query[queryKey] as string | undefined);
    if (!value) continue;
    params.push(value);
    clauses.push(`${columnName} = $${params.length}`);
  }

  const statuses = normalizeSourceReceiptStatuses(query.status);
  if (statuses) {
    params.push(statuses);
    clauses.push(`status = ANY($${params.length}::text[])`);
  }

  const whereSql = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
  const limit = Math.max(1, Math.min(query.limit ?? 100, 500));
  const limitParam = `$${params.length + 1}`;

  const items = await db.query<RuntimeSourceReceiptRow>(
    `SELECT *
       FROM runtime.source_receipts
       ${whereSql}
      ORDER BY created_at DESC, receipt_id DESC
      LIMIT ${limitParam}`,
    [...params, limit],
  );
  const count = await db.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.source_receipts
       ${whereSql}`,
    params,
  );

  return {
    items: items.rows.map(mapRuntimeSourceReceiptRow),
    count: Number(count.rows[0]?.count ?? 0),
  };
}
