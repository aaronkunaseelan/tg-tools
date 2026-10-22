"""Faithful-mode artifact compiler."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tg_tools_common.progress import tracked_stage


def _read_part(part: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(part, dict):
        md_path = Path(part.get("markdown_path") or part.get("path") or part.get("source_md") or "")
        meta_path = Path(part.get("meta_path") or part.get("meta") or md_path.with_suffix(".meta.json"))
    else:
        md_path = Path(part)
        meta_path = md_path.with_suffix(".meta.json")
    md = md_path.read_text(encoding="utf-8")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.setdefault("markdown_sha256", hashlib.sha256(md.encode("utf-8")).hexdigest())
    meta.setdefault("source_ref", str(md_path))
    return md, meta


def _frontmatter(metas: list[dict[str, Any]], policy: dict[str, Any] | None) -> str:
    created = datetime.now(timezone.utc).isoformat()
    ident = hashlib.sha256(json.dumps(metas, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    lines = ["---", f"id: {ident}", "mode: faithful", f"created_at: {created}", "sources:"]
    for m in metas:
        lines.append(f"  - ref: {json.dumps(m.get('source_ref') or m.get('source'))}")
        lines.append(f"    source_sha256: {json.dumps(m.get('source_sha256', ''))}")
        lines.append(f"    markdown_sha256: {json.dumps(m.get('markdown_sha256', ''))}")
    if policy:
        lines.append(f"policy: {json.dumps(policy, sort_keys=True)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _clean(md: str) -> str:
    return md.strip() + "\n"


@tracked_stage("prepare_reading")
def compile_artifact(materialised_parts: list[Any], input_class: str, policy: dict[str, Any] | None = None, out_path: str | Path | None = None) -> Path:
    parts = [_read_part(p) for p in materialised_parts]
    metas = [m for _, m in parts]
    body = ""
    if input_class == "single_piece":
        body = _clean(parts[0][0])
    elif input_class == "conversation":
        body = _clean(parts[0][0])
    elif input_class == "multipart_bundle":
        chunks = []
        for i, (md, meta) in enumerate(parts, 1):
            title = meta.get("title") or f"Part {i}"
            chunks.append(f"# Part {i}: {title}\n\n{_clean(md)}")
        body = "\n".join(chunks)
    else:
        body = "\n\n".join(_clean(md) for md, _ in parts)
    text = _frontmatter(metas, policy) + body
    path = Path(out_path) if out_path else Path("artifact.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
