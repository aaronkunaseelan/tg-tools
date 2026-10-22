from __future__ import annotations

import json
import os
import ipaddress
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from tg_tools_common.progress import current_progress
from tg_tools_common.subprocesses import run_with_authority
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://\S+")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac")
TRACKING_QUERY_KEYS = {
    "fbclid",
    "feature",
    "si",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
_SENSITIVE_ENV_EXACT = {
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "KUBECONFIG",
    "NODE_AUTH_TOKEN",
    "NPM_TOKEN",
    "OPENAI_API_KEY",
    "RSS_AUDIO_API_TOKEN",
    "RSS_AUDIO_BOT_TOKEN",
    "TELEGRAM_API_HASH",
}
_SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN", "_SECRET", "_TOKEN", "_PASSWORD")
_SENSITIVE_ENV_PREFIXES = ("AZURE_", "VAULT_")
_MAC_UNAVAILABLE_MARKERS = (
    "connection timed out",
    "operation timed out",
    "timed out",
    "connection refused",
    "no route to host",
    "could not resolve hostname",
    "network is unreachable",
    "broken pipe",
    "connection closed",
    "connection reset",
    "kex_exchange_identification",
)
_AUTH_REQUIRED_MARKERS = (
    "sign in to confirm",
    "sign in to verify",
    "sign in to view",
    "login required",
    "requires login",
    "use --cookies",
    "use --cookies-from-browser",
    "age-restricted",
    "members-only",
)
_SOURCE_UNAVAILABLE_MARKERS = (
    "this video is not available",
    "this video is unavailable",
    "video unavailable",
    "private video",
    "has been removed",
    "does not exist",
    "no longer available",
    "unavailable in your country",
    "not available in your country",
)
_FORMAT_UNAVAILABLE_MARKERS = (
    "requested format is not available",
    "no video formats found",
    "no audio formats found",
    "no suitable formats",
)
_NETWORK_TRANSIENT_MARKERS = (
    "http error 429",
    "too many requests",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "temporary failure",
    "connection aborted",
    "remote end closed connection",
)


class SourceAudioError(RuntimeError):
    def __init__(self, stage: str, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.stderr = stderr


class SourceRoute(StrEnum):
    OCI_MULLVAD = "oci_mullvad"
    MAC_SSH = "mac_ssh"


class SourceFailureKind(StrEnum):
    UNKNOWN = "unknown"
    ROUTE_UNAVAILABLE = "route_unavailable"
    NETWORK_TRANSIENT = "network_transient"
    AUTH_REQUIRED = "auth_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FORMAT_UNAVAILABLE = "format_unavailable"


@dataclass(frozen=True)
class RouteHealth:
    route: SourceRoute
    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class SourceToolSettings:
    mac_ssh_target: str
    mac_ytdlp_path: str
    yt_dlp_bin: str
    ffmpeg_bin: str
    mullvad_proxy: str
    mullvad_control_url: str = "http://127.0.0.1:8000"
    mullvad_control_token: str = ""
    yt_dlp_remote_components: str = ""
    yt_dlp_format: str = "ba"


@dataclass(frozen=True)
class SourceMetadata:
    title: str
    channel: str
    description: str
    duration_seconds: int
    published_human: str
    source_url: str | None = None
    creators: tuple[str, ...] = ()
    series: str = ""
    # Provenanced original-publication date. `source_published_at` is an ISO
    # date or None; it is never inferred from ingestion time. `date_basis`
    # records where it came from so downstream documents can be honest about
    # how much the date is worth.
    source_published_at: str | None = None
    date_basis: str = "unknown"


def classify_text(text: str) -> tuple[str, str] | None:
    url = extract_first_url(text or "")
    if not url:
        return None
    kind = (
        "direct_audio_url"
        if url.lower().split("?", 1)[0].endswith(AUDIO_EXTENSIONS)
        else "url"
    )
    return kind, url


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,!?:;]>}")


def stable_url_for_display(url: str) -> str:
    split = urlsplit(url)
    filtered = []
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        if key.startswith("utm_") or key in TRACKING_QUERY_KEYS:
            continue
        filtered.append((key, value))
    return urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urlencode(filtered, doseq=True),
            "",
        )
    )


