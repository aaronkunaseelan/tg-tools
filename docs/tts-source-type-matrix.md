# TTS supported-source-type matrix

## Runtime source input union

A TTS/X-TTS job provides exactly one source form:

- `sourceText`: exact inline UTF-8 text;
- `sourceUrl`: an HTTP(S) fetch URL; or
- staged file metadata including `localSourcePath`.

Acquisition precedence is staged file, URL, then inline text. A text job
without `sourceText` is rejected.

## Fetch and conversion

`tts_common/source_fetch.py` fetches URLs; `tts_common/convert.py` converts by
effective type (see `tts/README.md`). Startup validates only the `pdftotext`
binary. The converter version (`tg-documents-v1`) is part of the execution
fingerprint.

## Effective type detection

Effective type precedence is deterministic:

1. strong supported byte signature (`%PDF-`, ZIP/OOXML central directory, HTML doctype/root markers);
2. specific non-generic declared MIME;
3. safe filename/URL suffix fallback;
4. clean UTF-8 text fallback only when no binary signature exists.

| Declared / suffix claim | Strong bytes | Effective type | Outcome |
|---|---|---|---|
| PDF | `%PDF-` | pdf | `pdftotext`, paragraphs re-joined |
| PDF | HTML + known challenge markers | html | `source_access_blocked` |
| PDF | HTML (other) | html | `source_type_mismatch` |
| PDF | random binary | none | `source_type_mismatch` |
| OOXML (docx/pptx/xlsx) | ZIP with matching content types | docx/pptx/xlsx | `unsupported_format` |
| OOXML | HTML | html | `source_access_blocked` or `source_type_mismatch` |
| HTML | HTML | html | visible text, `<main>`/`<article>` preferred |
| HTML | `%PDF-` | pdf | `pdftotext` (signature wins) |
| text/markdown | clean UTF-8 | as declared | passed through |
| json/xml/csv | clean UTF-8 | as declared | fenced code block |
| generic (`application/octet-stream`, none) | `%PDF-` | pdf | `pdftotext` |
| generic | clean UTF-8 | text | passed through |
| generic | other binary | none | `source_type_mismatch` |
| `.doc` or `application/msword` | any | none | `unsupported_format` |
| image MIME or image suffix | any | none | `unsupported_format` |
| any | over `TTS_MAX_SOURCE_BYTES` while reading | n/a | `source_too_large` |
| any | conversion over `TTS_MAX_EXTRACT_CHARS` | n/a | `extract_truncated` |
| any | conversion yields no text (e.g. scanned PDF) | n/a | `extract_insufficient_text` |
| any | `pdftotext` fails or times out | n/a | `extract_failed` |

The error taxonomy is frozen in `tts/tts_common/taxonomy.py`:
`source_access_blocked`, `source_too_large`, `source_type_mismatch`,
`source_fetch_transient`, `extract_insufficient_text`, `extract_truncated`,
and `lease_lost`. A missing `pdftotext` is a startup configuration failure,
not a per-source failure.
