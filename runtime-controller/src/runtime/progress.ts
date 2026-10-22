import type pg from "pg";
import { withTransaction } from "../db/tx.js";
import { mapRuntimeJobRow, mapRuntimeJobAttemptRow, type RuntimeJobRow, type RuntimeJobAttemptRow } from "../db/rows.js";
import { badRequest, conflict, notFound } from "../errors/controller-error.js";
import { lockedAuthorityTime, requireCurrentAttempt } from "./lease-authority.js";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const key = /^[a-zA-Z0-9_.:-]{1,100}$/;
const kinds = new Set(["started", "advanced", "completed", "reused", "warning", "interrupted"]);
type Stage = { stage: string; occurrence: string; kind: string; startedAt: string; updatedAt: string; facts: Record<string, unknown> };

function validate(raw: Record<string, unknown>) {
  const fields = new Set(["schemaVersion", "jobId", "attemptId", "leaseToken", "eventId", "sequence", "stage", "occurrence", "kind", "facts"]);
  if (Object.keys(raw).some(k => !fields.has(k)) || raw.schemaVersion !== 1) throw badRequest("invalid_progress", "invalid progress schema");
  for (const f of ["jobId", "attemptId", "leaseToken"]) if (typeof raw[f] !== "string" || !uuid.test(raw[f] as string)) throw badRequest("invalid_progress", `invalid ${f}`);
  for (const f of ["eventId", "stage", "occurrence"]) if (typeof raw[f] !== "string" || !key.test(raw[f] as string)) throw badRequest("invalid_progress", `invalid ${f}`);
  if (!Number.isSafeInteger(raw.sequence) || (raw.sequence as number) < 1 || !kinds.has(String(raw.kind))) throw badRequest("invalid_progress", "invalid sequence or kind");
  const facts = raw.facts ?? {};
  if (!facts || typeof facts !== "object" || Array.isArray(facts)) throw badRequest("invalid_progress", "invalid facts");
  const normalized: Record<string, unknown> = {};
  for (const [f, value] of Object.entries(facts).sort()) {
    if (["completed", "total", "words", "topics", "references", "bytes", "durationSeconds", "retrySeconds", "audioDurationSeconds"].includes(f)) {
      if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1e12) throw badRequest("invalid_progress", `invalid ${f}`);
      if (!["durationSeconds","retrySeconds","audioDurationSeconds"].includes(f) && !Number.isSafeInteger(value)) throw badRequest("invalid_progress", `non-integral ${f}`);
    } else if (f === "title") {
      if (typeof value !== "string" || value.length > 200 || /[\u0000-\u001f]/.test(value)) throw badRequest("invalid_progress", "invalid source title");
    } else if (f === "units") {
      if (!["chunks", "bytes", "items"].includes(String(value))) throw badRequest("invalid_progress", "invalid units");
    } else if (f === "reason") {
      if (!["source_retry", "provider_wait", "checkpoint_reused"].includes(String(value))) throw badRequest("invalid_progress", "invalid reason");
    } else throw badRequest("invalid_progress", `unsupported fact ${f}`);
    normalized[f] = value;
  }
  if (typeof normalized.completed === "number" && typeof normalized.total === "number" && normalized.completed > normalized.total) throw badRequest("invalid_progress", "completed exceeds total");
  return { schemaVersion: 1, jobId: raw.jobId as string, attemptId: raw.attemptId as string,
    eventId: raw.eventId as string, sequence: raw.sequence as number, stage: raw.stage as string,
    occurrence: raw.occurrence as string, kind: raw.kind as string, facts: normalized };
}

export async function appendProgress(db: pg.Pool | pg.PoolClient, raw: Record<string, unknown>) {
  const event = validate(raw); // Never persist the lease token.
  return withTransaction(db, async client => {
    const jr = await client.query<RuntimeJobRow>("SELECT * FROM runtime.jobs WHERE job_id=$1 FOR UPDATE", [event.jobId]);
    if (!jr.rowCount) throw notFound("job_not_found", "job not found");
    const prior = await client.query("SELECT version,payload_json FROM runtime.progress_events WHERE job_id=$1 AND event_id=$2", [event.jobId,event.eventId]);
    if (prior.rowCount) {
      // jsonb equality avoids property-order ambiguity and survives closed attempts.
      const equal = await client.query("SELECT $1::jsonb = $2::jsonb AS equal", [prior.rows[0].payload_json,event]);
      if (!equal.rows[0].equal) throw conflict("progress_event_conflict", "event ID reused with different facts");
      return { action: "existing", version: Number(prior.rows[0].version) };
    }
    const ar = await client.query<RuntimeJobAttemptRow>("SELECT * FROM runtime.job_attempts WHERE job_id=$1 AND attempt_id=$2 FOR UPDATE", [event.jobId,event.attemptId]);
    if (!ar.rowCount) throw notFound("attempt_not_found", "attempt not found");
    const job = mapRuntimeJobRow(jr.rows[0]!); const attempt = mapRuntimeJobAttemptRow(ar.rows[0]!);
    if (job.status !== "leased" || !["leased", "running"].includes(attempt.status) || job.leaseToken !== raw.leaseToken || attempt.leaseToken !== raw.leaseToken) throw conflict("stale_lease_token", "attempt no longer owns progress");
    const now = await lockedAuthorityTime(client);
    requireCurrentAttempt(job,attempt,now);
    const rate = await client.query(`SELECT count(*)::int AS n FROM
      (SELECT 1 FROM runtime.progress_events WHERE job_id=$1 AND created_at>$2::timestamptz-interval '1 minute' LIMIT 120) recent`,[event.jobId,now]);
    if (rate.rows[0].n >= 120) throw badRequest("progress_rate_limited", "progress event rate exceeded; coalesce counters");
    await client.query("INSERT INTO runtime.progress_snapshots(job_id) VALUES($1) ON CONFLICT DO NOTHING",[event.jobId]);
    const snapshot = (await client.query("SELECT * FROM runtime.progress_snapshots WHERE job_id=$1 FOR UPDATE",[event.jobId])).rows[0];
    const current = snapshot.attempt_id === event.attemptId;
    if (current && event.sequence <= Number(snapshot.sequence)) throw conflict("progress_sequence_conflict", "out-of-order progress sequence");
    const stages: Stage[] = current ? snapshot.stages_json : [];
    const previous = stages.find(s => s.occurrence === event.occurrence);
    if (previous && previous.stage !== event.stage) throw conflict("progress_stage_conflict", "occurrence belongs to another stage");
    if (previous && ["completed","reused","interrupted"].includes(previous.kind)) throw conflict("progress_stage_closed", "stage occurrence already closed");
    const priorCount = previous?.facts.completed;
    if (typeof priorCount === "number" && typeof event.facts.completed === "number" && event.facts.completed < priorCount) throw conflict("progress_count_regression", "completion count regressed");
    const stage: Stage = { stage:event.stage,occurrence:event.occurrence,kind:event.kind,
      startedAt:previous?.startedAt ?? now.toISOString(),updatedAt:now.toISOString(),facts:{...previous?.facts,...event.facts} };
    if (typeof stage.facts.completed === "number" && typeof stage.facts.total === "number" && stage.facts.completed > stage.facts.total) throw badRequest("invalid_progress", "completed exceeds known total");
    const updated = [...stages.filter(s => s.occurrence !== event.occurrence),stage];
    if (updated.length > 32) {
      const removable = updated.findIndex(s => ["completed","reused","interrupted"].includes(s.kind));
      if (removable < 0) throw badRequest("invalid_progress", "too many active stages");
      updated.splice(removable,1);
    }
    const version = Number(snapshot.version) + 1;
    await client.query("UPDATE runtime.progress_snapshots SET version=$2,attempt_id=$3,sequence=$4,stages_json=$5,updated_at=$6 WHERE job_id=$1",[event.jobId,version,event.attemptId,event.sequence,JSON.stringify(updated),now]);
    await client.query("INSERT INTO runtime.progress_events(job_id,version,event_id,attempt_id,payload_json) VALUES($1,$2,$3,$4,$5)",[event.jobId,version,event.eventId,event.attemptId,event]);
    return { action:"created",version };
  });
}

export async function getProgress(db: pg.Pool | pg.PoolClient, jobId: string, after=0) {
  if (!uuid.test(jobId) || !Number.isSafeInteger(after) || after < 0) throw badRequest("invalid_progress", "invalid job ID or cursor");
  // One statement establishes a consistent read of snapshot, lifecycle and history.
  const r = await db.query(`SELECT to_jsonb(j) AS job,to_jsonb(a) AS attempt,
    COALESCE(s.version,0)::int8 AS version,COALESCE(s.stages_json,'[]'::jsonb) AS stages,
    s.updated_at AS progress_at,s.attempt_id AS progress_attempt,to_jsonb(r) AS result,clock_timestamp() AS server_time,
    COALESCE((SELECT jsonb_agg(x ORDER BY x.version) FROM
      (SELECT version,event_id,payload_json,created_at FROM runtime.progress_events WHERE job_id=j.job_id AND version>$2 ORDER BY version LIMIT 100) x),'[]'::jsonb) AS events
    FROM runtime.jobs j LEFT JOIN runtime.progress_snapshots s ON s.job_id=j.job_id
    LEFT JOIN LATERAL (SELECT * FROM runtime.job_attempts WHERE job_id=j.job_id ORDER BY attempt_no DESC LIMIT 1) a ON true
    LEFT JOIN runtime.job_results r ON r.result_id=j.terminal_result_id WHERE j.job_id=$1`,[jobId,after]);
  if (!r.rowCount) throw notFound("job_not_found", "job not found");
  const x=r.rows[0];
  return { schemaVersion:1, job:mapRuntimeJobRow(x.job),attempt:x.attempt ? mapRuntimeJobAttemptRow(x.attempt) : null,
    version:Number(x.version),stages:!x.attempt || x.progress_attempt === x.attempt.attempt_id ? x.stages : [],progressAt:x.progress_at,serverTime:x.server_time,
    // Product result stays in the established result API; expose no private result body here.
    resultId:x.result?.result_id ?? null,events:x.events };
}
