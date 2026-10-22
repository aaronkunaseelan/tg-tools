# Design decisions

These decisions describe execution, delivery and publication boundaries. They
are not a claim that an external provider offers transactional effects.

## One controller, product-owned meaning

runtime-controller owns jobs, attempts, leases, terminal results, artifact
references, source receipts and the delivery outbox. tg-tools owns Telegram
routing, product processing and rendering. This keeps retry authority in one
place and rules out a second queue, catalog or general workflow engine inside
each adapter. Runtime receives a producer-resolved route; it does not parse
Telegram commands to discover product meaning.

## Persist the source handoff first

A gateway writes a durable handoff before staging or submitting. Its drain
replays pending work until it is submitted or terminally rejected. Retain input
before acknowledging durable handoff: otherwise a successful acknowledgment
could conceal lost source bytes. Preserve chat/topic/message provenance.
Unavailable source data is unknown or an error, never empty success.

Receipts hold metadata only. Private inline text or URLs live in top-level
`sourceText` or `sourceUrl` job input fields, rather than being copied into
receipt metadata or nested prompt fields. Shared runtime payload builders
validate this boundary. It rules out provenance records becoming another
uncontrolled copy of private source text.

## Processing ends before delivery ends

A terminal result retains enough output for a product projector to reconstruct
its delivery. Stable owner/source keys make projection repairable. Notification
failure does not transition a terminal job back to executable work; otherwise
an outbox outage could repeat paid transcription or synthesis.

Meeting-summary has its own resumable result-consumption phases: the review
message, Markdown preview and Accept control must become visible in order.
Accept is version-fenced, so an old callback cannot commit replaced content.
This rules out treating a queued review as an already-visible accepted note.

## A lease is attempt authority, not an external transaction

Start, renewal, abandon and finalization compare job/attempt expiry using
Postgres `clock_timestamp()` after locking the canonical rows. Matching a token
is insufficient once its authority has expired. Terminal jobs do not reopen;
an expired attempt cannot finalize under old authority.

The fence governs database mutation, not a remote provider's actions. It rules
out claims that a lease alone prevents every duplicated external effect.
External publication and delivery need their own reconciliation rules.

## Dispatch uncertainty is durable

The controller outbox is the delivery queue. Gateway dispatch markers hold
effect ownership, tied to the delivery UUID, and are committed before the
provider operation. Authority is renewed immediately before that operation.
A known provider receipt allows hooks and finalization to resume without
sending again.

An explicit refusal can release ownership; valid rate-limit guidance drives
bounded retry. Timeouts, server failures, malformed success, receipt-commit
failure and a marker without a receipt are uncertain. They retain ownership
and stop automatic replay. Markers do not expire automatically: reconcile
provider evidence before deliberately retrying.

This chooses visible uncertainty over silent duplicates. It rules out calling
enqueue deduplication “exactly-once Telegram delivery,” or running a parallel
sender that ignores the markers. Each drain performs one provider operation;
overlong rich text falls back to a single Markdown document.

## Immutable bytes before metadata publication

Upload and verify artifacts before publishing the manifest and registering the
result. B2 retains bytes; controller artifact records bind storage URI, hash,
size and producer/job identity. A URL is an access path, not durable identity.
Never overwrite bytes referenced by an earlier result.

Upload, registration and episode creation are separate operations. Keep
uploaded objects after publication failures because a lost response may hide
a committed create. An uncertain create or duplicate response triggers exact
`GET /api/episodes?feed=<feed>&slug=<identity>` read-back, not fuzzy `search`
or a blind repeated POST. This rules out deleting the evidence needed to
recover, and separates publication retry from downloader/VPN failover.

## Progress reports facts, not narration

Workers emit typed facts through `tg_tools_common.progress`; the bounded sender
can fail without failing processing. Lease renewal is independent. Controller
lifecycle, versions and snapshots are authoritative; gateways own recipient
bindings and Telegram presentation. No model call narrates execution.

