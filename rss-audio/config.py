"""rss-audio settings, read from the process environment (systemd EnvironmentFile)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tg_tools_common.config import required_env


def _path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def _flag(name: str, default: bool, environ=None) -> bool:
    raw = ((os.environ if environ is None else environ).get(name) or "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    fail_log_dir: Path
    mac_ssh_target: str
    mac_ytdlp_path: str
    yt_dlp_bin: str
    yt_dlp_format: str
    ffmpeg_bin: str
    ffprobe_bin: str
    rclone_bin: str
    mullvad_proxy: str
    yt_dlp_remote_components: str
    b2_remote: str
    b2_bucket: str
    api_base_url: str
    api_token: str
    public_audio_base_url: str
    public_md_base_url: str
    feed_url: str
    runtime_controller_url: str
    runtime_controller_bearer_token: str
    runtime_worker_id: str
    work_dir: Path
    scribe_db_path: Path
    mullvad_control_url: str
    mullvad_control_token: str
    enrichment_enabled: bool = True
    # When enrichment fails (e.g. the model hits a usage limit) the audio still
    # publishes without script/notes links. Set true to fail the job instead,
    # so no feed item lacks a transcript.
    enrichment_required: bool = False
    enrichment_stt_model: str = "mini"
    enrichment_jobs: int = 4


PUBLISH_LEASE_SECONDS = 7200
TRANSCRIBE_LEASE_SECONDS = 1800


def transcribe_worker_id(worker_id: str) -> str:
    return f"{worker_id}-transcribe"


def load_settings(environ=None, *, pid=None) -> Settings:
    source = os.environ if environ is None else environ
    env = source.get
    base_url = required_env("RSS_AUDIO_BASE_URL", source).rstrip("/")
    public_audio_base_url = env("RSS_AUDIO_PUBLIC_AUDIO_BASE_URL", f"{base_url}/audio/episodes").rstrip("/")
    return Settings(
        bot_token=env("RSS_AUDIO_BOT_TOKEN", ""),
        fail_log_dir=_path(env("RSS_AUDIO_FAIL_LOG_DIR", "~/tools/logs/rss-audio")),
        mac_ssh_target=env("RSS_AUDIO_MAC_SSH", ""),
        mac_ytdlp_path=env("RSS_AUDIO_MAC_YTDLP_PATH", ""),
        yt_dlp_bin=env("RSS_AUDIO_YTDLP_BIN", "yt-dlp"),
        yt_dlp_format=env("RSS_AUDIO_YTDLP_FORMAT", "ba").strip() or "ba",
        ffmpeg_bin=env("RSS_AUDIO_FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=env("RSS_AUDIO_FFPROBE_BIN", "ffprobe"),
        rclone_bin=env("RSS_AUDIO_RCLONE_BIN", "rclone"),
        mullvad_proxy=env("RSS_AUDIO_MULLVAD_PROXY", ""),
        yt_dlp_remote_components=env("RSS_AUDIO_YTDLP_REMOTE_COMPONENTS", ""),
        b2_remote=env("RSS_AUDIO_B2_REMOTE", "ak_b2"),
        b2_bucket=required_env("RSS_AUDIO_B2_BUCKET", source),
        api_base_url=env("RSS_AUDIO_API_BASE_URL", base_url).rstrip("/"),
        api_token=env("RSS_AUDIO_API_TOKEN", ""),
        public_audio_base_url=public_audio_base_url,
        # Transcript Markdown lives beside the MP3 under the same public prefix.
        public_md_base_url=env("RSS_AUDIO_PUBLIC_MD_BASE_URL", public_audio_base_url).rstrip("/"),
        feed_url=env("RSS_AUDIO_FEED_URL", f"{base_url}/audio/podcast.xml"),
        runtime_controller_url=env("RUNTIME_CONTROLLER_API_URL", "http://127.0.0.1:8787").rstrip("/"),
        runtime_controller_bearer_token=env("RUNTIME_CONTROLLER_BEARER_TOKEN", ""),
        runtime_worker_id=env("RSS_AUDIO_RUNTIME_WORKER_ID", f"rss-audio-{os.getpid() if pid is None else pid}"),
        work_dir=_path(env("RSS_AUDIO_WORK_DIR", "~/.local/share/tg-tools/rss-audio/work")),
        scribe_db_path=_path(env("RSS_AUDIO_SCRIBE_DB_PATH", "~/.local/share/tg-tools/rss-audio/scribe.sqlite3")),
        mullvad_control_url=env("RSS_AUDIO_MULLVAD_CONTROL_URL", "http://127.0.0.1:8000"),
        mullvad_control_token=env("RSS_AUDIO_MULLVAD_CONTROL_TOKEN", ""),
        enrichment_enabled=_flag("RSS_AUDIO_ENRICHMENT_ENABLED", True, source),
        enrichment_required=_flag("RSS_AUDIO_ENRICHMENT_REQUIRED", False, source),
        enrichment_stt_model=env("RSS_AUDIO_ENRICHMENT_STT_MODEL", "mini"),
        enrichment_jobs=max(1, int(env("RSS_AUDIO_ENRICHMENT_JOBS", "4"))),
    )


def validate_settings(settings: Settings) -> None:
    """Fail fast on route settings that must not have a built-in default.

    The proxy and Mac endpoints are facts about this deployment, not about the
    code; a default would turn a config error into a silent route fallback.
    Workers call this at startup; the gateway adapter only loads settings.
    """
    missing: list[str] = []

    proxy = settings.mullvad_proxy.strip()
    if not proxy:
        missing.append("RSS_AUDIO_MULLVAD_PROXY")
    else:
        split = urlsplit(proxy)
        if split.scheme != "http" or not split.hostname:
            missing.append(f"RSS_AUDIO_MULLVAD_PROXY must be an http proxy URL ({proxy!r})")

    if not settings.mac_ssh_target.strip():
        missing.append("RSS_AUDIO_MAC_SSH")

    mac_ytdlp = settings.mac_ytdlp_path.strip()
    if not mac_ytdlp:
        missing.append("RSS_AUDIO_MAC_YTDLP_PATH")
    elif not mac_ytdlp.startswith("/"):
        missing.append(f"RSS_AUDIO_MAC_YTDLP_PATH must be an absolute path ({mac_ytdlp!r})")

    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
