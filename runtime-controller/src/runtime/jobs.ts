import type pg from "pg";
import { mapRuntimeJobRow, type RuntimeJobRow } from "../db/rows.js";
import type { RuntimeJob } from "../types/runtime.js";

export interface RuntimeJobQuery {
  producerSystem?: string;
  producerRef?: string;
  producerRefPrefix?: string;
  resultOwner?: string;
  workloadType?: string;
  status?: readonly RuntimeJob["status"][];
  limit?: number;
}

export interface RuntimeJobListResult {
  items: RuntimeJob[];
  count: number;
}

function trimOrNull(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function normalizeStatuses(statuses: readonly RuntimeJob["status"][] | undefined): RuntimeJob["status"][] | null {
  if (!statuses || statuses.length === 0) return null;
  const normalized = statuses.map((status) => status.trim()).filter(Boolean) as RuntimeJob["status"][];
  return normalized.length > 0 ? Array.from(new Set(normalized)) : null;
}

function buildRuntimeJobQuery(query: RuntimeJobQuery): {
  whereSql: string;
  params: unknown[];
  limit: number;
} {
  const clauses: string[] = [];
  const params: unknown[] = [];

  const producerSystem = trimOrNull(query.producerSystem);
  if (producerSystem) {
    params.push(producerSystem);
    clauses.push(`producer_system = $${params.length}`);
  }

  const producerRef = trimOrNull(query.producerRef);
  if (producerRef) {
    params.push(producerRef);
    clauses.push(`producer_ref = $${params.length}`);
  }

  const producerRefPrefix = trimOrNull(query.producerRefPrefix);
  if (producerRefPrefix) {
    params.push(`${producerRefPrefix}%`);
    clauses.push(`producer_ref LIKE $${params.length}`);
  }

  const resultOwner = trimOrNull(query.resultOwner);
  if (resultOwner) {
    params.push(resultOwner);
    clauses.push(`result_owner = $${params.length}`);
  }

  const workloadType = trimOrNull(query.workloadType);
  if (workloadType) {
    params.push(workloadType);
    clauses.push(`workload_type = $${params.length}`);
  }

  const statuses = normalizeStatuses(query.status);
  if (statuses) {
    params.push(statuses);
    clauses.push(`status = ANY($${params.length}::text[])`);
  }

  return {
    whereSql: clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "",
    params,
    limit: Math.max(1, Math.min(query.limit ?? 100, 500)),
  };
}

export async function listRuntimeJobs(
  db: pg.Pool | pg.PoolClient,
  query: RuntimeJobQuery,
): Promise<RuntimeJobListResult> {
  const built = buildRuntimeJobQuery(query);
  const limitParam = `$${built.params.length + 1}`;
  const itemsResult = await db.query<RuntimeJobRow>(
    `SELECT *
       FROM runtime.jobs
       ${built.whereSql}
      ORDER BY created_at DESC, job_id DESC
      LIMIT ${limitParam}`,
    [...built.params, built.limit],
  );

  const countResult = await db.query<{ count: string }>(
    `SELECT COUNT(*)::text AS count
       FROM runtime.jobs
       ${built.whereSql}`,
    built.params,
  );

  return {
    items: itemsResult.rows.map(mapRuntimeJobRow),
    count: Number(countResult.rows[0]?.count ?? 0),
  };
}

export async function getRuntimeJob(
  db: pg.Pool | pg.PoolClient,
  jobId: string,
): Promise<RuntimeJob | null> {
  const result = await db.query<RuntimeJobRow>(
    `SELECT *
       FROM runtime.jobs
      WHERE job_id = $1::uuid
      LIMIT 1`,
    [jobId],
  );

  if (result.rowCount !== 1) {
    return null;
  }

  return mapRuntimeJobRow(result.rows[0]!);
}
