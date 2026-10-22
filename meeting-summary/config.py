"""meeting-summary settings, read from the process environment (systemd EnvironmentFile).

Telegram access, tokens and sessions belong to the meeting-summary gateway; the
workers here only need the runtime controller, the database and media tools.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    pi_auth_path: Path
    summary_model: str
    transcribe_model: str
    transcribe_jobs: int
    max_audio_duration_sec: int
    chunk_seconds: int
    summary_timeout_sec: int
    state_dir: Path
    spool_dir: Path
    database_url: str
    retention_days: int
    ffmpeg_bin: str
    ffprobe_bin: str
    runtime_controller_url: str
    runtime_controller_bearer_token: str | None
    runtime_worker_id: str
    runtime_worker_venue: str
    runtime_worker_capacity: int
    runtime_supported_execution_profiles: tuple[str, ...]
    runtime_claim_workload_types: tuple[str, ...]
    runtime_claim_producer_systems: tuple[str, ...]
    runtime_allowed_queue_classes: tuple[str, ...]
    runtime_queue_preference: tuple[str, ...]
    runtime_lease_duration_sec: int
    runtime_heartbeat_interval_sec: int
    runtime_result_poll_interval_sec: int


def load_settings() -> Settings:
    env = os.environ.get
    state_dir = _path(env("MEETING_SUMMARY_STATE_DIR", "~/.local/share/meeting-summary"))
    return Settings(
        pi_auth_path=_path(env("MEETING_SUMMARY_PI_AUTH_PATH", "~/.pi/agent/auth.json")),
        summary_model=env("MEETING_SUMMARY_SUMMARY_MODEL", "gpt-5.4-mini"),
        transcribe_model=env("MEETING_SUMMARY_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        transcribe_jobs=max(1, int(env("MEETING_SUMMARY_TRANSCRIBE_JOBS", "3"))),
        max_audio_duration_sec=int(env("MEETING_SUMMARY_MAX_AUDIO_DURATION_SEC", "7200")),
        chunk_seconds=int(env("MEETING_SUMMARY_CHUNK_SECONDS", "600")),
        summary_timeout_sec=int(env("MEETING_SUMMARY_SUMMARY_TIMEOUT_SEC", "180")),
        state_dir=state_dir,
        spool_dir=_path(env("MEETING_SUMMARY_SPOOL_DIR", str(state_dir / "spool"))),
        database_url=env("MEETING_SUMMARY_DATABASE_URL", ""),
        retention_days=int(env("MEETING_SUMMARY_RETENTION_DAYS", "7")),
        ffmpeg_bin=env("MEETING_SUMMARY_FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=env("MEETING_SUMMARY_FFPROBE_BIN", "ffprobe"),
        runtime_controller_url=env("RUNTIME_CONTROLLER_API_URL", "http://127.0.0.1:8787").rstrip("/"),
        runtime_controller_bearer_token=env("RUNTIME_CONTROLLER_BEARER_TOKEN") or None,
        runtime_worker_id=env("MEETING_SUMMARY_RUNTIME_WORKER_ID", f"meeting-summary-worker-{os.getpid()}"),
        runtime_worker_venue=(env("MEETING_SUMMARY_RUNTIME_WORKER_VENUE") or "generic").strip().lower(),
        runtime_worker_capacity=max(1, int(env("MEETING_SUMMARY_RUNTIME_WORKER_CAPACITY", "1"))),
        runtime_supported_execution_profiles=_csv(env("MEETING_SUMMARY_RUNTIME_SUPPORTED_PROFILES", "cpu_media")),
        runtime_claim_workload_types=_csv(env("MEETING_SUMMARY_RUNTIME_CLAIM_WORKLOAD_TYPES", "meeting_summary")),
        runtime_claim_producer_systems=_csv(env("MEETING_SUMMARY_RUNTIME_CLAIM_PRODUCER_SYSTEMS", "")),
        runtime_allowed_queue_classes=_csv(env("MEETING_SUMMARY_RUNTIME_ALLOWED_QUEUE_CLASSES",
                                               "foreground,background,ops")),
        runtime_queue_preference=_csv(env("MEETING_SUMMARY_RUNTIME_QUEUE_PREFERENCE", "foreground,background,ops")),
        runtime_lease_duration_sec=int(env("MEETING_SUMMARY_RUNTIME_LEASE_DURATION_SEC", "300")),
        runtime_heartbeat_interval_sec=int(env("MEETING_SUMMARY_RUNTIME_HEARTBEAT_INTERVAL_SEC", "30")),
        runtime_result_poll_interval_sec=int(env("MEETING_SUMMARY_RUNTIME_RESULT_POLL_INTERVAL_SEC", "5")),
    )


def ensure_directories(settings: Settings) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.spool_dir.mkdir(parents=True, exist_ok=True)


def _has_executable(command: str) -> bool:
    candidate = (command or "").strip()
    if "/" in candidate:
        path = Path(os.path.expanduser(candidate))
        return path.is_file() and os.access(path, os.X_OK)
    return bool(candidate) and shutil.which(candidate) is not None


def validate_settings(settings: Settings) -> None:
    missing: list[str] = []
    if not settings.database_url:
        missing.append("MEETING_SUMMARY_DATABASE_URL")
    if not _has_executable(settings.ffmpeg_bin):
        missing.append(f"MEETING_SUMMARY_FFMPEG_BIN ({settings.ffmpeg_bin})")
    if not _has_executable(settings.ffprobe_bin):
        missing.append(f"MEETING_SUMMARY_FFPROBE_BIN ({settings.ffprobe_bin})")
    if settings.runtime_worker_venue not in {"generic", "oci"}:
        missing.append("MEETING_SUMMARY_RUNTIME_WORKER_VENUE must be one of generic, oci")
    if not settings.summary_model.strip():
        missing.append("MEETING_SUMMARY_SUMMARY_MODEL")
    if not settings.transcribe_model.strip():
        missing.append("MEETING_SUMMARY_TRANSCRIBE_MODEL")
    if not settings.runtime_supported_execution_profiles:
        missing.append("MEETING_SUMMARY_RUNTIME_SUPPORTED_PROFILES")
    if not settings.runtime_claim_workload_types:
        missing.append("MEETING_SUMMARY_RUNTIME_CLAIM_WORKLOAD_TYPES")
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
