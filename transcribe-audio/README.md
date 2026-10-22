# transcribe-audio

Transcribe a local audio file or supported media URL to formatted text.

## Pipeline

```text
audio file or URL → rss-audio helper download (for links) → ffprobe (detect)
                  → ffmpeg compact ogg/opus (32kbps mono 16kHz) → chunk if >20MB
                  → Azure OpenAI deployment-specific transcription endpoint
                    using a bearer token from the dedicated service principal
                  → local cleanup (spacing + paragraph breaks, no added content)
```

## Usage

```bash
# Formatted to stdout
~/tools/tg-tools/transcribe-audio/transcribe.py recording.m4a

# A supported media URL works too
~/tools/tg-tools/transcribe-audio/transcribe.py 'https://x.com/i/status/1234567890123456789' -o clip.md

# Raw transcript only (skip local cleanup)
~/tools/tg-tools/transcribe-audio/transcribe.py recording.m4a --raw

# Write markdown/plain text to file
~/tools/tg-tools/transcribe-audio/transcribe.py recording.m4a -o transcript.md

# Pick the full Azure deployment
~/tools/tg-tools/transcribe-audio/transcribe.py recording.m4a --model full

# Parallel chunk uploads for long files
~/tools/tg-tools/transcribe-audio/transcribe.py long-meeting.m4a --jobs 4
```

## Offline enrichment review

`enrich.py` is an operator CLI for producing script, notes, manifest, and gate
artifacts in a local work directory without uploading or publishing them:

```bash
~/tools/tg-tools/transcribe-audio/enrich.py \
  --audio episode.mp3 --title "Episode title" \
  --workdir /tmp/enrich-review --published 2026-08-03
```

Reusing the same work directory resumes completed enrichment checkpoints.

## Auth

Transcription uses the dedicated Azure OpenAI service principal. Required:

- `AZURE_OPENAI_TRANSCRIPTION_ENDPOINT`
- `AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT`
- `AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT`
- `AZURE_OPENAI_TRANSCRIPTION_TENANT_ID`
- `AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID`
- `AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET`

Optional:

- `AZURE_OPENAI_TRANSCRIPTION_API_VERSION`
  Default: `2024-10-21`

Run as a CLI (qmd2 does, with a scrubbed environment) it loads, without
overriding anything already exported:

1. `~/.config/agent-secrets/azure-openai-transcription.env`
2. `~/.config/tg-tools/rss-audio.env` (source routes for URL inputs)

## Conversion details

- **Target format:** ogg/opus, 32kbps, mono, 16kHz
- **Why compact audio:** ~4 KB/s — a 60-min recording becomes ~14 MB, well under typical request limits
- **Chunking:** files over 10 min get split into 10-min segments
- **Concurrency:** chunk transcription can run in parallel with `--jobs`
- **Already Azure-ready and <20 MB?** Skips conversion entirely

## Dependencies

- `ffmpeg` + `ffprobe` (system)
- `python3` (stdlib only — no pip packages)
- For URL inputs: `yt-dlp`, plus the same network/mac helper env used by `rss-audio`

## Output contract

- `stdout` prints transcript text only
- `-o/--output` writes transcript text/markdown, not JSON
- default output uses deterministic local cleanup only
- `--raw` bypasses local cleanup and prints the exact joined transcript text

## Model selection

The default is the configured mini Azure deployment. `--model` takes a
deployment name or an alias that resolves to the configured mini or full
deployment: `mini`, `full`, `transcribe-mini`, `transcribe-full`,
`gpt-4o-mini-transcribe`, `gpt-4o-transcribe`.

This tool also reuses the URL download helper logic from
`~/tools/tg-tools/rss-audio`, so the standalone CLI and the Telegram scribe
flow accept the same kinds of links.
