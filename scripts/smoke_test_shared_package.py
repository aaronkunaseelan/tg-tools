#!/usr/bin/env python3
"""Build, install, and import tg-tools-common from a clean source copy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_ROOT = REPO_ROOT / "shared"


def run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tg-tools-package-smoke-") as temporary:
        work = Path(temporary)
        source = work / "source"
        package = source / "tg_tools_common"
        dist = work / "dist"
        install = work / "install"
        source.mkdir()
        dist.mkdir()

        shutil.copy2(SHARED_ROOT / "pyproject.toml", source / "pyproject.toml")
        shutil.copy2(SHARED_ROOT / "README.md", source / "README.md")
        shutil.copytree(
            SHARED_ROOT / "tg_tools_common",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if list(source.glob("*.egg-info")):
            raise RuntimeError("clean package source unexpectedly contains egg-info")

        run(
            [
                sys.executable,
                "-c",
                (
                    "import setuptools.build_meta as backend, sys; "
                    "backend.build_sdist(sys.argv[1])"
                ),
                os.fspath(dist),
            ],
            cwd=source,
        )
        archives = list(dist.glob("*.tar.gz"))
        if len(archives) != 1:
            raise RuntimeError(f"expected one sdist, found {len(archives)}")

        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--target",
                os.fspath(install),
                os.fspath(archives[0]),
            ],
            cwd=work,
        )

        modules = sorted(path.stem for path in package.glob("*.py") if path.stem != "__init__")
        run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import importlib, pathlib, sys; "
                    "root = pathlib.Path(sys.argv[1]).resolve(); "
                    "sys.path.insert(0, str(root)); "
                    "package = importlib.import_module('tg_tools_common'); "
                    "[importlib.import_module('tg_tools_common.' + name) for name in sys.argv[2:]]; "
                    "assert pathlib.Path(package.__file__).resolve().is_relative_to(root)"
                ),
                os.fspath(install),
                *modules,
            ],
            cwd=work,
        )

    print(f"tg-tools-common package smoke passed ({len(modules) + 1} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
