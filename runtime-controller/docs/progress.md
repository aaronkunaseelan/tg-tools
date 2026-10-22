# Progress API and lifecycle records

`POST /v1/jobs/:jobId/progress` accepts schema version 1, job/attempt IDs, lease token, stable event ID, positive attempt sequence, stage key, occurrence ID, kind and typed facts. Kinds are started, advanced, completed, reused, warning and interrupted. Supported facts include measured counts, units, durations, bounded public source title and safe reason codes. Unknown keys and raw text are rejected; lease tokens are never stored in event history.

`GET /v1/jobs/:jobId/progress?after=0` returns a consistent snapshot, canonical job/attempt, server time, version, terminal result ID and at most 100 subsequent event records. Use the final returned event version for history pagination; snapshot version can be ahead of the returned history page. Reads use the existing trusted bearer boundary. They do not consume results or register recipients.

Migration 011 adds lifecycle triggers and progress tables. Job admission/state/attempt changes and attempt state changes emit a record in their existing transaction; heartbeats remain in attempt rows rather than generating an event flood. Existing jobs are not backfilled with synthetic historical activity. Reads of untouched older jobs derive lifecycle from the canonical tables with version zero.

Worker append locks job then attempt, checks PostgreSQL clock after lock acquisition and validates current matching lease authority. Exact event replay returns its prior acknowledgment even after closure; payload changes under an old event ID are rejected. Unknown out-of-order identities, closed occurrences and measured counter regressions cannot change the snapshot. A new attempt resets its stage view. The snapshot holds at most 32 occurrences; event requests are capped at 8KiB and 120 records per job/minute.

Ordering is per job, under canonical row ownership; consumers must not use a global sequence allocation as a transaction commit cursor. Gateways poll their durable dirty/due recipient bindings and coalesce snapshots. Provider calls never hold a job lock or participate in a lifecycle transaction.

This release adds persistence and bounded reads without a broker, public SSE service or retention daemon. Existing result owners and delivery outbox scheduling remain unchanged. Provider dispatch uncertainty stays in gateway state and must survive rollback and future history pruning.
