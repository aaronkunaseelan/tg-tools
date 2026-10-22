#!/usr/bin/env python3
"""Create immutable, content-checked releases from a local Git commit."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path


_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")


class MaterializeError(Exception):
    """An expected materialization failure."""


def _error_from_process(label, result):
    detail = result.stderr.decode("utf-8", "replace").strip()
    if detail:
        raise MaterializeError(label + ": " + detail)
    raise MaterializeError(label + " failed")


def _git(repo, arguments, stdout=None):
    result = subprocess.run(
        ["git", "-C", os.fspath(repo)] + list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        _error_from_process("git " + arguments[0], result)
    return result.stdout if stdout is None else b""


def _tar_extract(archive, destination):
    result = subprocess.run(
        ["tar", "-xf", os.fspath(archive), "-C", os.fspath(destination)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        _error_from_process("tar extraction", result)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normal_member_name(name):
    name = name.rstrip("/")
    parts = name.split("/")
    if not name or any(part in ("", ".", "..") for part in parts):
        raise MaterializeError("unsafe path in git archive")
    if name.startswith("/"):
        raise MaterializeError("unsafe path in git archive")
    return os.fsencode(name)


def _link_bytes(name):
    if isinstance(name, bytes):
        return name
    return name.encode(sys.getfilesystemencoding(), "surrogateescape")


def _manifest_from_archive(archive):
    """Return the independent file manifest and the complete directory set."""
    files = {}
    directories = set()
    try:
        with tarfile.open(archive, "r:") as source:
            for member in source:
                path = _normal_member_name(member.name)
                if member.isdir():
                    if path in files:
                        raise MaterializeError("conflicting paths in git archive")
                    directories.add(path)
                    continue
                if member.isfile():
                    if path in files or path in directories:
                        raise MaterializeError("duplicate paths in git archive")
                    member_file = source.extractfile(member)
                    if member_file is None:
                        raise MaterializeError("cannot read git archive member")
                    digest = hashlib.sha256()
                    with member_file:
                        for block in iter(lambda: member_file.read(1024 * 1024), b""):
                            digest.update(block)
                    files[path] = (b"file", digest.hexdigest())
                    continue
                if member.issym():
                    if path in files or path in directories:
                        raise MaterializeError("duplicate paths in git archive")
                    files[path] = (b"symlink", hashlib.sha256(_link_bytes(member.linkname)).hexdigest())
                    continue
                raise MaterializeError("unsupported member type in git archive")
    except (tarfile.TarError, OSError) as error:
        raise MaterializeError("cannot read independent git archive: " + str(error))

    # Some tar writers omit explicit parent directory records.  The extracted
    # tree still necessarily has them, so make the comparison independent of
    # that representation detail.
    for path in list(files) + list(directories):
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            directories.add(b"/".join(parts[:index]))

    canonical = bytearray()
    for path in sorted(files):
        kind, digest = files[path]
        canonical.extend(path)
        canonical.extend(b"\0")
        canonical.extend(kind)
        canonical.extend(b"\0")
        canonical.extend(digest.encode("ascii"))
        canonical.extend(b"\n")
    return files, directories, hashlib.sha256(canonical).hexdigest()


def _scan_tree(root, require_readonly):
    files = {}
    directories = set()

    def check_mode(path, mode):
        if require_readonly and mode & 0o222:
            raise MaterializeError("release contains writable path: " + os.fsdecode(path))

    root_bytes = os.fsencode(os.fspath(root))
    root_status = os.lstat(root_bytes)
    if not stat.S_ISDIR(root_status.st_mode):
        raise MaterializeError("release is not a directory")
    check_mode(root_bytes, root_status.st_mode)

    def visit(parent, relative):
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name if isinstance(entry.name, bytes) else os.fsencode(entry.name)
                path = os.path.join(parent, name)
                item_relative = name if not relative else relative + b"/" + name
                info = os.lstat(path)
                if stat.S_ISDIR(info.st_mode):
                    check_mode(path, info.st_mode)
                    directories.add(item_relative)
                    visit(path, item_relative)
                elif stat.S_ISREG(info.st_mode):
                    check_mode(path, info.st_mode)
                    files[item_relative] = (b"file", _sha256_file(path))
                elif stat.S_ISLNK(info.st_mode):
                    files[item_relative] = (b"symlink", hashlib.sha256(os.readlink(path)).hexdigest())
                else:
                    raise MaterializeError("unsupported path in release: " + os.fsdecode(path))

    visit(root_bytes, b"")
    return files, directories


def _verify_release(path, expected_files, expected_directories, require_readonly):
    found_files, found_directories = _scan_tree(path, require_readonly)
    if found_files != expected_files or found_directories != expected_directories:
        raise MaterializeError("existing or extracted release differs from git archive")


def _remove_write_bits(root):
    root_bytes = os.fsencode(os.fspath(root))

    def freeze(path):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            return
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise MaterializeError("unsupported path in extracted release: " + os.fsdecode(path))
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    name = entry.name if isinstance(entry.name, bytes) else os.fsencode(entry.name)
                    freeze(os.path.join(path, name))

    freeze(root_bytes)


def _destination_state(destination):
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        return "absent"
    return "directory" if stat.S_ISDIR(info.st_mode) else "other"


def _promote(current_link, destination):
    current = os.path.abspath(os.fspath(current_link))
    parent = os.path.dirname(current)
    if not os.path.isdir(parent):
        raise MaterializeError("current-link parent is not a directory")
    if os.path.lexists(current) and not stat.S_ISLNK(os.lstat(current).st_mode):
        raise MaterializeError("current exists and is not a symlink")

    temporary = None
    for _ in range(20):
        candidate = os.path.join(parent, ".current-" + uuid.uuid4().hex)
        try:
            os.symlink(os.path.abspath(os.fspath(destination)), candidate)
            temporary = candidate
            break
        except FileExistsError:
            continue
    if temporary is None:
        raise MaterializeError("cannot allocate temporary current symlink")
    try:
        # Both names are in one directory, making replace an atomic rename.
        os.replace(temporary, current)
    finally:
        if temporary is not None and os.path.lexists(temporary):
            os.unlink(temporary)


def _clean_repository(repo):
    root_output = _git(repo, ["rev-parse", "--show-toplevel"])
    root_text = root_output.decode(sys.getfilesystemencoding(), "surrogateescape").rstrip("\n")
    if not root_text:
        raise MaterializeError("git repository has no worktree root")
    root = Path(root_text)
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        raise MaterializeError("source repository is not clean")
    return root


def materialize(repo_argument, requested_commit, releases_root_argument,
                promote=False, current_link_argument=None):
    if not _HEX40.fullmatch(requested_commit):
        raise MaterializeError("--commit must be a full 40-hex commit id")

    repository = _clean_repository(Path(repo_argument))
    resolved = _git(repository, ["rev-parse", "--verify", requested_commit + "^{commit}"])
    commit = resolved.decode("ascii", "strict").strip()
    if not _HEX40.fullmatch(commit) or commit.lower() != requested_commit.lower():
        raise MaterializeError("--commit does not resolve exactly to the requested commit")

    releases_root = Path(releases_root_argument)
    releases_root.mkdir(parents=True, exist_ok=True)
    releases_root = releases_root.resolve(strict=True)
    if not releases_root.is_dir():
        raise MaterializeError("--releases-root is not a directory")
    destination = releases_root / commit

    work = tempfile.mkdtemp(prefix=".tg-tools-work-", dir=os.fspath(releases_root))
    candidate = None
    release = None
    try:
        archive = os.path.join(work, "release.tar")
        independent_archive = os.path.join(work, "independent.tar")
        with open(archive, "wb") as output:
            _git(repository, ["archive", "--format=tar", commit], stdout=output)
        archive_sha256 = _sha256_file(archive)
        with open(independent_archive, "wb") as output:
            _git(repository, ["archive", "--format=tar", commit], stdout=output)
        expected_files, expected_directories, manifest_sha256 = _manifest_from_archive(independent_archive)

        state = _destination_state(destination)
        if state == "other":
            raise MaterializeError("release destination exists and is not a directory")
        if state == "directory":
            _verify_release(destination, expected_files, expected_directories, True)
            release = destination
        else:
            candidate = tempfile.mkdtemp(prefix=".tg-tools-release-", dir=os.fspath(releases_root))
            _tar_extract(archive, candidate)
            _verify_release(candidate, expected_files, expected_directories, False)
            _remove_write_bits(candidate)
            _verify_release(candidate, expected_files, expected_directories, True)

            # Do not use replace here: an existing release must never be removed.
            if _destination_state(destination) == "absent":
                os.rename(candidate, destination)
                candidate = None
                release = destination
            else:
                shutil.rmtree(candidate)
                candidate = None
                if _destination_state(destination) != "directory":
                    raise MaterializeError("release destination appeared and is not a directory")
                _verify_release(destination, expected_files, expected_directories, True)
                release = destination

        shutil.rmtree(work)
        work = None
        if promote:
            current_link = (Path(current_link_argument) if current_link_argument
                            else releases_root.parent / "current")
            _promote(current_link, release)

        return {
            "archive_sha256": archive_sha256,
            "commit": commit,
            "manifest_sha256": manifest_sha256,
            "promoted": bool(promote),
            "release": os.fspath(release),
            "tracked_file_count": len(expected_files),
        }
    finally:
        if candidate is not None:
            shutil.rmtree(candidate, ignore_errors=True)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--current-link")
    parser.add_argument("--promote", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        receipt = materialize(arguments.repo, arguments.commit,
                              arguments.releases_root, arguments.promote,
                              arguments.current_link)
    except (MaterializeError, OSError, subprocess.SubprocessError) as error:
        print("materialize_tg_tools_release: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
