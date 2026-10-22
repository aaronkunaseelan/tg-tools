import type pg from "pg";
import { mapRuntimeWorkerRow, type RuntimeWorkerRow } from "../db/rows.js";
import type { QueueClass } from "../types/runtime.js";
import {
  DEFAULT_WORKER_HEARTBEAT_TTL_SEC,
  describeWorkerAvailability,
  type RuntimeWorkerAvailability,
} from "./workers.js";

export interface RuntimeHealthWorkerHeartbeat {
  workerId: string;
  state: string;
  capacity: number;
  activeLeaseCount: number;
  availableCapacity: number;
  heartbeatAgeSec: number;
  heartbeatFresh: boolean;
  available: boolean;
}

export interface RuntimeQueueCapacityHealth {
  workloadType: string;
  producerSystem: string;
  executionProfile: string;
  queueClass: string;
  runnablePendingJobs: number;
  oldestPendingJob: {
    jobId: string;
    runnableAt: string;
    ageSec: number;
  };
  freshEligibleWorkerCount: number;
  freshEligibleWorkerCapacity: number;
  degraded: boolean;
  alert: string | null;
}

export interface RuntimeHealthStatus {
  ok: boolean;
  checkedAt: string;
  service: {
    name: string;
    version: string | null;
  };
  database: {
    reachable: boolean;
    currentDatabase?: string;
    currentUser?: string;
    currentTime?: string;
    error?: string;
  };
  runtime?: {
    jobsByStatus: Record<string, number>;
    readyResults: number;
    deliveriesByStatus?: Record<string, number>;
    sourceReceiptsByStatus?: Record<string, number>;
    workerHeartbeatTtlSec?: number;
    workerHeartbeats?: RuntimeHealthWorkerHeartbeat[];
    queueCapacity?: RuntimeQueueCapacityHealth[];
    degraded?: boolean;
    alerts?: string[];
  };
}

export interface RuntimeHealthOptions {
  now?: Date;
  workerHeartbeatTtlSec?: number;
}

interface RunnableQueueRow {
  workload_type: string;
  producer_system: string;
  execution_profile: string;
  queue_class: QueueClass;
  runnable_pending_jobs: string;
  oldest_job_id: string;
  oldest_runnable_at: Date;
}

function workerCanRunQueue(worker: RuntimeWorkerAvailability, queue: RunnableQueueRow): boolean {
  if (!worker.available) return false;
  if (!worker.supportedExecutionProfiles.includes(queue.execution_profile)) return false;
  if (worker.supportedWorkloadTypes.length > 0 && !worker.supportedWorkloadTypes.includes(queue.workload_type)) return false;
  if (worker.allowedProducerSystems.length > 0 && !worker.allowedProducerSystems.includes(queue.producer_system)) return false;
  if (worker.allowedQueueClasses.length > 0 && !worker.allowedQueueClasses.includes(queue.queue_class)) return false;
  return true;
}

function toHeartbeatStatus(worker: RuntimeWorkerAvailability): RuntimeHealthWorkerHeartbeat {
  return {
    workerId: worker.workerId,
    state: worker.state,
    capacity: worker.capacity,
    activeLeaseCount: worker.activeLeaseCount,
    availableCapacity: worker.availableCapacity,
    heartbeatAgeSec: worker.heartbeatAgeSec,
    heartbeatFresh: worker.heartbeatFresh,
    available: worker.available,
  };
}

