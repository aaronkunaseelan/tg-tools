from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_OAUTH_REFRESH_WINDOW_MS = 60 * 60 * 1000


@dataclass(frozen=True)
class OpenAICodexAuth:
    token: str
    account_id: str


class OpenAIAuthError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _store_refreshed_entry(path: Path, previous: dict, updated: dict) -> dict:
    # Use Pi's actual credential owner: its shared lock and latest-file merge.
    # A concurrent rotation/deletion of this provider wins over our stale refresh.
    pi = shutil.which("pi")
    if not pi:
        raise OpenAIAuthError("Pi credential storage unavailable")
    module = Path(pi).resolve().parents[1] / "core" / "auth-storage.js"
    script = """
        import { readFileSync } from 'node:fs';
        const { AuthStorage } = await import(process.argv[1]);
        const { path, previous, updated } = JSON.parse(readFileSync(0, 'utf8'));
        const storage = AuthStorage.create(path);
        const result = await storage.modify('openai-codex', async current => {
            if (JSON.stringify(current) !== JSON.stringify(previous)) return undefined;
            return updated;
        });
        process.stdout.write(JSON.stringify(result ?? {}));
    """
    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, module.as_uri()],
            input=json.dumps({"path": str(path.resolve()), "previous": previous, "updated": updated}),
            text=True, capture_output=True, timeout=60, check=True,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # Child output may carry credentials; never put it into the error.
        raise OpenAIAuthError("Pi credential mutation failed") from None


def _refresh_openai_codex_oauth(auth_path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(entry.get("refresh") or "").strip()
    if not refresh_token:
        raise OpenAIAuthError("openai-codex token expired and refresh token is missing")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OPENAI_OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:1000]
        raise OpenAIAuthError(f"openai-codex OAuth refresh failed with HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise OpenAIAuthError(f"openai-codex OAuth refresh failed: {exc}") from exc

    access_token = str(payload.get("access_token") or "").strip()
    new_refresh_token = str(payload.get("refresh_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not access_token or not new_refresh_token or expires_in <= 0:
        raise OpenAIAuthError("openai-codex OAuth refresh returned incomplete token payload")

    updated = {
        **entry,
        "access": access_token,
        "refresh": new_refresh_token,
        "expires": int(time.time() * 1000) + expires_in * 1000,
    }
    return _store_refreshed_entry(auth_path, entry, updated)


def load_openai_codex_auth(auth_path: Path) -> OpenAICodexAuth:
    if not auth_path.exists():
        raise OpenAIAuthError(f"auth file missing: {auth_path}")
    auth_data = _read_json(auth_path)
    entry = auth_data.get("openai-codex") or {}
    if not isinstance(entry, dict):
        raise OpenAIAuthError("openai-codex entry malformed in auth file")
    if entry.get("type") != "oauth":
        raise OpenAIAuthError("openai-codex entry is not OAuth-backed")
    if not entry.get("accountId"):
        raise OpenAIAuthError("openai-codex entry missing accountId")

    now_ms = int(time.time() * 1000)
    expires = int(entry.get("expires") or 0)
    access = str(entry.get("access") or "").strip()
    should_refresh = not access or (expires and expires <= now_ms + OPENAI_OAUTH_REFRESH_WINDOW_MS)
    if should_refresh:
        entry = _refresh_openai_codex_oauth(auth_path, entry)
        if entry.get("type") != "oauth" or not entry.get("accountId"):
            raise OpenAIAuthError("openai-codex credentials changed during refresh")
        access = str(entry.get("access") or "").strip()
        expires = int(entry.get("expires") or 0)

    if not access:
        raise OpenAIAuthError("openai-codex entry missing valid oauth access token")
    if expires and expires <= now_ms:
        raise OpenAIAuthError("openai-codex token expired and refresh did not recover it")
    return OpenAICodexAuth(token=access, account_id=str(entry["accountId"]))
