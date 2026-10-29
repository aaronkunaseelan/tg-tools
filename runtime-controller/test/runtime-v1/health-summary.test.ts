import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { Pool } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import { getHealthSummary, summaryNames, summaryWindow, SUMMARY_LIMIT } from "../../src/runtime/health-summary.js";
import { admitAndSubmitRuntimeJob } from "../../src/runtime/submit.js";
import { claimRuntimeJob } from "../../src/runtime/claim.js";
import { finalizeAttempt } from "../../src/runtime/finalize.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import { appendDelivery, claimDelivery, finalizeDeliveryDead } from "../../src/runtime/delivery.js";

function validates(value: unknown) {
  const result = spawnSync("python3", ["python/contract_validate.py", "contracts/runtime.health-summary-1.json"],
    { input: JSON.stringify(value), encoding: "utf8" });
  expect(result.status, result.stdout + result.stderr).toBe(0);
}
describe.sequential("bounded payload-free summary", () => {
  let admin: Pool, pool: Pool, name: string;
  beforeAll(async () => {
    name = "summary_" + randomUUID().replaceAll("-", "");
    admin = new Pool({ ...testDatabaseConfig(), database: "postgres" });
    await admin.query(`CREATE DATABASE ${name}`);
    pool = new Pool({ ...testDatabaseConfig(), database: name });
    await applyMigrations(pool);
  }, 20000);
  afterAll(async () => { await pool?.end(); await admin?.query(`DROP DATABASE IF EXISTS ${name}`); await admin?.end(); });

  it("validates real output; uses dead transition time, strips private fields, includes orphan delivery", async () => {
    await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer", producerRef: "private-ref", resultOwner: "scorer",
      resultRoute: "scorer.reply", workloadType: "scorer", queueClass: "foreground",
      executionProfile: "cpu_small", executionShape: "local_bounded", idempotencyKey: "summary",
      inputJson: { text: "PRIVATE_SENTINEL" },
    }, resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!);
    const claim = (await claimRuntimeJob(pool, { workerId: "summary-worker",
      supportedExecutionProfiles: ["cpu_small"], workloadTypes: ["scorer"], leaseDurationSec: 300 }))!;
    await pool.query("UPDATE runtime.jobs SET created_at = now() - interval '2 days' WHERE job_id=$1", [claim.job.jobId]);
    await finalizeAttempt(pool, { jobId: claim.job.jobId, attemptId: claim.attempt.attemptId,
      leaseToken: claim.attempt.leaseToken, outcome: "terminal_error", errorClass: "speech_timeout",
      errorText: "PRIVATE_SENTINEL" });
    await appendDelivery(pool, { resultOwner: "scorer", resultRoute: "scorer.reply",
      deliveryProvider: "telegram", deliveryKind: "text_message", deliveryKey: "summary",
      payloadJson: { text: "PRIVATE_SENTINEL" } });
    const delivery = (await claimDelivery(pool, { claimOwner: "summary-gateway", deliveryProvider: "telegram" }))!;
    await finalizeDeliveryDead(pool, { deliveryId: delivery.delivery.deliveryId,
      claimToken: delivery.delivery.claimToken!, errorClass: "telegram_forbidden", errorText: "PRIVATE_SENTINEL" });
    const value = await getHealthSummary(pool, ["scorer", "quiet"], ["scorer"]);
    validates(value);
    expect(value.failures).toEqual([
      { kind: "job", workloadType: "scorer", errorClass: "speech_timeout", count: 1 },
      { kind: "delivery", workloadType: "scorer", errorClass: "telegram_forbidden", count: 1 }]);
    expect(value.workloads[0]!.jobsByStatus).toEqual({ dead: 1 });
    expect(value.workloads[1]!.newestJobAt).toBeNull();
    expect(JSON.stringify(value)).not.toContain("PRIVATE_SENTINEL");
    expect(JSON.stringify(value)).not.toContain(claim.job.jobId);
  });

  it("reports current attempt age/deadline/expiry independently of renewals", async () => {
    await admitAndSubmitRuntimeJob(pool, {
      producerSystem: "scorer", producerRef: "active", resultOwner: "scorer", resultRoute: "scorer.reply",
      workloadType: "scorer", queueClass: "foreground", executionProfile: "cpu_small",
      executionShape: "local_bounded", idempotencyKey: "active",
    }, resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!);
    const claim = (await claimRuntimeJob(pool, { workerId: "summary-worker",
      supportedExecutionProfiles: ["cpu_small"], workloadTypes: ["scorer"], leaseDurationSec: 300 }))!;
    await pool.query("UPDATE runtime.jobs SET deadline_at=now()-interval '1 second' WHERE job_id=$1", [claim.job.jobId]);
    await pool.query("UPDATE runtime.job_attempts SET started_at=now()-interval '1 hour', lease_expires_at=now()-interval '1 second' WHERE attempt_id=$1", [claim.attempt.attemptId]);
    const value = await getHealthSummary(pool, ["scorer"], ["scorer"]);
    validates(value);
    expect(value.workloads[0]!.oldestActiveAgeSec).toBeGreaterThanOrEqual(3600);
    expect(value.workloads[0]!.pastDeadline).toBe(true);
    expect(value.workloads[0]!.expiredLease).toBe(true);
  });

  it("marks capped counts and failures incomplete; every row query is limited", async () => {
    const at = new Date();
    const query = vi.fn(async (sql: string) => {
      if (sql.includes("clock_timestamp")) return { rows: [{ at }] };
      expect(sql).toContain("LIMIT");
      if (sql.includes("SELECT status")) return { rows: Array.from({ length: SUMMARY_LIMIT + 1 }, () => ({ status: "dead", created_at: at })) };
      if (sql.includes("a.started_at")) return { rows: [] };
      if (sql.includes("r.error_class")) return { rows: Array.from({ length: SUMMARY_LIMIT + 1 }, () => ({ workload_type: "scorer", error_class: "bad private text" })) };
      return { rows: [] };
    });
    const value = await getHealthSummary({ query } as unknown as Pool, ["scorer"], ["scorer"]);
    validates(value);
    expect(value.workloads[0]!.countsComplete).toBe(false);
    expect(value.failuresComplete).toBe(false);
    expect(value.failures[0]!.errorClass).toBe("unclassified");
  });
});
it("bounds and validates request parameters", () => {
  expect(summaryWindow(null)).toBe(21600);
  for (const raw of ["0", "604801", "NaN", "1.5"]) expect(() => summaryWindow(raw)).toThrow();
  for (const raw of [null, "", "a b", Array.from({ length: 17 }, (_, i) => "a"+i).join(",")]) expect(() => summaryNames(raw)).toThrow();
  expect(summaryNames("a,a,b")).toEqual(["a","b"]);
});
