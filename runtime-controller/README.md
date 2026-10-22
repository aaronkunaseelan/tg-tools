# runtime-controller

Runtime substrate for jobs, attempts, artifacts and delivery. Current source lives
at `~/tools/runtime-controller/`. The April 11 design references pointed
to a retired project directory; use this repository's code, SQL and docs for current
contracts, and [NODE_RUNTIME_POLICY.md](NODE_RUNTIME_POLICY.md) for dependency setup.

## Status

This repo is currently a **deployable early runtime**.

It includes:

- the v1 SQL migration for the four-table runtime schema
- TypeScript types for jobs, attempts, results, artifacts, and policy
- DB-backed implementations for submit / claim / mark-running / heartbeat / finalize / reconcile / result-ack flows
- a durable `runtime.delivery_outbox` for provider delivery retries that do not
  rerun execution
- a durable `runtime.source_receipts` table for producer-admitted source dedupe
  and route provenance
- a JSON-over-stdin CLI for migrations and runtime actions
- an HTTP API with optional bearer-token protection
- a long-running reconciler daemon
- a reference worker daemon that processes jobs through the HTTP API
- systemd user units and env-file deployment scaffolding
- nginx and Caddy reverse-proxy deployment templates
- a Vitest suite covering pure helpers and real Postgres end-to-end integration flows

It does **not** yet include:

- producer-specific business adapters beyond the reference worker
- richer authn/authz beyond the optional bearer token

## Core model

The v1 runtime centers on:

- `runtime.jobs`
- `runtime.job_attempts`
- `runtime.job_results`
- `runtime.artifacts`
- `runtime.delivery_outbox`
- `runtime.source_receipts`

Key boundary:

- producers keep business meaning
- runtime keeps execution mechanics
- workers do bounded execution only
- owners consume terminal handoff from `runtime.job_results`
- provider delivery is retried separately from execution through
  `runtime.delivery_outbox`
- producers may record a cheap source receipt before job submission; runtime
  stores dedupe/provenance but does not resolve business routes

Runtime v1 enum constraints:

- `queueClass` must be one of `foreground | background | ops`
- `executionShape` must be one of `local_bounded | remote_async_artifact | ops_bounded`
- `resultKind` must be one of `succeeded | dead | cancelled`

For artifact-batch workloads, workload-specific meaning should travel in:

- `resultRoute`
- `resultJson`
- `outputArtifactId`

## Project layout

```text
runtime-controller/
  config/
  docs/
  sql/migrations/
  src/
    apps/
    config/
    db/
    policy/
    runtime/
    types/
  systemd/
  test/runtime-v1/
```

## Quick start

```bash
cd ~/tools/runtime-controller
npm run deps:ci
npm run build
npm run typecheck
npm test
```

`deps:ci` installs into this checkout. Production `node_modules` requires a deliberate
promotion; phase-zero qualification uses a separate host-local candidate. The full
test command includes isolated-database integration tests; see the test sources before
running it against a host with production Postgres.

## CLI

The CLI reads JSON from stdin and writes JSON to stdout.

By default it connects through the local Postgres unix socket at `/var/run/postgresql` using the current OS user. You can also pass `--database-url`.

### Apply migrations

```bash
cd ~/tools/runtime-controller
npm run build
npm run migrate -- --database runtime_controller_dev
```

### Submit a job

```bash
echo '{
  "producerSystem": "scorer",
  "producerRef": "item-1",
  "resultOwner": "owner-a",
  "resultRoute": "owner-a.route",
  "workloadType": "scorer",
  "queueClass": "foreground",
  "executionProfile": "cpu_small",
  "executionShape": "local_bounded",
  "idempotencyKey": "score:item-1"
}' | node dist/src/cli.js submit --database runtime_controller_dev
```

### Register an artifact

QMD embedding batches are artifact-in / artifact-out. Register the top-level input manifest first:

```bash
echo '{
  "artifactKind": "qmd_embedding_input_manifest_json",
  "storageUri": "b2://qmd-artifacts/qmd/v5/runs/embeddings/2026/04/12/run-2026-04-12T141300Z-shard-0003/input/manifest.json",
  "contentHash": "sha256:6e4f0b...c1",
  "byteSize": 4096,
  "mimeType": "application/json",
  "schemaRef": "qmd.embedding.input-manifest.v1",
  "retentionClass": "durable",
  "producerSystem": "qmd",
  "metadataJson": {
    "workloadType": "qmd_embedding",
    "runId": "2026-04-12T141300Z-shard-0003"
  }
}' | node dist/src/cli.js artifact:register --database runtime_controller_dev
```

