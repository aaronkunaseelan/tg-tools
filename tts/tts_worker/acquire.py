from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tg_tools_common.progress import tracked_stage
from tts_common import typed_fetch

class AcquireError(RuntimeError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class AcquiredSource:
    path: Path
    source_ref: str
    source_hash: str
    mime_type: str
    ext: str
    original_name: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_staged_file(p: Path, *, expected_sha256: Any = None, expected_size: Any = None) -> bytes:
    """Read a staged source with path safety.

    Rejects symlinks, FIFOs/devices, paths outside the configured staging
    root (TTS_STAGING_ROOT, when set), and size/hash changes since staging.
    The returned bytes are copied into fingerprint work state by the caller.
    """
    root = os.environ.get("TTS_STAGING_ROOT", "")
    if root:
        resolved = p.resolve()
        root_path = Path(root).resolve()
        if root_path != resolved and root_path not in resolved.parents:
            raise AcquireError("invalid_input", f"staged path outside staging root: {p}")
    if p.is_symlink():
        raise AcquireError("invalid_input", f"staged path is a symlink: {p}")
    if not p.is_file():
        raise AcquireError("invalid_input", f"staged path is not a regular file: {p}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise AcquireError("source_fetch_transient", str(exc)) from exc
    if expected_size is not None and len(data) != int(expected_size):
        raise AcquireError("invalid_input", "staged file size changed since staging")
    if expected_sha256 and _sha(data) != str(expected_sha256):
        raise AcquireError("invalid_input", "staged file hash changed since staging")
    return data


def _ext(name: str, mime: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix and len(suffix) <= 10:
        return suffix.lstrip(".")
    guessed = mimetypes.guess_extension(mime) or ".txt"
    return guessed.lstrip(".")


@tracked_stage("acquire_source")
def acquire_source(input_json: dict[str, Any], work_dir: Path, *, fetcher: Any | None = None) -> AcquiredSource:
    work_dir.mkdir(parents=True, exist_ok=True)
    source_key = str(input_json.get("sourceKey") or "")
    data: bytes
    source_ref: str
    inline_text_source = False
    mime = str(input_json.get("mimeType") or "text/plain")
    name = str(input_json.get("telegramFileName") or "source.txt")
    if input_json.get("localSourcePath"):
        p = Path(str(input_json["localSourcePath"]))
        data = _read_staged_file(p, expected_sha256=input_json.get("stagedSha256"), expected_size=input_json.get("stagedSizeBytes"))
        source_ref = str(p)
        mime = str(input_json.get("mimeType") or mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        name = p.name
    elif input_json.get("sourceUrl"):
        url = str(input_json["sourceUrl"])
        try:
            if fetcher is not None:
                data, mime = fetcher(url)
            else:
                fetched = typed_fetch.fetch_url(url, work_dir / "fetch")
                data, mime = fetched.data, fetched.declared_mime
        except typed_fetch.TypedFetchError as exc:
            raise AcquireError(exc.error_class, str(exc)) from exc
        except AcquireError:
            raise
        except Exception as exc:
            raise AcquireError("source_fetch_transient", str(exc)) from exc
        source_ref = url
        # Effective name: fetch metadata filename first, then final-URL
        # suffix, then requested-URL suffix — so binary-claim conflict checks
        # still see the claim after redirects.
        name = ""
        if fetcher is None:
            name = fetched.filename or Path(fetched.final_url.split("?", 1)[0]).name
        name = name or Path(url.split("?", 1)[0]).name or "source.html"
    elif "sourceText" in input_json:
        source_text = input_json["sourceText"]
        if not isinstance(source_text, str):
            raise AcquireError("invalid_input", "sourceText must be a string")
        data = source_text.encode("utf-8")
        source_ref = source_key
        mime = "text/plain"
        name = "source.txt"
        inline_text_source = True
    else:
        raise AcquireError("input_empty", "unsupported or empty TTS input")
    if not data.strip():
        raise AcquireError("input_empty", "source is empty")
    cap = typed_fetch.max_source_bytes()
    if len(data) > cap:
        raise AcquireError("source_too_large", f"source is {len(data)} bytes over cap {cap}")
    digest = _sha(data)
    # Deterministic effective type: strong signature > declared MIME > safe
    # suffix > clean-text fallback. Binary claims carrying HTML become
    # source_access_blocked / source_type_mismatch.
    if inline_text_source:
        ext = "txt"
    else:
        try:
            ext = typed_fetch.effective_extension(data=data, declared_mime=mime, name=name)
        except typed_fetch.TypedFetchError as exc:
            raise AcquireError(exc.error_class, str(exc)) from exc
    out = work_dir / f"source-original.{ext}"
    out.write_bytes(data)
    return AcquiredSource(path=out, source_ref=source_ref, source_hash=digest, mime_type=mime, ext=ext, original_name=name)