def validate_source_url(url: str) -> str:
    split = urlsplit(url)
    if split.scheme not in {"http", "https"} or not split.hostname:
        raise SourceAudioError("input", "Source URL must be http or https")
    if split.username or split.password:
        raise SourceAudioError("input", "Source URL must not include credentials")

    host = split.hostname.rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".local"):
        raise SourceAudioError("input", "Source URL host is not allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        _validate_resolved_host(host, split.port)
    else:
        _validate_public_ip(ip, host)
    return url


def route_label(route: SourceRoute) -> str:
    return "OCI" if route == SourceRoute.OCI_MULLVAD else "Mac"


def is_mac_reachable(
    settings: SourceToolSettings,
    *,
    timeout: int = 8,
) -> bool:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        settings.mac_ssh_target,
        (
            "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "
            f"test -x {shlex.quote(settings.mac_ytdlp_path)}"
        ),
    ]
    try:
        run_command(command, stage="mac-check", timeout=timeout, env=sanitized_source_subprocess_env())
        return True
    except SourceAudioError:
        return False


def is_mac_unavailable_error(error: SourceAudioError | str) -> bool:
    haystack = error if isinstance(error, str) else f"{error}\n{error.stderr}"
    haystack = haystack.lower()
    return any(marker in haystack for marker in _MAC_UNAVAILABLE_MARKERS)


def classify_source_failure(error: SourceAudioError | str) -> SourceFailureKind:
    haystack = error if isinstance(error, str) else f"{error}\n{error.stderr}"
    haystack = haystack.lower()
    if any(marker in haystack for marker in _MAC_UNAVAILABLE_MARKERS):
        return SourceFailureKind.ROUTE_UNAVAILABLE
    if any(marker in haystack for marker in _NETWORK_TRANSIENT_MARKERS):
        return SourceFailureKind.NETWORK_TRANSIENT
    if any(marker in haystack for marker in _AUTH_REQUIRED_MARKERS):
        return SourceFailureKind.AUTH_REQUIRED
    if any(marker in haystack for marker in _SOURCE_UNAVAILABLE_MARKERS):
        return SourceFailureKind.SOURCE_UNAVAILABLE
    if any(marker in haystack for marker in _FORMAT_UNAVAILABLE_MARKERS):
        return SourceFailureKind.FORMAT_UNAVAILABLE
    return SourceFailureKind.UNKNOWN


def source_failure_summary(error: SourceAudioError | str) -> str | None:
    kind = classify_source_failure(error)
    if kind == SourceFailureKind.SOURCE_UNAVAILABLE:
        return "Source unavailable: extractor reports this media is not available"
    if kind == SourceFailureKind.AUTH_REQUIRED:
        return "Source requires sign-in or browser cookies"
    if kind == SourceFailureKind.FORMAT_UNAVAILABLE:
        return "Source has no downloadable audio format for the configured extractor"
    if kind == SourceFailureKind.NETWORK_TRANSIENT:
        return "Transient network error while fetching source"
    if kind == SourceFailureKind.ROUTE_UNAVAILABLE:
        return "Route unavailable while fetching source"
    return None


def first_actionable_error_line(stderr: str) -> str:
    fallback = ""
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if line.startswith("[") and "]" in line:
            continue
        if lowered.startswith("controlsocket ") or lowered.startswith("mux_client_request_session:"):
            continue
        if not fallback:
            fallback = line
        if line.startswith("ERROR:"):
            return line[:300]
    return fallback[:300]


