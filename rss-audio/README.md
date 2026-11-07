# rss-audio

The `audio` and `surveillance` podcast feeds on the configured rss-edge host (`RSS_AUDIO_BASE_URL`), plus the scribe
(transcription) topic. Telegram ingress and delivery belong to
`tg-gateway-rss` through `rss_gateway_adapter.py`; the workers here execute
the jobs. Configuration: `config/rss-audio.env.example`. Operations:
`docs/telegram-tools-operations.md`.

## Two pipelines (`rss_publish`)

The worker (`runtime_worker.execute_runtime_job`) runs a publish job on its feed's pipeline
(`pipeline.PIPELINES`). Each pipeline is one short `run` that reads as its steps, and one chain test
drives it end to end on the private stack:

| pipeline | feed | steps | chain test |
|---|---|---|---|
| episodes: a Telegram file or link | `audio` | `episodes.py: run` | `tests/test_chain_rss_publish.py` |
| Surveillance: a scheduled show | `surveillance` | `surveillance.py: run` | `tests/test_chain_surveillance.py` |

A publish job ends with the episode live (mp3, script and segments) and submits the episode's notes
job (`notes_job.py`), which writes the notes and the page. The notes, page and rebuild jobs are later
steps both pipelines share, as are the steps in `pipeline.py`. What the steps hold to:

- Source audio: direct audio URLs and Telegram files are fetched directly; extractor URLs try OCI
  through the Mullvad proxy (rotating the exit on IP-shaped failures, `exit_rotation.py`), then the
  Mac over SSH with browser cookies. Route failures are classified retryable or terminal.
- Speech-to-text reuses a script checkpoint or transcribes once (`shared/episode_enrichment`). If it
  fails the audio still publishes alone; a configuration error fails the job.
- Uploads to `<RSS_AUDIO_B2_REMOTE>:<RSS_AUDIO_B2_BUCKET>/<feed>/episodes/` are immutable. The episode
  is installed through the Worker API with the script link and no notes.
- A Telegram publish queues one confirmation through the outbox (audio and script links);
  `result_projector.py` recovers a missed one from the durable result without republishing.
- The notes job (`inputKind: "notes"`, keyed on slug and version) reads the stored script and segments,
  never the source, writes notes and manifest once, and switches the live episode with one partial
  PUT (`md_url`, `manifest_url`, `description`). It sends nothing. A failed notes job leaves the
  episode live with its script. Notes jobs claim just below their publish job
  (`notes_job.notes_priority`: parent minus 5), so queued notes never hold a publish beyond the one
  in flight.
- The notes model is the tier of the feed's pipeline in `~/.config/tg-tools/enrichment.json`, read at
  the start of every job; a missing or wrong entry fails the job (`enrichment_config`) before any paid
  step.
- The page (`<prefix>[.v<n>].page.html`, `episode_enrichment/page.py`: the notes, then the script with
  linked paragraph ids and times, deterministic, no JavaScript) is an add-on: derived and uploaded
  allowing overwrite, and a page that fails is logged and left out of the PUT, so it never blocks the
  notes. When it succeeds, the switch PUT carries `page_url`. An episode that already has notes gets
  its page from a `page` job (`notes_job.run_page`), with no model call.

Every episode has one metadata-led prefix (proper nouns from creators, title,
series and channel, plus a stable identity suffix; `undated` when no date is
known):

```text
<prefix>.mp3  <prefix>.script.md  <prefix>.segments.json  <prefix>.notes.md  <prefix>.manifest.json
```

`script.md` is the speech-to-text transcript, verbatim. `segments.json` says when it was
spoken (`shared/episode_enrichment/timing.py`): each paragraph id's start and end in seconds,
plus the timed segments. Its `basis` is `words` (m3-asr's word timings) or `chunks` (the Azure
fallback, accurate to its 10-minute chunk). Without timings there is no file, and the manifest's
`script.timing` gives the reason. `notes.md` holds the summary, topic ranges and evidence-bound
references. The episode API calls notes `md_url` and the script `speech_text_url`; the segments
URL is in the manifest (`artifacts.segmentsUrl`). Notes and manifest arrive with the notes job;
until then the episode has its script and segments only.

