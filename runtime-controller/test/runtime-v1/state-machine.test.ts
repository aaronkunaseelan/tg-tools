import { describe, expect, it } from "vitest";
import {
  assertAttemptTransition,
  assertJobTransition,
  canTransitionAttemptStatus,
  canTransitionJobStatus,
  isActiveAttemptStatus,
  isTerminalJobStatus,
  terminalResultKindForJobStatus,
} from "../../src/runtime/state-machine.js";

describe("runtime state machine", () => {
  it("accepts valid job transitions", () => {
    expect(canTransitionJobStatus("pending", "leased")).toBe(true);
    expect(canTransitionJobStatus("leased", "succeeded")).toBe(true);
    expect(canTransitionJobStatus("retryable", "cancelled")).toBe(true);
  });

  it("rejects invalid job transitions", () => {
    expect(canTransitionJobStatus("pending", "retryable")).toBe(false);
    expect(() => assertJobTransition("succeeded", "leased")).toThrow(/invalid job transition/);
  });

  it("accepts valid attempt transitions", () => {
    expect(canTransitionAttemptStatus("leased", "running")).toBe(true);
    expect(canTransitionAttemptStatus("running", "retryable_error")).toBe(true);
  });

  it("rejects invalid attempt transitions", () => {
    expect(canTransitionAttemptStatus("succeeded", "running")).toBe(false);
    expect(() => assertAttemptTransition("expired", "running")).toThrow(/invalid attempt transition/);
  });

  it("marks terminal and active states correctly", () => {
    expect(isTerminalJobStatus("dead")).toBe(true);
    expect(isTerminalJobStatus("pending")).toBe(false);
    expect(isActiveAttemptStatus("leased")).toBe(true);
    expect(isActiveAttemptStatus("abandoned")).toBe(false);
  });

  it("maps terminal job status to terminal result kind", () => {
    expect(terminalResultKindForJobStatus("succeeded")).toBe("succeeded");
    expect(terminalResultKindForJobStatus("cancelled")).toBe("cancelled");
    expect(terminalResultKindForJobStatus("pending")).toBeNull();
  });
});
