# tg-tools

Telegram-facing products built on the runtime controller
(`~/tools/runtime-controller`): Telegram updates become durable jobs, workers
process them, and replies go back through the controller's delivery outbox.

## Layout

| Path | What |
|---|---|
| `tg-gateway/` | one gateway process per bot identity: ingress, admission, staging, delivery, progress messages |
| `rss-audio/` | audio podcast publishing and the scribe (transcription) topic |
| `tts/` | faithful (TTS) and explained (X-TTS) reads published as episodes |
| `meeting-summary/` | meeting audio to reviewed meeting notes |
| `transcribe-audio/` | Azure OpenAI transcription CLI, shared by rss-audio, meeting-summary and qmd2 |
| `shared/` | `tg_tools_common` contracts, `episode_enrichment`, `pi_harness` (model runners) |
| `services/` | route manifests, one per product |
| `systemd/` | the user units, installed verbatim |
| `config/` | templates for `~/.config/tg-tools/*.env` |
| `tg_tools_cli/` | `validate service` and `routes explain` for the manifests |
| `scripts/` | release, runtime, validation and maintenance tools |
| `tests/` | chain harness and repo-level tests |

## Documents

- `telegram-tools-operations.md`: deployment, configuration and runbooks.
- `config.md`: configuration files, environment keys and portable examples.
- `execution-contracts.md`: what the controller owns, artifact and delivery rules.
- `shared-progress.md`: live status messages.
- `tts-source-type-matrix.md`: TTS source types and failure classes.
- `dependencies.md`: pinned dependencies and Python runtimes.
- Product READMEs: `rss-audio/`, `tts/`, `meeting-summary/`, `transcribe-audio/`.

## Validation

```sh
python3 scripts/validate_tg_tools.py --quick   # unit tests, no chains
python3 scripts/validate_tg_tools.py --full    # plus chain tests against a private stack
```

Chain tests run the real gateway, workers and controller against a throwaway
Postgres and a local HTTP edge; they need the promoted runtime-controller
release and `pdftotext`.

Docs in this repo describe the current system. Plans, reviews and dated
records belong in your external operations archive.
