"""Startup validation for the TTS runtime worker."""

from __future__ import annotations

import os
import shlex
import shutil

from .document_tools import DocumentToolError, validate_document_tools

DEFAULT_MEMBERSHIP_HEARTBEAT_SEC = 30.0


def membership_heartbeat_interval() -> float:
    raw = os.environ.get(
        "TTS_WORKER_MEMBERSHIP_HEARTBEAT_SEC",
        str(DEFAULT_MEMBERSHIP_HEARTBEAT_SEC),
    )
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("TTS_WORKER_MEMBERSHIP_HEARTBEAT_SEC must be numeric") from exc
    if value < 5 or value > 60:
        raise ValueError("TTS_WORKER_MEMBERSHIP_HEARTBEAT_SEC must be between 5 and 60")
    return value


def validate_startup(settings) -> tuple[list[str], list[str]]:
    """Validate required binaries and non-secret configuration. Returns
    (fatal, warnings). Fatal problems are dependencies required by the
    advertised tts/x-tts workload types: the worker must abort before
    registering rather than claim jobs it cannot complete. No provider
    probes here — quota probes are explicit canary actions, never startup
    traffic."""
    fatal: list[str] = []
    warnings: list[str] = []
    try:
        membership_heartbeat_interval()
    except ValueError as exc:
        fatal.append(str(exc))
    for name, value in (("ffmpeg", settings.ffmpeg_bin), ("ffprobe", settings.ffprobe_bin),
                        ("rclone", settings.rclone_bin)):
        if not shutil.which(value):
            fatal.append(f"required binary missing: {name} ({value})")
    # PDF sources need pdftotext; do not register a worker that would claim
    # document work it cannot convert.
    try:
        validate_document_tools()
    except DocumentToolError as exc:
        fatal.append(f"document conversion: {exc}")
    if not settings.public_base_url:
        fatal.append("TTS_PUBLIC_BASE_URL is not set")
    if not settings.api_base_url:
        fatal.append("TTS_API_BASE_URL is not set")
    if settings.speech_provider == "self-hosted" and not (settings.self_hosted_speech_url and settings.self_hosted_speech_token):
        fatal.append("TTS_SPEECH_PROVIDER=self-hosted requires TTS_SELF_HOSTED_SPEECH_URL and TTS_SELF_HOSTED_SPEECH_TOKEN")
    if settings.speech_provider in ("azure", "self-hosted"):
        # Azure speaks, or is the self-hosted fallback. Check configuration only; do not spend quota or expose keys at startup.
        credentials = [("TTS_AZURE_TTS", settings.azure_tts_api_key, settings.azure_tts_key_cmd)]
        if settings.title_model:
            credentials.append(("TTS_AZURE_CHAT", settings.azure_chat_api_key, settings.azure_chat_key_cmd))
        for label, key, command in credentials:
            if key.strip():
                continue
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = []
            if not argv or not shutil.which(argv[0]):
                fatal.append(f"{label} requires a key or an executable key command")
    if "x_tts_explain" in settings.workload_types:
        try:
            from pi_harness.preflight import assert_provider_auth
            from pi_harness.roles import resolve_role
            from .fingerprint import reader_role_map

            role_map = reader_role_map()
            assert_provider_auth(
                [cfg["provider"] for name, cfg in role_map.items() if name != "_hash"] +
                [resolve_role(r)["provider"] for r in ("transcript_topics", "transcript_mentions")])
            warnings.append("x-tts reader role-map hash: " + str(role_map["_hash"]))
            warnings.append("x-tts reader roles: " + ", ".join(
                "%s=%s/%s@%s" % (name, cfg["provider"], cfg["model"], cfg["thinking"])
                for name, cfg in role_map.items() if name != "_hash"))
        except Exception as exc:
            fatal.append("x-tts reader preflight: " + str(exc))
        try:
            from episode_enrichment import ScriptSource  # noqa: F401
        except Exception as exc:
            fatal.append("x-tts enrichment unavailable: " + str(exc))
    return fatal, warnings
