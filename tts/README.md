# tts

Faithful reads (TTS topic) and explained reads (X-TTS topic) published as
podcast episodes. Telegram ingress and delivery belong to `tg-gateway-rss`;
`tts_worker` runs `tts_synthesize` and `x_tts_explain` jobs, and
`tts_worker.result_projector` turns their results into replies. The x-tts
script is written by `tts_reader/v4.py`. Configuration:
`config/tts.env.example`. Identity, publication and work state:
`docs/telegram-tools-operations.md`. Source types and failure classes:
`docs/tts-source-type-matrix.md`.

## Documents

TTS fetches and converts its own sources.

- **URLs**: `tts_common/source_fetch.py`. This is http/https only, with no
  userinfo. Every resolved address must be public, and the socket connects to
  the address that was checked. Each redirect hop is checked again (at most
  5). There is no proxy. The byte cap is enforced while streaming (decoded
  size for gzip), and the whole fetch has a 120 s deadline. `typed_fetch`
  maps failures onto the TTS taxonomy.
- **Conversion**: `tts_common/convert.py`, routed by the staged file's
  effective suffix.
  - Markdown and plain text pass through.
  - JSON, XML and CSV are fenced.
  - HTML becomes title, description and visible text, with nav, footer, aside
    and scripts dropped. `<main>`/`<article>` is used when it holds at least
    500 chars.
  - PDF goes through poppler `pdftotext` (reading order), with printed lines
    joined back into paragraphs.
  - Office files and images are `unsupported_format`.
  - Output over `TTS_MAX_EXTRACT_CHARS` (300k) is `extract_truncated`, never a
    silent slice.

The only external binary is `pdftotext` (`TTS_PDFTOTEXT_BIN`, or PATH). The
worker's `--startup-check` runs its version probe. Scanned PDFs without a text
layer fail as `extract_insufficient_text`, because there is no OCR.

Known limits:
- `pdftotext` interleaves side columns (correspondence and funding boxes) with
  the first-page abstract on some journal layouts.
- Tables come out as text runs, not markdown tables.
