import { exec } from "node:child_process";
import os from "node:os";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";

const execAsync = promisify(exec);

export type OciCapacityState = "available" | "busy" | "constrained";

export interface OciHostSignals {
  sampledAt: string;
  cpuCount: number;
  load1m: number;
  memAvailableMiB: number | null;
  swapUsedMiB: number | null;
  interactiveSessions: number | null;
}

export interface OciCapacitySnapshot extends OciHostSignals {
  state: OciCapacityState;
  reasons: string[];
}

async function interactiveSessions(): Promise<number | null> {
  try {
    const { stdout } = await execAsync("who");
    return stdout.split(/\r?\n/).filter((line) => line.trim()).length;
  } catch {
    return null;
  }
}

export async function collectOciHostSignals(): Promise<OciHostSignals> {
  let memAvailableMiB: number | null = null;
  let swapUsedMiB: number | null = null;
  try {
    const meminfo = await readFile("/proc/meminfo", "utf8");
    const values = Object.fromEntries(
      meminfo
        .split(/\r?\n/)
        .filter((line) => line.includes(":"))
        .map((line) => {
          const [key, rest] = line.split(":", 2);
          return [key.trim(), Number.parseInt(rest.trim().split(/\s+/, 1)[0] ?? "0", 10)];
        }),
    );
    if (Number.isFinite(values.MemAvailable)) {
      memAvailableMiB = Math.floor(values.MemAvailable / 1024);
    }
    if (Number.isFinite(values.SwapTotal) && Number.isFinite(values.SwapFree)) {
      swapUsedMiB = Math.max(0, Math.floor((values.SwapTotal - values.SwapFree) / 1024));
    }
  } catch {
    // best-effort only
  }
  return {
    sampledAt: new Date().toISOString(),
    cpuCount: os.cpus().length,
    load1m: Number(os.loadavg()[0].toFixed(3)),
    memAvailableMiB,
    swapUsedMiB,
    interactiveSessions: await interactiveSessions(),
  };
}

export function classifyOciCapacity(signals: OciHostSignals): OciCapacitySnapshot {
  const reasons: string[] = [];
  const cpuBusyThreshold = Math.max(1, signals.cpuCount * 0.65);
  const cpuConstrainedThreshold = Math.max(1, signals.cpuCount * 0.9);

  if (signals.memAvailableMiB != null && signals.memAvailableMiB < 4096) {
    reasons.push("low_memory");
  }
  if (signals.swapUsedMiB != null && signals.swapUsedMiB > 4096) {
    reasons.push("high_swap");
  }
  if (signals.load1m >= cpuConstrainedThreshold) {
    reasons.push("cpu_constrained");
  }
  if (reasons.length > 0) {
    return { ...signals, state: "constrained", reasons };
  }

  if (signals.memAvailableMiB != null && signals.memAvailableMiB < 8192) {
    reasons.push("memory_tight");
  }
  if (signals.load1m >= cpuBusyThreshold) {
    reasons.push("cpu_busy");
  }
  if (signals.interactiveSessions != null && signals.interactiveSessions >= 12) {
    reasons.push("interactive_pressure");
  }
  if (reasons.length > 0) {
    return { ...signals, state: "busy", reasons };
  }

  return { ...signals, state: "available", reasons };
}

export async function snapshotOciCapacity(): Promise<OciCapacitySnapshot> {
  return classifyOciCapacity(await collectOciHostSignals());
}
