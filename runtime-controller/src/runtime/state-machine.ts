import type { AttemptStatus, JobStatus, ResultKind } from "../types/runtime.js";

export const JOB_STATUS_TRANSITIONS: Record<JobStatus, readonly JobStatus[]> = {
  pending: ["leased", "dead", "cancelled"],
  leased: ["retryable", "succeeded", "dead"],
  retryable: ["leased", "dead", "cancelled"],
  succeeded: [],
  dead: [],
  cancelled: [],
};

export const ATTEMPT_STATUS_TRANSITIONS: Record<AttemptStatus, readonly AttemptStatus[]> = {
  leased: ["running", "succeeded", "retryable_error", "terminal_error", "expired", "abandoned"],
  running: ["succeeded", "retryable_error", "terminal_error", "expired", "abandoned"],
  succeeded: [],
  retryable_error: [],
  terminal_error: [],
  expired: [],
  abandoned: [],
};

const TERMINAL_JOB_STATUSES = new Set<JobStatus>(["succeeded", "dead", "cancelled"]);
const ACTIVE_ATTEMPT_STATUSES = new Set<AttemptStatus>(["leased", "running"]);

export function isTerminalJobStatus(status: JobStatus): boolean {
  return TERMINAL_JOB_STATUSES.has(status);
}

export function isActiveAttemptStatus(status: AttemptStatus): boolean {
  return ACTIVE_ATTEMPT_STATUSES.has(status);
}

export function canTransitionJobStatus(from: JobStatus, to: JobStatus): boolean {
  return JOB_STATUS_TRANSITIONS[from].includes(to);
}

export function canTransitionAttemptStatus(from: AttemptStatus, to: AttemptStatus): boolean {
  return ATTEMPT_STATUS_TRANSITIONS[from].includes(to);
}

export function assertJobTransition(from: JobStatus, to: JobStatus): void {
  if (!canTransitionJobStatus(from, to)) {
    throw new Error(`invalid job transition: ${from} -> ${to}`);
  }
}

export function assertAttemptTransition(from: AttemptStatus, to: AttemptStatus): void {
  if (!canTransitionAttemptStatus(from, to)) {
    throw new Error(`invalid attempt transition: ${from} -> ${to}`);
  }
}

export function terminalResultKindForJobStatus(status: JobStatus): ResultKind | null {
  if (status === "succeeded" || status === "dead" || status === "cancelled") {
    return status;
  }
  return null;
}
