"""Provider availability preflight."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .runner import ENV_DENY, PiRunner, pruned_env

# Codex/OpenAI access must be OAuth (ChatGPT login), never a static API key.
# The authoritative record is the codex auth store, not config.toml: with
# `auth_mode == "chatgpt"` a bogus OPENAI_API_KEY in the environment is
# ignored, and `--ignore-user-config` does not weaken it (both verified).
# An API key appears here only if someone ran
# `codex login --api-key`, which is exactly what this guards against.
CODEX_AUTH_PATH = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


class AuthModeError(RuntimeError):
    """Codex credentials are not OAuth."""


def assert_codex_oauth(path: Path | None = None) -> str:
    """Assert codex auth is OAuth. Returns the auth mode; raises otherwise.

    Checks three things, cheaply and offline:
      1. no API key is present in the process environment,
      2. the auth store records `auth_mode == "chatgpt"`,
      3. the auth store carries no embedded API key.
    """
    leaked = sorted(n for n in ENV_DENY if os.environ.get(n))
    if leaked:
        raise AuthModeError(
            "API key present in the environment (" + ", ".join(leaked) +
            "); codex access must be OAuth only. Unset it.")

    store = Path(path) if path is not None else CODEX_AUTH_PATH
    if not store.is_file():
        raise AuthModeError(f"no codex auth store at {store}; run `codex login`")
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthModeError(f"unreadable codex auth store {store}: {exc}") from exc

    mode = str(data.get("auth_mode") or "")
    if mode != "chatgpt":
        raise AuthModeError(
            f"codex auth_mode is {mode!r}, expected 'chatgpt' (OAuth). "
            "Re-authenticate with `codex login`.")
    if data.get("OPENAI_API_KEY"):
        raise AuthModeError(
            f"{store} carries an embedded OPENAI_API_KEY; codex access must be "
            "OAuth only. Re-authenticate with `codex login`.")
    if not (data.get("tokens") or {}).get("refresh_token"):
        raise AuthModeError(f"{store} has auth_mode 'chatgpt' but no refresh token")
    return mode


def assert_claude_oauth() -> str:
    """Assert `claude -p` runs on the subscription login, not an API key.

    The runner strips ANTHROPIC_API_KEY, so check the login it will actually
    see: `claude auth status` under the same pruned environment.
    """
    proc = subprocess.run(["claude", "auth", "status"], env=pruned_env("claude"),
                          capture_output=True, text=True, timeout=30)
    status = json.loads(proc.stdout or "{}")
    method = str(status.get("authMethod") or "")
    if not status.get("loggedIn") or method != "claude.ai":
        raise AuthModeError(
            f"claude auth is {method or 'logged out'!r}, expected 'claude.ai' "
            "(subscription OAuth). Run `claude auth login`.")
    return method


PROVIDER_AUTH = {"openai-codex": assert_codex_oauth, "claude": assert_claude_oauth}


def assert_provider_auth(providers) -> list[str]:
    """Run the auth check for each distinct provider; returns the checked names."""
    checked = sorted({p for p in providers if p in PROVIDER_AUTH})
    for provider in checked:
        PROVIDER_AUTH[provider]()
    return checked


def preflight_provider(role: str, timeout_s=60) -> tuple[bool, str]:
    """Check that the provider can produce the expected quota probe reply."""
    if os.environ.get("TTS_PI_MOCK") == "1":
        return True, "mock"
    with tempfile.TemporaryDirectory(prefix="tts-pi-preflight-") as cwd:
        result = PiRunner(role, cwd=cwd).run(
            "Reply with exactly: QUOTA-OK", allow_tools=False,
            timeout_s=timeout_s)
    if result.ok and "QUOTA-OK" in result.text:
        return True, result.text
    return False, result.error or result.text
