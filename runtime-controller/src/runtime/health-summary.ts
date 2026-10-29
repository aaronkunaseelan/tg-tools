import type pg from "pg";

export const SUMMARY_LIMIT = 10000;
const WORD = /^[A-Za-z0-9_.:-]{1,80}$/;
export function summaryNames(raw: string | null): string[] {
  const names = [...new Set((raw ?? "").split(","))];
  if (!names.length || names.length > 16 || names.some((s) => !WORD.test(s))) {
    throw new Error("invalid health-summary names (1..16 required)");
  }
  return names;
}
export function summaryWindow(raw: string | null): number {
  const seconds = raw === null ? 21600 : Number(raw);
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > 604800) throw new Error("invalid health-summary window (1..604800)");
  return seconds;
}
function errorClass(value: unknown): string {
  return typeof value === "string" && WORD.test(value) ? value : "unclassified";
}

/** Producer-owned, payload-free view. Every history read has an indexed row cap.
 * complete=false means a cap was reached: consumers must not infer absence/counts.
 */
export async function getHealthSummary(db: pg.Pool | pg.PoolClient, workloads: string[], owners: string[], windowSec = 21600) {
  summaryNames(workloads.join(","));
  summaryNames(owners.join(","));
  summaryWindow(String(windowSec));
  const time = await db.query<{ at: Date }>("SELECT clock_timestamp() AS at");
  const at = time.rows[0]!.at;
  const since = new Date(at.getTime() - windowSec * 1000);
  const items = [];
  for (const workloadType of workloads) {
    const jobs = await db.query<{ status: string; created_at: Date }>(
      `SELECT status, created_at FROM runtime.jobs WHERE workload_type = $1
       ORDER BY created_at DESC LIMIT $2`, [workloadType, SUMMARY_LIMIT + 1]);
    const active = await db.query<{ started_at: Date; deadline_at: Date | null; lease_expires_at: Date }>(
      `SELECT a.started_at, j.deadline_at, a.lease_expires_at
       FROM (SELECT job_id, deadline_at FROM runtime.jobs
             WHERE workload_type = $1 AND status = 'leased' LIMIT $2) j
       JOIN runtime.job_attempts a ON a.job_id = j.job_id AND a.status IN ('leased','running')`,
      [workloadType, SUMMARY_LIMIT + 1]);
    const jobsByStatus: Record<string, number> = {};
    for (const row of jobs.rows) jobsByStatus[row.status] = (jobsByStatus[row.status] ?? 0) + 1;
    items.push({ workloadType, newestJobAt: jobs.rows[0]?.created_at.toISOString() ?? null,
      jobsByStatus, countsComplete: jobs.rows.length <= SUMMARY_LIMIT,
      activeComplete: active.rows.length <= SUMMARY_LIMIT,
      oldestActiveAgeSec: active.rows.length ? Math.max(0, ...active.rows.map((r) => (at.getTime() - r.started_at.getTime()) / 1000)) : null,
      pastDeadline: active.rows.some((r) => r.deadline_at !== null && r.deadline_at <= at),
      expiredLease: active.rows.some((r) => r.lease_expires_at <= at) });
  }
  // Terminal result creation is the job's dead transition, not job creation or updated_at.
  const dead = await db.query<{ workload_type: string; error_class: string | null }>(
    `SELECT j.workload_type, r.error_class FROM
       (SELECT job_id, error_class FROM runtime.job_results
        WHERE result_kind = 'dead' AND created_at >= $1 AND created_at <= $2
        ORDER BY created_at DESC LIMIT $3) r
     JOIN runtime.jobs j ON j.job_id = r.job_id`, [since, at, SUMMARY_LIMIT + 1]);
  const grouped = new Map<string, { kind: string; workloadType: string; errorClass: string; count: number }>();
  const add = (kind: string, workloadType: string, error: unknown) => {
    const cls = errorClass(error);
    const key = JSON.stringify([kind, workloadType, cls]);
    const existing = grouped.get(key);
    if (existing) existing.count++; else grouped.set(key, { kind, workloadType, errorClass: cls, count: 1 });
  };
  for (const r of dead.rows) if (workloads.includes(r.workload_type)) add("job", r.workload_type, r.error_class);
  let failuresComplete = dead.rows.length <= SUMMARY_LIMIT;
  for (const owner of owners) {
    const deliveries = await db.query<{ workload_type: string | null; last_error_class: string | null }>(
      `SELECT j.workload_type, d.last_error_class FROM
         (SELECT job_id, last_error_class FROM runtime.delivery_outbox
          WHERE result_owner = $1 AND status = 'dead' AND dead_at >= $2 AND dead_at <= $3
          ORDER BY dead_at DESC LIMIT $4) d
       LEFT JOIN runtime.jobs j ON j.job_id = d.job_id`, [owner, since, at, SUMMARY_LIMIT + 1]);
    failuresComplete &&= deliveries.rows.length <= SUMMARY_LIMIT;
    for (const r of deliveries.rows) add("delivery", r.workload_type ?? owner, r.last_error_class);
  }
  return { schema: "runtime.health-summary/1", at: at.getTime() / 1000, windowSec,
    workloads: items, failuresComplete, failures: [...grouped.values()] };
}
