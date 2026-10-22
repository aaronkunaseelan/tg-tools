import type { QueueClass, RuntimeArtifact, RuntimeDeliveryOutbox, RuntimeJob, RuntimeJobAttempt, RuntimeJobResult, RuntimeSourceReceipt, RuntimeWorker, WorkerState, WorkerVenue } from "../types/runtime.js";

function toIso(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string") return new Date(value).toISOString();
  throw new Error(`cannot convert value to ISO string: ${String(value)}`);
}

function toNullableIso(value: unknown): string | null {
  if (value == null) return null;
  return toIso(value);
}

function toRecord(value: unknown): Record<string, unknown> | null {
  if (value == null) return null;
  if (typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  throw new Error(`expected record JSON value, got ${typeof value}`);
}

function toNumber(value: unknown): number {
  if (typeof value === "number") return value;
  if (typeof value === "string") return Number(value);
  throw new Error(`expected numeric value, got ${typeof value}`);
}

function toStringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((entry) => String(entry));
  }
  if (value == null) {
    return [];
  }
  throw new Error(`expected string array value, got ${typeof value}`);
}

export interface RuntimeJobRow {
  job_id: string;
  producer_system: string;
  producer_ref: string;
  result_owner: string;
  result_route: string;
  workload_type: string;
  queue_class: RuntimeJob["queueClass"];
  execution_profile: string;
  execution_shape: RuntimeJob["executionShape"];
  submission_mode: RuntimeJob["submissionMode"];
  priority: number;
  input_item_count: number | null;
  status: RuntimeJob["status"];
  runnable_at: Date | string;
  deadline_at: Date | string | null;
  idempotency_key: string;
  effect_key: string | null;
  attempt_count: number;
  max_attempts: number;
  lease_owner: string | null;
  lease_token: string | null;
  lease_expires_at: Date | string | null;
  input_json: unknown;
  input_artifact_id: string | null;
  output_contract_json: unknown;
  terminal_result_id: string | null;
  last_error_class: string | null;
  last_error_text: string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

export interface RuntimeJobAttemptRow {
  attempt_id: string;
  job_id: string;
  attempt_no: number;
  worker_id: string;
  execution_profile: string;
  lease_token: string;
  lease_expires_at: Date | string;
  status: RuntimeJobAttempt["status"];
  started_at: Date | string;
  heartbeat_at: Date | string;
  completed_at: Date | string | null;
  error_class: string | null;
  error_text: string | null;
  telemetry_json: unknown;
  provider_receipt_json: unknown;
  probe_summary_json: unknown;
  claim_json: unknown;
  attempt_output_artifact_id: string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

export interface RuntimeJobResultRow {
  result_id: string;
  job_id: string;
  attempt_id: string | null;
  result_owner: string;
  result_route: string;
  result_kind: RuntimeJobResult["resultKind"];
  result_json: unknown;
  output_artifact_id: string | null;
  error_class: string | null;
  error_text: string | null;
  created_at: Date | string;
  claimed_at: Date | string | null;
  consumed_at: Date | string | null;
}

export interface RuntimeArtifactRow {
  artifact_id: string;
  artifact_kind: string;
  storage_uri: string;
  content_hash: string;
  byte_size: number | string;
  mime_type: string | null;
  schema_ref: string | null;
  retention_class: RuntimeArtifact["retentionClass"];
  producer_system: string;
  created_by_job_id: string | null;
  created_by_attempt_id: string | null;
  metadata_json: unknown;
  created_at: Date | string;
}

export interface RuntimeWorkerRow {
  worker_id: string;
  venue: WorkerVenue;
  state: WorkerState;
  supported_execution_profiles: string[] | null;
  supported_workload_types: string[] | null;
  allowed_producer_systems: string[] | null;
  allowed_queue_classes: string[] | null;
  queue_preference: string[] | null;
  capacity: number;
  metadata_json: unknown;
  last_heartbeat_at: Date | string;
  last_claim_at: Date | string | null;
  drain_requested_at: Date | string | null;
  drain_deadline_at: Date | string | null;
  drain_reason: string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

export interface RuntimeDeliveryOutboxRow {
  delivery_id: string;
  result_id: string | null;
  job_id: string | null;
  result_owner: string;
  result_route: string;
  delivery_provider: string;
  delivery_kind: string;
  delivery_key: string;
  priority: number;
  status: RuntimeDeliveryOutbox["status"];
  runnable_at: Date | string;
  deadline_at: Date | string | null;
  attempt_count: number;
  max_attempts: number;
  claim_owner: string | null;
  claim_token: string | null;
  claim_expires_at: Date | string | null;
  payload_json: unknown;
  provider_receipt_json: unknown;
  last_error_class: string | null;
  last_error_text: string | null;
  sent_at: Date | string | null;
  dead_at: Date | string | null;
  cancelled_at: Date | string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

export interface RuntimeSourceReceiptRow {
  receipt_id: string;
  source_system: string;
  source_key: string;
  producer_system: string;
  workload_type: string;
  route_id: string;
  route_kind: string;
  channel_id: string | null;
  matched_rule_version: string;
  source_json: unknown;
  status: RuntimeSourceReceipt["status"];
  job_id: string | null;
  created_at: Date | string;
  updated_at: Date | string;
}

export function mapRuntimeJobRow(row: RuntimeJobRow): RuntimeJob {
  return {
    jobId: row.job_id,
    producerSystem: row.producer_system,
    producerRef: row.producer_ref,
    resultOwner: row.result_owner,
    resultRoute: row.result_route,
    workloadType: row.workload_type,
    queueClass: row.queue_class,
    executionProfile: row.execution_profile,
    executionShape: row.execution_shape,
    submissionMode: row.submission_mode,
    priority: row.priority,
    inputItemCount: row.input_item_count,
    status: row.status,
    runnableAt: toIso(row.runnable_at),
    deadlineAt: toNullableIso(row.deadline_at),
    idempotencyKey: row.idempotency_key,
    effectKey: row.effect_key,
    attemptCount: row.attempt_count,
    maxAttempts: row.max_attempts,
    leaseOwner: row.lease_owner,
    leaseToken: row.lease_token,
    leaseExpiresAt: toNullableIso(row.lease_expires_at),
    inputJson: toRecord(row.input_json),
    inputArtifactId: row.input_artifact_id,
    outputContractJson: toRecord(row.output_contract_json),
    terminalResultId: row.terminal_result_id,
    lastErrorClass: row.last_error_class,
    lastErrorText: row.last_error_text,
    createdAt: toIso(row.created_at),
    updatedAt: toIso(row.updated_at),
  };
}

export function mapRuntimeJobAttemptRow(row: RuntimeJobAttemptRow): RuntimeJobAttempt {
  return {
    attemptId: row.attempt_id,
    jobId: row.job_id,
    attemptNo: row.attempt_no,
    workerId: row.worker_id,
    executionProfile: row.execution_profile,
    leaseToken: row.lease_token,
    leaseExpiresAt: toIso(row.lease_expires_at),
    status: row.status,
    startedAt: toIso(row.started_at),
    heartbeatAt: toIso(row.heartbeat_at),
    completedAt: toNullableIso(row.completed_at),
    errorClass: row.error_class,
    errorText: row.error_text,
    telemetryJson: toRecord(row.telemetry_json),
    providerReceiptJson: toRecord(row.provider_receipt_json),
    probeSummaryJson: toRecord(row.probe_summary_json),
    claimJson: toRecord(row.claim_json),
    attemptOutputArtifactId: row.attempt_output_artifact_id,
    createdAt: toIso(row.created_at),
    updatedAt: toIso(row.updated_at),
  };
}

export function mapRuntimeJobResultRow(row: RuntimeJobResultRow): RuntimeJobResult {
  return {
    resultId: row.result_id,
    jobId: row.job_id,
    attemptId: row.attempt_id,
    resultOwner: row.result_owner,
    resultRoute: row.result_route,
    resultKind: row.result_kind,
    resultJson: toRecord(row.result_json),
    outputArtifactId: row.output_artifact_id,
    errorClass: row.error_class,
    errorText: row.error_text,
    createdAt: toIso(row.created_at),
    claimedAt: toNullableIso(row.claimed_at),
    consumedAt: toNullableIso(row.consumed_at),
  };
}

export function mapRuntimeArtifactRow(row: RuntimeArtifactRow): RuntimeArtifact {
  return {
    artifactId: row.artifact_id,
    artifactKind: row.artifact_kind,
    storageUri: row.storage_uri,
    contentHash: row.content_hash,
    byteSize: toNumber(row.byte_size),
    mimeType: row.mime_type,
    schemaRef: row.schema_ref,
    retentionClass: row.retention_class,
    producerSystem: row.producer_system,
    createdByJobId: row.created_by_job_id,
    createdByAttemptId: row.created_by_attempt_id,
    metadataJson: toRecord(row.metadata_json) ?? {},
    createdAt: toIso(row.created_at),
  };
}

export function mapRuntimeWorkerRow(row: RuntimeWorkerRow): RuntimeWorker {
  return {
    workerId: row.worker_id,
    venue: row.venue,
    state: row.state,
    supportedExecutionProfiles: toStringArray(row.supported_execution_profiles),
    supportedWorkloadTypes: toStringArray(row.supported_workload_types),
    allowedProducerSystems: toStringArray(row.allowed_producer_systems),
    allowedQueueClasses: toStringArray(row.allowed_queue_classes) as QueueClass[],
    queuePreference: toStringArray(row.queue_preference) as QueueClass[],
    capacity: row.capacity,
    metadataJson: toRecord(row.metadata_json) ?? {},
    lastHeartbeatAt: toIso(row.last_heartbeat_at),
    lastClaimAt: toNullableIso(row.last_claim_at),
    drainRequestedAt: toNullableIso(row.drain_requested_at),
    drainDeadlineAt: toNullableIso(row.drain_deadline_at),
    drainReason: row.drain_reason,
    createdAt: toIso(row.created_at),
    updatedAt: toIso(row.updated_at),
  };
}

export function mapRuntimeDeliveryOutboxRow(row: RuntimeDeliveryOutboxRow): RuntimeDeliveryOutbox {
  return {
    deliveryId: row.delivery_id,
    resultId: row.result_id,
    jobId: row.job_id,
    resultOwner: row.result_owner,
    resultRoute: row.result_route,
    deliveryProvider: row.delivery_provider,
    deliveryKind: row.delivery_kind,
    deliveryKey: row.delivery_key,
    priority: row.priority,
    status: row.status,
    runnableAt: toIso(row.runnable_at),
    deadlineAt: toNullableIso(row.deadline_at),
    attemptCount: row.attempt_count,
    maxAttempts: row.max_attempts,
    claimOwner: row.claim_owner,
    claimToken: row.claim_token,
    claimExpiresAt: toNullableIso(row.claim_expires_at),
    payloadJson: toRecord(row.payload_json) ?? {},
    providerReceiptJson: toRecord(row.provider_receipt_json),
    lastErrorClass: row.last_error_class,
    lastErrorText: row.last_error_text,
    sentAt: toNullableIso(row.sent_at),
    deadAt: toNullableIso(row.dead_at),
    cancelledAt: toNullableIso(row.cancelled_at),
    createdAt: toIso(row.created_at),
    updatedAt: toIso(row.updated_at),
  };
}

export function mapRuntimeSourceReceiptRow(row: RuntimeSourceReceiptRow): RuntimeSourceReceipt {
  return {
    receiptId: row.receipt_id,
    sourceSystem: row.source_system,
    sourceKey: row.source_key,
    producerSystem: row.producer_system,
    workloadType: row.workload_type,
    routeId: row.route_id,
    routeKind: row.route_kind,
    channelId: row.channel_id,
    matchedRuleVersion: row.matched_rule_version,
    sourceJson: toRecord(row.source_json) ?? {},
    status: row.status,
    jobId: row.job_id,
    createdAt: toIso(row.created_at),
    updatedAt: toIso(row.updated_at),
  };
}
