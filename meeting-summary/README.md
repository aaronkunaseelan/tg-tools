# meeting-summary

Meeting audio in a Telegram topic becomes a reviewed meeting note.
Telegram ingress, review replies and the Accept callback belong to
`tg-gateway-ms` through `meeting_gateway_adapter.py`. Configuration:
`config/meeting-summary.env.example`. Operations:
`docs/telegram-tools-operations.md`.

## Pipeline

1. The gateway stages the audio and submits a `meeting_summary` job
   (`executionProfile=cpu_media`).
2. `runtime_worker.py` registers as a worker, claims the job, prepares and
   chunks the audio (`audio.py`), transcribes each chunk through
   `transcribe-audio/transcribe.py` on Azure OpenAI (`transcribe_endpoint.py`),
   and asks for a strict-JSON summary from the Codex responses endpoint with
   the `openai-codex` OAuth login in `~/.pi/agent/auth.json` (`summarizer.py`;
   `openai_auth.py` refreshes the token). No OpenAI API key is used.
3. `result_consumer.py` stores the note in `tg_meeting_summary.*`, then queues
   the review message, the Markdown preview and, once the preview is sent, the
   Accept button. It consumes the result only after the review is visible;
   receipts in `result_receipts.py` make each phase resumable.

## Storage

Notes live in database `qmd_gsd_runtime_v1` (`MEETING_SUMMARY_DATABASE_URL`),
schema `meeting-summary/schema.sql`: `meeting_inputs`, `meeting_notes`,
`meeting_note_reviews` and `meeting_note_appends`. Downstream readers use
`tg_meeting_summary.meeting_notes_snapshot_v1`, which admits only notes that are
ready, committed, hashed, have a document URL and are accepted.

## Review

1. The consumer creates review version 1 (date, title, attendees) and sends it
   with the full note as a Markdown preview.
2. Authorized replies to the review message edit `Date:`, `Title:` and
   `Attendees:` by label or line number `1`-`3`. Metadata-looking text that does
   not parse stays a metadata error; it never becomes an append.
3. Other authorized replies longer than 10 characters append notes, in order,
   with their Telegram message provenance. Before acceptance an append creates a
   new preview version; after acceptance it creates a new final revision
   without reopening the metadata.
4. Accept is version-fenced: stale callback data cannot accept replaced
   metadata or appends. Callback data is opaque and under 64 bytes.

## Manual commands

Run from `meeting-summary/` with the unit's environment loaded
(`set -a; . ~/tools/tools.env; . ~/.config/tg-tools/meeting-summary.env; set +a`):

```sh
../.venv/bin/python runtime_worker.py --once --json
../.venv/bin/python result_consumer.py --once --json
../.venv/bin/python replay_dead_job.py <dead-job-id> --reason "operator replay" --json
```

A replay keeps the job's `producerRef`/`sourceKey` and issues fresh idempotency
and effect keys, so the business identity is unchanged.

Bot commands: `/status` (recent jobs), `/queue` (pending, retryable, leased).
