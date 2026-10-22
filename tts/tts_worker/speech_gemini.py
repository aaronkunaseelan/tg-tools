from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from tg_tools_common.progress import current_progress
from tts_common.speech_text import chunk_speech_text
from .key_command import KeyCommandError, resolve_key_command


# Explicit caps on script size, chunk count, provider
# response bytes, decoded PCM bytes, and total output duration.
MAX_SCRIPT_CHARS = 120_000
MAX_SCRIPT_WORDS = 20_000
MAX_CHUNKS = 64
MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PCM_BYTES = 48 * 1024 * 1024  # ~16 min of 24kHz s16 mono per chunk
MAX_TOTAL_DURATION_S = 2 * 3600
# Plausible spoken pace window (spoken units/second); conservative both ways.
MIN_WORDS_PER_SEC = 0.5
MAX_WORDS_PER_SEC = 6.0
# Han, kana, and Hangul are normally written without whitespace. Count each
# character as a spoken unit in addition to whitespace-delimited tokens.
_EAST_ASIAN_SPOKEN_CHAR_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af\U00020000-\U0002ebef]"
)
# Azure's 2,000-token speech-input cap can be reached by fewer than 2,000 CJK
# characters. This leaves ample headroom without changing Latin-script chunks.
AZURE_EAST_ASIAN_MAX_CHARS = 900


