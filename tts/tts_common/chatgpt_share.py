"""ChatGPT public share adapter."""

from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (compatible; tts-tool/1.0)"
MAX_BYTES = 10 * 1024 * 1024


def fetch_share_html(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - caller supplies public share URL
        return resp.read(MAX_BYTES + 1)[:MAX_BYTES]


def _decode_escapes(s: str) -> str:
    if "\\" in s:
        try:
            s = s.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
    return html.unescape(s)


def _walk(obj: Any):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_text_from_content(x) for x in content)))
    if isinstance(content, dict):
        if content.get("content_type") in {"text", "multimodal_text"}:
            parts = content.get("parts") or []
            return _text_from_content(parts)
        for key in ("text", "value", "markdown"):
            if key in content:
                return _text_from_content(content.get(key))
        if "parts" in content:
            return _text_from_content(content.get("parts"))
    return ""


def _extract_citations(obj: Any) -> list[str]:
    urls: list[str] = []
    for node in _walk(obj):
        if isinstance(node, dict):
            for key in ("url", "uri"):
                val = node.get(key)
                if isinstance(val, str) and val.startswith(("http://", "https://")):
                    urls.append(val)
    seen = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _title_from_html(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else "ChatGPT conversation"
    title = re.sub(r"^ChatGPT\s*[-|]\s*", "", title).strip()
    title = re.sub(r"\s*[-|]\s*ChatGPT\s*$", "", title).strip()
    return title or "ChatGPT conversation"


def _resolve_rr_root(pool: list[Any]) -> Any:
    memo: dict[int, Any] = {}
    active: set[int] = set()

    def one(obj: Any, depth: int = 0) -> Any:
        if depth > 60:
            return obj
        if isinstance(obj, int) and 0 <= obj < len(pool):
            if obj in memo:
                return memo[obj]
            if obj in active:
                return None
            active.add(obj)
            val = one(pool[obj], depth + 1)
            active.discard(obj)
            memo[obj] = val
            return val
        if isinstance(obj, list):
            return [one(v, depth + 1) for v in obj]
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key = k
                if isinstance(k, str) and k.startswith("_") and k[1:].isdigit():
                    rv = one(int(k[1:]), depth + 1)
                    if isinstance(rv, str):
                        key = rv
                out[key] = one(v, depth + 1)
            return out
        return obj

    return one(0)


def _json_objects_from_html(text: str) -> list[Any]:
    objs: list[Any] = []
    for m in re.finditer(r"streamController\.enqueue\(", text):
        try:
            i = m.end()
            while i < len(text) and text[i].isspace():
                i += 1
            dec = json.JSONDecoder()
            payload, _ = dec.raw_decode(text[i:])
            arr = json.loads(payload)
            if isinstance(arr, list):
                objs.append(_resolve_rr_root(arr))
        except Exception:
            pass
    dec = json.JSONDecoder()
    candidates = []
    candidates.extend(re.findall(r"<script[^>]*>(.*?)</script>", text, re.I | re.S))
    candidates.append(text)
    for blob in candidates:
        if "linear_conversation" not in blob and "mapping" not in blob and "messages" not in blob:
            continue
        for marker in ("linear_conversation", "mapping", "messages"):
            start = 0
            while True:
                pos = blob.find(marker, start)
                if pos < 0:
                    break
                j = blob.rfind("{", 0, pos)
                while j >= 0:
                    try:
                        obj, _ = dec.raw_decode(blob[j:])
                        objs.append(obj)
                        break
                    except Exception:
                        j = blob.rfind("{", 0, j)
                start = pos + len(marker)
    return objs


def _turns_from_obj(obj: Any) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    title = None
    turns: list[dict[str, Any]] = []
    citations = _extract_citations(obj)
    for node in _walk(obj):
        if isinstance(node, dict) and isinstance(node.get("title"), str) and not title:
            title = node["title"]
        msg = None
        if isinstance(node, dict) and isinstance(node.get("message"), dict):
            msg = node["message"]
        elif isinstance(node, dict) and ("author" in node or "role" in node) and ("content" in node or "text" in node):
            msg = node
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        role = author.get("role") if isinstance(author, dict) else msg.get("role")
        role = msg.get("role") or role
        if role not in {"user", "assistant"}:
            continue
        text = _text_from_content(msg.get("content") or msg.get("text"))
        text = re.sub(r"\n{3,}", "\n\n", _decode_escapes(text)).strip()
        if not text:
            continue
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            continue
        if role == "assistant" and len(text) < 1000:
            continue
        if any(t["role"] == role and t["text"] == text for t in turns):
            continue
        turns.append({"role": role, "text": text})
    return title, turns, citations


def parse_share_html(data: bytes | str, source_url: str = "") -> dict[str, Any]:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    title = _title_from_html(text)
    turns: list[dict[str, Any]] = []
    citations: list[str] = []
    for obj in _json_objects_from_html(text):
        t, ts, cs = _turns_from_obj(obj)
        if t and (title == "ChatGPT conversation" or len(t) > len(title)):
            title = t
        if len(ts) > len(turns):
            turns = ts
        citations.extend(cs)
    if not turns:
        turns = _fallback_regex_turns(text)
    seen = set()
    citations = [u for u in citations if not (u in seen or seen.add(u))]
    meta_turns = [{"role": t["role"], "kind": "question" if t["role"] == "user" else "answer", "chars": len(t["text"])} for t in turns]
    return {"title": title, "turns": turns, "meta_turns": meta_turns, "citations": citations, "source_url": source_url}


def _fallback_regex_turns(text: str) -> list[dict[str, str]]:
    out = []
    for role, body in re.findall(r'"role"\s*:\s*"(user|assistant)".*?"parts"\s*:\s*\[(.*?)\]', text, re.S):
        vals = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        clean = "\n".join(_decode_escapes(v).strip() for v in vals if v.strip())
        if clean:
            out.append({"role": role, "text": clean})
    return out


def render_conversation(parsed: dict[str, Any]) -> str:
    lines = [f"# {parsed.get('title') or 'ChatGPT conversation'}", ""]
    q = a = 0
    for turn in parsed.get("turns", []):
        if turn["role"] == "user":
            q += 1
            lines += [f"## Q{q}", "", turn["text"].strip(), ""]
        elif turn["role"] == "assistant":
            a += 1
            lines += [f"## A{a}", "", turn["text"].strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def materialise_share(url: str, out_dir: str | Path, html_bytes: bytes | None = None) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = html_bytes if html_bytes is not None else fetch_share_html(url)
    parsed = parse_share_html(data, url)
    md = render_conversation(parsed)
    md_path = out / "conversation.md"
    meta_path = out / "conversation.meta.json"
    md_path.write_text(md, encoding="utf-8")
    meta = {"title": parsed["title"], "turns": parsed["meta_turns"], "citations": parsed["citations"], "source_url": url}
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return md_path, meta_path
