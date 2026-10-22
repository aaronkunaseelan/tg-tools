import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadServerConfigFromEnv, loadWorkerConfigFromEnv } from "../../src/config/env.js";

const ORIGINAL_ENV = { ...process.env };

beforeEach(() => {
  process.env.RUNTIME_CONTROLLER_QMD_REPO_ROOT = "/tmp/example-qmd";
});

afterEach(() => {
  process.env = { ...ORIGINAL_ENV };
});

describe("env parsing", () => {
  it("requires the external QMD checkout rather than selecting an account path", () => {
    delete process.env.RUNTIME_CONTROLLER_QMD_REPO_ROOT;
    expect(() => loadWorkerConfigFromEnv()).toThrow("RUNTIME_CONTROLLER_QMD_REPO_ROOT is required");
  });

  it("parses optional reference worker claim filters", () => {
    process.env.RUNTIME_CONTROLLER_API_URL = "http://127.0.0.1:8787";
    process.env.RUNTIME_CONTROLLER_WORKER_ID = "worker-a";
    process.env.RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES = "cpu_small,cpu_media";
    process.env.RUNTIME_CONTROLLER_WORKER_CAPACITY = "3";
    process.env.RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES = "scorer,meeting_summary";
    process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS = "meeting-summary";
    process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES = "foreground,background";
    process.env.RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE = "foreground,background,ops";

    const config = loadWorkerConfigFromEnv();
    expect(config.capacity).toBe(3);
    expect(config.supportedExecutionProfiles).toEqual(["cpu_small", "cpu_media"]);
    expect(config.supportedWorkloadTypes).toEqual(["scorer", "meeting_summary"]);
    expect(config.allowedProducerSystems).toEqual(["meeting-summary"]);
    expect(config.allowedQueueClasses).toEqual(["foreground", "background"]);
    expect(config.queuePreference).toEqual(["foreground", "background", "ops"]);
  });

  it("defaults optional claim filters to empty lists", () => {
    delete process.env.RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES;
    delete process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS;
    delete process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES;
    delete process.env.RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE;
    process.env.RUNTIME_CONTROLLER_API_URL = "http://127.0.0.1:8787";
    process.env.RUNTIME_CONTROLLER_WORKER_ID = "worker-b";

    const config = loadWorkerConfigFromEnv();
    expect(config.supportedWorkloadTypes).toEqual([]);
    expect(config.allowedProducerSystems).toEqual([]);
    expect(config.allowedQueueClasses).toEqual([]);
    expect(config.queuePreference).toEqual([]);
  });

  it("uses a configurable 120-second worker heartbeat TTL by default", () => {
    delete process.env.RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC;
    expect(loadServerConfigFromEnv().workerHeartbeatTtlSec).toBe(120);

    process.env.RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC = "45";
    expect(loadServerConfigFromEnv().workerHeartbeatTtlSec).toBe(45);
  });

  it("rejects invalid worker queue classes", () => {
    process.env.RUNTIME_CONTROLLER_API_URL = "http://127.0.0.1:8787";
    process.env.RUNTIME_CONTROLLER_WORKER_ID = "worker-c";
    process.env.RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES = "foreground,interactive";

    expect(() => loadWorkerConfigFromEnv()).toThrow(/RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES/);
  });

});