class SpeechError(RuntimeError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def spoken_unit_count(text: str) -> int:
    """Count words for space-delimited scripts and characters for CJK scripts."""
    return len(text.split()) + len(_EAST_ASIAN_SPOKEN_CHAR_RE.findall(text))


def speech_chunk_char_limit(provider_name: str, speech_text: str, default: int = 3000) -> int:
    """Choose an Azure-safe chunk limit for scripts that contain CJK text."""
    if provider_name == "azure" and _EAST_ASIAN_SPOKEN_CHAR_RE.search(speech_text):
        return min(default, AZURE_EAST_ASIAN_MAX_CHARS)
    return default


def validate_duration_pace(speech_text: str, duration: float) -> None:
    units = spoken_unit_count(speech_text)
    if not units:
        return
    pace = units / duration
    if not (MIN_WORDS_PER_SEC <= pace <= MAX_WORDS_PER_SEC):
        raise SpeechError(
            "audio_pace_implausible",
            f"implausible duration-to-spoken-unit ratio: {pace:.2f} units/sec",
        )


def classify_provider_http(status: int, provider: str) -> str:
    """Typed provider HTTP outcomes."""
    if status in (401, 403):
        return f"{provider}_auth_required"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "model_transient"
    return "invalid_input" if status == 400 else "audio_validation_failed"


@dataclass(frozen=True)
class SpeechResult:
    audio_path: Path
    receipts: list[dict[str, Any]]
    duration_seconds: float


class GeminiSpeechProvider:
    def __init__(self, *, api_key: str, voice: str = "Kore", http_post: Any | None = None) -> None:
        self.api_key = api_key
        self.voice = voice
        self.http_post = http_post

    def synthesize_pcm(self, chunk: str) -> tuple[bytes, str]:
        if not self.api_key and self.http_post is None:
            raise SpeechError("gemini_auth_required", "GEMINI_API_KEY is required")
        payload = {
            "contents": [{"parts": [{"text": chunk}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
        }
        if self.http_post is not None:
            try:
                obj = self.http_post(payload)
            except SpeechError:
                raise
            except Exception as exc:
                raise SpeechError("gemini_error", str(exc)) from exc
        else:
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key=" + self.api_key
            req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib_request.urlopen(req, timeout=120) as resp:
                    raw = resp.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise SpeechError("audio_validation_failed", "Gemini TTS response exceeds byte cap")
                    obj = json.loads(raw.decode("utf-8"))
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise SpeechError(classify_provider_http(exc.code, "gemini"), f"Gemini TTS HTTP {exc.code}: {detail}") from exc
            except urllib_error.URLError as exc:
                raise SpeechError("gemini_error", f"Gemini TTS failed: {exc.reason}") from exc
        try:
            inline = obj["candidates"][0]["content"]["parts"][0]["inlineData"]
            mime = str(inline["mimeType"])
            data = base64.b64decode(str(inline["data"]))
        except Exception as exc:
            raise SpeechError("gemini_error", "Gemini TTS returned invalid payload") from exc
        if mime != "audio/L16;codec=pcm;rate=24000" or not data:
            raise SpeechError("gemini_error", "Gemini TTS returned unsupported audio")
        if len(data) > MAX_PCM_BYTES:
            raise SpeechError("audio_validation_failed", "Gemini TTS PCM exceeds byte cap")
        return data, mime


def _azure_text_error(data: bytes, content_type: str = "") -> str | None:
    """Return a bounded provider error only when the full response is textual.

    Raw PCM is arbitrary binary and may begin with JSON/HTML punctuation, so a
    leading-byte check is not a valid discriminator.
    """
    media_type = content_type.partition(";")[0].strip().lower()
    declared_text = media_type.startswith("text/") or media_type == "application/json" or media_type.endswith("+json")
    parsed: Any | None = None
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if declared_text:
            return f"Azure TTS returned {media_type or 'text'} instead of PCM"
        return None
    detail = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
    if isinstance(detail, dict):
        code = str(detail.get("code") or "").strip()
        message = str(detail.get("message") or "").strip()
        summary = ": ".join(part for part in (code, message) if part)
        if summary:
            return f"Azure TTS returned JSON instead of PCM: {summary[:500]}"
    return "Azure TTS returned JSON instead of PCM"


class AzureSpeechProvider:
    def __init__(self, *, base_url: str, api_key: str, key_cmd: str = "", model: str = "gpt-4o-mini-tts", voice: str = "nova", http_post: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.key_cmd = key_cmd
        self.model = model
        self.voice = voice
        self.http_post = http_post

    def _resolve_key(self) -> str:
        if self.api_key.strip():
            return self.api_key.strip()
        if not self.key_cmd.strip():
            raise SpeechError("azure_auth_required", "azure TTS api key is required")
        try:
            key = resolve_key_command(self.key_cmd)
        except KeyCommandError as exc:
            raise SpeechError("azure_auth_required", f"azure TTS key command failed: {exc}") from exc
        self.api_key = key
        return key

    def synthesize_pcm(self, chunk: str) -> tuple[bytes, str]:
        payload = {"model": self.model, "input": chunk, "voice": self.voice, "response_format": "pcm"}
        content_type = ""
        if self.http_post is not None:
            try:
                data = self.http_post(payload)
            except SpeechError:
                raise
            except Exception as exc:
                raise SpeechError("azure_tts_error", str(exc)) from exc
        else:
            key = self._resolve_key()
            url = self.base_url + "/openai/v1/audio/speech?api-version=preview"
            req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/octet-stream", "api-key": key}, method="POST")
            try:
                with urllib_request.urlopen(req, timeout=300) as resp:
                    content_type = str(resp.headers.get("Content-Type") or "")
                    data = resp.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise SpeechError(classify_provider_http(exc.code, "azure"), f"Azure TTS HTTP {exc.code}: {detail}") from exc
            except urllib_error.URLError as exc:
                raise SpeechError("azure_tts_error", f"Azure TTS failed: {exc.reason}") from exc
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise SpeechError("azure_tts_error", "Azure TTS returned empty audio")
        data = bytes(data)
        if len(data) > MAX_PROVIDER_RESPONSE_BYTES:
            raise SpeechError("audio_validation_failed", "Azure TTS response exceeds byte cap")
        text_error = _azure_text_error(data, content_type)
        if text_error:
            raise SpeechError("azure_tts_error", text_error)
        return data, "audio/L16;codec=pcm;rate=24000"


def _validate_pcm(pcm: bytes, index: int, *, real_media: bool) -> None:
    """PCM sanity: non-empty, even s16 length, byte cap, and a
    conservative non-silence energy check on real media runs."""
    if not pcm:
        raise SpeechError("audio_validation_failed", f"chunk {index}: empty PCM")
    if len(pcm) % 2 != 0:
        raise SpeechError("audio_validation_failed", f"chunk {index}: odd PCM byte length")
    if len(pcm) > MAX_PCM_BYTES:
        raise SpeechError("audio_validation_failed", f"chunk {index}: PCM exceeds byte cap")
    if real_media:
        import array
        # Whole-stream activity check: strided windows across the entire
        # chunk; a single stray sample cannot validate a silent stream.
        samples = array.array("h")
        samples.frombytes(pcm)
        n = len(samples)
        window = max(1, n // 16)
        active_windows = 0
        for start in range(0, n, window):
            seg = samples[start:start + window]
            if not seg:
                continue
            mean_abs = sum(abs(v) for v in seg[::16]) / max(1, len(seg[::16]))
            if mean_abs >= 16:
                active_windows += 1
        if active_windows < 2 and n > window:
            raise SpeechError("audio_validation_failed", f"chunk {index}: PCM is silent")
        if active_windows < 1:
            raise SpeechError("audio_validation_failed", f"chunk {index}: PCM is silent")


def _run(args: list[str], *, error_class: str, runner: Any | None = None) -> subprocess.CompletedProcess[str]:
    try:
        if runner is not None:
            return runner(args)
        return subprocess.run(args, text=True, capture_output=True, check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        raise SpeechError(error_class, (exc.stderr or str(exc))[:500]) from exc
    except Exception as exc:
        raise SpeechError(error_class, str(exc)) from exc


def synthesize_speech(
    speech_text: str,
    out_dir: Path,
    *,
    provider: Any,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    runner: Any | None = None,
    max_chars: int = 3000,
    cancel: Any | None = None,
) -> SpeechResult:
    sink = current_progress()
    sink.check()
    started = time.monotonic()
    occurrence = sink.emit("synthesize_speech", "started")

    def report(completed: int, total: int) -> None:
        sink.emit("synthesize_speech", "advanced", occurrence=occurrence,
                  completed=completed, total=total, units="chunks")

    try:
        result = _synthesize_speech(
            speech_text, out_dir, provider=provider, ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin, runner=runner, max_chars=max_chars,
            cancel=cancel, report=report,
        )
        sink.check()
    except BaseException:
        sink.emit("synthesize_speech", "interrupted", occurrence=occurrence)
        raise
    sink.emit("synthesize_speech", "completed", occurrence=occurrence,
              completed=len(result.receipts), total=len(result.receipts), units="chunks",
              audioDurationSeconds=result.duration_seconds,
              durationSeconds=round(time.monotonic() - started, 1))
    return result


def _synthesize_speech(
    speech_text: str,
    out_dir: Path,
    *,
    provider: Any,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    runner: Any | None = None,
    max_chars: int = 3000,
    cancel: Any | None = None,
    report: Any,
) -> SpeechResult:
    _cancel = cancel or (lambda: None)
    if len(speech_text) > MAX_SCRIPT_CHARS:
        raise SpeechError("invalid_input", f"speech text exceeds {MAX_SCRIPT_CHARS} chars")
    spoken_units = spoken_unit_count(speech_text)
    if spoken_units > MAX_SCRIPT_WORDS:
        raise SpeechError("invalid_input", f"speech text exceeds {MAX_SCRIPT_WORDS} spoken units")
    chunks = chunk_speech_text(speech_text, max_chars=max_chars)
    if not chunks:
        raise SpeechError("input_empty", "speech text is empty")
    if len(chunks) > MAX_CHUNKS:
        raise SpeechError("invalid_input", f"speech text needs {len(chunks)} chunks over cap {MAX_CHUNKS}")
    out_dir.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    mp3s: list[Path] = []
    total_pcm_bytes = 0
    report(0, len(chunks))
    for index, chunk in enumerate(chunks, 1):
        _cancel()  # lease/cancellation fence before each provider request
        started = time.time()
        pcm, _ = provider.synthesize_pcm(chunk)
        _validate_pcm(pcm, index, real_media=runner is None)
        total_pcm_bytes += len(pcm)
        # Cumulative budget BEFORE writing/transcoding:
        # 24kHz s16 mono => projected seconds = bytes / 48000.
        if total_pcm_bytes / 48000 > MAX_TOTAL_DURATION_S:
            raise SpeechError("audio_validation_failed",
                              "cumulative PCM exceeds the total duration cap")
        pcm_path = out_dir / f"chunk-{index:04d}.pcm"
        mp3_path = out_dir / f"chunk-{index:04d}.mp3"
        pcm_path.write_bytes(pcm)
        _run([ffmpeg_bin, "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(pcm_path), "-b:a", "96k", str(mp3_path)], error_class="audio_validation_failed", runner=runner)
        if not mp3_path.exists():
            mp3_path.write_bytes(b"fake-mp3") if runner is not None else None
        pcm_path.unlink(missing_ok=True)  # bound disk after conversion
        receipts.append({"index": index, "chars": len(chunk), "bytes": len(pcm), "duration": round(time.time() - started, 3), "status": "ok"})
        mp3s.append(mp3_path)
        report(index, len(chunks))
    _cancel()  # fence before concatenation/ffmpeg
    concat = out_dir / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in mp3s), encoding="utf-8")
    audio = out_dir / "audio.mp3"
    _run([ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(audio)], error_class="audio_validation_failed", runner=runner)
    if not audio.exists() and runner is not None:
        audio.write_bytes(b"fake-final-mp3")
    cp = _run([ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio)], error_class="audio_validation_failed", runner=runner)
    try:
        duration = float((json.loads(cp.stdout or "{}" ).get("format") or {}).get("duration") or 0)
    except Exception as exc:
        raise SpeechError("audio_validation_failed", "ffprobe returned invalid duration") from exc
    if duration <= 0:
        raise SpeechError("audio_validation_failed", "audio duration is not positive")
    if duration > MAX_TOTAL_DURATION_S:
        raise SpeechError("audio_validation_failed", f"audio duration {duration:.0f}s exceeds cap")
    if runner is None:
        validate_duration_pace(speech_text, duration)
    return SpeechResult(audio_path=audio, receipts=receipts, duration_seconds=duration)
