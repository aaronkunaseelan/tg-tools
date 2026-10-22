export const JOB_STATUSES = [
  "pending",
  "leased",
  "retryable",
  "succeeded",
  "dead",
  "cancelled",
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const ATTEMPT_STATUSES = [
  "leased",
  "running",
  "succeeded",
  "retryable_error",
  "terminal_error",
  "expired",
  "abandoned",
] as const;
export type AttemptStatus = (typeof ATTEMPT_STATUSES)[number];

export const RESULT_KINDS = ["succeeded", "dead", "cancelled"] as const;
export type ResultKind = (typeof RESULT_KINDS)[number];

export const QUEUE_CLASSES = ["foreground", "background", "ops"] as const;
export type QueueClass = (typeof QUEUE_CLASSES)[number];

export const EXECUTION_SHAPES = ["local_bounded", "remote_async_artifact", "ops_bounded"] as const;
export type ExecutionShape = (typeof EXECUTION_SHAPES)[number];

export const SUBMISSION_MODES = ["singleton", "explicit_batch"] as const;
export type SubmissionMode = (typeof SUBMISSION_MODES)[number];

export const RETENTION_CLASSES = ["ephemeral", "durable", "published"] as const;
export type RetentionClass = (typeof RETENTION_CLASSES)[number];

export const WORKER_VENUES = ["generic", "oci", "modal"] as const;
export type WorkerVenue = (typeof WORKER_VENUES)[number];

export const WORKER_STATES = ["active", "draining", "drained"] as const;
export type WorkerState = (typeof WORKER_STATES)[number];

export const DELIVERY_STATUSES = [
  "pending",
  "claimed",
  "retryable",
  "sent",
  "dead",
  "cancelled",
] as const;
export type DeliveryStatus = (typeof DELIVERY_STATUSES)[number];

export const SOURCE_RECEIPT_STATUSES = ["accepted", "submitted", "cancelled"] as const;
export type SourceReceiptStatus = (typeof SOURCE_RECEIPT_STATUSES)[number];

export interface RuntimeJob {
  jobId: string;
  producerSystem: string;
  producerRef: string;
  resultOwner: string;
  resultRoute: string;
  workloadType: string;
  queueClass: QueueClass;
  executionProfile: string;
  executionShape: ExecutionShape;
  submissionMode: SubmissionMode;
  priority: number;
  inputItemCount: number | null;
  status: JobStatus;
  runnableAt: string;
  deadlineAt: string | null;
  idempotencyKey: string;
  effectKey: string | null;
  attemptCount: number;
  maxAttempts: number;
  leaseOwner: string | null;
  leaseToken: string | null;
  leaseExpiresAt: string | null;
  inputJson: Record<string, unknown> | null;
  inputArtifactId: string | null;
  outputContractJson: Record<string, unknown> | null;
  terminalResultId: string | null;
  lastErrorClass: string | null;
  lastErrorText: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface RuntimeJobAttempt {
  attemptId: string;
  jobId: string;
  attemptNo: number;
  workerId: string;
  executionProfile: string;
  leaseToken: string;
  leaseExpiresAt: string;
  status: AttemptStatus;
  startedAt: string;
  heartbeatAt: string;
  completedAt: string | null;
  errorClass: string | null;
  errorText: string | null;
  telemetryJson: Record<string, unknown> | null;
  providerReceiptJson: Record<string, unknown> | null;
  probeSummaryJson: Record<string, unknown> | null;
  claimJson: Record<string, unknown> | null;
  attemptOutputArtifactId: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface RuntimeJobResult {
  resultId: string;
  jobId: string;
  attemptId: string | null;
  resultOwner: string;
  resultRoute: string;
  resultKind: ResultKind;
  resultJson: Record<string, unknown> | null;
  outputArtifactId: string | null;
  errorClass: string | null;
  errorText: string | null;
  createdAt: string;
  claimedAt: string | null;
  consumedAt: string | null;
}

export interface RuntimeArtifact {
  artifactId: string;
  artifactKind: string;
  storageUri: string;
  contentHash: string;
  byteSize: number;
  mimeType: string | null;
  schemaRef: string | null;
  retentionClass: RetentionClass;
  producerSystem: string;
  createdByJobId: string | null;
  createdByAttemptId: string | null;
  metadataJson: Record<string, unknown>;
  createdAt: string;
}

export interface RuntimeWorker {
  workerId: string;
  venue: WorkerVenue;
  state: WorkerState;
  supportedExecutionProfiles: string[];
  supportedWorkloadTypes: string[];
  allowedProducerSystems: string[];
  allowedQueueClasses: QueueClass[];
  queuePreference: QueueClass[];
  capacity: number;
  metadataJson: Record<string, unknown>;
  lastHeartbeatAt: string;
  lastClaimAt: string | null;
  drainRequestedAt: string | null;
  drainDeadlineAt: string | null;
  drainReason: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface RuntimeDeliveryOutbox {
  deliveryId: string;
  resultId: string | null;
  jobId: string | null;
  resultOwner: string;
  resultRoute: string;
  deliveryProvider: string;
  deliveryKind: string;
  deliveryKey: string;
  priority: number;
  status: DeliveryStatus;
  runnableAt: string;
  deadlineAt: string | null;
  attemptCount: number;
  maxAttempts: number;
  claimOwner: string | null;
  claimToken: string | null;
  claimExpiresAt: string | null;
  payloadJson: Record<string, unknown>;
  providerReceiptJson: Record<string, unknown> | null;
  lastErrorClass: string | null;
  lastErrorText: string | null;
  sentAt: string | null;
  deadAt: string | null;
  cancelledAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface RuntimeSourceReceipt {
  receiptId: string;
  sourceSystem: string;
  sourceKey: string;
  producerSystem: string;
  workloadType: string;
  routeId: string;
  routeKind: string;
  channelId: string | null;
  matchedRuleVersion: string;
  sourceJson: Record<string, unknown>;
  status: SourceReceiptStatus;
  jobId: string | null;
  createdAt: string;
  updatedAt: string;
}
