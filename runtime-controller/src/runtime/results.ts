import type pg from "pg";
import { mapRuntimeJobResultRow, type RuntimeJobResultRow } from "../db/rows.js";
import { withTransaction } from "../db/tx.js";
import type { RuntimeJobResult } from "../types/runtime.js";

export type ResultAckState = "ready" | "claimed" | "consumed";

export interface ResultOwnerQuery {
  resultOwner: string;
  limit?: number;
}

export interface ConsumeResultRequest {
  resultId: string;
  resultOwner: string;
  now?: Date;
}

export function deriveResultAckState(result: Pick<RuntimeJobResult, "claimedAt" | "consumedAt">): ResultAckState {
  if (result.consumedAt) return "consumed";
  if (result.claimedAt) return "claimed";
  return "ready";
}

export function canConsumeResult(resultOwner: string, expectedOwner: string): boolean {
  return resultOwner.trim() !== "" && resultOwner === expectedOwner;
}

export async function listReadyResultsForOwner(
  db: pg.Pool | pg.PoolClient,
  query: ResultOwnerQuery,
): Promise<RuntimeJobResult[]> {
  const result = await db.query<RuntimeJobResultRow>(
    `SELECT *
       FROM runtime.job_results
      WHERE result_owner = $1
        AND consumed_at IS NULL
      ORDER BY created_at ASC, result_id ASC
      LIMIT $2`,
    [query.resultOwner, query.limit ?? 100],
  );

  return result.rows.map(mapRuntimeJobResultRow);
}

export async function claimResultsForOwner(
  db: pg.Pool | pg.PoolClient,
  query: ResultOwnerQuery,
): Promise<RuntimeJobResult[]> {
  return withTransaction(db, async (client) => {
    const result = await client.query<RuntimeJobResultRow>(
      `WITH target AS (
         SELECT result_id
           FROM runtime.job_results
          WHERE result_owner = $1
            AND claimed_at IS NULL
            AND consumed_at IS NULL
          ORDER BY created_at ASC, result_id ASC
          LIMIT $2
          FOR UPDATE SKIP LOCKED
       )
       UPDATE runtime.job_results r
          SET claimed_at = NOW()
         FROM target
        WHERE r.result_id = target.result_id
        RETURNING r.*`,
      [query.resultOwner, query.limit ?? 100],
    );

    return result.rows.map(mapRuntimeJobResultRow);
  });
}

export async function consumeResult(
  db: pg.Pool | pg.PoolClient,
  request: ConsumeResultRequest,
): Promise<RuntimeJobResult | null> {
  return withTransaction(db, async (client) => {
    const result = await client.query<RuntimeJobResultRow>(
      `UPDATE runtime.job_results
          SET claimed_at = COALESCE(claimed_at, $3::timestamptz),
              consumed_at = $3::timestamptz
        WHERE result_id = $1::uuid
          AND result_owner = $2
          AND consumed_at IS NULL
        RETURNING *`,
      [request.resultId, request.resultOwner, (request.now ?? new Date()).toISOString()],
    );

    if (result.rowCount !== 1) {
      return null;
    }

    return mapRuntimeJobResultRow(result.rows[0]!);
  });
}
