import { createHash } from "node:crypto";
import type { RuntimeJob } from "../types/runtime.js";

export interface WorkerAdapterOutcome {
  adapter: string;
  outcome: "succeeded" | "retryable_error" | "terminal_error";
  resultJson?: Record<string, unknown> | null;
  errorClass?: string | null;
  errorText?: string | null;
  sleepMs?: number;
  backoffSec?: number;
}

function stableFraction(input: string): number {
  const hash = createHash("sha256").update(input).digest("hex");
  const value = Number.parseInt(hash.slice(0, 8), 16);
  return value / 0xffffffff;
}

function round(value: number, places = 6): number {
  const factor = Math.pow(10, places);
  return Math.round(value * factor) / factor;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter((item) => item.trim().length > 0);
}

function simulatedOutcome(job: RuntimeJob): WorkerAdapterOutcome | null {
  const input = job.inputJson ?? {};
  const simulate = asRecord(input.simulate);
  if (!simulate) return null;

  const sleepMs = typeof simulate.sleepMs === "number" && Number.isFinite(simulate.sleepMs)
    ? Math.max(0, simulate.sleepMs)
    : 0;

  if (simulate.outcome === "retryable_error") {
    return {
      adapter: "simulate",
      outcome: "retryable_error",
      errorClass: typeof simulate.errorClass === "string" ? simulate.errorClass : "simulated_retryable_error",
      errorText: typeof simulate.errorText === "string" ? simulate.errorText : "Simulated retryable error",
      sleepMs,
    };
  }

  if (simulate.outcome === "terminal_error") {
    return {
      adapter: "simulate",
      outcome: "terminal_error",
      errorClass: typeof simulate.errorClass === "string" ? simulate.errorClass : "simulated_terminal_error",
      errorText: typeof simulate.errorText === "string" ? simulate.errorText : "Simulated terminal error",
      sleepMs,
    };
  }

  const resultJson = asRecord(simulate.resultJson);
  if (simulate.outcome === "succeeded" || resultJson) {
    return {
      adapter: "simulate",
      outcome: "succeeded",
      resultJson: resultJson ?? { ok: true, simulated: true },
      sleepMs,
    };
  }

  return null;
}

function scorerOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  const input = job.inputJson ?? {};
  const text = typeof input.text === "string"
    ? input.text
    : typeof input.candidate === "string"
      ? input.candidate
      : JSON.stringify(input);
  const score = typeof input.rawScore === "number"
    ? Math.max(0, Math.min(1, input.rawScore))
    : round(stableFraction(`${job.producerSystem}:${job.producerRef}:${text}`));
  const label = score >= 0.85 ? "strong" : score >= 0.6 ? "good" : score >= 0.35 ? "weak" : "reject";
  return {
    adapter: "scorer",
    outcome: "succeeded",
    resultJson: {
      adapter: "scorer",
      score,
      label,
      rationale: `Deterministic scorer adapter assigned ${label} with score=${score}.`,
      workloadType: job.workloadType,
      producerRef: job.producerRef,
      textPreview: text.slice(0, 160),
    },
  };
}

function drafterOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  const input = job.inputJson ?? {};
  const recipient = typeof input.recipient === "string" ? input.recipient : "there";
  const topic = typeof input.topic === "string" ? input.topic : job.producerRef;
  const bullets = asStringArray(input.bullets);
  const tone = typeof input.tone === "string" ? input.tone : "clear";
  const intro = `Hi ${recipient},`;
  const bodyLines = [
    `I wanted to follow up regarding ${topic}.`,
    ...bullets.map((bullet) => `- ${bullet}`),
    `Let me know if you'd like me to refine this further in a ${tone} tone.`,
  ];
  const draftText = [intro, "", ...bodyLines, "", "Best,"]
    .join("\n");
  return {
    adapter: "drafter",
    outcome: "succeeded",
    resultJson: {
      adapter: "drafter",
      subject: `Draft: ${topic}`,
      draftText,
      bulletCount: bullets.length,
      tone,
      recipient,
    },
  };
}

function transcriptionOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  const input = job.inputJson ?? {};
  const segments = Array.isArray(input.segments) ? input.segments : [];
  const segmentTexts = segments.map((segment) => {
    const record = asRecord(segment);
    if (!record) return String(segment);
    return typeof record.text === "string" ? record.text : JSON.stringify(record);
  });
  const transcriptText = segmentTexts.length > 0
    ? segmentTexts.join(" ")
    : typeof input.text === "string"
      ? input.text
      : "";
  const durationSec = typeof input.durationSec === "number"
    ? input.durationSec
    : segments.reduce((max, segment) => {
        const record = asRecord(segment);
        const end = typeof record?.endSec === "number" ? record.endSec : 0;
        return Math.max(max, end);
      }, 0);
  return {
    adapter: "transcription",
    outcome: "succeeded",
    resultJson: {
      adapter: "transcription",
      transcriptText,
      segmentCount: segmentTexts.length,
      durationSec,
      language: typeof input.language === "string" ? input.language : "unknown",
    },
  };
}

function embeddingVector(text: string, dimensions: number): number[] {
  const vector: number[] = [];
  let seed = createHash("sha256").update(text).digest();
  while (vector.length < dimensions) {
    for (const byte of seed) {
      vector.push(round((byte / 255) * 2 - 1, 6));
      if (vector.length >= dimensions) break;
    }
    seed = createHash("sha256").update(seed).digest();
  }
  return vector;
}

function embeddingsOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  const input = job.inputJson ?? {};
  const dimensions = typeof input.dimensions === "number" && Number.isFinite(input.dimensions)
    ? Math.max(1, Math.min(32, Math.trunc(input.dimensions)))
    : 8;
  const rawItems = Array.isArray(input.items) ? input.items : [];
  const items = rawItems.map((item, index) => {
    const record = asRecord(item);
    const id = typeof record?.id === "string" ? record.id : `item-${index + 1}`;
    const text = typeof record?.text === "string" ? record.text : String(item);
    return {
      id,
      text,
      embedding: embeddingVector(text, dimensions),
    };
  });
  return {
    adapter: "embeddings",
    outcome: "succeeded",
    resultJson: {
      adapter: "embeddings",
      dimensions,
      itemCount: items.length,
      items,
    },
  };
}

function opsOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  const input = job.inputJson ?? {};
  return {
    adapter: "ops",
    outcome: "succeeded",
    resultJson: {
      adapter: "ops",
      acknowledged: true,
      action: typeof input.action === "string" ? input.action : null,
      metadata: asRecord(input.metadata) ?? {},
    },
  };
}

function genericOutcome(job: RuntimeJob): WorkerAdapterOutcome {
  return {
    adapter: "generic",
    outcome: "succeeded",
    resultJson: {
      ok: true,
      handledBy: "reference-worker",
      workloadType: job.workloadType,
      producerSystem: job.producerSystem,
      producerRef: job.producerRef,
      queueClass: job.queueClass,
      executionProfile: job.executionProfile,
      echoedInput: job.inputJson,
    },
  };
}

export function executeJobViaAdapter(job: RuntimeJob): WorkerAdapterOutcome {
  const simulated = simulatedOutcome(job);
  if (simulated) return simulated;

  switch (job.workloadType) {
    case "scorer":
      return scorerOutcome(job);
    case "drafter":
      return drafterOutcome(job);
    case "transcription":
      return transcriptionOutcome(job);
    case "embeddings":
      return embeddingsOutcome(job);
    case "ops":
      return opsOutcome(job);
    default:
      return genericOutcome(job);
  }
}
