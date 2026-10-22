#!/usr/bin/env python3
"""Validate the tg-tools development checkout in isolated component processes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = os.environ.get(
    "TG_TOOLS_PYTHON", sys.executable)


@dataclass(frozen=True)
class ValidationCommand:
    label: str
    args: tuple[str, ...]
    cwd: str = "."

    @property
    def working_dir(self) -> Path:
        return REPO_ROOT / self.cwd

    def display(self) -> str:
        return f"cd {self.cwd} && {' '.join(self.args)}"


# Each package runs in its own pytest process: its pytest.ini owns the import path,
# and no production .env or secret file is ever loaded into a test environment.
PACKAGES = ("shared", "tg-gateway", "rss-audio", "meeting-summary", "tts", "transcribe-audio", ".")


def pytest_command(cwd: str, *marker: str) -> ValidationCommand:
    paths = ("tests", "pi_harness/tests") if cwd == "shared" else ("tests",)
    return ValidationCommand(label=cwd if cwd != "." else "root",
                             args=(VENV_PYTHON, "-m", "pytest", "-q", *marker, *paths), cwd=cwd)


def quick_commands() -> tuple[ValidationCommand, ...]:
    return tuple(pytest_command(cwd, "-m", "not chain") for cwd in PACKAGES)


def full_commands() -> tuple[ValidationCommand, ...]:
    # Full adds the native lane chains (private PostgreSQL + controller) and the
    # installer boundary: build and import tg-tools-common from a clean copy.
    return (ValidationCommand(label="shared-package", args=(VENV_PYTHON, "scripts/smoke_test_shared_package.py")),
            *(pytest_command(cwd) for cwd in PACKAGES))


SUITES: dict[str, tuple[ValidationCommand, ...]] = {
    "quick": quick_commands(),
    "full": full_commands(),
}


def list_suites() -> None:
    for suite_name, commands in SUITES.items():
        print(f"{suite_name}:")
        for command in commands:
            print(f"  [{command.label}] {command.display()}")


def run_suite(suite_name: str, commands: Sequence[ValidationCommand]) -> int:
    print(f"Running {suite_name} checkout suite ({len(commands)} commands)")
    failures = 0
    for command in commands:
        print(f"\n[{command.label}] {command.display()}", flush=True)
        result = subprocess.run(
            command.args,
            cwd=command.working_dir,
            check=False,
        )
        if result.returncode == 0:
            print(f"[{command.label}] ok", flush=True)
        else:
            failures += 1
            print(f"[{command.label}] failed: exit {result.returncode}", flush=True)

    if failures:
        print(f"\n{failures} validation command(s) failed")
        return 1

    print("\nAll validation commands passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the tg-tools development checkout in isolated subprocesses."
    )
    suite = parser.add_mutually_exclusive_group(required=True)
    suite.add_argument("--quick", action="store_true", help="Run the quick suite")
    suite.add_argument("--full", action="store_true", help="Run the full suite")
    suite.add_argument("--list", action="store_true", help="List available suites")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        list_suites()
        return 0

    suite_name = "quick" if args.quick else "full"
    return run_suite(suite_name, SUITES[suite_name])


if __name__ == "__main__":
    sys.exit(main())
