#!/usr/bin/env python3
"""
transcribe-audio: Transcribe a local audio file or supported URL to formatted text.

Pipeline:
  1. URL helper   → optional fetch/download to MP3 via rss-audio helper logic
  2. ffprobe      → detect duration, codec, bitrate
  3. ffmpeg       → convert to compact Azure-friendly audio
                  → ogg/opus 32k mono 16kHz
                  → split into compact chunks when needed
  4. Azure OpenAI → deployment-specific audio transcriptions endpoint
                  → bearer token from the dedicated service principal env
  5. Local        → deterministic cleanup + paragraph formatting
                  → spacing, punctuation, paragraph breaks only

Auth:
  Required env:
    $AZURE_OPENAI_TRANSCRIPTION_ENDPOINT
    $AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT
    $AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT
    $AZURE_OPENAI_TRANSCRIPTION_TENANT_ID
    $AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID
    $AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET

Usage:
    transcribe-audio <audio-file-or-url>              # formatted to stdout
    transcribe-audio <audio-file-or-url> --raw        # skip local cleanup
    transcribe-audio <audio-file-or-url> -o out.md    # formatted to file
    transcribe-audio <audio-file-or-url> --model full # use full deployment alias
    transcribe-audio <audio-file-or-url> --jobs 4     # parallel chunk uploads

--model accepts a deployment name or an alias that resolves to the configured
mini or full deployment: mini, full, gpt-4o-mini-transcribe, gpt-4o-transcribe.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("transcribe-audio")

TG_TOOLS_ROOT = Path(__file__).resolve().parents[1]
RSS_AUDIO_HELPER_DIR = TG_TOOLS_ROOT / "rss-audio"
# Run as a CLI (qmd2 does, with a scrubbed env) it loads its own settings:
# the Azure service principal, and rss-audio's source route for URL inputs.
# Explicit environment always wins.
LOCAL_ENV_PATHS = (
    Path.home() / ".config" / "agent-secrets" / "azure-openai-transcription.env",
    Path.home() / ".config" / "tg-tools" / "rss-audio.env",
)
# Consumers run this file as a subprocess with a scrubbed env (no PYTHONPATH),
# so it puts its own helper dirs on the path: rss-audio, and shared for tg_tools_common.
for helper_dir in (RSS_AUDIO_HELPER_DIR, TG_TOOLS_ROOT / "shared"):
    if str(helper_dir) not in sys.path:
        sys.path.insert(0, str(helper_dir))

from source_audio import (  # noqa: E402
    SourceAudioError,
    SourceToolSettings,
    classify_text,
    download_source_to_mp3,
    stable_url_for_display,
)

# ── Config ────────────────────────────────────────────────────────────────────

AZURE_OPENAI_TRANSCRIPTION_ENDPOINT_ENV = "AZURE_OPENAI_TRANSCRIPTION_ENDPOINT"
AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT_ENV = "AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT"
AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT_ENV = "AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT"
AZURE_OPENAI_TRANSCRIPTION_TENANT_ID_ENV = "AZURE_OPENAI_TRANSCRIPTION_TENANT_ID"
AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID_ENV = "AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID"
AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET_ENV = "AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET"
AZURE_OPENAI_TRANSCRIPTION_API_VERSION_ENV = "AZURE_OPENAI_TRANSCRIPTION_API_VERSION"
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
AZURE_OPENAI_TRANSCRIPTION_API_VERSION_DEFAULT = "2024-10-21"

TRANSCRIBE_AUDIO_MODEL_ENV = "TRANSCRIBE_AUDIO_MODEL"
TRANSCRIBE_AUDIO_MODEL_MINI_ENV = "TRANSCRIBE_AUDIO_MODEL_MINI"
TRANSCRIBE_AUDIO_MODEL_FULL_ENV = "TRANSCRIBE_AUDIO_MODEL_FULL"

TRANSCRIBE_AUDIO_YTDLP_BIN_ENV = "TRANSCRIBE_AUDIO_YTDLP_BIN"
TRANSCRIBE_AUDIO_MAC_SSH_ENV = "TRANSCRIBE_AUDIO_MAC_SSH"
TRANSCRIBE_AUDIO_MAC_YTDLP_PATH_ENV = "TRANSCRIBE_AUDIO_MAC_YTDLP_PATH"
TRANSCRIBE_AUDIO_MULLVAD_PROXY_ENV = "TRANSCRIBE_AUDIO_MULLVAD_PROXY"

TRANSCRIPTION_PROMPT = (
    "Transcribe this audio faithfully. Return only the transcript text. "
    "Do not add headings, summaries, speaker labels, timestamps, or commentary."
)
FFMPEG_BIN = os.environ.get("TRANSCRIBE_AUDIO_FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("TRANSCRIBE_AUDIO_FFPROBE_BIN", "ffprobe")

# ffmpeg conversion target:
# - Azure OpenAI transcription path  → ogg/opus 32k mono 16kHz
OPUS_BITRATE = "32k"
OPUS_SAMPLE_RATE = 16000
CHUNK_MAX_BYTES = 20 * 1024 * 1024  # keep audio payloads compact for model/provider compatibility

# 10-minute chunks are the sweet spot:
# - At 32kbps opus → ~2.3 MB per chunk
# - Short chunks are faster and more reliable
# - Chunking anything over 10 min avoids large single-request payloads
CHUNK_DURATION_SEC = 10 * 60  # 10 min per chunk
CHUNK_THRESHOLD_SEC = 10 * 60  # chunk if duration > 10 min

ACCEPTED_INPUT = {
    ".ogg", ".oga", ".mp3", ".wav", ".m4a", ".flac", ".webm",
    ".mp4", ".mpeg", ".mpga", ".aac", ".wma", ".opus", ".amr",
    ".3gp", ".caf", ".aiff", ".aif",
}

DEFAULT_JOBS = 3
MIN_PARAGRAPH_CHARS = 160
TARGET_PARAGRAPH_CHARS = 420
MAX_PARAGRAPH_CHARS = 700
FALLBACK_WORDS_PER_PARAGRAPH = 90

SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
SPACE_AFTER_PUNCT_RE = re.compile(r"([,;:])(?=[A-Za-z])")
SENTENCE_SPACE_RE = re.compile(r"([.!?])(?=[A-Z])")
WHITESPACE_RE = re.compile(r"\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# ── Local env + auth loading ─────────────────────────────────────────────────

def _load_local_env() -> None:
    for env_path in LOCAL_ENV_PATHS:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class AzureOpenAIConfig:
    endpoint: str
    api_version: str
    mini_deployment: str
    full_deployment: str
    tenant_id: str
    client_id: str
    client_secret: str


def load_azure_openai_config() -> AzureOpenAIConfig:
    required_env = (
        AZURE_OPENAI_TRANSCRIPTION_ENDPOINT_ENV,
        AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT_ENV,
        AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT_ENV,
        AZURE_OPENAI_TRANSCRIPTION_TENANT_ID_ENV,
        AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID_ENV,
        AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET_ENV,
    )
    values: dict[str, str] = {}
    missing: list[str] = []
    for env_var in required_env:
        value = (os.environ.get(env_var) or "").strip()
        if value:
            values[env_var] = value
        else:
            missing.append(env_var)

    if missing:
        log.error("Missing Azure OpenAI transcription env vars: %s", ", ".join(missing))
        sys.exit(1)

    api_version = (
        os.environ.get(AZURE_OPENAI_TRANSCRIPTION_API_VERSION_ENV)
        or AZURE_OPENAI_TRANSCRIPTION_API_VERSION_DEFAULT
    ).strip()
    return AzureOpenAIConfig(
        endpoint=values[AZURE_OPENAI_TRANSCRIPTION_ENDPOINT_ENV].rstrip("/"),
        api_version=api_version,
        mini_deployment=values[AZURE_OPENAI_TRANSCRIPTION_MINI_DEPLOYMENT_ENV],
        full_deployment=values[AZURE_OPENAI_TRANSCRIPTION_FULL_DEPLOYMENT_ENV],
        tenant_id=values[AZURE_OPENAI_TRANSCRIPTION_TENANT_ID_ENV],
        client_id=values[AZURE_OPENAI_TRANSCRIPTION_CLIENT_ID_ENV],
        client_secret=values[AZURE_OPENAI_TRANSCRIPTION_CLIENT_SECRET_ENV],
    )


def _transcribe_model_defaults(config: AzureOpenAIConfig) -> tuple[str, str, str]:
    mini = (os.environ.get(TRANSCRIBE_AUDIO_MODEL_MINI_ENV) or "").strip() or config.mini_deployment
    full = (os.environ.get(TRANSCRIBE_AUDIO_MODEL_FULL_ENV) or "").strip() or config.full_deployment
    default = (os.environ.get(TRANSCRIBE_AUDIO_MODEL_ENV) or "").strip() or mini
    return default, mini, full


def resolve_transcribe_model(model: str | None, config: AzureOpenAIConfig) -> str:
    requested = (model or "").strip()
    default_model, mini_model, full_model = _transcribe_model_defaults(config)
    aliases = {
        "mini": mini_model,
        "full": full_model,
        "transcribe-mini": mini_model,
        "transcribe-full": full_model,
        "gpt-4o-mini-transcribe": mini_model,
        "gpt-4o-transcribe": full_model,
        "openai/gpt-4o-mini-transcribe": mini_model,
        "openai/gpt-4o-transcribe": full_model,
    }
    if not requested:
        return default_model
    return aliases.get(requested.lower(), requested)


def fetch_azure_access_token(config: AzureOpenAIConfig) -> str:
    authority = (os.environ.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com").rstrip("/")
    token_url = f"{authority}/{config.tenant_id}/oauth2/v2.0/token"
    payload = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": AZURE_OPENAI_SCOPE,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:500]
        log.error("Azure token request failed with HTTP %s: %s", e.code, body_text)
        sys.exit(1)
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
        log.error("Azure token request failed: %s", e)
        sys.exit(1)

    access_token = (data.get("access_token") or "").strip()
    if not access_token:
        log.error("Azure token response did not include access_token")
        sys.exit(1)
    return access_token


def _env_first(*keys: str, default: str) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return default


def load_source_tool_settings() -> SourceToolSettings:
    # Host addresses have no built-in default on purpose: this tool is pinned to
    # the OCI/Mullvad route with no health check and no fallback, so a stale
    # default would fail at download time instead of at configuration time.
    mullvad_proxy = _env_first(
        TRANSCRIBE_AUDIO_MULLVAD_PROXY_ENV,
        "RSS_AUDIO_MULLVAD_PROXY",
        default="",
    )
    if not mullvad_proxy.strip():
        raise ValueError(
            f"{TRANSCRIBE_AUDIO_MULLVAD_PROXY_ENV} or RSS_AUDIO_MULLVAD_PROXY is required"
        )
    return SourceToolSettings(
        mac_ssh_target=_env_first(
            TRANSCRIBE_AUDIO_MAC_SSH_ENV,
            "RSS_AUDIO_MAC_SSH",
            default="",
        ),
        mac_ytdlp_path=_env_first(
            TRANSCRIBE_AUDIO_MAC_YTDLP_PATH_ENV,
            "RSS_AUDIO_MAC_YTDLP_PATH",
            default="",
        ),
        yt_dlp_bin=_env_first(
            TRANSCRIBE_AUDIO_YTDLP_BIN_ENV,
            "RSS_AUDIO_YTDLP_BIN",
            default="yt-dlp",
        ),
        ffmpeg_bin=FFMPEG_BIN,
        mullvad_proxy=mullvad_proxy,
    )


def resolve_source_to_path(source: str, tmpdir: str) -> str:
    classified = classify_text(source)
    if classified:
        _, url = classified
        output_path = Path(tmpdir) / "source.mp3"
        log.info("Fetching URL source: %s", stable_url_for_display(url))
        try:
            download_source_to_mp3(load_source_tool_settings(), url, output_path)
        except SourceAudioError as exc:
            log.error("Source fetch failed at %s: %s", exc.stage, exc)
            if exc.stderr:
                log.error(exc.stderr[:500])
            sys.exit(1)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            log.error("Downloaded source is empty")
            sys.exit(1)
        return str(output_path)

    audio_path = os.path.expanduser(source)
    if not os.path.isfile(audio_path):
        log.error(f"File not found: {audio_path}")
        sys.exit(1)
    return audio_path


# ── ffmpeg / ffprobe ──────────────────────────────────────────────────────────

def _ensure_tool(command: str, env_key: str) -> None:
    candidate = (command or "").strip()
    if not candidate:
        log.error(f"{env_key} is empty")
        sys.exit(1)
    if "/" in candidate:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return
        log.error(f"{env_key} is not executable: {expanded}")
        sys.exit(1)
    if shutil.which(candidate):
        return
    log.error(f"Required tool not found: {candidate} (set {env_key})")
    sys.exit(1)


def ensure_dependencies() -> None:
    _ensure_tool(FFPROBE_BIN, "TRANSCRIBE_AUDIO_FFPROBE_BIN")
    _ensure_tool(FFMPEG_BIN, "TRANSCRIBE_AUDIO_FFMPEG_BIN")


def probe(path: str) -> dict:
    """Return ffprobe JSON for the file."""
    r = subprocess.run(
        [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        log.error(f"ffprobe failed: {r.stderr[:300]}")
        sys.exit(1)
    return json.loads(r.stdout)


def prepare_chunks(audio_path: str, tmpdir: str) -> list[str]:
    """Convert to Azure-friendly chunks and return chunk paths."""
    info = probe(audio_path)
    duration = float(info["format"].get("duration", 0))
    size = int(info["format"].get("size", 0))
    codec = ""
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            codec = s.get("codec_name", "")
            break

    log.info(
        f"Input: {duration:.0f}s, {size / 1024 / 1024:.1f} MB, codec={codec}"
    )

    ext = os.path.splitext(audio_path)[1].lower()
    ready_direct = ext in {".ogg", ".opus"} and codec == "opus"
    ready_message = "Already ogg/opus, short, and under 20 MB — skipping conversion"
    target_ext = "ogg"
    target_label = "opus"
    encode_args = [
        "-c:a", "libopus", "-b:a", OPUS_BITRATE,
        "-ar", str(OPUS_SAMPLE_RATE), "-ac", "1",
    ]

    if ready_direct and size <= CHUNK_MAX_BYTES and duration <= CHUNK_THRESHOLD_SEC:
        log.info(ready_message)
        return [audio_path]

    # Short file — single conversion, no chunking needed
    if duration <= CHUNK_THRESHOLD_SEC:
        out = os.path.join(tmpdir, f"full.{target_ext}")
        cmd = [
            FFMPEG_BIN, "-y", "-v", "warning",
            "-i", audio_path,
            *encode_args,
            "-vn",  # strip any video/artwork
            out,
        ]
        estimated_bytes = (32000 / 8) * duration
        log.info(f"Converting to {target_label} ({estimated_bytes / 1024 / 1024:.1f} MB est)...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log.error(f"ffmpeg failed: {r.stderr[:500]}")
            sys.exit(1)
        actual = os.path.getsize(out)
        log.info(f"Converted: {actual / 1024 / 1024:.1f} MB")
        return [out]

    # Split into chunks using seek + duration per chunk.
    # Encoding each chunk independently avoids non-zero start_time offsets that
    # some audio-input providers reject.
    n_chunks = int(duration / CHUNK_DURATION_SEC) + 1
    log.info(f"Splitting into ~{n_chunks} chunks ({CHUNK_DURATION_SEC}s each)...")

    chunks: list[str] = []
    offset = 0.0
    idx = 0
    while offset < duration:
        out = os.path.join(tmpdir, f"chunk_{idx:03d}.{target_ext}")
        cmd = [
            FFMPEG_BIN, "-y", "-v", "warning",
            "-ss", str(offset),
            "-i", audio_path,
            "-t", str(CHUNK_DURATION_SEC),
            *encode_args,
            "-vn",
            out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.error(f"ffmpeg chunk {idx} failed: {r.stderr[:500]}")
            sys.exit(1)

        sz = os.path.getsize(out)
        if sz < 1000:
            # Empty/negligible tail chunk — skip
            os.unlink(out)
            break
        log.info(f"  chunk_{idx:03d}.{target_ext}: {sz / 1024 / 1024:.1f} MB")
        chunks.append(out)
        offset += CHUNK_DURATION_SEC
        idx += 1

    return chunks


# ── Azure OpenAI transcription ───────────────────────────────────────────────

def _audio_content_type(chunk_path: str) -> str:
    ext = Path(chunk_path).suffix.lower()
    return {
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }.get(ext, "application/octet-stream")


def transcribe_chunk(
    config: AzureOpenAIConfig,
    access_token: str,
    chunk_path: str,
    model: str,
) -> str | None:
    """Transcribe one audio chunk via Azure OpenAI."""
    filename = os.path.basename(chunk_path)
    boundary = f"----FormBoundary{int(time.time() * 1000)}"

    with open(chunk_path, "rb") as f:
        audio_data = f.read()

    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {_audio_content_type(chunk_path)}\r\n\r\n".encode()
    body += audio_data + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += f"{model}\r\n".encode()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
    body += b"json\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="prompt"\r\n\r\n'
    body += f"{TRANSCRIPTION_PROMPT}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()

    transcription_url = (
        f"{config.endpoint}/openai/deployments/{urllib.parse.quote(model, safe='')}"
        f"/audio/transcriptions?api-version={config.api_version}"
    )
    req = urllib.request.Request(
        transcription_url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        data = json.loads(resp.read())
        text = (data.get("text") or "").strip()
        if not text:
            usage = data.get("usage", {})
            log.warning(f"Empty transcription response for {filename}; usage={usage}")
            return ""
        return text
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:500]
        log.error("Azure OpenAI transcription error %s: %s", e.code, body_text)
        return None
    except (ValueError, json.JSONDecodeError, KeyError, TypeError, urllib.error.URLError) as e:
        log.error(f"Transcription response parse failed for {filename}: {e}")
        return None


def _transcribe_indexed(
    config: AzureOpenAIConfig,
    access_token: str,
    chunk_path: str,
    model: str,
    index: int,
    total: int,
) -> tuple[int, str | None]:
    sz = os.path.getsize(chunk_path)
    tag = f"[{index + 1}/{total}]" if total > 1 else ""
    log.info(
        f"Transcribing {tag} {os.path.basename(chunk_path)} "
        f"({sz / 1024:.0f} KB) with Azure deployment {model}..."
    )
    text = transcribe_chunk(config, access_token, chunk_path, model)
    if text is not None:
        log.info(f"  → chunk {index + 1}: {len(text)} chars")
    return index, text


def transcribe_all(
    config: AzureOpenAIConfig,
    access_token: str,
    chunks: list[str],
    model: str,
    jobs: int = DEFAULT_JOBS,
    on_progress=None,
) -> str | None:
    """Transcribe all chunks and concatenate in source order."""
    if not chunks:
        return None

    worker_count = max(1, min(jobs, len(chunks)))
    if worker_count == 1:
        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            _, text = _transcribe_indexed(config, access_token, chunk, model, i, len(chunks))
            if text is None:
                log.error(f"Chunk {i + 1} failed")
                return None
            parts.append(text)
            if on_progress:
                on_progress(len(parts), len(chunks))
        return "\n\n".join(part for part in parts if part)

    parts: list[str | None] = [None] * len(chunks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(_transcribe_indexed, config, access_token, chunk, model, i, len(chunks))
            for i, chunk in enumerate(chunks)
        ]
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            index, text = future.result()
            if text is None:
                log.error(f"Chunk {index + 1} failed")
                return None
            parts[index] = text
            completed += 1
            if on_progress:
                on_progress(completed, len(chunks))

    return "\n\n".join(part for part in parts if part)


# ── Local formatting ──────────────────────────────────────────────────────────

def _split_unpunctuated_words(text: str) -> list[str]:
    """Fallback paragraph splitter for transcripts with little punctuation."""
    words = text.split()
    if not words:
        return []
    return [
        " ".join(words[i:i + FALLBACK_WORDS_PER_PARAGRAPH])
        for i in range(0, len(words), FALLBACK_WORDS_PER_PARAGRAPH)
    ]


def _normalize_transcript_text(transcript: str) -> str:
    text = transcript.strip()
    if not text:
        return ""
    text = WHITESPACE_RE.sub(" ", text)
    text = SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = SPACE_AFTER_PUNCT_RE.sub(r"\1 ", text)
    text = SENTENCE_SPACE_RE.sub(r"\1 ", text)
    return text.strip()


def format_transcript(transcript: str) -> str:
    """Deterministically clean spacing and split transcript into paragraphs."""
    text = _normalize_transcript_text(transcript)
    if not text:
        return ""
    if len(text) <= MIN_PARAGRAPH_CHARS:
        return text

    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        return "\n\n".join(_split_unpunctuated_words(text))

    paragraphs: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)
        next_len = current_len + sentence_len + (1 if current else 0)
        should_flush = (
            current
            and (
                next_len > MAX_PARAGRAPH_CHARS
                or (current_len >= TARGET_PARAGRAPH_CHARS and sentence_len >= 40)
                or (len(current) >= 4 and current_len >= MIN_PARAGRAPH_CHARS)
            )
        )
        if should_flush:
            paragraphs.append(" ".join(current))
            current = [sentence]
            current_len = sentence_len
            continue

        current.append(sentence)
        current_len = next_len

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Transcribe a local audio file or supported URL to formatted text"
    )
    parser.add_argument("source", help="Path to audio file or supported URL")
    parser.add_argument(
        "--raw", action="store_true",
        help="Output raw transcript (skip local cleanup)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Write result to file instead of stdout",
    )
    parser.add_argument(
        "--model",
        default="",
        help=(
            "Azure deployment name or alias. "
            "Default is the configured mini deployment. "
            "Aliases: mini, full, transcribe-mini, transcribe-full, "
            "gpt-4o-mini-transcribe, gpt-4o-transcribe."
        ),
    )
    parser.add_argument(
        "--jobs", type=int, default=DEFAULT_JOBS,
        help=f"Parallel chunk transcription jobs (default: {DEFAULT_JOBS})",
    )
    args = parser.parse_args()

    _load_local_env()
    ensure_dependencies()

    with tempfile.TemporaryDirectory(prefix="transcribe-") as tmpdir:
        audio_path = resolve_source_to_path(args.source, tmpdir)

        ext = os.path.splitext(audio_path)[1].lower()
        if ext and ext not in ACCEPTED_INPUT:
            log.warning(f"Unknown extension '{ext}' — will attempt conversion anyway")

        size = os.path.getsize(audio_path)
        if size == 0:
            log.error("File is empty")
            sys.exit(1)

        log.info(f"Input: {audio_path} ({size / 1024 / 1024:.1f} MB)")

        config = load_azure_openai_config()
        access_token = fetch_azure_access_token(config)

        # Step 1: Convert + chunk
        chunks = prepare_chunks(audio_path, tmpdir)

        # Step 2: Transcribe
        resolved_model = resolve_transcribe_model(args.model, config)
        if args.model and resolved_model != args.model:
            log.info("Resolved Azure deployment alias %s -> %s", args.model, resolved_model)
        elif not args.model:
            log.info("Selected default Azure transcription deployment: %s", resolved_model)
        transcript = transcribe_all(config, access_token, chunks, resolved_model, jobs=args.jobs)
        if not transcript:
            log.error("Transcription failed")
            sys.exit(1)

        log.info(f"Total transcript: {len(transcript)} chars")

        # Step 3: Format (unless --raw)
        if args.raw:
            output = transcript
        else:
            output = format_transcript(transcript)

    # Output
    if args.output:
        out_path = os.path.expanduser(args.output)
        with open(out_path, "w") as f:
            f.write(output)
            if output and not output.endswith("\n"):
                f.write("\n")
        log.info(f"Written markdown transcript to {out_path}")
    else:
        print(output)


if __name__ == "__main__":
    main()
