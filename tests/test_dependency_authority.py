from __future__ import annotations

import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_requirements_workspace  # noqa: E402


def test_pyproject_is_the_single_dependency_version_authority() -> None:
    assert generate_requirements_workspace.OUTPUT.read_text(encoding="utf-8") == (
        generate_requirements_workspace.render_requirements()
    )
    assert not (REPO_ROOT / "rss-audio" / "requirements.txt").exists()
    assert not (REPO_ROOT / "meeting-summary" / "requirements.txt").exists()


def test_build_requirements_are_exact_pins() -> None:
    for pyproject in (REPO_ROOT / "pyproject.toml", REPO_ROOT / "shared" / "pyproject.toml"):
        build_requirements = tomllib.loads(pyproject.read_text(encoding="utf-8"))["build-system"]["requires"]
        assert build_requirements
        assert all("==" in requirement for requirement in build_requirements)
