"""Read-only retry state decisions and bounded checkpoint recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any

from .generation_checkpoint import (
    GenerationCheckpoint,
    GenerationCheckpointIdentityConflict,
    GenerationCheckpointInvalid,
    load_generation_checkpoint,
)
from .publisher import PublishedEpisode


_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")
_G_NAME = "generation.complete.json"
_P_NAME = "publish.prepared.json"
_I_NAME = "publish.installed.json"


class RetryDecision(Enum):
    GENERATE = "GENERATE"
    C2_RECOVERY = "C2_RECOVERY"
    PUBLISH_FROM_G = "PUBLISH_FROM_G"
    INSTALLED = "INSTALLED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class RetryGuardResult:
    decision: RetryDecision
    checkpoint: GenerationCheckpoint | None
    episode: PublishedEpisode | None


class RetryIdentityConflict(RuntimeError):
    """Final retry state cannot safely be replayed."""


class RetryMarkerCorrupt(RetryIdentityConflict):
    """A publish marker is unreadable or schema-invalid, not conflicting.

    Corruption and conflict are different facts and deserve different
    policies. A *conflicting* marker proves a different identity owns this
    work root, so replaying would overwrite or duplicate someone else's
    episode -- always terminal. A *corrupt* marker proves nothing at all; the
    publisher's own reader already quarantines it and carries on, so a guard
    that instead kills the job forever leaves the two halves of the publish
    barrier disagreeing about the same bytes.

    It subclasses ``RetryIdentityConflict`` so any caller that does not know
    the distinction keeps the old, safe behaviour: unrecognised corruption
    stays terminal unless a branch below has explicitly proven recovery safe.
    """


class GenerationCheckpointRecoveryError(RuntimeError):
    """C2 checkpoint quarantine did not complete durably."""

    error_class = "generation_checkpoint_recovery_failed"


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_marker(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("invalid JSON constant")
            ),
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RetryMarkerCorrupt("invalid final marker") from exc


def _is_identity(value: Any) -> bool:
    return isinstance(value, str) and _IDENTITY_RE.fullmatch(value) is not None


def _prescan_identity(path: Path, expected_identity: str) -> None:
    try:
        value = _read_marker(path)
    except RetryMarkerCorrupt:
        return
    if isinstance(value, dict) and _is_identity(value.get("identity")):
        if value["identity"] != expected_identity:
            raise RetryIdentityConflict("final marker identity differs from expected identity")


def _valid_prepared(path: Path, expected_identity: str) -> dict[str, Any]:
    value = _read_marker(path)
    if not isinstance(value, dict) or set(value) != {
        "identity", "source_hash", "slug", "title", "pub_date"
    }:
        raise RetryMarkerCorrupt("invalid prepared marker schema")
    if not all(isinstance(value[field], str) for field in value):
        raise RetryMarkerCorrupt("invalid prepared marker fields")
    if not _is_identity(value["identity"]) or not _is_identity(value["source_hash"]):
        raise RetryMarkerCorrupt("invalid prepared marker identity")
    if value["identity"] != expected_identity:
        raise RetryIdentityConflict("prepared marker identity differs from expected identity")
    return value


def _valid_installed(path: Path, expected_identity: str) -> tuple[PublishedEpisode, str]:
    value = _read_marker(path)
    if not isinstance(value, dict) or set(value) != {"identity", "source_hash", "episode"}:
        raise RetryMarkerCorrupt("invalid installed marker schema")
    if not _is_identity(value["identity"]) or not _is_identity(value["source_hash"]):
        raise RetryMarkerCorrupt("invalid installed marker identity")
    if value["identity"] != expected_identity:
        raise RetryIdentityConflict("installed marker identity differs from expected identity")

    episode = value["episode"]
    episode_fields = {field.name for field in fields(PublishedEpisode)}
    if not isinstance(episode, dict) or set(episode) != episode_fields:
        raise RetryMarkerCorrupt("invalid installed episode schema")
    for field in episode_fields - {"duration", "mp3_size"}:
        if not isinstance(episode[field], str):
            raise RetryMarkerCorrupt("invalid installed episode field")
    if episode["duration"] is not None and not isinstance(episode["duration"], str):
        raise RetryMarkerCorrupt("invalid installed episode duration")
    size = episode["mp3_size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RetryMarkerCorrupt("invalid installed episode size")
    return PublishedEpisode(**episode), value["source_hash"]


def _superseded_identity(
    job_root: Path,
    paths: dict[str, Path],
    exists: dict[str, bool],
    expected_identity: str,
) -> str | None:
    """Identify a *complete, consistent* prior publication of this source.

    Work roots are keyed by sourceKey, but identity also folds in mode, voice,
    speech provider/model, the reader role map and the QMD transform contract.
    So any configuration change -- a different voice, a provider switch, the
    v1->v2 contract bump -- gives the same source a new identity while reusing
    the same root, and the markers left by the finished previous run then read
    as an identity conflict. That made the source permanently unpublishable
    without manual filesystem surgery.

    Recognising the case is only safe when the prior state proves nothing is
    in flight: G, P and I must all be present, readable, agree on one
    identity, and individually validate against it. A partial or inconsistent
    prior state stays a hard conflict, because then we genuinely cannot tell
    what was published.

    Superseding is safe for the published artifacts too: every remote key is
    identity-addressed (``tts/episodes/{identity}/...``) and the readable slug
    carries an identity suffix, so a new identity writes a disjoint key set.
    Nothing is overwritten and no second episode is minted for the *same*
    identity -- which is what the conflict fence actually exists to prevent.
    """
    if not all(exists[name] for name in (_G_NAME, _P_NAME, _I_NAME)):
        return None

    identities: set[str] = set()
    for name in (_G_NAME, _P_NAME, _I_NAME):
        try:
            value = _read_marker(paths[name])
        except RetryMarkerCorrupt:
            return None
        if not isinstance(value, dict) or not _is_identity(value.get("identity")):
            return None
        identities.add(str(value["identity"]))
    if len(identities) != 1:
        return None
    prior = identities.pop()
    if prior == expected_identity:
        return None

    try:
        _valid_prepared(paths[_P_NAME], prior)
        _valid_installed(paths[_I_NAME], prior)
        checkpoint = load_generation_checkpoint(job_root, expected_identity=prior)
    except (RetryIdentityConflict, GenerationCheckpointIdentityConflict,
            GenerationCheckpointInvalid):
        return None
    if checkpoint is None:
        return None
    return prior


def evaluate_retry_state(job_root: Path, expected_identity: str) -> RetryGuardResult:
    """Classify final retry state without modifying any marker."""
    paths = {name: job_root / name for name in (_G_NAME, _P_NAME, _I_NAME)}
    exists = {name: os.path.lexists(path) for name, path in paths.items()}

    if _superseded_identity(job_root, paths, exists, expected_identity) is not None:
        return RetryGuardResult(RetryDecision.SUPERSEDED, None, None)

    for name in (_G_NAME, _P_NAME, _I_NAME):
        if exists[name]:
            _prescan_identity(paths[name], expected_identity)

    checkpoint: GenerationCheckpoint | None
    if exists[_G_NAME]:
        try:
            checkpoint = load_generation_checkpoint(
                job_root, expected_identity=expected_identity
            )
        except GenerationCheckpointIdentityConflict as exc:
            raise RetryIdentityConflict("generation checkpoint identity conflict") from exc
        except GenerationCheckpointInvalid:
            checkpoint = None
    else:
        checkpoint = None

    installed: PublishedEpisode | None = None
    installed_source_hash: str | None = None
    installed_corrupt = False
    if exists[_I_NAME]:
        try:
            installed, installed_source_hash = _valid_installed(
                paths[_I_NAME], expected_identity)
        except RetryMarkerCorrupt:
            installed_corrupt = True

    if checkpoint is not None and installed is not None:
        if installed_source_hash != checkpoint.source["hash"]:
            raise RetryIdentityConflict("installed marker source hash differs from checkpoint")
        return RetryGuardResult(RetryDecision.INSTALLED, checkpoint, installed)

    if exists[_I_NAME] and not installed_corrupt:
        raise RetryIdentityConflict("installed marker exists without valid checkpoint")

    prepared: dict[str, Any] | None = None
    if exists[_P_NAME]:
        try:
            prepared = _valid_prepared(paths[_P_NAME], expected_identity)
        except RetryMarkerCorrupt:
            prepared = None
        if (prepared is not None and checkpoint is not None
                and prepared["source_hash"] != checkpoint.source["hash"]):
            raise RetryIdentityConflict("prepared marker source hash differs from checkpoint")

    if checkpoint is None:
        # An unproven publish: a publication marker exists but no checkpoint
        # accounts for what was generated. Terminal regardless of corruption.
        if exists[_P_NAME] or exists[_I_NAME]:
            raise RetryIdentityConflict("final publication marker exists without valid checkpoint")
        if exists[_G_NAME]:
            return RetryGuardResult(RetryDecision.C2_RECOVERY, None, None)
        return RetryGuardResult(RetryDecision.GENERATE, None, None)

    if installed_corrupt and prepared is None:
        # The install marker is unreadable, so the episode may or may not be
        # live, and no valid prepared marker survives to tell us which slug it
        # would have used. Re-publishing would have to mint a fresh slug,
        # which is exactly how a duplicate episode gets minted. Republishing
        # under the *pinned* slug is safe -- the episode API answers the
        # re-POST with a UNIQUE violation and the publisher converges on the
        # existing row -- so recovery is allowed only when that slug survives.
        raise RetryIdentityConflict(
            "corrupt installed marker with no pinned slug to re-post safely")

    return RetryGuardResult(RetryDecision.PUBLISH_FROM_G, checkpoint, None)


def result_from_checkpoint(
    checkpoint: GenerationCheckpoint,
    episode: PublishedEpisode,
    *,
    source_key: str,
) -> dict[str, Any]:
    publication = checkpoint.publication
    return {
        "ok": True,
        "schemaVersion": 1,
        "sourceKey": source_key,
        "title": episode.title,
        "slug": episode.slug,
        "publicAudioUrl": episode.mp3_url,
        "scriptUrl": episode.speech_text_url,
        "notesUrl": episode.md_url,
        "manifestUrl": episode.manifest_url,
        "feedUrl": episode.feed_url,
        "durationSeconds": publication["durationSeconds"],
        "mp3Size": episode.mp3_size,
        "modelUsage": publication["modelUsage"],
        "hangups": publication["hangups"],
        "xMode": publication["xMode"],
    }


def recover_corrupt_checkpoint(job_root: Path, attempt_id: str) -> Path:
    """Quarantine one invalid final checkpoint with one sibling rename."""
    try:
        digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
        source = job_root / _G_NAME
        destination = job_root / (_G_NAME + ".quarantined-" + digest)
        if not os.path.lexists(source):
            raise FileNotFoundError(source)
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        os.rename(source, destination)
        fd = os.open(job_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.path.lexists(source):
            raise OSError("generation checkpoint remains after quarantine")
        return destination
    except Exception as exc:
        raise GenerationCheckpointRecoveryError("generation checkpoint recovery failed") from exc


def archive_superseded_publication(job_root: Path, attempt_id: str) -> list[Path]:
    """Move a completed prior publication's markers aside, durably.

    Renames rather than deletes: the previous episode stays live and its
    checkpoint remains on disk for forensics, while the work root becomes
    usable again for the new identity. Any failure raises so the caller can
    fail the attempt instead of regenerating over ambiguous state.
    """
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    moved: list[Path] = []
    try:
        for name in (_I_NAME, _P_NAME, _G_NAME):
            source = job_root / name
            if not os.path.lexists(source):
                continue
            destination = job_root / (name + ".superseded-" + digest)
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            os.rename(source, destination)
            moved.append(destination)
        fd = os.open(job_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        for name in (_G_NAME, _P_NAME, _I_NAME):
            if os.path.lexists(job_root / name):
                raise OSError(f"{name} remains after supersede")
        return moved
    except Exception as exc:
        raise GenerationCheckpointRecoveryError(
            "superseded publication archive failed") from exc


decide_retry = evaluate_retry_state
RetryGuardConflict = RetryIdentityConflict
quarantine_generation_checkpoint = recover_corrupt_checkpoint
CheckpointRecoveryError = GenerationCheckpointRecoveryError
