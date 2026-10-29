#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { loadServerConfigFromEnv } from "./config/env.js";
import { createPool, localPeerDatabaseConfig } from "./db/client.js";
import { applyMigrations, defaultMigrationsDir } from "./db/migrate.js";
import { resolve } from "node:path";
import { claimRuntimeJob } from "./runtime/claim.js";
import { finalizeAttempt } from "./runtime/finalize.js";
import { heartbeatAttempt, markAttemptRunning } from "./runtime/heartbeat.js";
import { getRuntimeHealth } from "./runtime/health.js";
import { cancelJobBeforeStart, reconcileExpiredDeadlines, reconcileExpiredLeases } from "./runtime/reconcile.js";
import { claimResultsForOwner, consumeResult, listReadyResultsForOwner } from "./runtime/results.js";
import { getRuntimeArtifact, registerArtifact } from "./runtime/artifacts.js";
import { appendDelivery, cancelDelivery, claimDelivery, finalizeDelivery, getDelivery, heartbeatDelivery, listDeliveries, reconcileExpiredDeliveryClaims } from "./runtime/delivery.js";
import { admitSourceReceipt, cancelSourceReceipt, getSourceReceipt, listSourceReceipts, markSourceReceiptSubmitted } from "./runtime/source-receipts.js";
import { admitAndSubmitRuntimeJob } from "./runtime/submit.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "./policy/default-policy.js";
import { loadPolicyMapFromFile } from "./policy/load-policy.js";
import type { RuntimePolicyMap } from "./types/policy.js";

function getArgValue(flag: string): string | null {
  const argv = process.argv.slice(2);
  const index = argv.indexOf(flag);
  if (index === -1) return null;
  return argv[index + 1] ?? null;
}

async function readJsonFromStdin(): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)));
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  return text ? JSON.parse(text) : null;
}

async function loadPolicyMap(): Promise<RuntimePolicyMap> {
  const policyFile = getArgValue("--policy-file");
  if (!policyFile) return DEFAULT_RUNTIME_POLICY;
  return loadPolicyMapFromFile(policyFile);
}