The progress API rejects unknown keys and raw text. Exact event replay returns
its prior acknowledgment; changed payload under an old identity is rejected.
Snapshots bound active occurrences, and event size/rate limits bound writes.
Per-job ordering is not a global transaction-commit cursor. This rules out
unbounded telemetry, invented percentages/ETAs and progress as a second source
of terminal truth. The bearer boundary assumes trusted hosts, not tenants.

## Status edits cannot overtake uncertainty

The acknowledgment binding is durable with the source handoff, before job
admission. It fixes bot identity and destination; workers do not choose
recipients. The projector persists an intent before appending its immutable
delivery envelope, with at most one pending envelope per binding.

Gateway edits serialize with an advisory lock and recheck canonical progress.
A dispatch marker without a receipt quarantines the status stream, preventing
a later edit from overtaking an uncertain earlier call. Final replies retain
the ordinary outbox path. This permits a visibly stale status card rather than
pretending edit ordering is known. Progress-only changes do not change prompts,
execution fingerprints or publication identities.

## Source bytes outrank labels

For TTS conversion, strong supported byte signatures outrank declared MIME,
then safe suffixes, then clean UTF-8 fallback without a binary signature.
A claimed PDF containing an HTML challenge must fail as blocked/mismatched,
not become speech. Fetching checks public resolved addresses and every
redirect, pins the checked address for connection and caps streamed bytes.

Conversion has explicit failure classes. Exceeding extraction limits is
`extract_truncated`, not silent slicing. Office files/images are unsupported;
a scanned PDF without text fails rather than invoking unconfigured OCR.
`pdftotext` is checked at startup, and the converter version participates in
the execution fingerprint. This rules out apparent success on the wrong
source and untracked changes in document interpretation.

## Faithful and explained reads are separate routes

`tts_synthesize` and `x_tts_explain` represent different promises: source-text
reading versus explanatory script generation. Separate manifest routes and
topics preserve that distinction. This rules out silently applying an
interpretive rewrite to a request for a faithful read. Persisted workload,
route and delivery identifiers remain protocol identities; rename them only
with a migration accounting for existing rows.

## Dependency reproducibility includes installation

Python direct/build/bootstrap pins originate in `pyproject.toml`; generated
requirements and the hashed closure must agree. Runtime candidates are
content/environment-addressed, built in a credential-stripped environment and
checked before switching. A failed candidate remains for inspection rather
than being repaired in place.

runtime-controller pins Node 24.20.0 and npm 12.0.2, with a lockfile and a
package-owned credential-stripping wrapper. Lifecycle scripts are disabled by
default; native execution requires explicit qualification. This rules out
broad credential inheritance, unpinned command runners, blind dependency churn
and source-checkout services. Immutable releases separate tested code from
in-flight development; rollback still needs schema compatibility.

## Serve stored feeds; keep the recovered edge honest

rss-edge uses D1 for episode/feed data and B2 for retained media. Audio/TTS feeds
are rendered on change and stored compressed in D1. API writes invalidate stored
copies; age and presentation-version checks refresh them. The representation
version participates in the ETag, so changed rendering cannot hide behind stale
conditional responses. This rules out rendering every episode on every poll.

Private-origin reads support HEAD, ranges and conditional requests. Sensitive
API/admin namespaces are no-store; public feeds have explicit cache policy.
These are separate policies, not a blanket cache switch over authenticated
responses.

The shipped edge is a recovered JavaScript bundle, not the original TypeScript
tree. Its documented compiled routing assumptions and external deployment
helper remain constraints. Do not imply that packaging the recovered code has
made it a turnkey portable Worker.

## Acceptance follows the real chain

Unit activity and a passing local suite are necessary checks, not proof of
working delivery. A product canary correlates source identity, receipt,
job/attempt/result, artifact URI/hash, outbox entry and provider receipt, then
checks the visible reply, feed and media byte ranges. Crash-recovery tests
check that delivery repair does not rerun processing. This rules out closing a
release merely because its service starts.
