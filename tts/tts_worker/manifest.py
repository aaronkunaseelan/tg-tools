from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_manifest(path: Path, *, source: dict[str, Any], artifacts: dict[str, Path], chunk_receipts: list[dict[str, Any]], model_usage: dict[str, Any] | None, voice: str, timings: dict[str, Any], warnings: list[str], presentation: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": source,
        "artifacts": {name: {"sha256": sha256_file(p), "bytes": p.stat().st_size} for name, p in artifacts.items() if p.exists()},
        "chunkReceipts": chunk_receipts,
        "modelUsage": model_usage or {},
        "voice": voice,
        "timings": timings,
        "warnings": warnings,
        "presentation": presentation or {},
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return data
