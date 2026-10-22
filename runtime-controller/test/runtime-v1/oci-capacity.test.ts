import { describe, expect, it } from "vitest";
import { classifyOciCapacity } from "../../src/host/oci-capacity.js";

describe("oci capacity classification", () => {
  it("marks healthy host as available", () => {
    const snapshot = classifyOciCapacity({
      sampledAt: "2026-04-14T00:00:00.000Z",
      cpuCount: 8,
      load1m: 1.2,
      memAvailableMiB: 16000,
      swapUsedMiB: 0,
      interactiveSessions: 2,
    });
    expect(snapshot.state).toBe("available");
    expect(snapshot.reasons).toEqual([]);
  });

  it("marks pressured host as busy or constrained", () => {
    const busy = classifyOciCapacity({
      sampledAt: "2026-04-14T00:00:00.000Z",
      cpuCount: 8,
      load1m: 6.0,
      memAvailableMiB: 7000,
      swapUsedMiB: 0,
      interactiveSessions: 10,
    });
    expect(busy.state).toBe("busy");

    const constrained = classifyOciCapacity({
      sampledAt: "2026-04-14T00:00:00.000Z",
      cpuCount: 8,
      load1m: 7.5,
      memAvailableMiB: 3000,
      swapUsedMiB: 5000,
      interactiveSessions: 2,
    });
    expect(constrained.state).toBe("constrained");
    expect(constrained.reasons.length).toBeGreaterThan(0);
  });
});
