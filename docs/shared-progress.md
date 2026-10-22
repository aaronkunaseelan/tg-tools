# Shared execution progress

Workers emit typed progress facts through `tg_tools_common.progress`; a bounded
sender posts them to the controller. Reporting failure never fails processing,
and attempt renewal is independent of it. The controller owns lifecycle,
versions, lease fencing, snapshots and event history; the gateway owns status
bindings and their Telegram presentation. Terminal results and delivery
receipts stay authoritative, and no model call is made for narration.

## Enabling

| Setting | Where | Effect |
|---|---|---|
| `TG_TOOLS_PROGRESS_ENABLED=1` | worker | detailed stage reporting |
| `TG_TOOLS_PROGRESS_ENABLED=1` | gateway | status messages at all |
| `TG_GATEWAY_PROGRESS_WORKLOADS` | gateway | workloads that get a status message |

Live: `rss_publish`, `tts_synthesize` and `x_tts_explain` on `tg-gateway-rss`.
Meeting-summary and standalone transcription have controller lifecycle records
but no user-facing status message.

Only newly admitted interactions get a status binding; removing a workload from
the list stops future bindings and leaves existing ones durable. Progress-only
changes never alter prompts, execution fingerprints or publication identities.

## Recovery and ordering

The acknowledgment binding is inserted with the source handoff; before job
admission it says the request is received and pending admission. Bindings name
the bot identity and admitted destination, so workers cannot choose recipients.

The projector persists each rendered intent before appending an immutable
delivery envelope, retrying an ambiguous append with the exact key and payload.
A binding has at most one pending envelope. The gateway serializes edits with a
PostgreSQL advisory lock and rechecks canonical progress right before editing.

Provider receipts repair binding state after restart without a second call. A
dispatch marker without a receipt quarantines the status message: no newer
edit may overtake an uncertain one. A permanent provider refusal closes the
stream. A quarantined status card may stay stale; the final reply still
arrives through the ordinary outbox.

Publication failures keep uploaded objects, because an episode create may have
committed before its response was lost; deterministic episode lookup
recognizes it on retry.

## Presentation and limits

Stage changes refresh on about five-second polling; long stages refresh
elapsed time and health every thirty seconds. Contact is labelled stale after
195 seconds without renewal. Retry delays, chunk counts, checkpoint reuse and
validated topic/reference counts are shown; there is no global percentage,
invented ETA or model reasoning.

The client buffers at most 128 unsent facts (intermediate counters are dropped
first) and retries an event three times with five-second timeouts. The
controller caps a request at 8 KiB and a job at 120 progress records per
minute. Progress deliveries rank below final replies in the outbox; Telegram
flood limits apply to both.

The bearer API is a trusted-host boundary, not multi-tenant authorization.
Event retention is not automated; measure growth before choosing a pruning
interval, and never prune provider receipts or terminal results.
