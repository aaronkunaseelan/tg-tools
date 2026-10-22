import { readFile } from "node:fs/promises";
import YAML from "yaml";
import { DEFAULT_RUNTIME_POLICY } from "./default-policy.js";
import { makePolicyKey, type RuntimePolicyMap, type WorkloadRuntimePolicy } from "../types/policy.js";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizePolicy(policy: Record<string, unknown>): WorkloadRuntimePolicy {
  if (typeof policy.workloadType !== "string" || !policy.workloadType.trim()) {
    throw new Error("policy.workloadType is required");
  }
  if (policy.admissionAction !== "admit_now" && policy.admissionAction !== "defer" && policy.admissionAction !== "reject_retry_later") {
    throw new Error(`invalid admissionAction for ${policy.workloadType}`);
  }
  if (policy.submissionMode !== "singleton" && policy.submissionMode !== "explicit_batch") {
    throw new Error(`invalid submissionMode for ${policy.workloadType}`);
  }

  const asNumberOrNull = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

  return {
    workloadType: policy.workloadType,
    queueClass: policy.queueClass === "foreground" || policy.queueClass === "background" || policy.queueClass === "ops"
      ? policy.queueClass
      : undefined,
    executionProfile: typeof policy.executionProfile === "string" ? policy.executionProfile : undefined,
    executionShape: policy.executionShape === "local_bounded" || policy.executionShape === "remote_async_artifact" || policy.executionShape === "ops_bounded"
      ? policy.executionShape
      : undefined,
    admissionAction: policy.admissionAction,
    submissionMode: policy.submissionMode,
    maxWaitSec: asNumberOrNull(policy.maxWaitSec),
    targetItems: asNumberOrNull(policy.targetItems),
    maxItems: asNumberOrNull(policy.maxItems),
    maxBytes: asNumberOrNull(policy.maxBytes),
  };
}

function normalizePolicyMap(input: unknown): RuntimePolicyMap {
  const policies = Array.isArray(input)
    ? input
    : isRecord(input) && Array.isArray(input.policies)
      ? input.policies
      : null;

  if (!policies) {
    throw new Error("policy file must be an array or an object with a policies array");
  }

  const normalized = policies.map((item) => {
    if (!isRecord(item)) {
      throw new Error("each policy entry must be an object");
    }
    const policy = normalizePolicy(item);
    return [makePolicyKey(policy.workloadType, policy.queueClass, policy.executionProfile), policy] as const;
  });

  return Object.fromEntries(normalized);
}

export function mergePolicyMaps(base: RuntimePolicyMap, overrides: RuntimePolicyMap): RuntimePolicyMap {
  return { ...base, ...overrides };
}

export async function loadPolicyMapFromFile(path: string): Promise<RuntimePolicyMap> {
  const text = await readFile(path, "utf8");
  const parsed = YAML.parse(text) as unknown;
  return mergePolicyMaps(DEFAULT_RUNTIME_POLICY, normalizePolicyMap(parsed));
}
