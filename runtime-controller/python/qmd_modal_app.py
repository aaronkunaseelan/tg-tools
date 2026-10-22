#!/usr/bin/env python3
from __future__ import annotations

"""Modal execution target for QMD embedding compute.

This file lives in runtime-controller because runtime-controller owns provider
integration and OCI-vs-Modal placement policy. QMD still owns embedding
semantics and bounded compute entrypoints.
"""

import os
import sys
from pathlib import Path

import modal

from qmd_config import qmd_repo_root

QMD_REPO_ROOT = qmd_repo_root()
QMD_REMOTE_ROOT = "/root/qmd"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("rclone")
    .pip_install(
        "requests>=2.31",
        "fastembed>=0.8.0",
        "pyarrow>=16.0",
        "psycopg2-binary>=2.9",
    )
    .add_local_dir(str(QMD_REPO_ROOT), QMD_REMOTE_ROOT)
)

app = modal.App("qmd-embedding-burst", tags={"project": "qmd", "workload": "embedding"})


def _ensure_qmd_importable() -> None:
    if QMD_REMOTE_ROOT not in sys.path:
        sys.path.insert(0, QMD_REMOTE_ROOT)


@app.function(image=image, timeout=60 * 60)
def run_burst(max_jobs: int = 1, idle_timeout_sec: int = 60) -> dict[str, object]:
    _ensure_qmd_importable()
    os.environ.setdefault("QMD_EMBED_WORKER_VENUE", "modal")
    from qmd.worker import run_worker

    exit_code = run_worker(max_jobs=max_jobs, idle_timeout_sec=idle_timeout_sec)
    return {"ok": exit_code == 0, "exit_code": exit_code, "max_jobs": max_jobs, "idle_timeout_sec": idle_timeout_sec}


@app.function(image=image, timeout=60 * 60)
def run_benchmark(
    input_manifest: str,
    venue_label: str = "modal",
    publish: bool = True,
    gpu_type: str = "L4",
    assume_billed_equals_wall: bool = True,
) -> dict[str, object]:
    _ensure_qmd_importable()
    from qmd.benchmark_canary import run_manifest_benchmark

    summary = run_manifest_benchmark(
        input_manifest_target=input_manifest,
        venue=venue_label,
        publish=publish,
        gpu_type=gpu_type,
        assume_billed_equals_wall=assume_billed_equals_wall,
    )
    return summary.to_dict()