def check_oci_mullvad_health(
    settings: SourceToolSettings,
    *,
    timeout: float = 5,
    target_host: str = "example.com",
    target_port: int = 443,
) -> RouteHealth:
    split = urlsplit(settings.mullvad_proxy or "")
    if split.scheme != "http" or not split.hostname:
        return RouteHealth(
            route=SourceRoute.OCI_MULLVAD,
            ok=False,
            reason="proxy_config_invalid",
            detail=f"RSS_AUDIO_MULLVAD_PROXY must be an http proxy URL: {settings.mullvad_proxy!r}",
        )

    proxy_port = split.port or 80
    request = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        "Proxy-Connection: close\r\n"
        "\r\n"
    )
    try:
        with socket.create_connection((split.hostname, proxy_port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request.encode("ascii"))
            response = sock.recv(256).decode("iso-8859-1", errors="replace")
    except OSError as exc:
        return RouteHealth(
            route=SourceRoute.OCI_MULLVAD,
            ok=False,
            reason="proxy_connect_error",
            detail=str(exc),
        )

    status_line = response.splitlines()[0] if response else ""
    if " 200 " in status_line or status_line.endswith(" 200"):
        return RouteHealth(route=SourceRoute.OCI_MULLVAD, ok=True)
    return RouteHealth(
        route=SourceRoute.OCI_MULLVAD,
        ok=False,
        reason="proxy_tunnel_unhealthy",
        detail=status_line or "proxy returned no CONNECT response",
    )


def fetch_source_metadata(
    settings: SourceToolSettings,
    source: str,
    route: SourceRoute | None = None,
) -> SourceMetadata:
    selected_route = route or SourceRoute.OCI_MULLVAD
    return fetch_source_metadata_for_route(settings, source, selected_route)


def fetch_source_metadata_for_route(
    settings: SourceToolSettings,
    source: str,
    route: SourceRoute,
    *,
    timeout: int | None = None,
) -> SourceMetadata:
    classified = classify_text(source)
    if classified and classified[0] == "direct_audio_url":
        url = classified[1]
        validate_source_url(url)
        title = Path(url.split("?", 1)[0]).stem or "audio"
        return SourceMetadata(
            title=title.replace("-", " ").strip() or "audio",
            channel="source",
            description="",
            duration_seconds=0,
            # A bare media URL reveals nothing about when it was published.
            published_human=UNKNOWN_PUBLISHED,
            source_url=url,
            source_published_at=None,
            date_basis="unknown",
        )

    url = classified[1] if classified else source
    validate_source_url(url)
    command = _metadata_command_for_route(settings, url, route)
    command_timeout = timeout if timeout is not None else _default_metadata_timeout(route)
    result = run_command(
        command,
        stage="metadata",
        timeout=command_timeout,
        env=sanitized_source_subprocess_env(),
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SourceAudioError("metadata", "yt-dlp returned invalid JSON", result.stdout[:500]) from exc
    title = (data.get("title") or "").strip()
    if not title:
        raise SourceAudioError("metadata", "yt-dlp returned no title", result.stderr)
    return SourceMetadata(
        title=title,
        channel=(data.get("channel") or data.get("uploader") or "").strip(),
        description=(data.get("description") or "").strip(),
        duration_seconds=int(data.get("duration") or 0),
        published_human=_human_date(data.get("upload_date")),
        source_url=url,
        creators=tuple(dict.fromkeys(
            value.strip() for value in (
                str(data.get("creator") or ""),
                str(data.get("artist") or ""),
                str(data.get("uploader") or ""),
            ) if value.strip())),
        series=str(data.get("series") or data.get("playlist_title") or "").strip(),
        source_published_at=_iso_date(data.get("upload_date")),
        date_basis=(
            "extractor_upload_date" if _iso_date(data.get("upload_date")) else "unknown"
        ),
    )


def download_source_to_mp3(
    settings: SourceToolSettings,
    source: str,
    output_path: Path,
    route: SourceRoute | None = None,
) -> None:
    selected_route = route or SourceRoute.OCI_MULLVAD
    download_source_to_mp3_for_route(settings, source, output_path, selected_route)


def download_source_to_mp3_for_route(
    settings: SourceToolSettings,
    source: str,
    output_path: Path,
    route: SourceRoute,
    *,
    timeout: int = 3600,
) -> None:
    classified = classify_text(source)
    if classified and classified[0] == "direct_audio_url":
        validate_source_url(classified[1])
        convert_input_to_mp3(settings, classified[1], output_path, stage="convert")
        return

    url = classified[1] if classified else source
    validate_source_url(url)
    with tempfile.TemporaryDirectory(prefix="rss-audio-source-") as tmpdir:
        source_path = _stage_source_for_route(settings, url, Path(tmpdir), route, timeout=timeout)
        convert_input_to_mp3(settings, str(source_path), output_path, stage="convert")
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    raise SourceAudioError("convert", "ffmpeg completed without creating output")


def convert_input_to_mp3(
    settings: SourceToolSettings,
    source: str,
    output_path: Path,
    *,
    stage: str,
) -> None:
    command = [settings.ffmpeg_bin, "-nostdin", "-y"]
    if _is_http_url(source):
        validate_source_url(source)
        command.extend(["-protocol_whitelist", "http,https,tcp,tls,crypto"])
    command.extend(
        [
            "-i",
            source,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "0",
            str(output_path),
        ]
    )
    run_command(
        command,
        stage=stage,
        timeout=3600,
        env=sanitized_source_subprocess_env(),
    )


def run_command(
    args: list[str],
    *,
    stage: str,
    timeout: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved_args = list(args)
    if resolved_args:
        resolved_args[0] = _resolve_binary(resolved_args[0])

    try:
        if getattr(current_progress(), "authority_guard", False):
            result = run_with_authority(resolved_args, timeout=timeout, cwd=cwd, env=env)
        else:
            result = subprocess.run(
                resolved_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise SourceAudioError(stage, f"Timed out after {timeout}s", str(exc)) from exc
    except FileNotFoundError as exc:
        raise SourceAudioError(stage, f"Executable not found: {args[0]}", str(exc)) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        summary = source_failure_summary(stderr)
        message = summary or f"Command failed with exit code {result.returncode}"
        raise SourceAudioError(stage, message, stderr)
    return result


def _metadata_command_for_route(
    settings: SourceToolSettings,
    url: str,
    route: SourceRoute,
) -> list[str]:
    if route == SourceRoute.MAC_SSH:
        return _mac_metadata_command(settings, url)
    return _local_metadata_command(settings, url)


def _stage_source_for_route(
    settings: SourceToolSettings,
    url: str,
    work_dir: Path,
    route: SourceRoute,
    *,
    timeout: int,
) -> Path:
    if route == SourceRoute.MAC_SSH:
        _stage_mac_source(settings, url, work_dir, timeout=timeout)
    else:
        command = _local_stage_download_command(settings, url, work_dir)
        run_command(command, stage="download", timeout=timeout, env=sanitized_source_subprocess_env())
    return _find_staged_source(work_dir)


def _default_metadata_timeout(route: SourceRoute) -> int:
    return 90 if route == SourceRoute.MAC_SSH else 30


def _mac_metadata_command(settings: SourceToolSettings, url: str) -> list[str]:
    remote_component_args = _remote_component_shell_args(settings)
    remote = (
        "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "
        f"{shlex.quote(settings.mac_ytdlp_path)} "
        "--dump-json --no-playlist "
        "--cookies-from-browser safari "
        f"{remote_component_args} "
        f"{shlex.quote(url)}"
    )
    return ["ssh", settings.mac_ssh_target, remote]


def _local_metadata_command(settings: SourceToolSettings, url: str) -> list[str]:
    command = [
        settings.yt_dlp_bin,
        "--proxy",
        settings.mullvad_proxy,
        "--dump-json",
        "--no-playlist",
    ]
    command.extend(_remote_component_argv(settings))
    command.extend(
        [
            "--extractor-args",
            "youtube:player_client=default,-web,-web_safari,-android_sdkless",
            url,
        ]
    )
    return command


def _mac_stage_download_command(settings: SourceToolSettings, url: str, remote_output_template: str) -> list[str]:
    remote_component_args = _remote_component_shell_args(settings)
    remote = (
        "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; "
        f"{shlex.quote(settings.mac_ytdlp_path)} "
        "--cookies-from-browser safari "
        f"{remote_component_args} "
        f"-f {shlex.quote(settings.yt_dlp_format)} "
        f"-o {shlex.quote(remote_output_template)} --no-playlist "
        f"{shlex.quote(url)}"
    )
    return ["ssh", settings.mac_ssh_target, remote]


def _local_stage_download_command(settings: SourceToolSettings, url: str, work_dir: Path) -> list[str]:
    command = [
        settings.yt_dlp_bin,
        "--proxy",
        settings.mullvad_proxy,
    ]
    command.extend(_remote_component_argv(settings))
    command.extend(
        [
            "--extractor-args",
            "youtube:player_client=default,-web,-web_safari,-android_sdkless",
            "-f",
            settings.yt_dlp_format,
            "-o",
            str(work_dir / "source.%(ext)s"),
            "--no-playlist",
            url,
        ]
    )
    return command


def _stage_mac_source(settings: SourceToolSettings, url: str, work_dir: Path, *, timeout: int) -> None:
    remote_dir = ""
    try:
        created = run_command(
            ["ssh", settings.mac_ssh_target, "mktemp -d /tmp/rss-audio-source-XXXXXXXX"],
            stage="download",
            timeout=30,
            env=sanitized_source_subprocess_env(),
        )
        remote_dir = created.stdout.strip()
        if not remote_dir.startswith("/tmp/rss-audio-source-"):
            raise SourceAudioError("download", f"unexpected remote staging directory: {remote_dir}")
        remote_template = f"{remote_dir}/source.%(ext)s"
        run_command(
            _mac_stage_download_command(settings, url, remote_template),
            stage="download",
            timeout=timeout,
            env=sanitized_source_subprocess_env(),
        )
        run_command(
            ["scp", "-q", f"{settings.mac_ssh_target}:{remote_dir}/source.*", str(work_dir) + "/"],
            stage="download",
            timeout=300,
            env=sanitized_source_subprocess_env(),
        )
    finally:
        if remote_dir.startswith("/tmp/rss-audio-source-"):
            try:
                run_command(
                    ["ssh", settings.mac_ssh_target, f"rm -rf {shlex.quote(remote_dir)}"],
                    stage="cleanup",
                    timeout=30,
                    env=sanitized_source_subprocess_env(),
                )
            except SourceAudioError:
                pass


def _find_staged_source(work_dir: Path) -> Path:
    candidates = [
        path
        for path in work_dir.iterdir()
        if path.is_file() and path.name.startswith("source.") and path.stat().st_size > 0
    ]
    if not candidates:
        raise SourceAudioError("download", "yt-dlp completed without creating a staged source file")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def sanitized_source_subprocess_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not _is_sensitive_env_key(key)}


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper in _SENSITIVE_ENV_EXACT
        or upper.startswith(_SENSITIVE_ENV_PREFIXES)
        or upper.endswith(_SENSITIVE_ENV_SUFFIXES)
    )


def _remote_component_argv(settings: SourceToolSettings) -> list[str]:
    value = settings.yt_dlp_remote_components.strip()
    if not value:
        return ["--no-remote-components"]
    return ["--remote-components", value]


def _remote_component_shell_args(settings: SourceToolSettings) -> str:
    return " ".join(shlex.quote(part) for part in _remote_component_argv(settings))


def _is_http_url(source: str) -> bool:
    return urlsplit(source).scheme in {"http", "https"}


def _validate_resolved_host(host: str, port: int | None) -> None:
    try:
        addresses = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceAudioError("input", f"Could not resolve source host: {host}", str(exc)) from exc
    if not addresses:
        raise SourceAudioError("input", f"Could not resolve source host: {host}")
    for _family, _type, _proto, _canonname, sockaddr in addresses:
        _validate_public_ip(ipaddress.ip_address(sockaddr[0]), host)


def _validate_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> None:
    if not ip.is_global:
        raise SourceAudioError("input", f"Source URL host resolves to a non-public address: {host}")


def _resolve_binary(binary: str) -> str:
    candidate = (binary or "").strip()
    if not candidate:
        return candidate

    expanded = str(Path(candidate).expanduser())
    if "/" in candidate:
        return expanded

    direct = shutil.which(candidate)
    if direct:
        return direct

    try:
        probe = subprocess.run(
            ["bash", "-lc", f"command -v {shlex.quote(candidate)}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return candidate

    resolved = probe.stdout.strip().splitlines()
    return resolved[0].strip() if probe.returncode == 0 and resolved else candidate


UNKNOWN_PUBLISHED = "Unknown"


def _parse_upload_date(upload_date: str | None) -> datetime | None:
    if not upload_date:
        return None
    try:
        return datetime.strptime(str(upload_date), "%Y%m%d")
    except ValueError:
        return None


def _human_date(upload_date: str | None) -> str:
    """Render an extractor upload date, or "Unknown"; never a guessed date."""
    parsed = _parse_upload_date(upload_date)
    return parsed.strftime("%b %d, %Y") if parsed else UNKNOWN_PUBLISHED


def _iso_date(upload_date: str | None) -> str | None:
    """ISO-8601 date for the manifest, or None when genuinely unknown."""
    parsed = _parse_upload_date(upload_date)
    return parsed.strftime("%Y-%m-%d") if parsed else None
