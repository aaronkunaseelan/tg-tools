from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


BASE_SUBPROCESS_ENV_KEYS = (
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSH_AUTH_SOCK",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
TRANSCRIBE_ENV_PREFIXES = (
    "AZURE_OPENAI_TRANSCRIPTION_",
    "TRANSCRIBE_AUDIO_",
)
EXCLUDED_ARTIFACT_ROOTS = (
    Path.home() / ".config" / "agent-secrets",
    Path.home() / ".pi" / "agent" / "sessions",
    Path.home() / ".pi" / "agent" / "tasks",
)


def build_markit_subprocess_env(source: Mapping[str, str]) -> dict[str, str]:
    return _copy_allowed_env(source)


def build_transcribe_subprocess_env(source: Mapping[str, str]) -> dict[str, str]:
    return _copy_allowed_env(source, prefixes=TRANSCRIBE_ENV_PREFIXES)


def artifact_allowed_roots(workdir: str | Path) -> tuple[Path, ...]:
    resolved_workdir = Path(workdir).expanduser().resolve()
    roots = [resolved_workdir]
    for parent in resolved_workdir.parents:
        if parent.name == os.environ.get("TG_TOOLS_WORKSPACE_NAME", "workbench"):
            roots.append(parent)
            break
    return tuple(_dedupe_paths(roots))


def normalize_allowed_artifact_path(
    raw_path: str,
    *,
    allowed_roots: tuple[Path, ...],
) -> Path | None:
    expanded = Path(os.path.expanduser(raw_path))
    if not expanded.is_absolute():
        return None
    if not expanded.exists() or not expanded.is_file():
        return None
    resolved = expanded.resolve()
    if not _is_within_any_root(resolved, allowed_roots):
        return None
    if _is_within_any_root(resolved, EXCLUDED_ARTIFACT_ROOTS):
        return None
    return resolved


def _copy_allowed_env(
    source: Mapping[str, str],
    *,
    exact_keys: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for key in BASE_SUBPROCESS_ENV_KEYS + exact_keys:
        value = source.get(key)
        if value is not None and value.strip():
            allowed[key] = value
    if prefixes:
        for key, value in source.items():
            if not value.strip():
                continue
            if any(key.startswith(prefix) for prefix in prefixes):
                allowed[key] = value
    return allowed


def _is_within_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped
