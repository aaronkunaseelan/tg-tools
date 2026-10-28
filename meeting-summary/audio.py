from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import Settings


@dataclass(frozen=True)
class PreparedAudio:
    duration_seconds: float
    full_wav_path: Path
    chunk_paths: tuple[Path, ...]


class AudioError(RuntimeError):
    pass


def probe_duration(source_path: Path, settings: Settings) -> float:
    proc = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe failed: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise AudioError("audio duration missing or zero")
    return duration


def prepare_audio(source_path: Path, work_dir: Path, settings: Settings) -> PreparedAudio:
    duration = probe_duration(source_path, settings)
    if duration > settings.max_audio_duration_sec:
        raise AudioError(
            f"audio duration {int(duration)}s exceeds limit {settings.max_audio_duration_sec}s"
        )

    wav_dir = work_dir / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)
    full_wav = wav_dir / "full.wav"
    proc = subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-v",
            "warning",
            "-i",
            str(source_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(full_wav),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60 * 30,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg normalize failed: {proc.stderr.strip()[:400]}")

    chunk_paths: list[Path] = []
    offset = 0
    chunk_index = 0
    while offset < duration:
        chunk_path = wav_dir / f"chunk-{chunk_index:03d}.wav"
        proc = subprocess.run(
            [
                settings.ffmpeg_bin,
                "-y",
                "-v",
                "warning",
                "-ss",
                str(offset),
                "-i",
                str(full_wav),
                "-t",
                str(settings.chunk_seconds),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(chunk_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60 * 10,
        )
        if proc.returncode != 0:
            raise AudioError(f"ffmpeg chunk failed: {proc.stderr.strip()[:400]}")
        if not chunk_path.exists() or chunk_path.stat().st_size < 1024:
            if chunk_path.exists():
                chunk_path.unlink()
            raise AudioError(f"audio coverage incomplete at {offset}s: missing or tiny chunk")
        chunk_duration = probe_duration(chunk_path, settings)
        expected = min(settings.chunk_seconds, duration - offset)
        if abs(chunk_duration - expected) > 0.25:
            raise AudioError(f"audio coverage incomplete at {offset}s: expected {expected}s, got {chunk_duration}s")
        chunk_paths.append(chunk_path)
        chunk_index += 1
        offset += settings.chunk_seconds

    if not chunk_paths:
        raise AudioError("no audio chunks produced")

    return PreparedAudio(
        duration_seconds=duration,
        full_wav_path=full_wav,
        chunk_paths=tuple(chunk_paths),
    )
