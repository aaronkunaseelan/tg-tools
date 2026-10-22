from __future__ import annotations

import json
import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tg_tools_cli", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_validate_service_uses_manifest() -> None:
    result = _run("validate", "service", "rss-audio")

    assert result.returncode == 0
    assert result.stdout.strip() == "service rss-audio valid"


def test_routes_explain_outputs_manifest_json() -> None:
    result = _run("routes", "explain", "--service", "meeting-summary", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["service"] == "meeting-summary"
    assert payload["routes"][0]["route_id"] == "meeting.note.telegram.v1"


def test_unknown_service_fails_cleanly() -> None:
    result = _run("validate", "service", "missing")

    assert result.returncode == 2
    assert "unknown service" in result.stderr
