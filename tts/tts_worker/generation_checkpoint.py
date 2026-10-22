"""Validation and materialisation of generation checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ARTIFACTS = frozenset(
    {
        "sourceOriginal",
        "sourceMarkdown",
        "sourceMeta",
        "artifactMarkdown",
        "speechText",
        "audioMp3",
        "manifestJson",
    }
)
_OPTIONAL_ARTIFACTS = frozenset({"primaryDocument", "publicScript", "publicNotes"})
_SOURCE_FIELDS = frozenset({"hash", "ref", "ext"})
_PUBLICATION_FIELDS = frozenset(
    {
        "title",
        "description",
        "route",
        "episodeKind",
        "durationSeconds",
        "xMode",
        "modelUsage",
        "hangups",
    }
)
_MAX_JSON_DEPTH = 32


@dataclass(frozen=True)
class ArtifactRecord:
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class GenerationCheckpoint:
    identity: str
    attempt_id: str
    source: dict[str, Any]
    publication: dict[str, Any]
    artifacts: dict[str, ArtifactRecord]


class GenerationCheckpointInvalid(RuntimeError):
    """The generation checkpoint or one of its artifacts is invalid."""


class GenerationCheckpointIdentityConflict(GenerationCheckpointInvalid):
    """The checkpoint is valid but belongs to another execution identity."""


def _invalid(message: str) -> GenerationCheckpointInvalid:
    return GenerationCheckpointInvalid(message)


def _validate_writer_json_value(value: Any) -> None:
    """Validate a value before serializing it in a checkpoint."""
    active: set[int] = set()
    pending: list[tuple[Any, int, bool]] = [(value, 1, False)]

    while pending:
        current, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(current))
            continue

        if depth > _MAX_JSON_DEPTH:
            raise _invalid("checkpoint exceeds maximum JSON depth")
        if current is None or isinstance(current, (bool, str, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise _invalid("checkpoint contains a non-finite float")
            continue
        if not isinstance(current, (list, dict)):
            raise _invalid("checkpoint contains a non-JSON value")

        current_id = id(current)
        if current_id in active:
            raise _invalid("checkpoint contains a cyclic value")
        active.add(current_id)
        pending.append((current, depth, True))
        if isinstance(current, dict):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise _invalid("checkpoint object keys must be strings")
                pending.append((nested, depth + 1, False))
        else:
            pending.extend((nested, depth + 1, False) for nested in current)


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise _invalid("invalid generation checkpoint JSON") from exc


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{name} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], name: str) -> None:
    if set(value) != expected:
        raise _invalid(f"{name} has the wrong fields")


def _require_identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise _invalid(f"{name} must be lowercase 64-hex")
    return value


def _validate_relative_path(value: Any) -> list[str]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _invalid("artifact path is not a normalized slash path")
    if value.startswith("/"):
        raise _invalid("artifact path must be relative")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise _invalid("artifact path is not normalized")
    return parts


def _validate_checkpoint_metadata(
    identity: Any,
    attempt_id: Any,
    source_value: Any,
    publication_value: Any,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    identity_value = _require_identity(identity, "identity")

    if not isinstance(attempt_id, str) or not attempt_id:
        raise _invalid("attemptId must be a non-empty string")

    source = _require_object(source_value, "source")
    _require_exact_keys(source, _SOURCE_FIELDS, "source")
    _require_identity(source["hash"], "source.hash")
    for field in ("ref", "ext"):
        if not isinstance(source[field], str):
            raise _invalid(f"source.{field} must be a string")

    publication = _require_object(publication_value, "publication")
    _require_exact_keys(publication, _PUBLICATION_FIELDS, "publication")
    for field in ("title", "description", "route", "episodeKind"):
        if not isinstance(publication[field], str):
            raise _invalid(f"publication.{field} must be a string")
    duration = publication["durationSeconds"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise _invalid("durationSeconds must be numeric")
    try:
        finite = math.isfinite(float(duration))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite or duration < 0:
        raise _invalid("durationSeconds must be finite and non-negative")
    if not isinstance(publication["xMode"], bool):
        raise _invalid("xMode must be boolean")
    if not isinstance(publication["modelUsage"], dict):
        raise _invalid("modelUsage must be an object")
    if not isinstance(publication["hangups"], list) or not all(
        isinstance(item, str) for item in publication["hangups"]
    ):
        raise _invalid("hangups must be a list of strings")

    return identity_value, attempt_id, source, publication


def _materialise_artifact(root: Path, record: dict[str, Any]) -> ArtifactRecord:
    record = _require_object(record, "artifact")
    _require_exact_keys(record, frozenset({"path", "size", "sha256"}), "artifact")

    parts = _validate_relative_path(record["path"])
    size = record["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _invalid("artifact size must be a non-negative integer")
    sha256 = record["sha256"]
    if not isinstance(sha256, str) or _IDENTITY_RE.fullmatch(sha256) is None:
        raise _invalid("artifact sha256 must be lowercase 64-hex")

    candidate = root
    try:
        for part in parts:
            candidate = candidate / part
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode):
                raise _invalid("artifact path traverses a symlink")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        info = os.stat(resolved)
    except GenerationCheckpointInvalid:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid("artifact file is missing or unsafe") from exc

    if not stat.S_ISREG(info.st_mode):
        raise _invalid("artifact is not a regular file")

    digest = hashlib.sha256()
    total = 0
    try:
        with resolved.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise _invalid("artifact could not be read") from exc

    if total != size:
        raise _invalid("artifact size mismatch")
    if digest.hexdigest() != sha256:
        raise _invalid("artifact hash mismatch")
    return ArtifactRecord(path=resolved, size=size, sha256=sha256)


def load_generation_checkpoint(
    job_root: Path, *, expected_identity: str
) -> GenerationCheckpoint | None:
    """Load, validate, and materialise a generation checkpoint.

    This function only reads the checkpoint and its named artifacts.  It does
    not make any recovery or publication decision.
    """
    checkpoint_path = job_root / "generation.complete.json"
    if not os.path.lexists(checkpoint_path):
        return None

    root = job_root.resolve()
    document = _read_json(checkpoint_path)
    top = _require_object(document, "checkpoint")
    _require_exact_keys(
        top,
        frozenset({"schemaVersion", "identity", "attemptId", "source", "publication", "artifacts"}),
        "checkpoint",
    )

    schema_version = top["schemaVersion"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise _invalid("unsupported schemaVersion")
    identity, attempt_id, source, publication = _validate_checkpoint_metadata(
        top["identity"], top["attemptId"], top["source"], top["publication"]
    )
    expected = _require_identity(expected_identity, "expected_identity")

    if identity != expected:
        raise GenerationCheckpointIdentityConflict(
            "generation checkpoint identity differs from expected_identity"
        )

    artifacts_value = _require_object(top["artifacts"], "artifacts")
    artifact_names = set(artifacts_value)
    if not _REQUIRED_ARTIFACTS.issubset(artifact_names) or not artifact_names <= (
        _REQUIRED_ARTIFACTS | _OPTIONAL_ARTIFACTS
    ):
        raise _invalid("artifacts has the wrong names")
    if publication["xMode"] and "primaryDocument" not in artifact_names:
        raise _invalid("explain checkpoint requires primaryDocument")
    artifacts = {
        name: _materialise_artifact(root, record)
        for name, record in artifacts_value.items()
    }

    return GenerationCheckpoint(
        identity=identity,
        attempt_id=attempt_id,
        source=source,
        publication=publication,
        artifacts=artifacts,
    )


def _materialise_written_artifact(root: Path, role: str, path: Any) -> ArtifactRecord:
    if not isinstance(path, Path):
        raise _invalid(f"artifact {role} must be a Path")

    try:
        lexical = Path(os.path.abspath(os.fspath(path)))
        relative = lexical.relative_to(root)
    except (OSError, TypeError, ValueError) as exc:
        raise _invalid(f"artifact {role} is outside the job root") from exc

    parts = relative.parts
    if not parts:
        raise _invalid(f"artifact {role} must be beneath the job root")
    relative_path = "/".join(parts)
    _validate_relative_path(relative_path)
    candidate = root.joinpath(*parts)

    try:
        current = root
        for part in parts:
            current = current / part
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise _invalid("artifact path traverses a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(candidate, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _invalid("artifact is not a regular file")
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    digest.update(chunk)
        finally:
            if fd != -1:
                os.close(fd)
    except GenerationCheckpointInvalid:
        raise
    except (OSError, ValueError) as exc:
        raise _invalid("artifact could not be read") from exc

    return ArtifactRecord(path=candidate, size=total, sha256=digest.hexdigest())


def _fsync_job_root(root: Path) -> None:
    try:
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


def write_generation_checkpoint(
    job_root: Path,
    *,
    identity: str,
    attempt_id: str,
    source: dict[str, Any],
    publication: dict[str, Any],
    artifacts: dict[str, Path],
) -> GenerationCheckpoint:
    """Validate generation output and atomically materialise its checkpoint."""
    if not isinstance(job_root, Path):
        raise _invalid("job_root must be a Path")
    try:
        root = job_root.resolve(strict=True)
        if not stat.S_ISDIR(os.stat(root).st_mode):
            raise _invalid("job_root must be a directory")
    except GenerationCheckpointInvalid:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _invalid("job_root is invalid") from exc

    identity, attempt_id, source, publication = _validate_checkpoint_metadata(
        identity, attempt_id, source, publication
    )
    if not isinstance(artifacts, dict):
        raise _invalid("artifacts must be an object")
    artifact_names = set(artifacts)
    if not _REQUIRED_ARTIFACTS.issubset(artifact_names) or not artifact_names <= (
        _REQUIRED_ARTIFACTS | _OPTIONAL_ARTIFACTS
    ):
        raise _invalid("artifacts has the wrong names")
    if publication["xMode"] and "primaryDocument" not in artifact_names:
        raise _invalid("explain checkpoint requires primaryDocument")
    materialised = {
        role: _materialise_written_artifact(root, role, path)
        for role, path in artifacts.items()
    }

    record = {
        "schemaVersion": 1,
        "identity": identity,
        "attemptId": attempt_id,
        "source": source,
        "publication": publication,
        "artifacts": {
            role: {
                "path": "/".join(materialised[role].path.relative_to(root).parts),
                "size": materialised[role].size,
                "sha256": materialised[role].sha256,
            }
            for role in materialised
        },
    }
    try:
        _validate_writer_json_value(record)
        encoded = (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except GenerationCheckpointInvalid:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _invalid("checkpoint fields are not JSON-serializable") from exc

    checkpoint_path = root / "generation.complete.json"
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix="generation.complete.json.tmp-", dir=os.fspath(root)
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, checkpoint_path)
        temporary_path = None
        _fsync_job_root(root)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    return GenerationCheckpoint(
        identity=identity,
        attempt_id=attempt_id,
        source=source,
        publication=publication,
        artifacts=materialised,
    )