Publication lookup uses the exact `slug` selector. A POST is never blindly
replayed: an uncertain create or a 400/409 duplicate triggers an exact
read-back, which returns the committed episode or keeps the error. GETs retry
once locally with jitter; other transient failures go back to the controller's
retry policy. Publication failures never rotate the VPN or switch downloaders.

## Rebuild (`rebuild.py`)

`rebuild.py SLUG --feed F [--segmenter NAME] [--context JSON] [--apply]` rebuilds one
episode's transcript and notes from its stored mp3, without refetching the source. It
is a dry run by default: it prints the plan and submits nothing.

The job (`inputKind: "rebuild"`, keyed on slug and version) works as follows:
- It runs speech-to-text on `<prefix>.mp3`, then uploads `<prefix>.v<n>.script.md`
  and `.v<n>.segments.json`. Uploads are immutable, so nothing is overwritten.
- It submits the notes job for version n. That job writes the `.v<n>` notes and
  manifest. One PUT then switches the episode's script, notes and manifest
  together, and only after that are the older versions' documents deleted. The
  mp3 stays. A rebuild that fails anywhere leaves the episode on its old documents.
- Only a confirmed-missing mp3 is terminal; a failed read is retried.
- The CLI prints what the controller did: `created`, or `existing` with that job's
  status. An existing dead job means the key is spent.
- `n` is fixed at submit time: the current script's version plus one, or 1 when
  the episode has no script yet.

`surveillance_backfill.py [--apply] [--only KEY ...]` is the X-76 backfill as one fixed
inventory, a dry run by default. It submits:
- rebuilds of the 3 Nov radio and TV and the 28/29 Oct radio;
- local-file publishes of the 30 Oct and 2 Nov radio windows captured in `/var/tmp/sv-capture`,
  with the timer's own title, date, segmenter and window context
  (`scheduled_sources.window_item`).
- publishes of the 14 TV shows from 14 Oct to 2 Nov, before the timer's `since`. They are pinned
  in the module, because the playlist feed lists only its newest 15 entries. Each is the nightly's
  own item (`scheduled_sources.playlist_item`, keyed `surveillance-tv:<video id>`).

`--only` takes item keys or `[SOURCE:]FROM[..TO]` air-day ranges, to pace the run (for example
`--only surveillance-tv:2026-10-14..2026-10-16`). An item that already has a succeeded or running
job is reported, not submitted again.

All of it runs below the scheduled publishes. Both CLIs read the running worker unit's
settings (`config.worker_settings`).

## Feeds and job options

The worker publishes to the feeds in `pipeline.FEEDS` (feed id -> B2 prefix, keyed-root env var):
`audio` (Telegram, the default) and `surveillance` (`SURVEILLANCE_PUBLIC_ROOT`). A job input
`feed` selects it (unknown = terminal `input` error); the B2 key is `<prefix>/episodes/<file>`
and the API body, the exact-slug lookup and message links follow. Other optional inputs:
`publishedAt` (ISO UTC; the episode's `pub_date`, its date in the slug, notes and manifest, with
`dateBasis` `scheduled_source`), `title` (the whole episode title, so channel, creators and series
no longer prefix it), `delivery: "none"` (no Telegram confirmation: the worker logs INFO and the
result projector consumes the result), and the Surveillance pipeline's `liveWindow` (below).

## Surveillance

A Surveillance publish carries one of: `liveWindow` (the radio show, a window of the live stream),
`sourceUrl` (the TV show, the playlist's video) or `localSourcePath` (a radio window captured earlier,
from the backfill). The `liveWindow` input:

```json
{"videoId": "f39oHo6vFLg", "start": "2026-11-03T12:00:00Z", "seconds": 10800, "expectOpening": "Bloomberg Surveillance"}
```

