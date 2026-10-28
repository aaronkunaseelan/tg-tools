# Telegram tools operations guide

How the tg-tools products run on the service host, how to deploy them, and what to check
when something goes wrong.

## Products and units

| Product | Telegram surface | Units (`systemd/`) | Durable state |
|---|---|---|---|
| rss-audio | publish topic, scribe (transcribe) topic | `tg-gateway-rss`, `rss-audio-runtime-worker`, `rss-audio-transcribe-runtime-worker` | runtime jobs/results/outbox, scribe SQLite, B2 episodes |
| tts / x-tts | TTS topic, X-TTS topic | `tg-gateway-rss`, `tts-worker`, `tts-result-projector` | runtime jobs/results/outbox, `TTS_WORK_DIR`, B2 episodes |
| meeting-summary | meeting topic, review replies and Accept button | `tg-gateway-ms`, `meeting-summary-runtime-worker`, `meeting-summary-result-consumer` | runtime jobs/results/outbox, `tg_meeting_summary.*` |

One gateway process serves one bot identity and owns all of its Telegram
ingress and delivery. `tg-gateway-rss` (bot identity `rss-audio`) serves the
`rss-audio` and `tts` manifests; `tg-gateway-ms` serves `meeting-summary`.
Manifests are discovered from `services/*.json` by `botIdentity`; the delivery
owners a gateway drains are the owners of its routes.

## Deployment

### Layout

| Path | What |
|---|---|
| `~/tools/tg-tools` | development checkout; never executed by a unit |
| `~/.local/lib/tg-tools/releases/<commit>` | immutable release, one per Git commit |
| `~/.local/lib/tg-tools/current` | symlink to the live release |
| `~/.local/lib/tg-tools/runtime` | symlink to the live Python runtime under `runtimes/` |
| `~/.config/tg-tools/<product>.env` | product configuration (0600) |
| `~/.config/systemd/user/*.service` | verbatim copies of the repo's `systemd/` units |

Units use only `%h` paths. Each loads the shared host files first
(`~/tools/tools.env` for the runtime controller, and
`~/.config/agent-secrets/credentials.env` or `azure-foundry.env` where needed),
then its product file(s) under `~/.config/tg-tools/`; later files win. Config
files must never set `PYTHONPATH`: an `EnvironmentFile=` value overrides the
unit's `Environment=PYTHONPATH` and would pin a stale release.

| Config file | Loaded by |
|---|---|
| `rss-audio.env` | rss-audio workers, `tg-gateway-rss` |
| `gateway-rss.env` | `tg-gateway-rss` |
| `tts.env` | `tts-worker`, `tts-result-projector` |
| `meeting-summary.env` | meeting-summary workers, `tg-gateway-ms` |
| `gateway-ms.env` | `tg-gateway-ms` |

Templates with every key are in `config/*.env.example`.

### Deploy a commit

```sh
cd ~/tools/tg-tools
python3 scripts/validate_tg_tools.py --full
git worktree add --detach /tmp/tg-release <commit>      # the script needs a clean tree
python3 scripts/materialize_tg_tools_release.py --repo /tmp/tg-release \
  --commit <full-sha> --releases-root ~/.local/lib/tg-tools/releases --promote
git worktree remove /tmp/tg-release
cp systemd/*.service ~/.config/systemd/user/           # only if units changed
systemctl --user daemon-reload
systemctl --user restart tg-gateway-rss tg-gateway-ms rss-audio-runtime-worker \
  rss-audio-transcribe-runtime-worker meeting-summary-runtime-worker \
  meeting-summary-result-consumer tts-worker tts-result-projector
```

Apply new `tg-gateway/migrations/*.sql` (numbered, each applied once) with
`psql -d runtime_controller -f` before restarting. Back up the affected schema
first when a migration drops anything.

Verify the running code, not the unit text: `current` must resolve to the
intended commit, and every unit must be active without a restart loop. For a
running process, `/proc/<pid>/environ` shows the effective `PYTHONPATH`.

Rollback: promote the previous release (`--commit <old-sha> --promote`) and
restart. Keep the live release plus one rollback release.

### Python runtime

`scripts/setup_python_runtime.py` builds a runtime from the hashed lock under
`runtimes/`; see `docs/dependencies.md`. Point the `runtime` symlink at it and
restart to switch.

## Everyday commands

