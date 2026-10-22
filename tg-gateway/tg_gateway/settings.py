"""Gateway configuration from the environment.

One gateway process serves one bot identity. The identity names its token
variable and route bindings; its manifests and delivery owners derive from the
identity, so nothing falls back to another one.
"""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SYSTEMD_ENV_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET")
DEFAULT_DATABASE_URL = "dbname=runtime_controller user=ubuntu host=/var/run/postgresql"
DEFAULT_STAGING_DIR = "~/.local/share/tg-tools/source-staging"


@dataclass(frozen=True)
class GatewaySettings:
    bot_identity: str
    bot_username: str
    bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    bot_session_path: Path
    route_bindings: dict[str, int | None]
    allowed_chat_id: int | None
    allowed_thread_id: int | None
    allowed_user_ids: tuple[int, ...]
    production: bool
    staging_dir: Path
    database_url: str
    runtime_controller_url: str
    runtime_controller_token: str
    progress_workloads: frozenset[str]


def load_settings(environ: Mapping[str, str] | None = None) -> GatewaySettings:
    env = os.environ if environ is None else environ

    def get(name: str, default: str = "") -> str:
        return (env.get(name) or default).strip()

    identity = get("TG_GATEWAY_BOT_IDENTITY")
    token_var = get("TG_GATEWAY_BOT_TOKEN_ENV")
    token = get(token_var) if token_var else ""
    api_id = get("TELEGRAM_API_ID")
    api_hash = get("TELEGRAM_API_HASH")
    problems = [f"{name} is required" for name, value in (
        ("TG_GATEWAY_BOT_IDENTITY", identity), ("TG_GATEWAY_BOT_TOKEN_ENV", token_var),
        ("TELEGRAM_API_ID", api_id), ("TELEGRAM_API_HASH", api_hash)) if not value]
    if token_var and not token:
        problems.append(f"{token_var} (named by TG_GATEWAY_BOT_TOKEN_ENV) is empty")

    production = get("TG_GATEWAY_DEPLOYMENT_MODE", "local") == "production"
    allowed_chat = get("TG_GATEWAY_ALLOWED_CHAT_ID")
    allowed_users = tuple(int(part) for part in _csv(get("TG_GATEWAY_ALLOWED_USER_IDS")))
    if production:
        if platform.system() != "Linux" or not any(get(name) for name in SYSTEMD_ENV_VARS):
            problems.append("production mode runs only under systemd on Linux")
        if not allowed_chat or not allowed_users:
            problems.append("production mode requires TG_GATEWAY_ALLOWED_CHAT_ID and TG_GATEWAY_ALLOWED_USER_IDS")
    if problems:
        raise RuntimeError("invalid gateway configuration: " + "; ".join(problems))

    allowed_thread = get("TG_GATEWAY_ALLOWED_THREAD_ID")
    return GatewaySettings(
        bot_identity=identity,
        bot_username=get("TG_GATEWAY_BOT_USERNAME").lower().lstrip("@"),
        bot_token=token,
        telegram_api_id=int(api_id),
        telegram_api_hash=api_hash,
        bot_session_path=_path(get("TG_GATEWAY_BOT_SESSION_PATH", f"~/.local/state/tg-tools/{identity}-bot.session")),
        route_bindings=_route_bindings(get("TG_GATEWAY_ROUTE_BINDINGS_JSON", "{}")),
        allowed_chat_id=int(allowed_chat) if allowed_chat else None,
        allowed_thread_id=int(allowed_thread) if allowed_thread else None,
        allowed_user_ids=allowed_users,
        production=production,
        staging_dir=_path(get("TG_GATEWAY_STAGING_DIR", DEFAULT_STAGING_DIR)),
        database_url=get("TG_GATEWAY_DATABASE_URL", DEFAULT_DATABASE_URL),
        runtime_controller_url=get("RUNTIME_CONTROLLER_API_URL", "http://127.0.0.1:8787").rstrip("/"),
        runtime_controller_token=get("RUNTIME_CONTROLLER_BEARER_TOKEN"),
        progress_workloads=frozenset(_csv(get("TG_GATEWAY_PROGRESS_WORKLOADS")))
        if get("TG_TOOLS_PROGRESS_ENABLED") == "1" else frozenset(),
    )


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def _route_bindings(raw: str) -> dict[str, int | None]:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not all(
            isinstance(key, str) and (value is None or isinstance(value, int)) for key, value in payload.items()):
        raise ValueError("TG_GATEWAY_ROUTE_BINDINGS_JSON must map names to integers or null")
    return payload
