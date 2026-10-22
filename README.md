# tg-tools

Telegram tools for turning links, documents and recordings into podcast episodes, transcripts and reviewed meeting notes. For engineers running their own private assistant stack, the central idea is to separate processing from delivery: a Telegram update becomes durable work, its result survives a worker restart, and a failed notification can be repaired without repeating the processing.

## What it offers

- **Faithful reads (TTS):** send text, a URL or a supported document to the configured topic and receive a podcast episode of the source text.
- **Explained reads (X-TTS):** send a source to a separate topic for an explanatory script, synthesized and published as an episode.
- **rss-audio:** turn an audio link or Telegram file into an MP3 podcast episode. Optional enrichment adds a verbatim transcript, notes, topic ranges and evidence-bound references. Audio can still publish when optional enrichment fails; required enrichment makes that failure block publication.
- **Scribe:** send audio, voice or a media URL to start a Markdown transcript. Reply to a linked message with text or more audio to append and receive the next complete revision.
- **Meeting notes:** audio becomes a Markdown preview with editable date, title and attendees. An Accept button commits the reviewed version; stale buttons cannot accept a newer preview.
- **Visible execution:** configured RSS/TTS jobs can show stage, elapsed time, measured counts, checkpoint reuse and contact health in Telegram. Progress is not a fabricated percentage or ETA; terminal results and provider receipts remain authoritative.
- **Podcast delivery:** episodes have stable identities and retained media/artifacts, served through RSS feeds and an HTTP edge with HEAD and byte-range support.

Example episode pages: pending publication review. Three links under
`https://github.com/aaronkunaseelan/_assets/tree/main/rss-episodes/<slug>`
will be added after the example artifacts and rights are approved.

## How it works

```text
Telegram update -> durable source handoff -> source receipt -> runtime job
worker -> terminal result -> product projector -> delivery outbox -> Telegram
       -> immutable artifacts in B2 -> episode API -> D1-backed RSS feed
```

The [layout index](docs/README.md) and product guides describe each adapter:
[TTS/X-TTS](tts/README.md), [rss-audio/Scribe](rss-audio/README.md),
[meeting-summary](meeting-summary/README.md) and
[standalone transcription](transcribe-audio/README.md).

| Primitive | Contract / implementation | Rule it enforces |
|---|---|---|
| Route manifest | `services/*.json`; `python3 -m tg_tools_cli routes explain --service tts --json` | Bot identity, chat, thread and update shape select a route and workload; topic titles do not. |
| Admission | `shared/tg_tools_common/telegram_routes.py`; gateway adapters | Deterministic local routing precedes submission; admission makes no provider or model call. Production requires explicit allowed chat/users. |
| Source handoff and staging | `tg-gateway/tg_gateway/`; `docs/execution-contracts.md` | Persist the handoff before staging/submission and retain input before acknowledging durable handoff. Replay converges to submitted work or terminal rejection. |
| Source receipt and job input | `shared/tg_tools_common/runtime_contract.py` | Receipts hold provenance metadata, not private text. Inline text/URLs belong at the top level of job input. |
| Job, attempt and lease | [runtime-controller state machine](runtime-controller/docs/state-machine.md) | Claim, heartbeat, abandon and finalize operate on an attempt's authority. Locked database rows and database time fence expired attempts. |
| Terminal result and projector | `tts/tts_worker/result_projector.py`, `rss-audio/result_projector.py`, `meeting-summary/result_consumer.py` | Persist output before notification; recover delivery from the result instead of rerunning inference. |
| Delivery outbox and dispatch | `shared/tg_tools_common/delivery_contract.py`; `docs/execution-contracts.md` | The controller queues deliveries; the gateway records dispatch ownership and provider receipts. Ambiguous sends stop automatic replay. |
| Progress snapshot | `shared/tg_tools_common/progress.py`; `docs/shared-progress.md` | Workers emit typed facts; controller snapshots own ordering and lease fencing; gateway bindings own recipients and presentation. |
| Episode publication | `shared/episode_enrichment/`; product publishers | Upload immutable objects before publishing metadata. An uncertain episode create is resolved by exact `slug` lookup, not a blind POST retry. |
| RSS edge | [rss-edge](rss-edge/README.md) | D1 stores episode/feed metadata; B2 retains bytes. The Worker serves feeds and private-origin objects through public delivery URLs. |

[runtime-controller](runtime-controller/README.md) is the Postgres-backed execution
substrate, not another product queue. It owns leases, retries, terminal results,
artifact references and the delivery outbox. Product adapters own what work means.

**rss-edge ships as a recovered bundle**, not the original TypeScript source tree.
Its own README and configuration guide describe deployment dependencies and
remaining compiled-in routing assumptions; it is not a generic-host distribution.

## Design decisions

