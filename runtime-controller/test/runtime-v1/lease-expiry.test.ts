import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool, type PoolClient } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../../src/policy/default-policy.js";
import { admitAndSubmitRuntimeJob } from "../../src/runtime/submit.js";
import { claimRuntimeJob } from "../../src/runtime/claim.js";
import { heartbeatAttempt, markAttemptRunning, type AttemptLeaseRef } from "../../src/runtime/heartbeat.js";
import { finalizeAttempt } from "../../src/runtime/finalize.js";
import { abandonAttempt } from "../../src/runtime/abandon.js";
import {
  appendDelivery, claimDelivery, heartbeatDelivery, cancelDelivery,
  finalizeDeliverySent, finalizeDeliveryRetryable, finalizeDeliveryDead,
} from "../../src/runtime/delivery.js";

type Database = Pool | PoolClient;
const attemptOperations = {
  heartbeat: (db: Database, ref: AttemptLeaseRef) => heartbeatAttempt(db, { ...ref, leaseDurationSec: 120 }),
  start: (db: Database, ref: AttemptLeaseRef) => markAttemptRunning(db, ref),
  finalize: (db: Database, ref: AttemptLeaseRef) => finalizeAttempt(db, { ...ref, outcome: "succeeded", resultJson: { ok: true } }),
  abandon: (db: Database, ref: AttemptLeaseRef) => abandonAttempt(db, { ...ref, reason: "fixture" }),
};
const deliveryOperations = {
  heartbeat: (db: Database, ref: { deliveryId: string; claimToken: string }) => heartbeatDelivery(db, ref),
  sent: (db: Database, ref: { deliveryId: string; claimToken: string }) => finalizeDeliverySent(db, { ...ref, providerReceiptJson: { messageId: 123 } }),
  retryable: (db: Database, ref: { deliveryId: string; claimToken: string }) => finalizeDeliveryRetryable(db, { ...ref, errorClass: "fixture" }),
  dead: (db: Database, ref: { deliveryId: string; claimToken: string }) => finalizeDeliveryDead(db, {
    ...ref, errorClass: "delivery_uncertain", providerReceiptJson: { messageId: 123, reconciliation: "required" },
  }),
  cancel: (db: Database, ref: { deliveryId: string; claimToken: string }) => cancelDelivery(db, { ...ref, reason: "fixture" }),
};

