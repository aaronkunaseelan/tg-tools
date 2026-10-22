import { randomUUID } from "node:crypto";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { Pool } from "pg";
import { testDatabaseConfig } from "./test-database.js";
import { applyMigrations } from "../../src/db/migrate.js";
import {
  appendDelivery,
  cancelDelivery,
  claimDelivery,
  finalizeDeliveryDead,
  finalizeDeliveryRetryable,
  finalizeDeliverySent,
  heartbeatDelivery,
  reconcileExpiredDeliveryClaims,
} from "../../src/runtime/delivery.js";

// A current relative timeline keeps successful claims valid against the real
// authority clock as well as the explicitly supplied internal test clock.
const timelineStart = Date.now();
function at(seconds: number): Date {
  return new Date(timelineStart + seconds * 1000);
}

function testDatabaseName(): string {
  return `runtime_controller_delivery_test_${Date.now()}_${randomUUID().replace(/-/g, "")}`;
}

describe.sequential("runtime delivery outbox", () => {
  let adminPool: Pool;
  let pool: Pool;
  let dbName: string;

  beforeAll(async () => {
    dbName = testDatabaseName();
    const config = testDatabaseConfig();
    adminPool = new Pool({ ...config, database: "postgres" });
    await adminPool.query(`CREATE DATABASE ${dbName}`);
    pool = new Pool({ ...config, database: dbName });
    await applyMigrations(pool);
  }, 20_000);

  afterAll(async () => {
    await pool?.end();
    await adminPool?.query(`DROP DATABASE IF EXISTS ${dbName}`);
    await adminPool?.end();
  }, 20_000);

  it("scopes claims to an exact UUID without claiming unrelated backlog", async () => {
    const owner = `selector-${randomUUID()}`;
    const create = (priority: number) => appendDelivery(pool, {
      resultOwner: owner, resultRoute: owner, deliveryProvider: "telegram",
      deliveryKind: "text_message", deliveryKey: randomUUID(), priority, payloadJson: { text: "fixture" },
    });
    const backlog = await create(100);
    const canary = await create(1);
    const request = { claimOwner: owner, resultOwner: owner, deliveryProvider: "telegram" };
    await expect(claimDelivery(pool, { ...request, deliveryId: "" })).rejects.toThrow(/UUID/);
    expect(await claimDelivery(pool, { ...request, deliveryId: randomUUID() })).toBeNull();
    expect(await claimDelivery(pool, { ...request, deliveryId: canary.delivery.deliveryId, resultOwner: "wrong-owner" })).toBeNull();
    const selected = await claimDelivery(pool, { ...request, deliveryId: canary.delivery.deliveryId });
    expect(selected?.delivery.deliveryId).toBe(canary.delivery.deliveryId);
    const untouched = await pool.query("SELECT status, attempt_count FROM runtime.delivery_outbox WHERE delivery_id = $1", [backlog.delivery.deliveryId]);
    expect(untouched.rows[0]).toEqual({ status: "pending", attempt_count: 0 });
    await finalizeDeliverySent(pool, {
      deliveryId: canary.delivery.deliveryId, claimToken: selected!.delivery.claimToken!,
      providerReceiptJson: { messageId: 1 },
    });
    await cancelDelivery(pool, { deliveryId: backlog.delivery.deliveryId, reason: "fixture cleanup" });
  });

  it("appends idempotently by owner and delivery key", async () => {
    const first = await appendDelivery(pool, {
      resultOwner: "owner-delivery-idem",
      resultRoute: "owner-delivery-idem.route",
      deliveryProvider: "telegram",
      deliveryKind: "text_message",
      deliveryKey: "delivery-idem-1",
      payloadJson: { text: "hello" },
    });

    expect(first.action).toBe("created");
    expect(first.delivery.status).toBe("pending");

    const duplicate = await appendDelivery(pool, {
      resultOwner: "owner-delivery-idem",
      resultRoute: "owner-delivery-idem.route",
      deliveryProvider: "telegram",
      deliveryKind: "text_message",
      deliveryKey: "delivery-idem-1",
      payloadJson: { text: "different payload should not rewrite" },
    });

    expect(duplicate.action).toBe("existing");
    expect(duplicate.delivery.deliveryId).toBe(first.delivery.deliveryId);
    expect(duplicate.delivery.payloadJson.text).toBe("hello");
  }, 20_000);

  it("claims only runnable deliveries and fences heartbeat/finalize by claim token", async () => {
    const now = at(0);
    const future = await appendDelivery(pool, {
      resultOwner: "owner-delivery-claim",
      resultRoute: "owner-delivery-claim.route",
      deliveryProvider: "telegram-claim",
      deliveryKind: "text_message",
      deliveryKey: "delivery-future",
      priority: 100,
      runnableAt: at(3600).toISOString(),
      payloadJson: { text: "future" },
    });
    const ready = await appendDelivery(pool, {
      resultOwner: "owner-delivery-claim",
      resultRoute: "owner-delivery-claim.route",
      deliveryProvider: "telegram-claim",
      deliveryKind: "text_message",
      deliveryKey: "delivery-ready",
      priority: 10,
      payloadJson: { text: "ready" },
      now,
    });

    const claimed = await claimDelivery(pool, {
      claimOwner: "telegram-worker-a",
      deliveryProvider: "telegram-claim",
      leaseDurationSec: 60,
      now,
    });
    expect(claimed?.delivery.deliveryId).toBe(ready.delivery.deliveryId);
    expect(claimed?.delivery.deliveryId).not.toBe(future.delivery.deliveryId);
    expect(claimed?.delivery.attemptCount).toBe(1);

    const claimToken = claimed?.delivery.claimToken;
    if (!claimToken) throw new Error("expected claim token");

    const heartbeated = await heartbeatDelivery(pool, {
      deliveryId: claimed.delivery.deliveryId,
      claimToken,
      leaseDurationSec: 120,
      now: at(30),
    });
    expect(heartbeated.delivery.claimExpiresAt).toBe(at(150).toISOString());

    await expect(finalizeDeliverySent(pool, {
      deliveryId: claimed.delivery.deliveryId,
      claimToken: randomUUID(),
      providerReceiptJson: { messageId: 1 },
    })).rejects.toThrow(/stale claim token/);

    const sent = await finalizeDeliverySent(pool, {
      deliveryId: claimed.delivery.deliveryId,
      claimToken,
      providerReceiptJson: { messageId: 12345 },
      now: at(60),
    });
    expect(sent.action).toBe("sent");
    expect(sent.delivery.status).toBe("sent");
    expect(sent.delivery.providerReceiptJson?.messageId).toBe(12345);
  }, 20_000);

  it("retries with backoff and dead-letters when delivery attempts are exhausted", async () => {
    const now = at(600);
    const appended = await appendDelivery(pool, {
      resultOwner: "owner-delivery-retry",
      resultRoute: "owner-delivery-retry.route",
      deliveryProvider: "telegram-retry",
      deliveryKind: "text_message",
      deliveryKey: "delivery-retry-1",
      maxAttempts: 2,
      payloadJson: { text: "retry me" },
      now,
    });
    expect(appended.action).toBe("created");

    const firstClaim = await claimDelivery(pool, {
      claimOwner: "telegram-worker-retry",
      deliveryProvider: "telegram-retry",
      now,
    });
    if (!firstClaim?.delivery.claimToken) throw new Error("expected first claim");

    const retryable = await finalizeDeliveryRetryable(pool, {
      deliveryId: firstClaim.delivery.deliveryId,
      claimToken: firstClaim.delivery.claimToken,
      errorClass: "telegram_429",
      errorText: "rate limited",
      backoffSec: 60,
      now,
    });
    expect(retryable.action).toBe("retryable");
    expect(retryable.delivery.status).toBe("retryable");
    expect(retryable.delivery.runnableAt).toBe(at(660).toISOString());

    const blocked = await claimDelivery(pool, {
      claimOwner: "telegram-worker-retry",
      deliveryProvider: "telegram-retry",
      now: at(630),
    });
    expect(blocked).toBeNull();

    const secondClaim = await claimDelivery(pool, {
      claimOwner: "telegram-worker-retry",
      deliveryProvider: "telegram-retry",
      now: at(661),
    });
    expect(secondClaim?.delivery.deliveryId).toBe(appended.delivery.deliveryId);
    expect(secondClaim?.delivery.attemptCount).toBe(2);
    if (!secondClaim?.delivery.claimToken) throw new Error("expected second claim");

    const dead = await finalizeDeliveryRetryable(pool, {
      deliveryId: secondClaim.delivery.deliveryId,
      claimToken: secondClaim.delivery.claimToken,
      errorClass: "telegram_429",
      errorText: "still rate limited",
      now: at(670),
    });
    expect(dead.action).toBe("dead");
    expect(dead.delivery.status).toBe("dead");
    expect(dead.delivery.deadAt).toBe(at(670).toISOString());
  }, 20_000);

  it("reconciles expired claims without rerunning execution", async () => {
    const now = at(1200);
    const appended = await appendDelivery(pool, {
      resultOwner: "owner-delivery-reconcile",
      resultRoute: "owner-delivery-reconcile.route",
      deliveryProvider: "telegram-reconcile",
      deliveryKind: "text_message",
      deliveryKey: "delivery-reconcile-1",
      maxAttempts: 2,
      payloadJson: { text: "expire me" },
      now,
    });

    const claimed = await claimDelivery(pool, {
      claimOwner: "telegram-worker-reconcile",
      deliveryProvider: "telegram-reconcile",
      leaseDurationSec: 1,
      now,
    });
    expect(claimed?.delivery.deliveryId).toBe(appended.delivery.deliveryId);

    const retried = await reconcileExpiredDeliveryClaims(pool, {
      limit: 10,
      baseBackoffSec: 5,
      maxBackoffSec: 5,
      now: at(1202),
    });
    expect(retried).toHaveLength(1);
    expect(retried[0]?.action).toBe("retried");
    expect(retried[0]?.delivery.status).toBe("retryable");
    expect(retried[0]?.delivery.runnableAt).toBe(at(1207).toISOString());
  }, 20_000);

  it("supports terminal error and cancellation paths", async () => {
    const terminal = await appendDelivery(pool, {
      resultOwner: "owner-delivery-terminal",
      resultRoute: "owner-delivery-terminal.route",
      deliveryProvider: "telegram-terminal",
      deliveryKind: "text_message",
      deliveryKey: "delivery-terminal-1",
      payloadJson: { text: "terminal" },
    });
    const terminalClaim = await claimDelivery(pool, {
      claimOwner: "telegram-worker-terminal",
      deliveryProvider: "telegram-terminal",
    });
    if (!terminalClaim?.delivery.claimToken) throw new Error("expected terminal claim");
    const dead = await finalizeDeliveryDead(pool, {
      deliveryId: terminalClaim.delivery.deliveryId,
      claimToken: terminalClaim.delivery.claimToken,
      errorClass: "telegram_forbidden",
      errorText: "bot cannot write to chat",
    });
    expect(dead.delivery.deliveryId).toBe(terminal.delivery.deliveryId);
    expect(dead.delivery.status).toBe("dead");

    const cancellable = await appendDelivery(pool, {
      resultOwner: "owner-delivery-cancel",
      resultRoute: "owner-delivery-cancel.route",
      deliveryProvider: "telegram-cancel",
      deliveryKind: "text_message",
      deliveryKey: "delivery-cancel-1",
      payloadJson: { text: "cancel" },
    });
    const cancelled = await cancelDelivery(pool, {
      deliveryId: cancellable.delivery.deliveryId,
      reason: "operator cancelled duplicate send",
    });
    expect(cancelled.status).toBe("cancelled");
    expect(cancelled.cancelledAt).not.toBeNull();
  }, 20_000);
});
