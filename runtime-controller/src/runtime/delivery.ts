import { randomUUID } from "node:crypto";
import type pg from "pg";
import {
  mapRuntimeDeliveryOutboxRow,
  mapRuntimeJobResultRow,
  type RuntimeDeliveryOutboxRow,
  type RuntimeJobResultRow,
} from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import { badRequest, conflict, notFound } from "../errors/controller-error.js";
import { DELIVERY_STATUSES, type DeliveryStatus, type RuntimeDeliveryOutbox, type RuntimeJobResult } from "../types/runtime.js";
import { lockedAuthorityTime, requireCurrentExpiry } from "./lease-authority.js";

export type AppendDeliveryAction = "created" | "existing";
export type FinalizeDeliveryOutcome = "sent" | "retryable_error" | "terminal_error";
export type FinalizeDeliveryAction = "sent" | "retryable" | "dead";
export type DeliveryReconcileAction = "retried" | "dead";

export interface AppendDeliveryRequest {
  resultId?: string | null;
  jobId?: string | null;
  resultOwner?: string | null;
  resultRoute?: string | null;
  deliveryProvider: string;
  deliveryKind: string;
  deliveryKey: string;
  priority?: number;
  payloadJson?: Record<string, unknown> | null;
  runnableAt?: string | null;
  deadlineAt?: string | null;
  maxAttempts?: number;
  now?: Date;
}

export interface AppendDeliveryResult {
  action: AppendDeliveryAction;
  delivery: RuntimeDeliveryOutbox;
}

export interface DeliveryClaimRequest {
  claimOwner: string;
  deliveryId?: string;
  deliveryProvider?: string | null;
  deliveryKinds?: readonly string[];
  resultOwner?: string | null;
  leaseDurationSec?: number;
  now?: Date;
}

export interface ClaimedDelivery {
  delivery: RuntimeDeliveryOutbox;
}

export interface DeliveryHeartbeatRequest {
  deliveryId: string;
  claimToken: string;
  leaseDurationSec?: number;
  now?: Date;
}

export interface FinalizeDeliveryRequest {
  deliveryId: string;
  claimToken: string;
  outcome: FinalizeDeliveryOutcome;
  providerReceiptJson?: Record<string, unknown> | null;
  errorClass?: string | null;
  errorText?: string | null;
  backoffSec?: number;
  now?: Date;
}

export interface FinalizeDeliveryResult {
  action: FinalizeDeliveryAction;
  delivery: RuntimeDeliveryOutbox;
}

export interface CancelDeliveryRequest {
  deliveryId: string;
  claimToken?: string | null;
  reason?: string | null;
  now?: Date;
}

export interface DeliveryReconcileOptions {
  limit?: number;
  baseBackoffSec?: number;
  maxBackoffSec?: number;
  now?: Date;
}

export interface DeliveryReconcileItem {
  action: DeliveryReconcileAction;
  delivery: RuntimeDeliveryOutbox;
}

export interface DeliveryOutboxQuery {
  resultOwner?: string;
  deliveryProvider?: string;
  deliveryKind?: string;
  status?: readonly DeliveryStatus[];
  limit?: number;
}

export interface DeliveryOutboxListResult {
  items: RuntimeDeliveryOutbox[];
  count: number;
}

