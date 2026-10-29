import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { URL } from "node:url";
import { createPool } from "../db/client.js";
import { loadServerConfigFromEnv } from "../config/env.js";
import { badRequest } from "../errors/controller-error.js";
import { getRuntimeHealth } from "../runtime/health.js";
import { getHealthSummary, summaryNames, summaryWindow } from "../runtime/health-summary.js";
import { claimRuntimeJob } from "../runtime/claim.js";
import { abandonAttempt } from "../runtime/abandon.js";
import { finalizeAttempt } from "../runtime/finalize.js";
import { heartbeatAttempt, markAttemptRunning } from "../runtime/heartbeat.js";
import { cancelJobBeforeStart, reconcileExpiredDeadlines, reconcileExpiredLeases } from "../runtime/reconcile.js";
import { claimResultsForOwner, consumeResult, listReadyResultsForOwner } from "../runtime/results.js";
import { getRuntimeArtifact, registerArtifact } from "../runtime/artifacts.js";
import { getRuntimeJob, listRuntimeJobs } from "../runtime/jobs.js";
import { appendProgress, getProgress } from "../runtime/progress.js";
import { DELIVERY_STATUSES, JOB_STATUSES, SOURCE_RECEIPT_STATUSES, type DeliveryStatus, type QueueClass, type SourceReceiptStatus, type WorkerState, type WorkerVenue } from "../types/runtime.js";
import { admitAndSubmitRuntimeJob } from "../runtime/submit.js";
import { appendDelivery, cancelDelivery, claimDelivery, finalizeDelivery, getDelivery, heartbeatDelivery, listDeliveries, reconcileExpiredDeliveryClaims } from "../runtime/delivery.js";
import { admitSourceReceipt, cancelSourceReceipt, getSourceReceipt, listSourceReceipts, markSourceReceiptSubmitted } from "../runtime/source-receipts.js";
import { DEFAULT_RUNTIME_POLICY, resolveWorkloadPolicy } from "../policy/default-policy.js";
import { loadPolicyMapFromFile } from "../policy/load-policy.js";
import { drainWorker, getWorker, heartbeatWorker, listWorkers, normalizeWorkerState, normalizeWorkerVenue, registerWorker, resumeWorker } from "../runtime/workers.js";
import type { RuntimePolicyMap } from "../types/policy.js";
import { toHttpError, toHttpErrorBody } from "./http-errors.js";

function writeJson(res: ServerResponse, statusCode: number, body: unknown): void {
  res.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body, null, 2));
}

function logEvent(event: string, details: Record<string, unknown>): void {
  console.log(JSON.stringify({ ts: new Date().toISOString(), event, ...details }));
}

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    bytes += buffer.length;
    const limit = req.url?.match(/^\/v1\/jobs\/[^/]+\/progress(?:\?|$)/) ? 8192 : 16 * 1024 * 1024;
    if (bytes > limit) throw badRequest("body_too_large", "request body exceeds limit");
    chunks.push(buffer);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    throw badRequest("invalid_json_body", "request body must be valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw badRequest("invalid_json_body", "expected JSON object body");
  }
  return parsed as Record<string, unknown>;
}

function isAuthorized(req: IncomingMessage, bearerToken: string | null): boolean {
  if (!bearerToken) return true;
  const header = req.headers.authorization ?? "";
  return header === `Bearer ${bearerToken}`;
}

function parseStatusFilter(raw: string | null): string[] | undefined {
  if (!raw?.trim()) return undefined;
  const values = raw.split(",").map((part) => part.trim()).filter(Boolean);
  if (values.length === 0) return undefined;
  const validStatuses = new Set<string>(JOB_STATUSES);
  for (const value of values) {
    if (!validStatuses.has(value)) {
      throw badRequest("invalid_status_filter", `invalid status filter: ${value}`, { value });
    }
  }
  return Array.from(new Set(values));
}

function parseWorkerStateFilter(raw: string | null): WorkerState | undefined {
  if (!raw?.trim()) return undefined;
  return normalizeWorkerState(raw, "state");
}

function parseWorkerVenueFilter(raw: string | null): WorkerVenue | undefined {
  if (!raw?.trim()) return undefined;
  return normalizeWorkerVenue(raw);
}

