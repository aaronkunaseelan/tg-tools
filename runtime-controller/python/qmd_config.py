"""Local QMD checkout configuration for Python entrypoints."""

import os
from pathlib import Path


def qmd_repo_root(value: object = None) -> Path:
    candidate = str(value or os.environ.get("RUNTIME_CONTROLLER_QMD_REPO_ROOT", "")).strip()
    if not candidate:
        raise ValueError("RUNTIME_CONTROLLER_QMD_REPO_ROOT is required")
    return Path(candidate).expanduser().resolve()
