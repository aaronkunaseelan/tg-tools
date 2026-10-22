# rss-audio

The `audio` podcast feed on the configured rss-edge host (`RSS_AUDIO_BASE_URL`), plus the scribe
(transcription) topic. Telegram ingress and delivery belong to
`tg-gateway-rss` through `rss_gateway_adapter.py`; the workers here execute
the jobs. Configuration: `config/rss-audio.env.example`. Operations:
`docs/telegram-tools-operations.md`.

## Publish (`rss_publish`, `runtime_worker.py`)

1. Source audio: direct audio URLs and Telegram files are fetched directly;
   extractor URLs try OCI through the Mullvad proxy (rotating the exit on
   IP-shaped failures, `exit_rotation.py`), then the Mac over SSH with browser
   cookies. Route failures are classified retryable or terminal.
2. Produce and validate an MP3.
3. Enrich: reuse a script checkpoint or transcribe once, then write separate
   script, notes and manifest documents (`shared/episode_enrichment`). If
   enrichment fails the audio still publishes without script and notes links,
   unless `RSS_AUDIO_ENRICHMENT_REQUIRED=true`.
4. Upload every artifact to `<RSS_AUDIO_B2_REMOTE>:<RSS_AUDIO_B2_BUCKET>/audio/episodes/`
   (immutable), then install the episode through the Worker API.
5. Queue one Telegram confirmation through the outbox; `result_projector.py`
   recovers a missed one from the durable result without republishing.

Every episode has one metadata-led prefix (proper nouns from creators, title,
series and channel, plus a stable identity suffix; `undated` when no date is
known):

```text
<prefix>.mp3  <prefix>.script.md  <prefix>.notes.md  <prefix>.manifest.json
```

`script.md` is the speech-to-text transcript, verbatim. `notes.md` holds the
summary, topic ranges and evidence-bound references. The episode API calls
notes `md_url` and the script `speech_text_url`.

Publication lookup uses the exact `slug` selector. A POST is never blindly
replayed: an uncertain create or a 400/409 duplicate triggers an exact
read-back, which returns the committed episode or keeps the error. GETs retry
once locally with jitter; other transient failures go back to the controller's
retry policy. Publication failures never rotate the VPN or switch downloaders.

## Scribe (`transcribe_audio`, `transcribe_runtime_worker.py`)

A non-reply voice note, audio file or media URL in the scribe topic starts a
document and returns `v001` as a `.md` file. A reply to any linked message (the
source, an append, a status message or a returned document) with text, audio
or a URL appends and returns the next complete revision. Transcription runs
`transcribe-audio/transcribe.py` on the Azure deployment chosen by `/model`.
Thread state lives in `RSS_AUDIO_SCRIBE_DB_PATH`
(`~/.local/share/tg-tools/rss-audio/scribe.sqlite3`).

## Telegram files

Files up to 19 MB come through the Bot API (`RSS_AUDIO_BOT_TOKEN`); larger ones
through MTProto (`mtproto_dl.py`, `TELEGRAM_API_ID`/`HASH`) with its own session
at `~/.local/share/telegram-mtproto/rss-audio-bot.session`, guarded by a
sidecar lock both workers share.

## Commands

`/where`, `/feed`, `/list`, `/delete <slug>`, `/queue`, `/model mini|full`.
Topic bindings are gateway configuration (`TG_GATEWAY_ROUTE_BINDINGS_JSON`).
