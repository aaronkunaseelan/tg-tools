import type { RuntimeJob, WorkerVenue, WorkerState } from "../types/runtime.js";

export interface PlacementWorkerSnapshot {
  workerId: string;
  venue: WorkerVenue;
  state: WorkerState;
  capacity: number;
  activeLeaseCount: number;
}

export interface PlacementDecision {
  allowed: boolean;
  scoreAdjust: number;
  policy: string;
  reason: string;
  details?: Record<string, unknown>;
}

export interface PlacementPolicy {
  name: string;
  applies(job: RuntimeJob): boolean;
  evaluate(job: RuntimeJob, worker: PlacementWorkerSnapshot): Promise<PlacementDecision>;
}