```sh
systemctl --user list-units 'tg-gateway-*' 'rss-audio-*' 'meeting-summary-*' 'tts-*'
journalctl --user -u tg-gateway-rss -n 100 --no-pager
systemctl --user show -p EnvironmentFiles tts-worker
python3 -m tg_tools_cli routes explain --service tts --json      # from the checkout
```

## Telegram

### Sessions and credentials

Each gateway holds one Telethon bot session, by default
`~/.local/state/tg-tools/<identity>-bot.session` (`TG_GATEWAY_BOT_SESSION_PATH`).
Never open a gateway session from another client while its unit runs; a
`database is locked` error almost always means two clients share a session
file. `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` come from `credentials.env`. The
bot token is read from the variable named by `TG_GATEWAY_BOT_TOKEN_ENV`.

The rss-audio workers download Telegram files with the Bot API and, above
19 MB, with their own MTProto session at
`~/.local/share/telegram-mtproto/rss-audio-bot.session`.

### Admission

Admission is deterministic and local: `(bot identity, chat, thread, update
shape)` resolves to exactly one route and workload through the manifests and
`TG_GATEWAY_ROUTE_BINDINGS_JSON`. It never calls a network provider or model.
Topics are identified by chat and thread IDs, never titles. In production the
gateway also requires `TG_GATEWAY_ALLOWED_CHAT_ID` and
`TG_GATEWAY_ALLOWED_USER_IDS`.

### Runtime spine

```text
update -> tg_gateway.source_handoffs (pending -> staged -> submitted | terminal_rejected)
       -> runtime.source_receipts (metadata only) -> runtime.jobs
worker -> runtime.job_results -> projector/consumer -> runtime.delivery_outbox
gateway drains its owners' outbox rows -> Telegram
```

A handoff is written before staging or submission, and the gateway's drain
loop replays it idempotently until it converges. Stuck handoffs:

```sql
SELECT handoff_id, state, attempts, lease_expires_at, last_error
  FROM tg_gateway.source_handoffs
 WHERE state IN ('pending', 'staged') AND created_at < now() - interval '15 minutes';
```

Commands use `tg_gateway.handled_updates.disposition`: a new claim is
`uncertain` until its adapter returns. Exceptions or replay of an interrupted
claim enqueue a stable-key uncertainty notification, never repeat the command.
A process loss leaves its disposition available for reconciliation; inspect
effects before issuing a new command. Migration 007 adds this column, preserving
older handled markers. Source acknowledgments are sent only after their own
handoff is staged and submitted, not after a different queued row drains.

Job input carries exactly the source the worker consumes: top-level
`sourceText` or `sourceUrl`, or staged file fields. Receipts are metadata
only. Job rows therefore hold private text: never log full input JSON, and
treat backups and API responses as sensitive.

Telegram delivery is at-least-once: a crash after sending and before recording
can repeat one message. A dead outbox row is replayed by resetting it to
pending after diagnosing it; never edit a payload in place.

### Progress messages

With `TG_TOOLS_PROGRESS_ENABLED=1`, workers report stages and the gateway keeps
one status message per job for the workloads in
`TG_GATEWAY_PROGRESS_WORKLOADS`. See `docs/shared-progress.md`.

## rss-audio

Bot commands in the rss-audio topics: `/where` (chat, thread and matching
route bindings), `/feed`, `/list`, `/delete <slug>`, `/queue`,
`/model mini|full` (transcription deployment).

- **Publish topic**: a URL or audio becomes an `rss_publish` job: source audio
  (service-host proxy route first, remote download host as fallback), MP3, B2 upload,
  episode row on rss-edge. Enrichment (script and notes) is optional unless
  `RSS_AUDIO_ENRICHMENT_REQUIRED=true`; without it the audio still publishes.
- **Scribe topic**: a non-reply URL or audio starts a document and returns
  `v001` as `.md`. Replying to any linked message (source, append, status or a
  returned document) with text, audio or a URL appends and returns the next
  complete revision. Thread state lives in `RSS_AUDIO_SCRIBE_DB_PATH`.
- Failures write logs to `RSS_AUDIO_FAIL_LOG_DIR` (`~/tools/logs/rss-audio`).

Source routes have no defaults: a missing `RSS_AUDIO_MULLVAD_PROXY`,
`RSS_AUDIO_MAC_SSH` or `RSS_AUDIO_MAC_YTDLP_PATH` stops the workers at
startup. If downloads fail, check `yt-dlp`, `ffmpeg`, the proxy container and
the remote download route; if publication fails, check `rclone`, `RSS_AUDIO_API_TOKEN` and
the fail log.

