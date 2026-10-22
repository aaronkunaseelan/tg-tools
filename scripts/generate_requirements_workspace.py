#!/usr/bin/env python3
"""Generate the exact-pinned workspace requirements from pyproject.toml."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
OUTPUT = REPO_ROOT / "requirements-workspace.txt"
EXACT_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+==[^<>=!~]+$")


def render_requirements() -> str:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]
    invalid = [dependency for dependency in dependencies if not EXACT_REQUIREMENT.fullmatch(dependency)]
    if invalid:
        raise ValueError(f"workspace dependencies must be exact pins: {invalid}")
    return "\n".join(dependencies) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render_requirements()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("requirements-workspace.txt is stale", file=sys.stderr)
            return 1
    else:
        OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
