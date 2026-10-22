"""TTS worker settings, read from the process environment (systemd EnvironmentFile)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tg_tools_common.config import required_env


def _csv(value: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value.strip():
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    worker_id: str
    worker_venue: str
    capacity: int
    supported_profiles: tuple[str, ...]
    workload_types: tuple[str, ...]
    runtime_url: str
    runtime_bearer_token: str
    lease_seconds: int
    database_dsn: str
    work_dir: Path
    gemini_api_key: str
    default_voice: str
    speech_provider: str
    azure_tts_base_url: str
    azure_tts_api_key: str
    azure_tts_key_cmd: str
    azure_tts_model: str
    ffmpeg_bin: str
    ffprobe_bin: str
    rclone_bin: str
    rclone_remote: str
    b2_bucket: str
    public_base_url: str
    api_base_url: str
    api_token: str
    feed_url: str
    feed_title: str
    azure_chat_base_url: str
    azure_chat_api_key: str
    azure_chat_key_cmd: str
    title_model: str


def feed_title(env: dict[str, str] | None = None) -> str:
    return required_env("TTS_FEED_TITLE", env)


def _require_capacity_one(raw: str) -> int:
    value = int(raw)
    if value != 1:
        raise ValueError("TTS_WORKER_CAPACITY must be 1 (serial worker); got %s" % value)
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        worker_id=e.get("TTS_WORKER_ID") or "tts-worker",
        worker_venue=e.get("TTS_WORKER_VENUE", "oci"),
        capacity=_require_capacity_one(e.get("TTS_WORKER_CAPACITY", "1")),
        supported_profiles=_csv(e.get("TTS_WORKER_SUPPORTED_PROFILES", ""), ("cpu_io", "model_tts_io")),
        workload_types=_csv(e.get("TTS_WORKER_CLAIM_WORKLOAD_TYPES", ""), ("tts_synthesize", "x_tts_explain")),
        runtime_url=(e.get("RUNTIME_CONTROLLER_API_URL") or "http://127.0.0.1:8787").rstrip("/"),
        runtime_bearer_token=e.get("RUNTIME_CONTROLLER_BEARER_TOKEN") or "",
        lease_seconds=max(60, int(e.get("TTS_WORKER_LEASE_SECONDS", "7200"))),
        database_dsn=e.get("TTS_DATABASE_URL") or "dbname=runtime_controller user=ubuntu host=/var/run/postgresql",
        work_dir=Path(e.get("TTS_WORK_DIR", "~/.local/share/tg-tools/tts/work")).expanduser().absolute(),
        gemini_api_key=e.get("GEMINI_API_KEY", ""),
        default_voice=e.get("TTS_DEFAULT_VOICE") or ("nova" if e.get("TTS_SPEECH_PROVIDER") == "azure" else "Kore"),
        speech_provider=e.get("TTS_SPEECH_PROVIDER", "gemini"),
        azure_tts_base_url=required_env("TTS_AZURE_TTS_BASE_URL", e).rstrip("/"),
        azure_tts_api_key=e.get("TTS_AZURE_TTS_API_KEY", ""),
        azure_tts_key_cmd=required_env("TTS_AZURE_TTS_KEY_CMD", e),
        azure_tts_model=e.get("TTS_AZURE_TTS_MODEL", "gpt-4o-mini-tts"),
        ffmpeg_bin=e.get("TTS_FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=e.get("TTS_FFPROBE_BIN", "ffprobe"),
        rclone_bin=e.get("TTS_RCLONE_BIN", "rclone"),
        rclone_remote=e.get("TTS_RCLONE_REMOTE", "ak_b2"),
        b2_bucket=required_env("TTS_B2_BUCKET", e),
        public_base_url=e.get("TTS_PUBLIC_BASE_URL", "").rstrip("/"),
        api_base_url=e.get("TTS_API_BASE_URL", "").rstrip("/"),
        api_token=e.get("TTS_API_TOKEN", ""),
        feed_url=required_env("TTS_FEED_URL", e),
        feed_title=feed_title(e),
        # Title naming for faithful reads only; not an x-tts reader backend.
        azure_chat_base_url=required_env("TTS_AZURE_CHAT_BASE_URL", e).rstrip("/"),
        azure_chat_api_key=e.get("TTS_AZURE_CHAT_API_KEY", ""),
        azure_chat_key_cmd=required_env("TTS_AZURE_CHAT_KEY_CMD", e),
        title_model=e.get("TTS_TITLE_MODEL", ""),
    )