function cleanRequiredText(value: string | null | undefined, fieldName: string): string {
  const trimmed = value?.trim();
  if (!trimmed) {
    throw badRequest("invalid_delivery_request", `${fieldName} is required`, { fieldName });
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
  throw badRequest("invalid_delivery_request", `${fieldName} must be a JSON object`, { fieldName });
}

function normalizeNullableRecord(value: Record<string, unknown> | null | undefined, fieldName: string): Record<string, unknown> | null {
  if (value == null) return null;
  if (typeof value === "object" && !Array.isArray(value)) return value;
  throw badRequest("invalid_delivery_request", `${fieldName} must be a JSON object`, { fieldName });
}

function normalizePriority(value: number | undefined): number {
  if (value == null) return 50;
  if (!Number.isInteger(value) || value < 0 || value > 100) {
    throw badRequest("invalid_delivery_request", "priority must be an integer between 0 and 100", { priority: value });
  }
  return value;
}

function normalizeMaxAttempts(value: number | undefined): number {
  if (value == null) return 5;
  if (!Number.isInteger(value) || value < 1) {
    throw badRequest("invalid_delivery_request", "maxAttempts must be a positive integer", { maxAttempts: value });
  }
  return value;
}

function normalizeTextFilters(values: readonly string[] | undefined, fieldName: string): string[] | null {
  if (!values) return null;
  const normalized = values.map((value) => value.trim()).filter(Boolean);
  if (normalized.length === 0) {
    throw badRequest("invalid_delivery_query", `${fieldName} must not be empty when provided`, { fieldName });
  }
  return Array.from(new Set(normalized));
}

const VALID_DELIVERY_STATUSES = new Set<string>(DELIVERY_STATUSES);

function normalizeDeliveryStatuses(statuses: readonly DeliveryStatus[] | undefined): DeliveryStatus[] | null {
  if (!statuses || statuses.length === 0) return null;
  const normalized = statuses.map((status) => status.trim()).filter(Boolean);
  if (normalized.length === 0) {
    throw badRequest("invalid_delivery_query", "status must not be empty when provided");
  }
  for (const status of normalized) {
    if (!VALID_DELIVERY_STATUSES.has(status)) {
      throw badRequest("invalid_delivery_query", `invalid delivery status: ${status}`, { status });
    }
  }
  return Array.from(new Set(normalized)) as DeliveryStatus[];
}

function deliveryBackoffSeconds(attemptCount: number, baseBackoffSec = 60, maxBackoffSec = 3600): number {
  const multiplier = Math.max(0, attemptCount - 1);
  return Math.min(maxBackoffSec, baseBackoffSec * Math.pow(2, multiplier));
}

function retryWouldExceedAttempts(delivery: RuntimeDeliveryOutbox): boolean {
  return delivery.attemptCount >= delivery.maxAttempts;
}

function retryWouldMissDeadline(delivery: RuntimeDeliveryOutbox, now: Date): boolean {
  if (!delivery.deadlineAt) return false;
  return new Date(delivery.deadlineAt).getTime() <= now.getTime();
}

async function getResultForAppend(
  client: pg.PoolClient,
  resultId: string | null,
): Promise<RuntimeJobResult | null> {
  if (!resultId) return null;
  const result = await client.query<RuntimeJobResultRow>(
    `SELECT *
       FROM runtime.job_results
      WHERE result_id = $1::uuid
      LIMIT 1`,
    [resultId],
  );
  if (result.rowCount !== 1) {
    throw notFound("result_not_found", `result not found: ${resultId}`, { resultId });
  }
  return mapRuntimeJobResultRow(result.rows[0]!);
}

async function lockDeliveryClaim(
  client: pg.PoolClient,
  deliveryId: string,
  claimToken: string,
): Promise<RuntimeDeliveryOutbox> {
  const result = await client.query<RuntimeDeliveryOutboxRow>(
    `SELECT *
       FROM runtime.delivery_outbox
      WHERE delivery_id = $1::uuid
      FOR UPDATE`,
    [deliveryId],
  );
  if (result.rowCount !== 1) {
    throw notFound("delivery_not_found", `delivery not found: ${deliveryId}`, { deliveryId });
  }

  const delivery = mapRuntimeDeliveryOutboxRow(result.rows[0]!);
  if (delivery.status !== "claimed") {
    throw conflict("invalid_delivery_state", `delivery ${delivery.deliveryId} is not claimed`, {
      deliveryId: delivery.deliveryId,
      status: delivery.status,
    });
  }
  if (!delivery.claimToken || delivery.claimToken !== claimToken) {
    throw conflict("stale_delivery_claim_token", `stale claim token for delivery ${delivery.deliveryId}`, {
      deliveryId: delivery.deliveryId,
    });
  }
  return delivery;
}

export async function appendDelivery(
  db: pg.Pool | pg.PoolClient,
  request: AppendDeliveryRequest,
): Promise<AppendDeliveryResult> {
  return withTransaction(db, async (client) => {
    const resultId = cleanOptionalText(request.resultId);
    const result = await getResultForAppend(client, resultId);
    const requestedJobId = cleanOptionalText(request.jobId);
    const jobId = result?.jobId ?? requestedJobId;
    if (result && requestedJobId && requestedJobId !== result.jobId) {
      throw conflict("delivery_result_mismatch", "jobId does not match resultId", {
        jobId: requestedJobId,
        resultId,
      });
    }

    const resultOwner = result?.resultOwner ?? cleanRequiredText(request.resultOwner, "resultOwner");
    const resultRoute = result?.resultRoute ?? cleanRequiredText(request.resultRoute, "resultRoute");
    if (result && request.resultOwner && cleanRequiredText(request.resultOwner, "resultOwner") !== result.resultOwner) {
      throw conflict("delivery_result_mismatch", "resultOwner does not match resultId", { resultId });
    }
    if (result && request.resultRoute && cleanRequiredText(request.resultRoute, "resultRoute") !== result.resultRoute) {
      throw conflict("delivery_result_mismatch", "resultRoute does not match resultId", { resultId });
    }

    const deliveryProvider = cleanRequiredText(request.deliveryProvider, "deliveryProvider");
    const deliveryKind = cleanRequiredText(request.deliveryKind, "deliveryKind");
    const deliveryKey = cleanRequiredText(request.deliveryKey, "deliveryKey");
    const now = request.now ?? new Date();
    const payloadJson = normalizeRecord(request.payloadJson, "payloadJson");

    const inserted = await client.query<RuntimeDeliveryOutboxRow>(
      `INSERT INTO runtime.delivery_outbox (
          result_id,
          job_id,
          result_owner,
          result_route,
          delivery_provider,
          delivery_kind,
          delivery_key,
          priority,
          status,
          runnable_at,
          deadline_at,
          max_attempts,
          payload_json,
          created_at
       )
       VALUES (
          $1::uuid,
          $2::uuid,
          $3,
          $4,
          $5,
          $6,
          $7,
          $8,
          'pending',
          $9::timestamptz,
          $10::timestamptz,
          $11,
          $12::jsonb,
          $13::timestamptz
       )
       ON CONFLICT (result_owner, delivery_key) DO NOTHING
       RETURNING *`,
      [
        resultId,
        jobId,
        resultOwner,
        resultRoute,
        deliveryProvider,
        deliveryKind,
        deliveryKey,
        normalizePriority(request.priority),
        request.runnableAt ?? now.toISOString(),
        request.deadlineAt ?? null,
        normalizeMaxAttempts(request.maxAttempts),
        JSON.stringify(payloadJson),
        now.toISOString(),
      ],
    );

    if (inserted.rowCount === 1) {
      return {
        action: "created",
        delivery: mapRuntimeDeliveryOutboxRow(inserted.rows[0]!),
      };
    }

    const existing = await client.query<RuntimeDeliveryOutboxRow>(
      `SELECT *
         FROM runtime.delivery_outbox
        WHERE result_owner = $1
          AND delivery_key = $2
        LIMIT 1`,
      [resultOwner, deliveryKey],
    );
    return {
      action: "existing",
      delivery: mapRuntimeDeliveryOutboxRow(existing.rows[0]!),
    };
  });
}

export async function getDelivery(
  db: pg.Pool | pg.PoolClient,
  deliveryId: string,
): Promise<RuntimeDeliveryOutbox | null> {
  const result = await db.query<RuntimeDeliveryOutboxRow>(
    `SELECT *
       FROM runtime.delivery_outbox
      WHERE delivery_id = $1::uuid
      LIMIT 1`,
    [deliveryId],
  );
  if (result.rowCount !== 1) return null;
  return mapRuntimeDeliveryOutboxRow(result.rows[0]!);
}

export async function listDeliveries(
  db: pg.Pool | pg.PoolClient,
  query: DeliveryOutboxQuery = {},
): Promise<DeliveryOutboxListResult> {
  const clauses: string[] = [];
  const params: unknown[] = [];

  const resultOwner = cleanOptionalText(query.resultOwner);
  if (resultOwner) {
    params.push(resultOwner);
    clauses.push(`result_owner = $${params.length}`);
  }

  const deliveryProvider = cleanOptionalText(query.deliveryProvider);
  if (deliveryProvider) {
    params.push(deliveryProvider);
    clauses.push(`delivery_provider = $${params.length}`);
  }

  const deliveryKind = cleanOptionalText(query.deliveryKind);
  if (deliveryKind) {
    params.push(deliveryKind);
    clauses.push(`delivery_kind = $${params.length}`);
  }

  const statuses = normalizeDeliveryStatuses(query.status);
  if (statuses) {
    params.push(statuses);
    clauses.push(`status = ANY($${params.length}::text[])`);
  }

  const whereSql = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
  const limit = Math.max(1, Math.min(query.limit ?? 100, 500));
  const limitParam = `$${params.length + 1}`;

  const items = await db.query<RuntimeDeliveryOutboxRow>(
    `SELECT *
       FROM runtime.delivery_outbox
       ${whereSql}
      ORDER BY created_at DESC, delivery_id DESC
      LIMIT ${limitParam}`,
    [...params, limit],
  );
  const count = await db.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.delivery_outbox
       ${whereSql}`,
    params,
  );

  return {
    items: items.rows.map(mapRuntimeDeliveryOutboxRow),
    count: Number(count.rows[0]?.count ?? 0),
  };
}

export async function claimDelivery(
  db: pg.Pool | pg.PoolClient,
  request: DeliveryClaimRequest,
): Promise<ClaimedDelivery | null> {
  const claimOwner = cleanRequiredText(request.claimOwner, "claimOwner");
  let deliveryId: string | undefined;
  if (request.deliveryId !== undefined) {
    if (typeof request.deliveryId !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(request.deliveryId)) {
      throw badRequest("invalid_delivery_request", "deliveryId must be a UUID");
    }
    deliveryId = request.deliveryId;
  }
  return withTransaction(db, async (client) => {
    const now = request.now ?? new Date();
    const clauses = [
      `status IN ('pending', 'retryable')`,
      `runnable_at <= $1::timestamptz`,
      `(deadline_at IS NULL OR deadline_at > $1::timestamptz)`,
    ];
    const params: unknown[] = [now.toISOString()];

    if (deliveryId !== undefined) {
      params.push(deliveryId);
      clauses.push(`delivery_id = $${params.length}::uuid`);
    }

    const deliveryProvider = cleanOptionalText(request.deliveryProvider);
    if (deliveryProvider) {
      params.push(deliveryProvider);
      clauses.push(`delivery_provider = $${params.length}`);
    }

    const deliveryKinds = normalizeTextFilters(request.deliveryKinds, "deliveryKinds");
    if (deliveryKinds) {
      params.push(deliveryKinds);
      clauses.push(`delivery_kind = ANY($${params.length}::text[])`);
    }

    const resultOwner = cleanOptionalText(request.resultOwner);
    if (resultOwner) {
      params.push(resultOwner);
      clauses.push(`result_owner = $${params.length}`);
    }

    const candidates = await client.query<RuntimeDeliveryOutboxRow>(
      `SELECT *
         FROM runtime.delivery_outbox
        WHERE ${clauses.join("\n          AND ")}
        ORDER BY priority DESC, runnable_at ASC, created_at ASC, delivery_id ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED`,
      params,
    );
    if (candidates.rowCount !== 1) {
      return null;
    }

    const selected = mapRuntimeDeliveryOutboxRow(candidates.rows[0]!);
    const claimToken = randomUUID();
    const claimExpiresAt = new Date(now.getTime() + (request.leaseDurationSec ?? 300) * 1000).toISOString();
    const updated = await client.query<RuntimeDeliveryOutboxRow>(
      `UPDATE runtime.delivery_outbox
          SET status = 'claimed',
              attempt_count = attempt_count + 1,
              claim_owner = $2,
              claim_token = $3::uuid,
              claim_expires_at = $4::timestamptz
        WHERE delivery_id = $1::uuid
        RETURNING *`,
      [selected.deliveryId, claimOwner, claimToken, claimExpiresAt],
    );

    return {
      delivery: mapRuntimeDeliveryOutboxRow(updated.rows[0]!),
    };
  });
}

export async function heartbeatDelivery(
  db: pg.Pool | pg.PoolClient,
  request: DeliveryHeartbeatRequest,
): Promise<ClaimedDelivery> {
  return withTransaction(db, async (client) => {
    const delivery = await lockDeliveryClaim(client, request.deliveryId, request.claimToken);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentExpiry(delivery.claimExpiresAt, now, "expired_delivery_claim");
    const claimExpiresAt = new Date(now.getTime() + (request.leaseDurationSec ?? 300) * 1000).toISOString();
    const updated = await client.query<RuntimeDeliveryOutboxRow>(
      `UPDATE runtime.delivery_outbox
          SET claim_expires_at = $3::timestamptz
        WHERE delivery_id = $1::uuid
          AND claim_token = $2::uuid
          AND status = 'claimed'
        RETURNING *`,
      [request.deliveryId, request.claimToken, claimExpiresAt],
    );
    if (updated.rowCount !== 1) {
      throw conflict("lost_delivery_claim", `lost claim for delivery ${request.deliveryId}`, {
        deliveryId: request.deliveryId,
      });
    }
    return {
      delivery: mapRuntimeDeliveryOutboxRow(updated.rows[0]!),
    };
  });
}

export async function finalizeDelivery(
  db: pg.Pool | pg.PoolClient,
  request: FinalizeDeliveryRequest,
): Promise<FinalizeDeliveryResult> {
  return withTransaction(db, async (client) => {
    const delivery = await lockDeliveryClaim(client, request.deliveryId, request.claimToken);
    const now = await lockedAuthorityTime(client, request.now);
    requireCurrentExpiry(delivery.claimExpiresAt, now, "expired_delivery_claim");
    const providerReceiptJson = normalizeNullableRecord(request.providerReceiptJson, "providerReceiptJson");

    if (request.outcome === "sent") {
      const updated = await client.query<RuntimeDeliveryOutboxRow>(
        `UPDATE runtime.delivery_outbox
            SET status = 'sent',
                claim_owner = NULL,
                claim_token = NULL,
                claim_expires_at = NULL,
                provider_receipt_json = $3::jsonb,
                last_error_class = NULL,
                last_error_text = NULL,
                sent_at = $4::timestamptz
          WHERE delivery_id = $1::uuid
            AND claim_token = $2::uuid
            AND status = 'claimed'
          RETURNING *`,
        [
          request.deliveryId,
          request.claimToken,
          providerReceiptJson ? JSON.stringify(providerReceiptJson) : null,
          now.toISOString(),
        ],
      );
      return {
        action: "sent",
        delivery: mapRuntimeDeliveryOutboxRow(updated.rows[0]!),
      };
    }

    const errorClass = cleanOptionalText(request.errorClass) ?? (
      request.outcome === "retryable_error" ? "delivery_retryable_error" : "delivery_terminal_error"
    );
    const errorText = cleanOptionalText(request.errorText);
    const mustDeadLetter = request.outcome === "terminal_error"
      || retryWouldExceedAttempts(delivery)
      || retryWouldMissDeadline(delivery, now);

    if (!mustDeadLetter) {
      const runnableAt = new Date(
        now.getTime() + (request.backoffSec ?? deliveryBackoffSeconds(delivery.attemptCount)) * 1000,
      ).toISOString();
      const updated = await client.query<RuntimeDeliveryOutboxRow>(
        `UPDATE runtime.delivery_outbox
            SET status = 'retryable',
                runnable_at = $3::timestamptz,
                claim_owner = NULL,
                claim_token = NULL,
                claim_expires_at = NULL,
                provider_receipt_json = NULL,
                last_error_class = $4,
                last_error_text = $5
          WHERE delivery_id = $1::uuid
            AND claim_token = $2::uuid
            AND status = 'claimed'
          RETURNING *`,
        [request.deliveryId, request.claimToken, runnableAt, errorClass, errorText],
      );
      return {
        action: "retryable",
        delivery: mapRuntimeDeliveryOutboxRow(updated.rows[0]!),
      };
    }

    const updated = await client.query<RuntimeDeliveryOutboxRow>(
      `UPDATE runtime.delivery_outbox
          SET status = 'dead',
              claim_owner = NULL,
              claim_token = NULL,
              claim_expires_at = NULL,
              provider_receipt_json = $6::jsonb,
              last_error_class = $3,
              last_error_text = $4,
              dead_at = $5::timestamptz
        WHERE delivery_id = $1::uuid
          AND claim_token = $2::uuid
          AND status = 'claimed'
        RETURNING *`,
      [request.deliveryId, request.claimToken, errorClass, errorText, now.toISOString(),
       providerReceiptJson ? JSON.stringify(providerReceiptJson) : null],
    );
    return {
      action: "dead",
      delivery: mapRuntimeDeliveryOutboxRow(updated.rows[0]!),
    };
  });
}

export function finalizeDeliverySent(
  db: pg.Pool | pg.PoolClient,
  request: Omit<FinalizeDeliveryRequest, "outcome">,
): Promise<FinalizeDeliveryResult> {
  return finalizeDelivery(db, { ...request, outcome: "sent" });
}

export function finalizeDeliveryRetryable(
  db: pg.Pool | pg.PoolClient,
  request: Omit<FinalizeDeliveryRequest, "outcome">,
): Promise<FinalizeDeliveryResult> {
  return finalizeDelivery(db, { ...request, outcome: "retryable_error" });
}

export function finalizeDeliveryDead(
  db: pg.Pool | pg.PoolClient,
  request: Omit<FinalizeDeliveryRequest, "outcome">,
): Promise<FinalizeDeliveryResult> {
  return finalizeDelivery(db, { ...request, outcome: "terminal_error" });
}

export async function cancelDelivery(
  db: pg.Pool | pg.PoolClient,
  request: CancelDeliveryRequest,
): Promise<RuntimeDeliveryOutbox> {
  return withTransaction(db, async (client) => {
    const result = await client.query<RuntimeDeliveryOutboxRow>(
      `SELECT *
         FROM runtime.delivery_outbox
        WHERE delivery_id = $1::uuid
        FOR UPDATE`,
      [request.deliveryId],
    );
    if (result.rowCount !== 1) {
      throw notFound("delivery_not_found", `delivery not found: ${request.deliveryId}`, {
        deliveryId: request.deliveryId,
      });
    }
    const delivery = mapRuntimeDeliveryOutboxRow(result.rows[0]!);
    if (delivery.status === "sent" || delivery.status === "dead" || delivery.status === "cancelled") {
      throw conflict("invalid_delivery_state", `delivery ${delivery.deliveryId} is already terminal`, {
        deliveryId: delivery.deliveryId,
        status: delivery.status,
      });
    }
    if (delivery.status === "claimed" && (!request.claimToken || request.claimToken !== delivery.claimToken)) {
      throw conflict("stale_delivery_claim_token", `stale claim token for delivery ${delivery.deliveryId}`, {
        deliveryId: delivery.deliveryId,
      });
    }

    const reason = cleanOptionalText(request.reason) ?? "Delivery cancelled";
    const now = await lockedAuthorityTime(client, request.now);
    if (delivery.status === "claimed") {
      requireCurrentExpiry(delivery.claimExpiresAt, now, "expired_delivery_claim");
    }
    const updated = await client.query<RuntimeDeliveryOutboxRow>(
      `UPDATE runtime.delivery_outbox
          SET status = 'cancelled',
              claim_owner = NULL,
              claim_token = NULL,
              claim_expires_at = NULL,
              last_error_class = 'delivery_cancelled',
              last_error_text = $2,
              cancelled_at = $3::timestamptz
        WHERE delivery_id = $1::uuid
        RETURNING *`,
      [request.deliveryId, reason, now.toISOString()],
    );
    return mapRuntimeDeliveryOutboxRow(updated.rows[0]!);
  });
}

export async function reconcileExpiredDeliveryClaims(
  db: pg.Pool | pg.PoolClient,
  options: DeliveryReconcileOptions = {},
): Promise<DeliveryReconcileItem[]> {
  const limit = options.limit ?? 100;
  const items: DeliveryReconcileItem[] = [];

  for (let i = 0; i < limit; i += 1) {
    const item = await withTransaction(db, async (client) => {
      const now = options.now ?? new Date();
      const locked = await client.query<RuntimeDeliveryOutboxRow>(
        `SELECT *
           FROM runtime.delivery_outbox
          WHERE status = 'claimed'
            AND claim_expires_at < $1::timestamptz
          ORDER BY claim_expires_at ASC, delivery_id ASC
          LIMIT 1
          FOR UPDATE SKIP LOCKED`,
        [now.toISOString()],
      );
      if (locked.rowCount !== 1) {
        return null;
      }

      const delivery = mapRuntimeDeliveryOutboxRow(locked.rows[0]!);
      const mustDeadLetter = retryWouldExceedAttempts(delivery) || retryWouldMissDeadline(delivery, now);
      if (!mustDeadLetter) {
        const backoffSec = deliveryBackoffSeconds(
          delivery.attemptCount,
          options.baseBackoffSec ?? 60,
          options.maxBackoffSec ?? 3600,
        );
        const retried = await client.query<RuntimeDeliveryOutboxRow>(
          `UPDATE runtime.delivery_outbox
              SET status = 'retryable',
                  runnable_at = $2::timestamptz,
                  claim_owner = NULL,
                  claim_token = NULL,
                  claim_expires_at = NULL,
                  last_error_class = 'delivery_claim_expired',
                  last_error_text = 'Delivery claim expired before fenced finalize'
            WHERE delivery_id = $1::uuid
            RETURNING *`,
          [delivery.deliveryId, new Date(now.getTime() + backoffSec * 1000).toISOString()],
        );
        return {
          action: "retried" as const,
          delivery: mapRuntimeDeliveryOutboxRow(retried.rows[0]!),
        };
      }

      const dead = await client.query<RuntimeDeliveryOutboxRow>(
        `UPDATE runtime.delivery_outbox
            SET status = 'dead',
                claim_owner = NULL,
                claim_token = NULL,
                claim_expires_at = NULL,
                last_error_class = 'delivery_claim_expired',
                last_error_text = 'Delivery claim expired before fenced finalize',
                dead_at = $2::timestamptz
          WHERE delivery_id = $1::uuid
          RETURNING *`,
        [delivery.deliveryId, now.toISOString()],
      );
      return {
        action: "dead" as const,
        delivery: mapRuntimeDeliveryOutboxRow(dead.rows[0]!),
      };
    });

    if (!item) break;
    items.push(item);
  }

  return items;
}