function parseDeliveryStatusFilter(raw: string | null): DeliveryStatus[] | undefined {
  if (!raw?.trim()) return undefined;
  const values = raw.split(",").map((part) => part.trim()).filter(Boolean);
  if (values.length === 0) return undefined;
  const validStatuses = new Set<string>(DELIVERY_STATUSES);
  for (const value of values) {
    if (!validStatuses.has(value)) {
      throw badRequest("invalid_delivery_status_filter", `invalid delivery status filter: ${value}`, { value });
    }
  }
  return Array.from(new Set(values)) as DeliveryStatus[];
}

function parseSourceReceiptStatusFilter(raw: string | null): SourceReceiptStatus[] | undefined {
  if (!raw?.trim()) return undefined;
  const values = raw.split(",").map((part) => part.trim()).filter(Boolean);
  if (values.length === 0) return undefined;
  const validStatuses = new Set<string>(SOURCE_RECEIPT_STATUSES);
  for (const value of values) {
    if (!validStatuses.has(value)) {
      throw badRequest("invalid_source_receipt_status_filter", `invalid source receipt status filter: ${value}`, { value });
    }
  }
  return Array.from(new Set(values)) as SourceReceiptStatus[];
}

async function resolvePolicyMap(policyFile: string | null): Promise<RuntimePolicyMap> {
  if (!policyFile) return DEFAULT_RUNTIME_POLICY;
  return loadPolicyMapFromFile(policyFile);
}