export async function getRuntimeHealth(
  db: pg.Pool | pg.PoolClient,
  options: RuntimeHealthOptions = {},
): Promise<RuntimeHealthStatus> {
  const now = options.now ?? new Date();
  const checkedAt = now.toISOString();
  const service = {
    name: "runtime-controller",
    version: process.env.RUNTIME_CONTROLLER_VERSION?.trim() || process.env.npm_package_version?.trim() || null,
  };
  try {
    const dbMeta = await db.query<{ current_database: string; current_user: string; current_time: Date }>(
      `SELECT current_database() AS current_database, current_user AS current_user, NOW() AS current_time`,
    );

    const jobs = await db.query<{ status: string; count: string }>(
      `SELECT status, COUNT(*)::text AS count
         FROM runtime.jobs
        GROUP BY status`,
    );

    const readyResults = await db.query<{ count: string }>(
      `SELECT COUNT(*)::text AS count
         FROM runtime.job_results
        WHERE consumed_at IS NULL`,
    );

    const deliveryTable = await db.query<{ name: string | null }>(
      `SELECT to_regclass('runtime.delivery_outbox')::text AS name`,
    );
    const deliveries = deliveryTable.rows[0]?.name
      ? await db.query<{ status: string; count: string }>(
        `SELECT status, COUNT(*)::text AS count
           FROM runtime.delivery_outbox
          GROUP BY status`,
      )
      : null;
    const sourceReceiptTable = await db.query<{ name: string | null }>(
      `SELECT to_regclass('runtime.source_receipts')::text AS name`,
    );
    const sourceReceipts = sourceReceiptTable.rows[0]?.name
      ? await db.query<{ status: string; count: string }>(
        `SELECT status, COUNT(*)::text AS count
           FROM runtime.source_receipts
          GROUP BY status`,
      )
      : null;
    const workersTable = await db.query<{ name: string | null }>(
      `SELECT to_regclass('runtime.workers')::text AS name`,
    );

    let workerHeartbeats: RuntimeHealthWorkerHeartbeat[] | undefined;
    let queueCapacity: RuntimeQueueCapacityHealth[] | undefined;
    let alerts: string[] = [];
    if (workersTable.rows[0]?.name) {
      const workers = await db.query<RuntimeWorkerRow & { active_lease_count: string }>(
        `SELECT w.*, COUNT(a.attempt_id)::text AS active_lease_count
           FROM runtime.workers w
           LEFT JOIN runtime.job_attempts a
             ON a.worker_id = w.worker_id
            AND a.status IN ('leased', 'running')
          GROUP BY w.worker_id
          ORDER BY w.worker_id ASC`,
      );
      const availabilityOptions = {
        now,
        workerHeartbeatTtlSec: options.workerHeartbeatTtlSec,
      };
      const workerAvailability = workers.rows.map((row) => describeWorkerAvailability(
        mapRuntimeWorkerRow(row),
        Number(row.active_lease_count),
        availabilityOptions,
      ));
      workerHeartbeats = workerAvailability.map(toHeartbeatStatus);

      const queues = await db.query<RunnableQueueRow>(
        `SELECT workload_type,
                producer_system,
                execution_profile,
                queue_class,
                COUNT(*)::text AS runnable_pending_jobs,
                (ARRAY_AGG(job_id::text ORDER BY runnable_at ASC, created_at ASC, job_id ASC))[1] AS oldest_job_id,
                MIN(runnable_at) AS oldest_runnable_at
           FROM runtime.jobs
          WHERE status IN ('pending', 'retryable')
            AND runnable_at <= $1::timestamptz
            AND (deadline_at IS NULL OR deadline_at > $1::timestamptz)
          GROUP BY workload_type, producer_system, execution_profile, queue_class
          ORDER BY workload_type ASC, producer_system ASC, execution_profile ASC, queue_class ASC`,
        [now.toISOString()],
      );
      queueCapacity = queues.rows.map((queue) => {
        const eligible = workerAvailability.filter((worker) => workerCanRunQueue(worker, queue));
        const freshEligibleWorkerCapacity = eligible.reduce((sum, worker) => sum + worker.availableCapacity, 0);
        const degraded = eligible.length === 0;
        const alert = degraded
          ? `runnable queue has no fresh eligible worker: workloadType=${queue.workload_type}, producerSystem=${queue.producer_system}, executionProfile=${queue.execution_profile}, queueClass=${queue.queue_class}`
          : null;
        if (alert) alerts.push(alert);
        return {
          workloadType: queue.workload_type,
          producerSystem: queue.producer_system,
          executionProfile: queue.execution_profile,
          queueClass: queue.queue_class,
          runnablePendingJobs: Number(queue.runnable_pending_jobs),
          oldestPendingJob: {
            jobId: queue.oldest_job_id,
            runnableAt: queue.oldest_runnable_at.toISOString(),
            ageSec: Math.max(0, Math.floor((now.getTime() - queue.oldest_runnable_at.getTime()) / 1000)),
          },
          freshEligibleWorkerCount: eligible.length,
          freshEligibleWorkerCapacity,
          degraded,
          alert,
        };
      });
    }

    const degraded = alerts.length > 0;
    return {
      // Queue-capacity degradation is an alerting condition, not API
      // liveness. Keeping healthz HTTP-200 while the database is reachable
      // avoids deadlocking workers whose ExecStartPre probes this endpoint.
      ok: true,
      checkedAt,
      service,
      database: {
        reachable: true,
        currentDatabase: dbMeta.rows[0]?.current_database,
        currentUser: dbMeta.rows[0]?.current_user,
        currentTime: dbMeta.rows[0]?.current_time.toISOString(),
      },
      runtime: {
        jobsByStatus: Object.fromEntries(jobs.rows.map((row) => [row.status, Number(row.count)])),
        readyResults: Number(readyResults.rows[0]?.count ?? 0),
        deliveriesByStatus: deliveries
          ? Object.fromEntries(deliveries.rows.map((row) => [row.status, Number(row.count)]))
          : undefined,
        sourceReceiptsByStatus: sourceReceipts
          ? Object.fromEntries(sourceReceipts.rows.map((row) => [row.status, Number(row.count)]))
          : undefined,
        workerHeartbeatTtlSec: workersTable.rows[0]?.name
          ? options.workerHeartbeatTtlSec ?? DEFAULT_WORKER_HEARTBEAT_TTL_SEC
          : undefined,
        workerHeartbeats,
        queueCapacity,
        degraded,
        alerts,
      },
    };
  } catch (error) {
    return {
      ok: false,
      checkedAt,
      service,
      database: {
        reachable: false,
        error: error instanceof Error ? error.message : String(error),
      },
    };
  }
}