1. **One execution substrate → one authority for retries and leases → no second queue or universal worker engine in product adapters.** Route meaning stays with the product; runtime-controller manages submitted work.
2. **Durable handoff before acknowledgment → a restart must not erase accepted input → no in-memory-only submission boundary.** Receipt provenance and retained source data remain separate.
3. **Results are independent of delivery → notification repair should not repeat paid inference → no delivery failure that reopens a terminal job.** Product projectors reconstruct replies from durable outputs.
4. **Fence attempts with database time after locking → expiry must be checked when authority is used → no finalization by an expired attempt.** This fences database mutations, not an external provider's actions.
5. **Preserve uncertain dispatch ownership → a lost response is not proof of a failed send → no automatic replay of ambiguous Telegram effects.** Deduplicated enqueue is not exactly-once delivery.
6. **Immutable artifacts and exact episode lookup → interruption can happen between upload and publication → no overwrite of referenced bytes or blind replay of an uncertain create.** URLs provide access; storage URI plus hash identifies bytes.
7. **Typed, bounded progress outside the processing dependency → status reporting must not become a second failure mode → no model narration, invented ETA or requirement that reporting succeed.** Renewal and terminal results remain independent.
8. **Ordered, durable status edits → an older uncertain edit must not overwrite newer truth → no edit overtaking a dispatch marker without a receipt.** A quarantined status card may stay stale while the final reply uses the ordinary outbox.
9. **Byte-led document detection and explicit errors → extensions and HTTP headers can lie → no HTML challenge page read as a PDF or silent truncation.** Unsupported Office/image inputs and scanned PDFs without text fail visibly; there is no OCR fallback.
10. **Separate faithful and explained reads → source reproduction and interpretation are different products → no hidden explanation mode inside a faithful read.** They have distinct routes and workload identities.
11. **Pinned, isolated dependency installation → reproducibility includes the installer environment → no broad credentials in routine dependency commands or unreviewed lifecycle scripts.** Python uses a hashed closure; runtime-controller pins Node/npm and disables lifecycle scripts by default.
12. **Prebuilt feed representations → repeated podcast polling should not render every episode → no full per-request feed rebuild.** rss-edge stores compressed feed bytes in D1, invalidates on API writes and versions presentation changes.

The [decision record](docs/decisions.md) expands the boundaries and trade-offs.

## The family

- [qmd](https://github.com/aaronkunaseelan/qmd-ak) holds and retrieves the data.
- [gsd](https://github.com/aaronkunaseelan/gsd-ak) decides **what** matters.
- [Steward](https://github.com/aaronkunaseelan/steward) decides **when** to act.
- [tg-tools](https://github.com/aaronkunaseelan/tg-tools) processes requests and delivers over Telegram.

These roles are boundaries, not a requirement to install every sibling for every
product. Meeting-note downstream readers use the accepted-note snapshot contract;
they do not make the Telegram gateway a planning system.

## Run it

This is a multi-service deployment, not a hosted bot or a one-command installer.
The supplied units assume a Linux user systemd service host. You need Python 3.12,
uv, Postgres and a configured runtime-controller HTTP API; Telegram bot/API
credentials; and the providers required by the products you enable. Podcast
publication additionally needs B2/rclone, the rss-edge Worker with D1, ffmpeg and
ffprobe. PDF reads need Poppler's `pdftotext`. Extractor routes can require a proxy
and a separate SSH/browser-cookie host. Meeting summaries use a configured Codex
OAuth login; transcription and speech need their configured Azure services.

1. Read [configuration](docs/config.md), copy the relevant `config/*.env.example`
   templates to protected deployment files, and fill in absolute paths, provider
   endpoints, credentials and logical Telegram topic bindings. No env file may
   override `PYTHONPATH`.
2. Install runtime-controller using its [release procedure](runtime-controller/docs/release.md).
   Configure rss-edge separately using its own [guide](rss-edge/README.md).
3. Build the locked Python runtime using
   `python3 scripts/setup_python_runtime.py --uv /path/to/uv` and point
   `~/.local/lib/tg-tools/runtime` at the resulting candidate. See
   [dependencies](docs/dependencies.md) for qualification and runtime switching.
4. Qualify a clean release commit and materialize it, rather than running services
   from a development checkout:

   ```sh
   python3 scripts/validate_tg_tools.py --full
   python3 scripts/materialize_tg_tools_release.py --repo /path/to/clean-release-tree \
     --commit <full-sha> --releases-root ~/.local/lib/tg-tools/releases --promote
   ```

5. Apply required gateway migrations before restart. Install the supplied
   `systemd/*.service` user units and their expected environment files, reload
   systemd and start the configured gateways, workers and result projectors.
   The [operations guide](docs/telegram-tools-operations.md) gives the exact unit
   and file mapping, migration order and rollback steps.

Check configuration and routing from the repository root:

```sh
python3 -m tg_tools_cli validate service tts
python3 -m tg_tools_cli routes explain --service tts --json
systemctl --user list-units 'tg-gateway-*' 'rss-audio-*' 'meeting-summary-*' 'tts-*'
journalctl --user -u tg-gateway-rss -n 100 --no-pager
```

There is no single end-to-end health command. A running unit is not acceptance:
verify the intended immutable release, then send a canary through each enabled
product. Correlate source receipt, job/attempt/result, artifact URI/hash, outbox
entry and Telegram receipt; check the actual reply, feed, HEAD and ranged media
reads. Do not open a second client on a running gateway's Telegram session.

## Developing

Keep [docs/README.md](docs/README.md) as the layout index. Shared wire contracts
live in `shared/tg_tools_common/`; product routes live in `services/`. Persisted
route, workload and delivery identifiers are protocol identities, so changing
them needs a migration, not just a rename.

```sh
python3 scripts/validate_tg_tools.py --quick
python3 scripts/validate_tg_tools.py --full
```

The validator runs each component in a separate process. `TG_TOOLS_PYTHON` selects
the test interpreter; otherwise it uses the invoking interpreter. Quick excludes
chain tests. Full additionally exercises native chains and the shared-package
installer boundary; it needs the private test-stack prerequisites, a promoted
runtime-controller release, disposable Postgres and `pdftotext`. Local tests do
not establish provider behavior or live delivery safety.

Dependency pins originate in `pyproject.toml`; regenerate requirements and the
hashed lock together following [dependencies](docs/dependencies.md). Release a
clean commit to an immutable directory, switch `current`, restart and run product
canaries. Keep the previous release for rollback; never patch the live tree.

## Licence

[MIT](LICENSE).
