# Execution and artifact boundaries

runtime-controller owns source admission, jobs, attempts, leases, terminal
results and the delivery outbox. tg-tools owns product adapters, processing and
rendering. There is no second queue, catalog or universal worker engine.

Persisted workload, route and delivery identifiers are protocol identities:
changing one requires a migration that accounts for existing rows.

## Source handoff

Retain the input before acknowledging the durable handoff. Preserve
chat/topic/message identity and source provenance. Admit work through the
versioned workload contracts; unavailable input is unknown, never empty
success. Receipts carry metadata only; private text lives in job input
(`sourceText`/`sourceUrl` at the top level) and nowhere else.

## Artifacts

B2 holds retained bytes. The controller registry binds them to producers and
jobs (`storageUri`, `contentHash`, `byteSize`, optional `mimeType`); match its
actual request and response shapes rather than inventing another catalog.

Upload and verify objects, then publish the manifest and register the result.
Upload and registration are separate operations and must tolerate interruption
and replay. Uploads are immutable: never overwrite bytes an earlier result
references. HTTP URLs are access paths; durable identity is the storage URI
plus content hash.

## Results and effects

Successful processing retains enough output to reconstruct its delivery.
Terminal results are projected through the product's render functions into the
outbox under stable owner/source keys, so a missed notification is repaired
without repeating paid inference.

A job lease covers one attempt. Expired authority must not finalize or publish;
attempt start, renewal, abandon and finalization compare job and attempt expiry
in the database (`clock_timestamp()`) after locking their rows. This fences
database mutations; it does not make an external provider honour a lease.

## Delivery effects

`runtime.delivery_outbox` is the queue; `tg_gateway.delivery_dispatches` holds
effect ownership only. The gateway renews delivery authority immediately before
the provider call and commits a dispatch marker first. Markers and provider
receipts bind the controller's delivery UUID, so different owners may reuse a
text key.

Each drain performs one provider operation; long rich text falls back to a
single Markdown document. An explicit 4xx refusal releases the marker; a valid
429 `retry_after` drives bounded outbox retry. Timeouts, 5xx, malformed
success, receipt-commit failure and a marker without a receipt are uncertain:
they keep dispatch ownership and stop automatic replay. A known receipt replays
product hooks and finalization without sending again. Markers never expire or
clear automatically; reconcile against provider evidence before any deliberate
retry.

So a lost provider response can leave one delivery uncertain, but retry never
silently duplicates it; do not describe enqueue deduplication as exactly-once
Telegram delivery.

Never run a sender that ignores dispatch markers alongside the gateway: it
bypasses the guard.

## Acceptance evidence

A canary correlates source identity, receipt, job/attempt/result, artifact
URI and hash, outbox key and provider receipt, and checks feed and media byte
ranges and the user-visible result. Crash-recovery tests show that delivery
repair does not rerun processing. A running unit alone does not establish this
chain.