describe.sequential("PostgreSQL lease expiry authority", () => {
  let admin: Pool;
  let pool: Pool;
  let dbName: string;

  beforeAll(async () => {
    dbName = `runtime_expiry_${randomUUID().replace(/-/g, "")}`;
    const config = testDatabaseConfig();
    admin = new Pool({ ...config, database: "postgres" });
    await admin.query(`CREATE DATABASE ${dbName}`);
    pool = new Pool({ ...config, database: dbName, max: 8 });
    await applyMigrations(pool);
  }, 20_000);

  afterAll(async () => {
    await pool?.end();
    if (dbName) await admin?.query(`DROP DATABASE IF EXISTS ${dbName}`);
    await admin?.end();
  }, 20_000);

  async function attemptFixture(): Promise<AttemptLeaseRef> {
    const id = randomUUID();
    const submitted = await admitAndSubmitRuntimeJob(pool, {
      producerSystem: id, producerRef: id, resultOwner: id, resultRoute: "fixture",
      workloadType: "scorer", queueClass: "foreground", executionProfile: "cpu_small",
      executionShape: "local_bounded", idempotencyKey: id,
    }, resolveWorkloadPolicy(DEFAULT_RUNTIME_POLICY, "scorer")!);
    if (submitted.action !== "created") throw new Error("expected fixture submission");
    const claimed = await claimRuntimeJob(pool, {
      workerId: id, supportedExecutionProfiles: ["cpu_small"], producerSystems: [id], leaseDurationSec: 120,
    });
    if (!claimed || claimed.job.jobId !== submitted.job.jobId) throw new Error("expected fixture claim");
    return { jobId: claimed.job.jobId, attemptId: claimed.attempt.attemptId, leaseToken: claimed.attempt.leaseToken };
  }

  async function deliveryFixture() {
    const id = randomUUID();
    const appended = await appendDelivery(pool, {
      resultOwner: id, resultRoute: "fixture", deliveryProvider: id, deliveryKind: "text_message",
      deliveryKey: id, payloadJson: { text: "offline" },
    });
    const claimed = await claimDelivery(pool, { claimOwner: id, deliveryProvider: id, leaseDurationSec: 120 });
    if (!claimed?.delivery.claimToken || claimed.delivery.deliveryId !== appended.delivery.deliveryId) {
      throw new Error("expected fixture delivery claim");
    }
    return { deliveryId: claimed.delivery.deliveryId, claimToken: claimed.delivery.claimToken };
  }

  async function attemptSnapshot(ref: AttemptLeaseRef) {
    const result = await pool.query(`SELECT
      (SELECT to_jsonb(j) FROM runtime.jobs j WHERE job_id = $1) AS job,
      (SELECT to_jsonb(a) FROM runtime.job_attempts a WHERE attempt_id = $2) AS attempt,
      (SELECT count(*)::int FROM runtime.job_results WHERE job_id = $1) AS results`, [ref.jobId, ref.attemptId]);
    return result.rows[0];
  }

  async function deliverySnapshot(deliveryId: string) {
    return (await pool.query("SELECT to_jsonb(d) AS delivery FROM runtime.delivery_outbox d WHERE delivery_id = $1", [deliveryId])).rows[0];
  }

  for (const field of ["job", "attempt"] as const) {
    for (const [name, operation] of Object.entries(attemptOperations)) {
      it(`rejects matching-token ${name} when only ${field} lease expired, without mutation`, async () => {
        const ref = await attemptFixture();
        const table = field === "job" ? "jobs" : "job_attempts";
        const column = field === "job" ? "job_id" : "attempt_id";
        await pool.query(`UPDATE runtime.${table} SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE ${column} = $1`,
          [field === "job" ? ref.jobId : ref.attemptId]);
        const before = await attemptSnapshot(ref);
        expect(before.job.lease_token).toBe(ref.leaseToken);
        expect(before.attempt.lease_token).toBe(ref.leaseToken);
        await expect(operation(pool, ref)).rejects.toMatchObject({ code: "expired_lease", statusCode: 409 });
        expect(await attemptSnapshot(ref)).toEqual(before);
      });
    }
  }

  it("does not authorize an already-running attempt's repeated start after expiry", async () => {
    const ref = await attemptFixture();
    await markAttemptRunning(pool, ref);
    await pool.query("UPDATE runtime.job_attempts SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE attempt_id = $1", [ref.attemptId]);
    const before = await attemptSnapshot(ref);
    await expect(markAttemptRunning(pool, ref)).rejects.toMatchObject({ code: "expired_lease" });
    expect(await attemptSnapshot(ref)).toEqual(before);
  });

  it("accepts valid start, repeated start, heartbeat and success finalize", async () => {
    const ref = await attemptFixture();
    expect((await markAttemptRunning(pool, ref)).attempt.status).toBe("running");
    expect((await markAttemptRunning(pool, ref)).attempt.status).toBe("running");
    const heartbeat = await heartbeatAttempt(pool, { ...ref, leaseDurationSec: 300 });
    expect(heartbeat.job.leaseExpiresAt).toBe(heartbeat.attempt.leaseExpiresAt);
    const finalized = await finalizeAttempt(pool, { ...ref, outcome: "succeeded", resultJson: { ok: true } });
    expect(finalized.job.status).toBe("succeeded");
    expect(finalized.result?.attemptId).toBe(ref.attemptId);
    expect((await attemptSnapshot(ref)).results).toBe(1);
  });

  it("accepts abandon with valid matching leases", async () => {
    const ref = await attemptFixture();
    const abandoned = await abandonAttempt(pool, { ...ref, reason: "fixture" });
    expect(abandoned.attempt.status).toBe("abandoned");
    expect(abandoned.job.status).toBe("retryable");
  });

  for (const [name, operation] of Object.entries(deliveryOperations)) {
    it(`rejects matching-token delivery ${name} after claim expiry, without mutation`, async () => {
      const ref = await deliveryFixture();
      await pool.query("UPDATE runtime.delivery_outbox SET claim_expires_at = clock_timestamp() - interval '1 second' WHERE delivery_id = $1", [ref.deliveryId]);
      const before = await deliverySnapshot(ref.deliveryId);
      expect(before.delivery.claim_token).toBe(ref.claimToken);
      await expect(operation(pool, ref)).rejects.toMatchObject({ code: "expired_delivery_claim", statusCode: 409 });
      expect(await deliverySnapshot(ref.deliveryId)).toEqual(before);
    });
  }

  for (const [name, operation] of Object.entries(deliveryOperations)) {
    it(`accepts delivery ${name} with an unexpired matching claim`, async () => {
      const ref = await deliveryFixture();
      await expect(operation(pool, ref)).resolves.toBeDefined();
      const row = (await deliverySnapshot(ref.deliveryId)).delivery;
      expect(row.status).toBe({ heartbeat: "claimed", sent: "sent", retryable: "retryable", dead: "dead", cancel: "cancelled" }[name]);
      if (name === "sent") expect(row.provider_receipt_json).toEqual({ messageId: 123 });
      if (name === "dead") {
        expect(row.provider_receipt_json).toEqual({ messageId: 123, reconciliation: "required" });
        expect(row.last_error_class).toBe("delivery_uncertain");
        expect(row.claim_token).toBeNull();
        expect(row.dead_at).not.toBeNull();
        // Receipt survives a new read on a separate physical DB connection.
        const first = await pool.connect();
        const reader = await pool.connect();
        try {
          const firstPid = (await first.query("SELECT pg_backend_pid() AS pid")).rows[0].pid;
          expect((await reader.query("SELECT pg_backend_pid() AS pid")).rows[0].pid).not.toBe(firstPid);
          expect((await reader.query("SELECT provider_receipt_json FROM runtime.delivery_outbox WHERE delivery_id = $1", [ref.deliveryId])).rows[0].provider_receipt_json)
            .toEqual({ messageId: 123, reconciliation: "required" });
        } finally {
          reader.release();
          first.release();
        }
      }
    });
  }

  // Hold a real row lock, verify the authority transaction is blocked BEFORE
  // expiry, and release only AFTER expiry. No supplied application clock.
  // This fails both entry-time sampling and transaction NOW() authorization.
  async function acrossExpiry(table: string, column: string, id: string, expiry: Date,
    operation: (client: PoolClient) => Promise<unknown>, code: string) {
    const holder = await pool.connect();
    const waiter = await pool.connect();
    let pending: Promise<unknown> | undefined;
    try {
      await holder.query("BEGIN");
      await holder.query(`SELECT 1 FROM runtime.${table} WHERE ${column} = $1 FOR UPDATE`, [id]);
      await waiter.query("BEGIN");
      const pid = (await waiter.query("SELECT pg_backend_pid() AS pid")).rows[0].pid;
      // Attach rejection handling immediately to avoid an unhandled rejection.
      pending = operation(waiter).then(() => ({ accepted: true }), error => error);
      let blocked = false;
      for (let i = 0; i < 100; i++) {
        const activity = await pool.query(`SELECT cardinality(pg_blocking_pids(pid)) > 0 AS blocked,
          xact_start < $2::timestamptz AS began_before_expiry,
          clock_timestamp() < $2::timestamptz AS still_valid
          FROM pg_stat_activity WHERE pid = $1`, [pid, expiry]);
        if (activity.rows[0]?.blocked) {
          expect(activity.rows[0].began_before_expiry).toBe(true);
          expect(activity.rows[0].still_valid).toBe(true);
          blocked = true;
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      expect(blocked, "operation must actually wait on the row lock before expiry").toBe(true);
      await holder.query("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM ($1::timestamptz - clock_timestamp()))) + 0.05)", [expiry]);
      await holder.query("COMMIT");
      expect(await pending).toMatchObject({ code, statusCode: 409 });
    } finally {
      await holder.query("ROLLBACK");
      if (pending) await pending;
      await waiter.query("ROLLBACK");
      holder.release();
      waiter.release();
    }
  }

  for (const field of ["job", "attempt"] as const) {
    for (const [name, operation] of Object.entries(attemptOperations)) {
      it(`rejects ${name} after waiting on ${field} lock across lease expiry`, async () => {
        const ref = await attemptFixture();
        const expiry = (await pool.query("SELECT clock_timestamp() + interval '1.5 seconds' AS expiry")).rows[0].expiry as Date;
        const table = field === "job" ? "jobs" : "job_attempts";
        const column = field === "job" ? "job_id" : "attempt_id";
        const id = field === "job" ? ref.jobId : ref.attemptId;
        await pool.query(`UPDATE runtime.${table} SET lease_expires_at = $2 WHERE ${column} = $1`, [id, expiry]);
        const before = await attemptSnapshot(ref);
        await acrossExpiry(table, column, id, expiry, client => operation(client, ref), "expired_lease");
        expect(await attemptSnapshot(ref)).toEqual(before);
      }, 10_000);
    }
  }

  for (const [name, operation] of Object.entries(deliveryOperations)) {
    it(`rejects delivery ${name} after row-lock wait crosses claim expiry`, async () => {
      const ref = await deliveryFixture();
      const expiry = (await pool.query("SELECT clock_timestamp() + interval '1.5 seconds' AS expiry")).rows[0].expiry as Date;
      await pool.query("UPDATE runtime.delivery_outbox SET claim_expires_at = $2 WHERE delivery_id = $1", [ref.deliveryId, expiry]);
      const before = await deliverySnapshot(ref.deliveryId);
      await acrossExpiry("delivery_outbox", "delivery_id", ref.deliveryId, expiry, client => operation(client, ref), "expired_delivery_claim");
      expect(await deliverySnapshot(ref.deliveryId)).toEqual(before);
    }, 10_000);
  }
});