`liveWindow` (`live_window.py`, OCI Mullvad route only) cuts a window out of a live stream's DASH
buffer: yt-dlp lists itag 140, segment `N` is `<url>&sq=N`, and the stream-head headers of any
segment response (a 404 too) give the segment length and map the start to a segment number. The
window must be over at the head (else retryable); a first segment older than the buffer is terminal
(`window_outside_buffer`). The first two minutes are transcribed before the rest is fetched and
must contain `expectOpening` (else terminal `window_not_show`, nothing published). Identity is
`videoId@start`, not the bytes.

Unattended jobs (`delivery: none`) retry a sign-in/bot wall from yt-dlp, which on this cookie-less
route belongs to the proxy exit: a dead job is never resubmitted (its idempotency key is spent), and
no one is waiting. The stream lookup of a `liveWindow` rotates the Mullvad exit on such a refusal,
as a URL job does. Telegram jobs still fail at once on a sign-in wall.

### Scheduled sources

`scheduled_sources.py` (unit `rss-audio-schedule.timer`, 01:30 UTC) reads
`config/scheduled-sources.json` (in git, no secrets). Each source lists what is due now:
`youtube_playlist` (Atom feed entries whose title matches `titleRegex`, within `lookbackDays` and
since `since`) or `youtube_live_window` (weekday windows in a time zone, due once the window ended
`settleMinutes` ago: `surveillance.window_days`). Each item is submitted like a Telegram message (source receipt with
`source_system="schedule"`, job, link) with idempotency key `<source id>:<video id | air date>`,
`delivery: "none"`, priority 20 (Telegram jobs are 60) and three attempts, then its job is read and
one line printed: `published`, `queued`, `running`, `skipped (window_not_show: <what was heard>)` or
`failed <errorClass>`. The opening match compares letters and digits only, so punctuation in the
transcript does not matter. The exit status is nonzero if a source cannot be listed or any job failed
other than `window_not_show`. A skip is a dead job, so tg-tools telemetry shows it and the supervisor
checks whether the show really did not air (X-75). A dead item fails
only the first run that sees it dead (it ended within 36 h), not every night of the lookback. A
nonzero exit fires `lane-alert@` (installed by the reading lane; see the operations guide). `python scheduled_sources.py --list [--now ISO]` prints what is due and submits
nothing.

A source's optional `segmenter` names how its episodes' notes split into sections
(`surveillance_seams.SEGMENTERS`, by `surveillance.named_segmenter`; an unknown name fails the inventory,
and a notes or rebuild job before any read). The Surveillance sources use
`surveillance-tv` and `surveillance-radio` (`surveillance_seams.py`). These split the 3-hour show
at its own hour opens, following the accepted seam study, into `## Hour n` sections. Each
section records the rule that began it: `seam`, `fallback` or `estimated`. A window source's
items also carry `segmentContext` (window start and time zone), so radio hours are labelled
on New York's wall clock.

`bin/surveillance-tui` (`surveillance_tui.py`, stdlib curses) follows every Surveillance item through the pipe
(queued, window/download, opening, STT, upload, feed, then the notes job) and, on Enter, the files at each stage: work dir, B2
objects, feed row, job, STT engine and the opening check. It is read only and uses the settings of the running
`rss-audio-runtime-worker`; a source it cannot read shows `unknown: <reason>`. The episode API and B2 are read at most once a minute (`r` reads them at once); the list shows `feed read <age> ago`. `--once` prints the list and one item.

## Jobs

`bin/rss-audio-jobs` (`jobs_view.py`) lists the rss-audio jobs on the runtime controller (newest first, `--status`, `--limit`), their counts by kind and status, and the rss-audio workers; `--job ID` shows one job, `--json` the same as one object. Read only (GET requests); an API failure prints `unknown: <reason>` and exits 1; no URL or 28+ character token is printed.

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
