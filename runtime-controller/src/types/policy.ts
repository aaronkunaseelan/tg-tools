import type { ExecutionShape, QueueClass, SubmissionMode } from "./runtime.js";

export const ADMISSION_ACTIONS = ["admit_now", "defer", "reject_retry_later"] as const;
export type AdmissionAction = (typeof ADMISSION_ACTIONS)[number];

export interface WorkloadRuntimePolicy {
  workloadType: string;
  queueClass?: QueueClass;
  executionProfile?: string;
  executionShape?: ExecutionShape;
  admissionAction: AdmissionAction;
  submissionMode: SubmissionMode;
  maxWaitSec: number | null;
  targetItems: number | null;
  maxItems: number | null;
  maxBytes: number | null;
}

export type RuntimePolicyMap = Record<string, WorkloadRuntimePolicy>;

export function makePolicyKey(
  workloadType: string,
  queueClass?: QueueClass,
  executionProfile?: string,
): string {
  return [workloadType, queueClass ?? "*", executionProfile ?? "*"].join(":");
}