## meeting-summary

- `tg-gateway-ms` accepts meeting audio, handles review replies and the Accept
  callback. Commands: `/status`, `/queue`.
- `meeting-summary-runtime-worker` runs `meeting_summary` jobs: transcription
  through `transcribe-audio/transcribe.py` (Azure OpenAI), summary through the
  `openai-codex` OAuth login in `~/.pi/agent/auth.json` (never an API key).
- `meeting-summary-result-consumer` stores the note in `tg_meeting_summary.*`
  (`MEETING_SUMMARY_DATABASE_URL`, your configured database), then queues
  the review message, the Markdown preview and the Accept button edit.

Notes are review-gated: they stay pending until an authorized user accepts the
current review version after its preview was delivered, and only accepted
notes enter `tg_meeting_summary.meeting_notes_snapshot_v1`. Reviewers edit
`Date:`, `Title:` and `Attendees:` (or lines `1`-`3`) by replying to the review
message; other replies longer than 10 characters append notes. Callback data
is opaque, versioned and under Telegram's 64-byte limit.

If audio is accepted but no review arrives, check the worker, then the result
consumer, then the controller, in that order. Schema: `meeting-summary/schema.sql`.

## TTS and X-TTS

A message, URL or document in the TTS topic becomes a faithful read; in the
X-TTS topic, an explained read. Sources are fetched and converted in-repo (see
`tts/README.md` and `docs/tts-source-type-matrix.md`); a URL that answers with
an access-challenge page fails as `source_access_blocked` before any model or
speech spend, and the user is asked to upload the file.

- **Identity**: an episode is its full execution fingerprint
  (`tts/tts_worker/fingerprint.py`); B2 keys are `tts/episodes/<fingerprint>/`.
  Any change to models, prompts, renderers or converter version changes the
  fingerprint, so earlier work is never reused for a different execution.
- **Publication**: four public roles under one prefix (`.mp3`, `.script.md`,
  `.notes.md`, `.manifest.json`), all uploaded before the episode row is
  installed. Uploads are immutable; a retry reuses the pinned slug, and a
  conflicting existing row is `publish_identity_conflict`, never an overwrite.
- **Speech**: Azure, one voice per job, one chunk at a time. Keys come from
  `~/.config/agent-secrets/azure-foundry.env`; startup refuses to run without
  a key source, but only a short synthesis proves the key is current.
- **Reader** (x-tts): `tts/tts_reader/v4.py` - one writer conversation, one
  whole-script review, at most one revision. Role models come from
  `shared/pi_harness/roles.py`, overridden by the JSON profile in
  `TTS_PI_ROLES_FILE`; a role resolves to `claude -p` (subscription login, API
  keys stripped) or `pi -p` (Codex OAuth). Startup checks every provider's auth.
- **Projector**: `tts-result-projector` is the only consumer of `tts`/`x-tts`
  results: it appends one stable-key delivery, then consumes the result.
- **Work state**: `TTS_WORK_DIR` holds per-source retry state
  (`source-<sha256(sourceKey)>`) and is never cleaned automatically.
  `scripts/reap_tts_work_roots.py` reclaims roots whose episode is installed
  (dry run unless `--apply`; it skips locked, recent and unfinished roots).
  Anything else in the work root is changed only with `tts-worker` stopped.

The worker holds the attempt lease from start through the finalize response.
`lease_lost` means no new external effect starts and late output is discarded;
look for `attempt lease` lines in the `tts-worker` journal.

The B2 bucket is private, but every path under it is public through the
rss-edge Worker: never store source media, logs or backups there.

## Troubleshooting

| Symptom | First checks |
|---|---|
| unit restarts immediately | `journalctl --user -u <unit>`, missing config key, missing binary |
| bot silent after restart | token variable, allowed chat/users, API id/hash, session file |
| `database is locked` (Telethon) | two clients on one session file |
| ingress works, no reply | handoff state, worker journal, projector/consumer, outbox row |
| wrong topic routing | `/where`, `TG_GATEWAY_ROUTE_BINDINGS_JSON`, `tg-tools routes explain` |
| large Telegram file fails | rss-audio MTProto session, Bot API 20 MB limit |