### Submit a QMD embedding batch

```bash
echo '{
  "producerSystem": "qmd",
  "producerRef": "qmd-embed:2026-04-12T141300Z:shard-0003",
  "resultOwner": "qmd",
  "resultRoute": "qmd.embedding.import.v1",
  "workloadType": "qmd_embedding",
  "queueClass": "foreground",
  "executionProfile": "embed_batch",
  "executionShape": "remote_async_artifact",
  "idempotencyKey": "qmd-embedding:BAAI-bge-base-en-v1.5:6e4f0b...c1",
  "inputItemCount": 301,
  "inputJson": {
    "schemaVersion": 1,
    "priorityClass": "interactive_freshness",
    "model": "BAAI/bge-base-en-v1.5",
    "selectionPolicyVersion": "qmd-hot-embed-v1",
    "selectedChunkCount": 812,
    "uniqueInputCount": 301,
    "artifactBundleVersion": "qmd-embedding-bundle-v1"
  },
  "inputArtifactId": "<artifact-uuid>",
  "outputContractJson": {
    "schemaVersion": 1,
    "resultRoute": "qmd.embedding.import.v1",
    "requiredArtifacts": [
      "qmd_embedding_output_manifest_json",
      "qmd_embedding_vectors_parquet"
    ]
  }
}' | node dist/src/cli.js submit --database runtime_controller_dev
```

Note: `explicit_batch` submissions must set `inputItemCount > 0`. For `qmd_embedding`, the runtime also enforces `inputArtifactId`, `outputContractJson`, `resultRoute=qmd.embedding.import.v1`, `executionProfile=embed_batch`, and `executionShape=remote_async_artifact`.

### Claim work

```bash
echo '{
  "workerId": "worker-a",
  "supportedExecutionProfiles": ["cpu_small"],
  "workloadTypes": ["scorer"],
  "leaseDurationSec": 300
}' | node dist/src/cli.js claim --database runtime_controller_dev
```

Optional queue-class filter example:

```bash
echo '{
  "workerId": "embed-worker-a",
  "workerVenue": "oci",
  "supportedExecutionProfiles": ["embed_batch"],
  "workloadTypes": ["qmd_embedding"],
  "allowedQueueClasses": ["foreground", "background"],
  "queuePreference": ["foreground", "background", "ops"],
  "leaseDurationSec": 900
}' | node dist/src/cli.js claim --database runtime_controller_dev
```

### Finalize success

```bash
echo '{
  "jobId": "<job-uuid>",
  "attemptId": "<attempt-uuid>",
  "leaseToken": "<lease-uuid>",
  "outcome": "succeeded",
  "resultJson": {"ok": true}
}' | node dist/src/cli.js finalize --database runtime_controller_dev
```

For `qmd_embedding`, successful finalize is stricter:

```bash
echo '{
  "jobId": "<job-uuid>",
  "attemptId": "<attempt-uuid>",
  "leaseToken": "<lease-uuid>",
  "outcome": "succeeded",
  "outputArtifactId": "<output-manifest-artifact-uuid>",
  "resultJson": {
    "schemaVersion": 1,
    "status": "artifact_ready",
    "runId": "2026-04-12T141300Z-shard-0003",
    "model": "BAAI/bge-base-en-v1.5",
    "artifactBundleVersion": "qmd-embedding-bundle-v1",
    "vectorRowCount": 301,
    "failureRowCount": 0
  }
}' | node dist/src/cli.js finalize --database runtime_controller_dev
```

### Reconcile expired work

```bash
echo '{"leaseLimit": 100, "deadlineLimit": 100}' | node dist/src/cli.js reconcile --database runtime_controller_dev
```

### Health

```bash
npm run cli -- health --database runtime_controller_dev
```

Health output includes:

- service name
- service version when available from env / npm
- DB reachability and runtime counts
- worker heartbeat age, freshness, active lease count, and available capacity
- per runnable `(workloadType, producerSystem, executionProfile, queueClass)` queue,
  fresh eligible worker count/capacity, and oldest pending job identity/age

`RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC` controls worker membership
freshness and defaults to `120`. An `active` database state alone is not
availability: a worker whose `lastHeartbeatAt` is older than the TTL is reported
as `heartbeatFresh: false` and `available: false`, without changing its stored
state or active leases. `GET /healthz` sets `runtime.degraded` and emits an
alert when runnable work has no fresh eligible worker, while remaining HTTP 200
when the database/API itself is reachable so worker startup probes cannot
deadlock capacity recovery. A fresh matching worker clears that queue alert. Restart-rate telemetry is intentionally
omitted because the controller does not receive reliable restart metadata.

### Delivery outbox

Append delivery work after a terminal result is ready, then let a provider
worker claim and finalize the delivery. Duplicate append by `(resultOwner,
deliveryKey)` returns the existing row.

```bash
echo '{
  "resultOwner": "meeting-summary",
  "resultRoute": "telegram.meeting-summary.reply.v1",
  "deliveryProvider": "telegram",
  "deliveryKind": "text_message",
  "deliveryKey": "telegram:chat-123:message-456:summary-reply",
  "payloadJson": {
    "chatId": "123",
    "text": "Done"
  }
}' | node dist/src/cli.js delivery:append --database runtime_controller_dev
```

```bash
echo '{
  "claimOwner": "meeting-summary-result-consumer",
  "deliveryProvider": "telegram",
  "leaseDurationSec": 120
}' | node dist/src/cli.js delivery:claim --database runtime_controller_dev
```

```bash
echo '{
  "deliveryId": "<delivery-uuid>",
  "claimToken": "<claim-token>",
  "outcome": "sent",
  "providerReceiptJson": {
    "messageId": 12345
  }
}' | node dist/src/cli.js delivery:finalize --database runtime_controller_dev
```

### Source receipts

Source receipts dedupe producer-resolved source updates before a job is created.
Runtime stores the source key and route provenance; the producer remains
responsible for route resolution.

```bash
echo '{
  "sourceSystem": "telegram",
  "sourceKey": "tg:rss-audio:-100123:456",
  "producerSystem": "rss-audio",
  "workloadType": "rss_publish",
  "routeId": "rss.publish.telegram.v1",
  "routeKind": "rss_publish_route",
  "channelId": "rss-audio:-100123:2",
  "matchedRuleVersion": "v1",
  "sourceJson": {
    "messageId": 456
  }
}' | node dist/src/cli.js source:admit --database runtime_controller_dev
```

After successful job submission, link the receipt to the runtime job:

```bash
echo '{
  "receiptId": "<receipt-uuid>",
  "jobId": "<job-uuid>"
}' | node dist/src/cli.js source:submit --database runtime_controller_dev
```

## HTTP API

Build first:

```bash
cd ~/tools/runtime-controller
npm run build
```

Then run:

```bash
set -a && source ./.env.example && set +a
npm run serve
```

Endpoints:

- `GET /healthz`
- `POST /v1/submit`
- `POST /v1/claim`
- `GET /v1/jobs`
- `GET /v1/jobs/:jobId`
- `GET /v1/workers`
- `GET /v1/workers/:workerId` (includes `heartbeatAgeSec`, `heartbeatFresh`,
  `activeLeaseCount`, `availableCapacity`, and `available`)
- `POST /v1/workers/register`
- `POST /v1/workers/heartbeat`
- `POST /v1/workers/drain`
- `POST /v1/workers/resume`
- `POST /v1/artifacts/register`
- `GET /v1/artifacts/:artifactId`
- `POST /v1/running`
- `POST /v1/heartbeat`
- `POST /v1/finalize`
- `POST /v1/attempts/abandon`
- `POST /v1/reconcile`
- `POST /v1/cancel`
- `GET /v1/results?resultOwner=...`
- `POST /v1/results/claim`
- `POST /v1/results/consume`
- `GET /v1/deliveries`
- `GET /v1/deliveries/:deliveryId`
- `POST /v1/deliveries/append`
- `POST /v1/deliveries/claim`
- `POST /v1/deliveries/heartbeat`
- `POST /v1/deliveries/finalize`
- `POST /v1/deliveries/cancel`
- `POST /v1/deliveries/reconcile`
- `GET /v1/source-receipts`
- `GET /v1/source-receipts/:receiptId`
- `POST /v1/source-receipts/admit`
- `POST /v1/source-receipts/submit`
- `POST /v1/source-receipts/cancel`

`GET /v1/workers` returns the same effective availability fields for each
worker. Worker state is preserved for audit and drain behavior; stale membership
is derived at read/health time rather than rewritten in the database.

If `RUNTIME_CONTROLLER_BEARER_TOKEN` is set, all `/v1/*` endpoints require:

