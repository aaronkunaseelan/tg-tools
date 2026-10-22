import type { RuntimeJob } from "../types/runtime.js";
import type { PlacementDecision, PlacementWorkerSnapshot } from "./types.js";

export interface EvaluatePlacementOptions {}

export function defaultPlacementDecision(job: RuntimeJob): PlacementDecision {
  return {
    allowed: true,
    scoreAdjust: 0,
    policy: "default",
    reason: `${job.workloadType}_default_allowed`,
  };
}

export async function evaluatePlacement(
  job: RuntimeJob,
  _worker: PlacementWorkerSnapshot,
  options: EvaluatePlacementOptions = {},
): Promise<PlacementDecision> {
  void options;
  return defaultPlacementDecision(job);
}
