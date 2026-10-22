import { setTimeout as sleep } from "node:timers/promises";
import { createPool } from "../db/client.js";
import { loadReconcilerConfigFromEnv } from "../config/env.js";
import { reconcileExpiredDeadlines, reconcileExpiredLeases } from "../runtime/reconcile.js";
import { reconcileExpiredDeliveryClaims } from "../runtime/delivery.js";

function logEvent(event: string, details: Record<string, unknown>): void {
  console.log(JSON.stringify({ ts: new Date().toISOString(), event, ...details }));
}

async function runPass() {
  const config = loadReconcilerConfigFromEnv();
  const pool = createPool(config.database);
  try {
    const lease = await reconcileExpiredLeases(pool, {
      limit: config.leaseLimit,
      baseBackoffSec: config.baseBackoffSec,
      maxBackoffSec: config.maxBackoffSec,
    });
    const deadline = await reconcileExpiredDeadlines(pool, {
      limit: config.deadlineLimit,
    });
    const delivery = await reconcileExpiredDeliveryClaims(pool, {
      limit: config.deliveryLimit,
      baseBackoffSec: config.baseBackoffSec,
      maxBackoffSec: config.maxBackoffSec,
    });

    const summary = {
      leaseRetried: lease.filter((item) => item.action === "retried").length,
      leaseDead: lease.filter((item) => item.action === "dead").length,
      deadlineDead: deadline.length,
      deliveryRetried: delivery.filter((item) => item.action === "retried").length,
      deliveryDead: delivery.filter((item) => item.action === "dead").length,
    };
    logEvent("reconcile_pass_complete", summary);
    return summary;
  } finally {
    await pool.end();
  }
}

async function main(): Promise<void> {
  const once = process.argv.includes("--once");
  const config = loadReconcilerConfigFromEnv();
  let running = true;

  const stop = (signal: string) => {
    logEvent("reconciler_stopping", { signal });
    running = false;
  };
  process.on("SIGINT", () => stop("SIGINT"));
  process.on("SIGTERM", () => stop("SIGTERM"));

  if (once) {
    await runPass();
    return;
  }

  logEvent("reconciler_started", {
    intervalSec: config.intervalSec,
    leaseLimit: config.leaseLimit,
    deadlineLimit: config.deadlineLimit,
    deliveryLimit: config.deliveryLimit,
    baseBackoffSec: config.baseBackoffSec,
    maxBackoffSec: config.maxBackoffSec,
  });

  while (running) {
    try {
      await runPass();
    } catch (error) {
      logEvent("reconcile_pass_error", {
        message: error instanceof Error ? error.message : String(error),
      });
    }

    if (!running) break;
    await sleep(config.intervalSec * 1000);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