```text
Authorization: Bearer <token>
```

Error responses are structured as:

```json
{
  "error": "human-readable message",
  "code": "stable_error_code"
}
```

Typical codes now distinguish invalid requests, not-found lookups, and state/lease conflicts.

## Reconciler daemon

Run once:

```bash
npm run reconcile:once
```

Run continuously:

```bash
npm run reconciler
```

## Reference worker

Run:

```bash
npm run worker
```

The reference worker:

- polls the HTTP API for claimable jobs
- can optionally constrain claims by `workloadTypes`, `producerSystems`, and `allowedQueueClasses`
- can optionally override `queuePreference`
- can identify its venue with `RUNTIME_CONTROLLER_WORKER_VENUE=generic|oci|modal`
- marks attempts running
- heartbeats active leases
- finalizes success or simulated error outcomes
- uses worker-pull placement: keep job rows pool-neutral and steer by worker activation, claim filters, and queue preference

By default it returns a generic success payload. For `qmd_embedding`, there are now two governed paths:

- **`reference_stub`**: emit a reference `artifact_ready` success, register a top-level output-manifest artifact, and finalize with `resultRoute=qmd.embedding.import.v1`
- **`qmd_bridge`**: invoke the real QMD FastEmbed implementation from `~/tools/qmd` under runtime-controller claim / heartbeat / finalize governance, using the local `BAAI/bge-base-en-v1.5` model configured in QMD

The worker defaults to `RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER=auto`, which uses `qmd_bridge` only when the QMD repo, its Python environment, and the bundled bridge script are present; otherwise it falls back to `reference_stub`.

Important: the default worker profile set still does **not** include `embed_batch`, so `qmd_embedding` remains opt-in. To let `npm run worker` process QMD embedding jobs directly, set for example:

```bash
export RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES=embed_batch
export RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES=qmd_embedding
export RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS=qmd
export RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER=auto
export RUNTIME_CONTROLLER_QMD_REPO_ROOT=/path/to/qmd
export RUNTIME_CONTROLLER_QMD_PYTHON=/path/to/qmd/.venv/bin/python
```

You can simulate outcomes via `inputJson.simulate`, for example:

```json
{
  "simulate": {
    "outcome": "retryable_error",
    "errorClass": "temporary_provider_error",
    "errorText": "Provider timed out",
    "sleepMs": 5000
  }
}
```

## Deployment docs

- deployment guide: `docs/deployment.md`
- systemd units: `systemd/README.md`
- reverse-proxy templates: `deploy/README.md`
- sample env: `.env.example`
- sample policy override: `config/runtime-policy.example.yaml`

Current live OCI deployment shape:

- app bound to `127.0.0.1:8787`
- nginx active on port `80`
- nginx restricted to localhost + Tailscale clients
- bearer token still required for `/v1/*`
- dedicated workload workers should claim primarily by `workloadType`; do not rely on `producerSystem` as the default hot-path selector
- practical placement in v1 is still worker-pull for job claiming, but the QMD OCI-vs-Modal overflow gate now lives in runtime-controller rather than QMD

## Recommended next implementation steps

1. add workload-specific worker adapters beyond the generic reference worker
2. add stronger authn/authz and optional request signing
3. add metrics / tracing / audit event sinks
4. wire automatic cert and reverse-proxy rollout
5. add producer SDKs for submit and result consumption

## Phase-one lease qualification

Active-attempt start, heartbeat, abandon and finalize require matching current
job/attempt authority. Expiry is checked with the PostgreSQL wall clock after
row locks; delivery heartbeat/finalize and claimed cancellation follow the same
rule. The HTTP API rejects the internal fixture `now` field. External effect
reconciliation remains the provider adapter's responsibility.

Database integration tests require an explicit private Unix socket:
`RUNTIME_TEST_PGHOST=/absolute/private/socket RUNTIME_TEST_PGPORT=15441`.
They create/drop disposable databases and refuse the canonical production
socket. Supply these variables inside the credential-free wrapper, for example:

```sh
python3 deploy/no-secret-env.py -- env \
  RUNTIME_TEST_PGHOST=/absolute/private/socket RUNTIME_TEST_PGPORT=15441 \
  node node_modules/vitest/vitest.mjs run
```

Keep private fixture clusters and compiled acceptance artifacts outside source.
Phase-one gateway schema/application promotion is coordinated separately; this
source qualification does not migrate or restart production.