async function main(): Promise<void> {
  const [command] = process.argv.slice(2).filter((arg) => !arg.startsWith("--") && !arg.endsWith(".yaml") && !arg.endsWith(".yml"));
  if (!command) {
    throw new Error("command is required");
  }

  if (command === "contract") {
    const version = /^runtime\.health-summary\/([12])$/.exec(process.argv[3] ?? "")?.[1];
    if (!version) throw new Error("unknown contract");
    console.log(await readFile(resolve(defaultMigrationsDir(), `../../contracts/runtime.health-summary-${version}.json`), "utf8"));
    return;
  }

  const databaseUrl = getArgValue("--database-url") ?? process.env.DATABASE_URL ?? null;
  const databaseName = getArgValue("--database") ?? process.env.PGDATABASE ?? "postgres";
  const pool = databaseUrl
    ? createPool({ connectionString: databaseUrl })
    : createPool(localPeerDatabaseConfig(databaseName));

  try {
    if (command === "migrate") {
      const migrationsDir = getArgValue("--migrations-dir") ?? undefined;
      const applied = await applyMigrations(pool, migrationsDir);
      console.log(JSON.stringify({ ok: true, applied }, null, 2));
      return;
    }

    if (command === "health") {
      console.log(JSON.stringify(await getRuntimeHealth(pool, {
        workerHeartbeatTtlSec: loadServerConfigFromEnv().workerHeartbeatTtlSec,
      }), null, 2));
      return;
    }

    const payload = await readJsonFromStdin();
    if (payload == null || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("expected a JSON object on stdin");
    }

    if (command === "submit") {
      const policyMap = await loadPolicyMap();
      const request = payload as Record<string, unknown>;
      if (typeof request.workloadType !== "string") {
        throw new Error("submit payload requires workloadType");
      }
      const policy = resolveWorkloadPolicy(
        policyMap,
        request.workloadType,
        request.queueClass === "foreground" || request.queueClass === "background" || request.queueClass === "ops"
          ? request.queueClass
          : undefined,
        typeof request.executionProfile === "string" ? request.executionProfile : undefined,
      );
      if (!policy) {
        throw new Error(`no policy found for workloadType=${request.workloadType}`);
      }
      const result = await admitAndSubmitRuntimeJob(pool, request as never, policy);
      console.log(JSON.stringify(result, null, 2));
      return;
    }

    if (command === "claim") {
      console.log(JSON.stringify(await claimRuntimeJob(pool, payload as never), null, 2));
      return;
    }

    if (command === "running") {
      console.log(JSON.stringify(await markAttemptRunning(pool, payload as never), null, 2));
      return;
    }

    if (command === "heartbeat") {
      console.log(JSON.stringify(await heartbeatAttempt(pool, payload as never), null, 2));
      return;
    }

    if (command === "finalize") {
      console.log(JSON.stringify(await finalizeAttempt(pool, payload as never), null, 2));
      return;
    }

    if (command === "reconcile") {
      const request = payload as Record<string, unknown>;
      const lease = await reconcileExpiredLeases(pool, {
        limit: typeof request.leaseLimit === "number" ? request.leaseLimit : undefined,
        baseBackoffSec: typeof request.baseBackoffSec === "number" ? request.baseBackoffSec : undefined,
        maxBackoffSec: typeof request.maxBackoffSec === "number" ? request.maxBackoffSec : undefined,
      });
      const deadline = await reconcileExpiredDeadlines(pool, {
        limit: typeof request.deadlineLimit === "number" ? request.deadlineLimit : undefined,
      });
      const delivery = await reconcileExpiredDeliveryClaims(pool, {
        limit: typeof request.deliveryLimit === "number" ? request.deliveryLimit : undefined,
        baseBackoffSec: typeof request.baseBackoffSec === "number" ? request.baseBackoffSec : undefined,
        maxBackoffSec: typeof request.maxBackoffSec === "number" ? request.maxBackoffSec : undefined,
      });
      console.log(JSON.stringify({ lease, deadline, delivery }, null, 2));
      return;
    }

    if (command === "cancel") {
      console.log(JSON.stringify(await cancelJobBeforeStart(pool, payload as never), null, 2));
      return;
    }

    if (command === "artifact:register") {
      console.log(JSON.stringify(await registerArtifact(pool, payload as never), null, 2));
      return;
    }

    if (command === "artifact:get") {
      const request = payload as Record<string, unknown>;
      if (typeof request.artifactId !== "string") {
        throw new Error("artifact:get requires artifactId");
      }
      console.log(JSON.stringify(await getRuntimeArtifact(pool, request.artifactId), null, 2));
      return;
    }

    if (command === "results:list") {
      console.log(JSON.stringify(await listReadyResultsForOwner(pool, payload as never), null, 2));
      return;
    }

    if (command === "results:claim") {
      console.log(JSON.stringify(await claimResultsForOwner(pool, payload as never), null, 2));
      return;
    }

    if (command === "results:consume") {
      console.log(JSON.stringify(await consumeResult(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:append") {
      console.log(JSON.stringify(await appendDelivery(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:get") {
      const request = payload as Record<string, unknown>;
      if (typeof request.deliveryId !== "string") {
        throw new Error("delivery:get requires deliveryId");
      }
      console.log(JSON.stringify(await getDelivery(pool, request.deliveryId), null, 2));
      return;
    }

    if (command === "delivery:list") {
      console.log(JSON.stringify(await listDeliveries(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:claim") {
      console.log(JSON.stringify(await claimDelivery(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:heartbeat") {
      console.log(JSON.stringify(await heartbeatDelivery(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:finalize") {
      console.log(JSON.stringify(await finalizeDelivery(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:cancel") {
      console.log(JSON.stringify(await cancelDelivery(pool, payload as never), null, 2));
      return;
    }

    if (command === "delivery:reconcile") {
      const request = payload as Record<string, unknown>;
      console.log(JSON.stringify(await reconcileExpiredDeliveryClaims(pool, {
        limit: typeof request.limit === "number" ? request.limit : undefined,
        baseBackoffSec: typeof request.baseBackoffSec === "number" ? request.baseBackoffSec : undefined,
        maxBackoffSec: typeof request.maxBackoffSec === "number" ? request.maxBackoffSec : undefined,
      }), null, 2));
      return;
    }

    if (command === "source:admit") {
      console.log(JSON.stringify(await admitSourceReceipt(pool, payload as never), null, 2));
      return;
    }

    if (command === "source:submit") {
      console.log(JSON.stringify(await markSourceReceiptSubmitted(pool, payload as never), null, 2));
      return;
    }

    if (command === "source:cancel") {
      console.log(JSON.stringify(await cancelSourceReceipt(pool, payload as never), null, 2));
      return;
    }

    if (command === "source:get") {
      const request = payload as Record<string, unknown>;
      if (typeof request.receiptId !== "string") {
        throw new Error("source:get requires receiptId");
      }
      console.log(JSON.stringify(await getSourceReceipt(pool, request.receiptId), null, 2));
      return;
    }

    if (command === "source:list") {
      console.log(JSON.stringify(await listSourceReceipts(pool, payload as never), null, 2));
      return;
    }

    if (command === "show-migration") {
      const file = getArgValue("--file");
      if (!file) throw new Error("--file is required");
      process.stdout.write(await readFile(file, "utf8"));
      return;
    }

    throw new Error(`unknown command: ${command}`);
  } finally {
    await pool.end();
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
