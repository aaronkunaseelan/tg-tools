# Architecture

## Intent

Build a **generic Postgres-backed leased job substrate** for bounded work.

The runtime owns:

- admission timing policy before submission
- durable submitted job state
- claiming, leasing, heartbeat, retry, and deadline rules
- explicit terminal handoff
- artifact references for large durable IO

The runtime does **not** own:

- producer-specific business meaning
- queued-job coalescing
- workflow graphs
- generalized pub/sub semantics

## Durable surfaces

| Surface | Question |
|---|---|
| `runtime.jobs` | What work exists and what is its current execution state? |
| `runtime.job_attempts` | What happened on each execution try? |
| `runtime.job_results` | What terminal result is ready for which named owner? |
| `runtime.artifacts` | Where do large durable input/output bytes live? |
| `runtime.delivery_outbox` | What provider delivery work remains after execution has already finished? |
| `runtime.source_receipts` | Which producer-resolved source updates have been accepted and deduped? |

## Transaction bundles

- A: pre-submit policy resolution
- B: job submission
- C: claim / lease start
- D: heartbeat / lease extend
- E: fenced finalize success
- F: fenced finalize retryable error
- G: fenced finalize terminal error
- H: lease expiry recovery
- I: deadline expiry before claim
- J: pre-start cancellation
- K: owner consume / acknowledge result
- L: append provider delivery
- M: claim / heartbeat / finalize provider delivery
- N: delivery claim expiry recovery
- O: admit source receipt
- P: link admitted source receipt to submitted job

## Current implementation state

This project currently provides:

- schema and DDL
- type definitions
- state-machine helpers
- policy helpers
- DB-backed implementations for submit / claim / running / heartbeat / finalize / reconcile / result acknowledgement
- DB-backed delivery outbox implementations for append / claim / heartbeat /
  sent / retryable / dead / cancel / reconcile
- DB-backed source receipt implementations for admit / submit-link / cancel /
  list / get
- a CLI that exposes those flows over JSON stdin/stdout
- an HTTP API for producers and external workers
- a long-running reconciler daemon
- a reference worker daemon that exercises the API end-to-end
- end-to-end integration tests against a disposable Postgres database
- systemd user units and env-file deployment scaffolding
- reverse-proxy deployment templates for nginx and Caddy

The delivery outbox is intentionally generic. Runtime stores provider, kind,
dedupe key, payload JSON, retry state, and provider receipt; producers and
delivery workers keep provider-specific business meaning. Delivery failure must
not mutate `runtime.jobs` back into an executable state.

Source receipts are also intentionally generic. Producers resolve Telegram or
other edge routes before insertion and pass `sourceSystem`, `sourceKey`,
`routeId`, `routeKind`, `matchedRuleVersion`, and narrow source metadata.
Runtime dedupes and links the receipt to a job after submission; it does not
parse Telegram commands or decide workload meaning.

What is still missing is richer worker specialization and producer-specific adapters.
