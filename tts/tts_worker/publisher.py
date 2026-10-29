from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

from tg_tools_common.progress import tracked_stage
from tg_tools_common.rclone_upload import immutable_copy_argv


class PublishError(RuntimeError):
    def __init__(self, error_class: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status


@dataclass(frozen=True)
class PublishedEpisode:
    slug: str
    title: str
    mp3_url: str
    md_url: str
    speech_text_url: str
    manifest_url: str
    source_md_url: str
    feed_url: str
    duration: str | None
    mp3_size: int


def slugify_title(title: str, identity: str, now: datetime | None = None) -> str:
    """Readable slug (metadata only, never the sole identity); carries a
    12-char fingerprint suffix for diagnostics."""
    dt = now or datetime.now(UTC)
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "tts"
    words = [w for w in base.split("-") if w][:8]
    return f"{dt:%Y-%m-%d}-{'-'.join(words)}-{identity[:12]}"


def _atomic_write(path: Path, text: str) -> None:
    """Durably replace ``path`` with ``text``.

    These markers are the publish barrier's only crash-recovery record, so
    they need the same durability as the generation checkpoint: fsync the
    payload before the rename and fsync the directory after it. Without the
    directory fsync a power loss can lose the rename itself, which for a
    prepared marker means a retry mints a fresh slug for an episode whose
    artifacts were already uploaded under the old one.
    """
    directory = path.parent
    handle, tmp_name = tempfile.mkstemp(
        dir=os.fspath(directory), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(os.fspath(tmp), os.fspath(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    dir_fd = os.open(os.fspath(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_marker(path: Path, required: tuple[str, ...]) -> dict[str, Any] | None:
    """Read a marker; quarantine corrupt/schema-mismatched markers."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rec, dict) and all(k in rec for k in required):
            return rec
    except (OSError, json.JSONDecodeError):
        pass
    try:
        path.replace(path.with_suffix(path.suffix + ".quarantined"))
    except OSError:
        pass
    return None


def peek_marker(path: Path, required: tuple[str, ...]) -> dict[str, Any] | None:
    """Read a marker without quarantining it.

    Callers outside the publish barrier (the worker deciding which prefix to
    render into documents) must not mutate publish state as a side effect of
    looking at it; quarantine stays the barrier's own decision.
    """
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(rec, dict) and all(k in rec for k in required):
        return rec
    return None


def pinned_artifact_prefix(state_dir: Path | None, identity: str) -> str | None:
    """Return the slug an earlier attempt pinned for ``identity``, if any.

    The publisher owns the key scheme. Before a retry renders any URL into a
    document it asks here, so the notes/script/manifest link the keys that
    publication will actually write instead of a freshly recomputed guess.
    """
    if not state_dir:
        return None
    prepared = Path(state_dir) / "publish.prepared.json"
    if not prepared.exists():
        return None
    rec = peek_marker(prepared, ("slug",))
    if not rec or rec.get("identity") != identity:
        return None
    slug = str(rec.get("slug") or "").strip()
    return slug or None


def public_url(base: str, key: str) -> str:
    return base.rstrip("/") + "/" + key.lstrip("/")


def _run(args: list[str], *, runner: Any | None = None) -> None:
    try:
        if runner is not None:
            runner(args)
        else:
            subprocess.run(args, text=True, capture_output=True, check=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        raise PublishError("publish_failed", (exc.stderr or str(exc))[:500]) from exc
    except Exception as exc:
        raise PublishError("publish_failed", str(exc)) from exc


def _copyto(bin_path: str, remote: str, bucket: str, local: Path, key: str, runner: Any | None) -> None:
    # Never overwrite an existing key with different bytes; a modification is an error.
    with immutable_copy_argv(bin_path, local, f"{remote}:{bucket}/{key}") as argv:
        _run(argv, runner=runner)


def _api_json(api_base: str, token: str, payload: dict[str, Any], *, http_post: Any | None = None) -> dict[str, Any]:
    if http_post is not None:
        return http_post(payload)
    if not api_base or not token:
        raise PublishError("publish_failed", "TTS_API_BASE_URL and TTS_API_TOKEN are required")
    req = urlrequest.Request(api_base.rstrip("/") + "/api/episodes", data=json.dumps(payload).encode("utf-8"), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "tts-worker/1.0"}, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8").strip()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise PublishError(_classify_http(exc.code, detail), f"episode publish HTTP {exc.code}: {detail}", http_status=exc.code) from exc
    except (TimeoutError, urlerror.URLError) as exc:
        raise PublishError("publish_transient", "episode create response unavailable") from exc
    return json.loads(body) if body else {}


def _api_get_episodes(api_base: str, token: str, *, feed: str, slug: str | None, http_get: Any | None = None) -> list[dict[str, Any]]:
    """Read the exact episode identity to converge a duplicate install."""
    if http_get is not None:
        return http_get(feed, slug)
    if not api_base or not token:
        raise PublishError("publish_failed", "TTS_API_BASE_URL and TTS_API_TOKEN are required")
    from urllib.parse import urlencode
    query = {"feed": feed}
    if slug is not None:
        query["slug"] = slug
    url = api_base.rstrip("/") + "/api/episodes?" + urlencode(query)
    req = urlrequest.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "tts-worker/1.0"}, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8").strip()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise PublishError(_classify_http(exc.code, detail), f"episode list HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, urlerror.URLError) as exc:
        raise PublishError("publish_transient", "episode lookup response unavailable") from exc
    rows = json.loads(body or "null")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PublishError("publish_failed", "invalid episode lookup response")
    return rows


def find_published_episode(*, settings: Any, fingerprint: str, route: str,
                           episode_kind: str, http_get: Any | None = None,
                           slug: str | None = None) -> PublishedEpisode | None:
    """Read the producer's catalog; a full identity in every public role is proof.

    The API has no fingerprint column/filter. Its feed listing is unpaginated;
    match the complete, producer-owned URL prefix, never the short slug suffix.
    A published identity wins over a newly generated (possibly stochastic) run.
    """
    prefix = public_url(settings.public_base_url, f"tts/episodes/{fingerprint}/")
    rows = _api_get_episodes(settings.api_base_url, settings.api_token,
                             feed="tts", slug=slug, http_get=http_get)
    matches = [row for row in rows if str(row.get("mp3_url", "")).startswith(prefix)]
    if not matches:
        return None
    if len(matches) != 1:
        raise PublishError("publish_identity_conflict", "multiple episodes for execution identity")
    row = matches[0]
    if (row.get("feed") != "tts" or row.get("route") != route
            or row.get("episode_kind") != episode_kind):
        raise PublishError("publish_identity_conflict", "episode route differs for execution identity")
    for field in ("mp3_url", "md_url", "speech_text_url", "manifest_url", "source_md_url"):
        value = row.get(field)
        if (not isinstance(value, str) or not value.startswith(prefix)
                or not value[len(prefix):] or "/" in value[len(prefix):]
                or urlparse(value).query or urlparse(value).fragment):
            raise PublishError("publish_identity_conflict", "episode artifacts differ for execution identity")
    if (not isinstance(row.get("slug"), str) or not row["slug"]
            or not isinstance(row.get("title"), str)
            or not isinstance(row.get("mp3_size"), int) or row["mp3_size"] < 0
            or not (row.get("duration") is None or isinstance(row["duration"], str))):
        raise PublishError("publish_identity_conflict", "incomplete episode for execution identity")
    return PublishedEpisode(slug=row["slug"], title=row["title"], mp3_url=row["mp3_url"],
                            md_url=row["md_url"], speech_text_url=row["speech_text_url"],
                            manifest_url=row["manifest_url"], source_md_url=row["source_md_url"],
                            feed_url=settings.feed_url, duration=row.get("duration"),
                            mp3_size=row["mp3_size"])


def require_empty_identity(settings: Any, fingerprint: str) -> None:
    """Without a usable checkpoint, retained objects require reconciliation.

    Do not spend on a new generation or guess a create payload from an orphan
    prefix. This is a read-only probe, not a second publication catalog.
    """
    remote = f"{settings.rclone_remote}:{settings.b2_bucket}/tts/episodes/{fingerprint}"
    result = subprocess.run([settings.rclone_bin, "lsjson", remote],
                            text=True, capture_output=True, timeout=60)
    if result.returncode == 3:  # rclone directory not found
        return
    if result.returncode:
        raise PublishError("publish_transient", "retained artifact lookup unavailable")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise PublishError("publish_failed", "invalid retained artifact lookup response")
    if rows:
        raise PublishError("publish_partial_identity_conflict",
                           "Unregistered identity has retained artifacts but no usable checkpoint; "
                           "operator reconciliation required (no regeneration or overwrite).")


def _classify_http(code: int, detail: str) -> str:
    """Typed publish failure classes (consumed by failure_policy)."""
    low = detail.lower()
    if code == 403 and ("1010" in low or "cloudflare" in low or "browser_signature" in low):
        return "publish_blocked"  # edge block, e.g. Cloudflare error 1010
    if code == 400 and "unique constraint failed" in low:
        # Proven live-API semantics (rss-edge createEpisode): POST is a
        # plain INSERT and a duplicate (feed, slug) surfaces as HTTP 400 with
        # a D1 "UNIQUE constraint failed" message, NOT 409. The caller
        # converges by reading the existing row back.
        return "publish_duplicate_slug"
    if code == 409:
        # Same identity with a different payload: conflict, never overwrite.
        return "publish_identity_conflict"
    if code in (401, 403):
        return "publish_auth_required"
    if code in (408, 429) or code >= 500:
        return "publish_transient"
    return "publish_failed"


def _duration(seconds: float) -> str | None:
    if seconds <= 0:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@tracked_stage("publish_episode")
def publish_episode(
    *,
    title: str,
    description: str,
    route: str,
    episode_kind: str,
    source_hash: str,
    source_original: Path,
    source_ext: str,
    source_md: Path,
    source_meta: Path,
    artifact_md: Path,
    speech_txt: Path,
    audio_mp3: Path,
    manifest_json: Path,
    duration_seconds: float,
    primary_document: Path | None = None,
    script_document: Path | None = None,
    notes_document: Path | None = None,
    artifact_prefix: str | None = None,
    settings: Any,
    keywords: list[str] | None = None,
    runner: Any | None = None,
    http_post: Any | None = None,
    http_get: Any | None = None,
    state_dir: Path | None = None,
    cancel: Any | None = None,
    fingerprint: str,
) -> PublishedEpisode:
    _cancel = cancel or (lambda: None)
    identity = fingerprint
    """Two-phase publication barrier (prepare -> upload -> install -> mark).

    With ``state_dir`` set, a durable ``publish.prepared.json`` pins the slug
    before any upload (so retries reuse it instead of minting a new one), and
    ``publish.installed.json`` records a completed install so a retried job
    returns the already-published episode instead of double-posting.

    The API catalog wins for an already-published full execution identity,
    including across work roots. Read it before uploads and after an uncertain
    create response; never compare newly generated stochastic output against
    the prior successful run or blindly repeat an uncertain POST.
    """
    prepared_p = state_dir / "publish.prepared.json" if state_dir else None
    installed_p = state_dir / "publish.installed.json" if state_dir else None
    def _marker_matches(rec: dict[str, Any] | None) -> bool:
        # Only the full execution identity matches: an episode produced under a
        # different mode, voice or pipeline is never returned for this call.
        return bool(rec) and rec.get("identity") == identity

    if installed_p and installed_p.exists():
        rec = _read_marker(installed_p, ("episode",))
        if _marker_matches(rec):
            return PublishedEpisode(**rec["episode"])
        if rec and rec.get("identity") and rec.get("identity") != identity:
            # A different identity already installed from this work state:
            # configuration drifted between attempts. Conflict, never mint a
            # second episode for the same job.
            raise PublishError(
                "publish_identity_conflict",
                f"installed marker pins identity {rec.get('identity')!r}, "
                f"but this execution computed {identity!r}")
    _cancel()
    existing = find_published_episode(settings=settings, fingerprint=identity,
                                      route=route, episode_kind=episode_kind, http_get=http_get)
    if existing is not None:
        if installed_p:
            _atomic_write(installed_p, json.dumps({"identity": identity, "source_hash": source_hash,
                                                  "episode": existing.__dict__}, ensure_ascii=True) + "\n")
        return existing
    # Pin identity, slug, publication date, keys, and payload before any
    # upload; a retry reuses the pinned values instead of minting new ones.
    slug = artifact_prefix or slugify_title(title, identity)
    pub_date = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if prepared_p and prepared_p.exists():
        rec = _read_marker(prepared_p, ("slug",))
        if _marker_matches(rec) and rec.get("slug"):
            slug = str(rec["slug"])
            pub_date = str(rec.get("pub_date") or pub_date)
        elif rec and rec.get("identity") and rec.get("identity") != identity:
            raise PublishError(
                "publish_identity_conflict",
                f"prepared marker pins identity {rec.get('identity')!r}, "
                f"but this execution computed {identity!r}")
    if prepared_p:
        _atomic_write(prepared_p, json.dumps({"identity": identity, "source_hash": source_hash, "slug": slug, "title": title, "pub_date": pub_date}, ensure_ascii=True) + "\n")
    # Immutable fingerprint-addressed keys: identity in the path,
    # readable slug is metadata only. Only the original bytes are shared per
    # source; source.md/meta are conversion output (the identity carries the
    # converter version), so a re-read under a new converter gets new keys.
    script_document = script_document or speech_txt
    notes_document = notes_document or primary_document or artifact_md
    keys = {
        "source_original": f"tts/source/{source_hash}/source-original.{source_ext}",
        "source_md": f"tts/episodes/{identity}/source.md",
        "source_meta": f"tts/episodes/{identity}/source.meta.json",
        "artifact_md": f"tts/episodes/{identity}/artifact.md",
        "speech_txt": f"tts/episodes/{identity}/speech.txt",
        "script_document": f"tts/episodes/{identity}/{slug}.script.md",
        "notes_document": f"tts/episodes/{identity}/{slug}.notes.md",
        "audio_mp3": f"tts/episodes/{identity}/{slug}.mp3",
        "manifest_json": f"tts/episodes/{identity}/{slug}.manifest.json",
    }
    uploads = ((source_original, keys["source_original"]), (source_md, keys["source_md"]),
               (source_meta, keys["source_meta"]), (artifact_md, keys["artifact_md"]),
               (speech_txt, keys["speech_txt"]),
               (script_document, keys["script_document"]),
               (notes_document, keys["notes_document"]),
               (audio_mp3, keys["audio_mp3"]),
               (manifest_json, keys["manifest_json"]))
    for local, key in uploads:
        _cancel()  # fence before each upload effect
        try:
            _copyto(settings.rclone_bin, settings.rclone_remote, settings.b2_bucket, local, key, runner)
        except PublishError as exc:
            if "immutable file modified" in str(exc):
                raise PublishError("publish_partial_identity_conflict",
                                   "Unregistered identity has conflicting retained artifacts; "
                                   "operator reconciliation required (no overwrite).") from exc
            raise
    mp3_url = public_url(settings.public_base_url, keys["audio_mp3"])
    md_url = public_url(settings.public_base_url, keys["notes_document"])
    speech_url = public_url(settings.public_base_url, keys["script_document"])
    manifest_url = public_url(settings.public_base_url, keys["manifest_json"])
    source_md_url = public_url(settings.public_base_url, keys["source_md"])
    dur = _duration(duration_seconds)
    payload = {
        "feed": "tts",
        "route": route,
        "episode_kind": episode_kind,
        "slug": slug,
        "title": title,
        "description": description or title,
        "pub_date": pub_date,
        "mp3_url": mp3_url,
        "mp3_size": audio_mp3.stat().st_size,
        "duration": dur,
        "md_url": md_url,
        "speech_text_url": speech_url,
        "manifest_url": manifest_url,
        "source_md_url": source_md_url,
        "keywords": ",".join(keywords or ["tts", route, episode_kind]),
        "explicit": "no",
        "block": "no",
    }
    payload["fingerprint"] = identity
    from .fingerprint import payload_hash as _payload_hash
    payload["payload_hash"] = _payload_hash(payload)
    _cancel()  # fence before the episode-install POST
    try:
        _api_json(settings.api_base_url, settings.api_token, payload, http_post=http_post)
    except PublishError as exc:
        duplicate = exc.error_class == "publish_duplicate_slug"
        if not (duplicate or exc.error_class == "publish_transient" or exc.http_status == 400):
            raise
        episode = find_published_episode(settings=settings, fingerprint=identity,
                                         route=route, episode_kind=episode_kind,
                                         slug=slug, http_get=http_get)
        if episode is None:
            if duplicate:
                raise PublishError("publish_identity_conflict",
                                   "duplicate slug has no matching full execution identity") from exc
            raise exc
        if exc.http_status == 400 and not duplicate:
            logging.getLogger(__name__).warning(
                "publish: duplicate-slug error-string drift; converged on full identity")
    else:
        episode = PublishedEpisode(slug=slug, title=title, mp3_url=mp3_url, md_url=md_url, speech_text_url=speech_url, manifest_url=manifest_url, source_md_url=source_md_url, feed_url=settings.feed_url, duration=dur, mp3_size=audio_mp3.stat().st_size)
    if installed_p:
        _atomic_write(installed_p, json.dumps({"identity": identity, "source_hash": source_hash, "episode": episode.__dict__}, ensure_ascii=True) + "\n")
    return episode