async function main(): Promise<void> {
  const config = loadServerConfigFromEnv();
  const pool = createPool(config.database);
  const policyMap = await resolvePolicyMap(config.policyFile);

  const server = createServer(async (req, res) => {
    const method = req.method ?? "GET";
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? `${config.bindHost}:${config.port}`}`);

    try {
      if (url.pathname === "/healthz" && method === "GET") {
        const health = await getRuntimeHealth(pool, {
          workerHeartbeatTtlSec: config.workerHeartbeatTtlSec,
        });
        writeJson(res, health.ok ? 200 : 503, health);
        return;
      }

      if (!isAuthorized(req, config.bearerToken)) {
        writeJson(res, 401, { error: "unauthorized", code: "unauthorized" });
        return;
      }

      if (url.pathname === "/v1/health-summary" && method === "GET") {
        let workloads: string[], owners: string[], windowSec: number;
        try {
          workloads = summaryNames(url.searchParams.get("workloadTypes"));
          owners = summaryNames(url.searchParams.get("resultOwners"));
          windowSec = summaryWindow(url.searchParams.get("windowSec"));
        } catch {
          throw badRequest("invalid_health_summary_query", "Invalid health-summary query");
        }
        writeJson(res, 200, await getHealthSummary(pool, workloads, owners, windowSec));
        return;
      }

      if (url.pathname === "/v1/results" && method === "GET") {
        const resultOwner = url.searchParams.get("resultOwner");
        if (!resultOwner) {
          writeJson(res, 400, { error: "resultOwner query param is required", code: "invalid_query" });
          return;
        }
        const limit = url.searchParams.get("limit");
        const results = await listReadyResultsForOwner(pool, {
          resultOwner,
          limit: limit ? Number(limit) : undefined,
        });
        writeJson(res, 200, results);
        return;
      }

      if (url.pathname === "/v1/deliveries" && method === "GET") {
        const deliveries = await listDeliveries(pool, {
          resultOwner: url.searchParams.get("resultOwner") ?? undefined,
          deliveryProvider: url.searchParams.get("deliveryProvider") ?? undefined,
          deliveryKind: url.searchParams.get("deliveryKind") ?? undefined,
          status: parseDeliveryStatusFilter(url.searchParams.get("status")),
          limit: url.searchParams.get("limit") ? Number(url.searchParams.get("limit")) : undefined,
        });
        writeJson(res, 200, deliveries);
        return;
      }

      if (url.pathname === "/v1/source-receipts" && method === "GET") {
        const receipts = await listSourceReceipts(pool, {
          sourceSystem: url.searchParams.get("sourceSystem") ?? undefined,
          sourceKey: url.searchParams.get("sourceKey") ?? undefined,
          producerSystem: url.searchParams.get("producerSystem") ?? undefined,
          workloadType: url.searchParams.get("workloadType") ?? undefined,
          routeKind: url.searchParams.get("routeKind") ?? undefined,
          routeId: url.searchParams.get("routeId") ?? undefined,
          channelId: url.searchParams.get("channelId") ?? undefined,
          status: parseSourceReceiptStatusFilter(url.searchParams.get("status")),
          limit: url.searchParams.get("limit") ? Number(url.searchParams.get("limit")) : undefined,
        });
        writeJson(res, 200, receipts);
        return;
      }

      if (url.pathname.startsWith("/v1/source-receipts/") && method === "GET") {
        const receiptId = url.pathname.slice("/v1/source-receipts/".length).trim();
        if (!receiptId) {
          writeJson(res, 400, { error: "receiptId path param is required", code: "invalid_path" });
          return;
        }
        const receipt = await getSourceReceipt(pool, receiptId);
        if (!receipt) {
          writeJson(res, 404, { error: "source receipt not found", code: "source_receipt_not_found" });
          return;
        }
        writeJson(res, 200, { receipt });
        return;
      }

      if (url.pathname.startsWith("/v1/deliveries/") && method === "GET") {
        const deliveryId = url.pathname.slice("/v1/deliveries/".length).trim();
        if (!deliveryId) {
          writeJson(res, 400, { error: "deliveryId path param is required", code: "invalid_path" });
          return;
        }
        const delivery = await getDelivery(pool, deliveryId);
        if (!delivery) {
          writeJson(res, 404, { error: "delivery not found", code: "delivery_not_found" });
          return;
        }
        writeJson(res, 200, { delivery });
        return;
      }

      if (url.pathname === "/v1/jobs" && method === "GET") {
        const statusFilter = parseStatusFilter(url.searchParams.get("status"));
        const jobs = await listRuntimeJobs(pool, {
          producerSystem: url.searchParams.get("producerSystem") ?? undefined,
          producerRef: url.searchParams.get("producerRef") ?? undefined,
          producerRefPrefix: url.searchParams.get("producerRefPrefix") ?? undefined,
          resultOwner: url.searchParams.get("resultOwner") ?? undefined,
          workloadType: url.searchParams.get("workloadType") ?? undefined,
          status: statusFilter as never,
          limit: url.searchParams.get("limit") ? Number(url.searchParams.get("limit")) : undefined,
        });
        writeJson(res, 200, jobs);
        return;
      }

      const progressPath = url.pathname.match(/^\/v1\/jobs\/([^/]+)\/progress$/);
      if (progressPath && method === "GET") {
        writeJson(res, 200, await getProgress(pool, progressPath[1]!, Number(url.searchParams.get("after") ?? 0)));
        return;
      }
      if (url.pathname.startsWith("/v1/jobs/") && method === "GET") {
        const jobId = url.pathname.slice("/v1/jobs/".length).trim();
        if (!jobId) {
          writeJson(res, 400, { error: "jobId path param is required", code: "invalid_path" });
          return;
        }
        const job = await getRuntimeJob(pool, jobId);
        if (!job) {
          writeJson(res, 404, { error: "job not found", code: "job_not_found" });
          return;
        }
        writeJson(res, 200, { job });
        return;
      }

      if (url.pathname === "/v1/workers" && method === "GET") {
        const workers = await listWorkers(pool, {
          state: parseWorkerStateFilter(url.searchParams.get("state")),
          venue: parseWorkerVenueFilter(url.searchParams.get("venue")),
          limit: url.searchParams.get("limit") ? Number(url.searchParams.get("limit")) : undefined,
          heartbeatTtlSec: config.workerHeartbeatTtlSec,
        });
        writeJson(res, 200, workers);
        return;
      }

      if (url.pathname.startsWith("/v1/workers/") && method === "GET") {
        const workerId = decodeURIComponent(url.pathname.slice("/v1/workers/".length).trim());
        if (!workerId) {
          writeJson(res, 400, { error: "workerId path param is required", code: "invalid_path" });
          return;
        }
        const worker = await getWorker(pool, workerId, {
          heartbeatTtlSec: config.workerHeartbeatTtlSec,
        });
        if (!worker) {
          writeJson(res, 404, { error: "worker not found", code: "worker_not_found" });
          return;
        }
        writeJson(res, 200, { worker });
        return;
      }

      if (url.pathname.startsWith("/v1/artifacts/") && method === "GET") {
        const artifactId = url.pathname.slice("/v1/artifacts/".length).trim();
        if (!artifactId) {
          writeJson(res, 400, { error: "artifactId path param is required", code: "invalid_path" });
          return;
        }
        const artifact = await getRuntimeArtifact(pool, artifactId);
        if (!artifact) {
          writeJson(res, 404, { error: "artifact not found", code: "artifact_not_found" });
          return;
        }
        writeJson(res, 200, { artifact });
        return;
      }

      if (method !== "POST") {
        writeJson(res, 404, { error: "not found", code: "not_found" });
        return;
      }

      const body = await readJson(req);
      if (Object.hasOwn(body, "now")) {
        throw badRequest("invalid_authority_clock", "now is an internal fixture clock, not HTTP authority");
      }
      if (progressPath) {
        if (body.jobId !== progressPath[1]) throw badRequest("invalid_progress", "path and job ID differ");
        writeJson(res, 200, await appendProgress(pool, body));
        return;
      }

      if (url.pathname === "/v1/submit") {
        if (typeof body.workloadType !== "string") {
          writeJson(res, 400, { error: "workloadType is required", code: "invalid_submission_request" });
          return;
        }
        const policy = resolveWorkloadPolicy(
          policyMap,
          body.workloadType,
          body.queueClass === "foreground" || body.queueClass === "background" || body.queueClass === "ops"
            ? body.queueClass
            : undefined,
          typeof body.executionProfile === "string" ? body.executionProfile : undefined,
        );
        if (!policy) {
          writeJson(res, 400, {
            error: `no policy found for workloadType=${body.workloadType}`,
            code: "policy_not_found",
          });
          return;
        }
        const result = await admitAndSubmitRuntimeJob(pool, body as never, policy);
        writeJson(res, 200, result);
        return;
      }

      if (url.pathname === "/v1/claim") {
        writeJson(res, 200, await claimRuntimeJob(pool, body as never, {
          workerHeartbeatTtlSec: config.workerHeartbeatTtlSec,
        }));
        return;
      }

      if (url.pathname === "/v1/workers/register") {
        writeJson(res, 200, await registerWorker(pool, {
          workerId: typeof body.workerId === "string" ? body.workerId : "",
          workerVenue: typeof body.workerVenue === "string" ? body.workerVenue as WorkerVenue : undefined,
          supportedExecutionProfiles: Array.isArray(body.supportedExecutionProfiles) ? body.supportedExecutionProfiles as string[] : [],
          workloadTypes: Array.isArray(body.workloadTypes) ? body.workloadTypes as string[] : undefined,
          producerSystems: Array.isArray(body.producerSystems) ? body.producerSystems as string[] : undefined,
          allowedQueueClasses: Array.isArray(body.allowedQueueClasses) ? body.allowedQueueClasses as QueueClass[] : undefined,
          queuePreference: Array.isArray(body.queuePreference) ? body.queuePreference as QueueClass[] : undefined,
          capacity: typeof body.capacity === "number" ? body.capacity : undefined,
          metadataJson: body.metadataJson && typeof body.metadataJson === "object" && !Array.isArray(body.metadataJson)
            ? body.metadataJson as Record<string, unknown>
            : null,
        }));
        return;
      }

      if (url.pathname === "/v1/workers/heartbeat") {
        writeJson(res, 200, await heartbeatWorker(pool, {
          workerId: typeof body.workerId === "string" ? body.workerId : "",
          metadataJson: body.metadataJson && typeof body.metadataJson === "object" && !Array.isArray(body.metadataJson)
            ? body.metadataJson as Record<string, unknown>
            : null,
        }));
        return;
      }

      if (url.pathname === "/v1/workers/drain") {
        writeJson(res, 200, await drainWorker(pool, {
          workerId: typeof body.workerId === "string" ? body.workerId : "",
          reason: typeof body.reason === "string" ? body.reason : null,
          gracePeriodSec: typeof body.gracePeriodSec === "number" ? body.gracePeriodSec : undefined,
          requestedBy: typeof body.requestedBy === "string" ? body.requestedBy : null,
        }));
        return;
      }

      if (url.pathname === "/v1/workers/resume") {
        writeJson(res, 200, {
          worker: await resumeWorker(pool, {
            workerId: typeof body.workerId === "string" ? body.workerId : "",
          }),
        });
        return;
      }

      if (url.pathname === "/v1/attempts/abandon") {
        writeJson(res, 200, await abandonAttempt(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/artifacts/register") {
        writeJson(res, 200, await registerArtifact(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/running") {
        writeJson(res, 200, await markAttemptRunning(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/heartbeat") {
        writeJson(res, 200, await heartbeatAttempt(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/finalize") {
        writeJson(res, 200, await finalizeAttempt(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/reconcile") {
        const lease = await reconcileExpiredLeases(pool, {
          limit: typeof body.leaseLimit === "number" ? body.leaseLimit : undefined,
          baseBackoffSec: typeof body.baseBackoffSec === "number" ? body.baseBackoffSec : undefined,
          maxBackoffSec: typeof body.maxBackoffSec === "number" ? body.maxBackoffSec : undefined,
        });
        const deadline = await reconcileExpiredDeadlines(pool, {
          limit: typeof body.deadlineLimit === "number" ? body.deadlineLimit : undefined,
        });
        const delivery = await reconcileExpiredDeliveryClaims(pool, {
          limit: typeof body.deliveryLimit === "number" ? body.deliveryLimit : undefined,
          baseBackoffSec: typeof body.baseBackoffSec === "number" ? body.baseBackoffSec : undefined,
          maxBackoffSec: typeof body.maxBackoffSec === "number" ? body.maxBackoffSec : undefined,
        });
        writeJson(res, 200, { lease, deadline, delivery });
        return;
      }

      if (url.pathname === "/v1/cancel") {
        writeJson(res, 200, await cancelJobBeforeStart(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/results/claim") {
        writeJson(res, 200, await claimResultsForOwner(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/results/consume") {
        writeJson(res, 200, await consumeResult(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/append") {
        writeJson(res, 200, await appendDelivery(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/claim") {
        writeJson(res, 200, await claimDelivery(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/heartbeat") {
        writeJson(res, 200, await heartbeatDelivery(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/finalize") {
        writeJson(res, 200, await finalizeDelivery(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/cancel") {
        writeJson(res, 200, await cancelDelivery(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/deliveries/reconcile") {
        writeJson(res, 200, await reconcileExpiredDeliveryClaims(pool, {
          limit: typeof body.limit === "number" ? body.limit : undefined,
          baseBackoffSec: typeof body.baseBackoffSec === "number" ? body.baseBackoffSec : undefined,
          maxBackoffSec: typeof body.maxBackoffSec === "number" ? body.maxBackoffSec : undefined,
        }));
        return;
      }

      if (url.pathname === "/v1/source-receipts/admit") {
        writeJson(res, 200, await admitSourceReceipt(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/source-receipts/submit") {
        writeJson(res, 200, await markSourceReceiptSubmitted(pool, body as never));
        return;
      }

      if (url.pathname === "/v1/source-receipts/cancel") {
        writeJson(res, 200, await cancelSourceReceipt(pool, body as never));
        return;
      }

      writeJson(res, 404, { error: "not found", code: "not_found" });
    } catch (error) {
      const normalized = toHttpError(error);
      logEvent("http_api_error", {
        method,
        path: url.pathname,
        statusCode: normalized.statusCode,
        code: normalized.code,
        message: normalized.message,
      });
      writeJson(res, normalized.statusCode, toHttpErrorBody(normalized));
    }
  });

  server.listen(config.port, config.bindHost, () => {
    logEvent("http_api_started", {
      bindHost: config.bindHost,
      port: config.port,
      hasBearerToken: Boolean(config.bearerToken),
      policyFile: config.policyFile,
    });
  });

  const shutdown = async (signal: string) => {
    logEvent("http_api_shutdown", { signal });
    server.close(async () => {
      await pool.end();
      process.exit(0);
    });
  };

  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
