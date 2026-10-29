"""Installer/consumer boundary: every shipped systemd unit runs promoted release
code with the pinned runtime, and its entrypoint imports under exactly the
unit's PYTHONPATH and WorkingDirectory (current/ mapped onto this checkout,
which has the release layout).

| check              | rule                                                                    |
| layout             | WorkingDirectory and every PYTHONPATH entry under current/; runtime python |
| host independence  | only %h paths; no checkout path, no user name                           |
| configuration      | product settings come from ~/.config/tg-tools/<product>.env, loaded last |
| entrypoint         | `-m mod`, script path or `-c "from x import main"` imports, main not run |
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CURRENT = "%h/.local/lib/tg-tools/current/"
PYTHON = "%h/.local/lib/tg-tools/runtime/bin/python"
UNITS = sorted((REPO / "systemd").glob("*.service"))


def directives(unit: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in unit.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found.setdefault(key, []).append(value)
    return found


def pythonpath(d: dict[str, list[str]]) -> str:
    return next(value.split("=", 1)[1] for value in d["Environment"] if value.startswith("PYTHONPATH="))


def test_units_shipped():
    assert [unit.stem for unit in UNITS] == [
        "meeting-summary-result-consumer", "meeting-summary-runtime-worker", "rss-audio-runtime-worker",
        "rss-audio-transcribe-runtime-worker", "tg-gateway-ms", "tg-gateway-rss", "tg-tools-telemetry", "tts-result-projector",
        "tts-worker"]


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.stem)
def test_unit_layout(unit):
    d = directives(unit)
    text = unit.read_text()
    assert d["WorkingDirectory"][0].startswith(CURRENT)
    assert all(path.startswith(CURRENT) for path in pythonpath(d).split(":"))
    assert d["ExecStart"][0].startswith(PYTHON + " ")
    assert "/home/" not in text and "tools/tg-tools" not in text
    assert d["EnvironmentFile"][-1].startswith("%h/.config/tg-tools/")


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.stem)
def test_unit_entrypoint_imports(unit):
    d = directives(unit)
    argv = shlex.split(d["ExecStart"][0].replace(CURRENT, f"{REPO}/"))[1:]
    if argv[0] == "-m":
        probe = f"import importlib; importlib.import_module({argv[1]!r})"
    elif argv[0] == "-c":
        probe = argv[1].split(";")[0]
    else:
        probe = f"import runpy; runpy.run_path({argv[0]!r}, run_name='unit_import')"
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent",
           "PYTHONPATH": pythonpath(d).replace(CURRENT, f"{REPO}/")}
    result = subprocess.run([sys.executable, "-c", probe],
                            cwd=d["WorkingDirectory"][0].replace(CURRENT, f"{REPO}/"), env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
