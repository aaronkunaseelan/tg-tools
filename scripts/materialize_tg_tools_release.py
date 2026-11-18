#!/usr/bin/env python3
"""Create immutable, content-checked releases from a local Git commit.

With ``--promote`` the script is the whole release: the guard first, then the runtime controller's jobs
checked so the restart kills nothing, then the ``current`` switch, then every running user unit whose
process runs from another release restarted and confirmed on the new one. Nothing here is a unit list:
the units to restart come from the processes that are running.
"""

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

# What a restart would kill: a job the controller could hand out or one a worker holds right now.
IN_FLIGHT = ("pending", "leased", "retryable")
# The read-only client the rss-audio units use (bin/rss-audio-jobs -> rss-audio/jobs_view.py), and procfs.
# Both are overridable so a test never reads the live controller or the live pid table.
JOBS_VIEW = os.environ.get("TG_TOOLS_JOBS_VIEW") or "bin/rss-audio-jobs"
PROC = os.environ.get("TG_TOOLS_PROC") or "/proc"
KIND_KEY = re.compile(r"^rss-audio:([a-z_]+):(.+)$")
KIND_WORKLOAD = {"transcribe_audio": "transcribe", "rss_publish": "publish"}


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


def _job_kind(job):
    """A job's kind, named as rss-audio/jobs_view.py names it: the key carries it, else the workload."""
    match = KIND_KEY.match(job.get("idempotencyKey") or "")
    if match:
        return match.group(1)
    return KIND_WORKLOAD.get(job.get("workloadType"), str(job.get("workloadType") or "?"))


def _first_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else ""


def in_flight_jobs(repo):
    """The controller's pending, leased and retryable jobs, through the client tg-tools already uses.

    A failed read raises: unknown is not idle.
    """
    view = JOBS_VIEW if os.path.isabs(JOBS_VIEW) else os.path.join(os.fspath(repo), JOBS_VIEW)
    try:
        result = subprocess.run(
            [view, "--json", "--status", ",".join(IN_FLIGHT), "--limit", "500"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise MaterializeError("runtime jobs unreadable: " + str(error))
    if result.returncode:
        # jobs_view prints ``unknown: <reason>`` and exits 1; that reason is the refusal's reason.
        detail = _first_line(result.stdout) or _first_line(result.stderr) or "exit " + str(result.returncode)
        raise MaterializeError("runtime jobs unreadable: " + detail)
    try:
        jobs = json.loads(result.stdout)["jobs"]
    except (ValueError, KeyError, TypeError):
        raise MaterializeError("runtime jobs unreadable: the client printed no jobs")
    return [job for job in jobs if isinstance(job, dict)]


def check_jobs(repo, force, out):
    """Print the jobs check. Return None when a restart is safe (or forced), else the refusal's exit code."""
    try:
        jobs = in_flight_jobs(repo)
    except MaterializeError as error:
        print("materialize_tg_tools_release: " + str(error), file=out)
        if force:
            print("materialize_tg_tools_release: --force: promoting although the controller cannot be read"
                  " (unknown is not idle)", file=out)
            return None
        return 1
    if not jobs:
        print("runtime jobs: none pending, leased or retryable", file=out)
        return None
    print("runtime jobs in flight, which a restart would kill: " + str(len(jobs)), file=out)
    for job in jobs:
        print("  {}  {}  {}".format(job.get("jobId"), _job_kind(job), job.get("status")), file=out)
    if force:
        print("materialize_tg_tools_release: --force: promoting with jobs in flight", file=out)
        return None
    print("materialize_tg_tools_release: refusing to promote: --force restarts anyway", file=out)
    return 4


def running_units():
    """Every running user service's MainPID, from one systemctl call."""
    result = subprocess.run(
        ["systemctl", "--user", "show", "--type=service", "--state=running", "-p", "Id", "-p", "MainPID"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise MaterializeError("systemctl --user show failed: "
                               + (_first_line(result.stderr) or "exit " + str(result.returncode)))
    units = {}
    for block in result.stdout.split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in fields:
            try:
                units[fields["Id"]] = int(fields.get("MainPID") or 0)
            except ValueError:
                units[fields["Id"]] = 0
    return units


def _process_paths(pid):
    """The working directory and executable of a process, as far as they can be read."""
    paths = []
    for name in ("cwd", "exe"):
        try:
            paths.append(os.readlink(os.path.join(PROC, str(pid), name)).removesuffix(" (deleted)"))
        except OSError:
            continue
    return paths


def _release_component(path, releases_root):
    """The release a path lies in under the releases root, else None."""
    if path == releases_root:
        return ""
    prefix = releases_root + os.sep
    for candidate in (path, os.path.realpath(path)):
        if candidate.startswith(prefix):
            return candidate[len(prefix):].split(os.sep)[0]
    return None


def stale_units(commit, releases_root):
    """Every running user service whose process runs from this releases root at another release."""
    root = os.path.realpath(os.fspath(releases_root))
    stale = []
    for unit, pid in sorted(running_units().items()):
        if not pid:                      # oneshot units, and a unit between restarts
            continue
        old = None
        for path in _process_paths(pid):
            component = _release_component(path, root)
            if component and component != commit:
                old = component
                break
        if old:
            stale.append((unit, old))
    return stale


def restart_stale_units(commit, releases_root):
    """Restart, in one call, every unit still on an older release, and confirm each now runs this one."""
    stale = stale_units(commit, releases_root)
    if not stale:
        return []
    result = subprocess.run(
        ["systemctl", "--user", "restart"] + [unit for unit, _ in stale],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    root = os.path.realpath(os.fspath(releases_root))
    after = running_units()
    restarted = []
    failed = []
    for unit, old in stale:
        pid = after.get(unit) or 0
        running = {_release_component(path, root) for path in _process_paths(pid)} if pid else set()
        if commit in running:
            print("restarted {} {} -> {}".format(unit, old[:7], commit[:7]), file=sys.stderr)
            restarted.append(unit)
        else:
            failed.append(unit)
    if failed:
        raise MaterializeError(
            "restart did not move {} to {}".format(", ".join(failed), commit[:7])
            + (" (systemctl --user restart exited " + str(result.returncode) + ")" if result.returncode else "")
        )
    if result.returncode:
        raise MaterializeError("systemctl --user restart exited " + str(result.returncode))
    return restarted


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
    parser.add_argument("--promote", action="store_true",
                        help="switch current to this release and restart every unit running another one")
    parser.add_argument("--force", action="store_true",
                        help="promote although runtime jobs are in flight or the controller cannot be read")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the jobs check and the units a promote would restart; change nothing")
    arguments = parser.parse_args(argv)
    try:
        if arguments.dry_run:
            return _dry_run(arguments)
        if arguments.promote:
            code = subprocess.run(["release-guard", "tg-tools", arguments.commit]).returncode
            if code:
                return code
            refusal = check_jobs(arguments.repo, arguments.force, sys.stderr)
            if refusal is not None:
                return refusal
        receipt = materialize(arguments.repo, arguments.commit,
                              arguments.releases_root, arguments.promote,
                              arguments.current_link)
        receipt["restarted"] = (restart_stale_units(receipt["commit"], arguments.releases_root)
                                if arguments.promote else [])
    except (MaterializeError, OSError, subprocess.SubprocessError) as error:
        print("materialize_tg_tools_release: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


def _dry_run(arguments):
    """The jobs check and the units a promote would restart, with nothing changed."""
    refusal = check_jobs(arguments.repo, arguments.force, sys.stdout)
    stale = stale_units(arguments.commit, arguments.releases_root)
    for unit, old in stale:
        print("would restart {} {} -> {}".format(unit, old[:7], arguments.commit[:7]))
    if not stale:
        print("would restart: no running unit runs from another release")
    return refusal or 0


if __name__ == "__main__":
    sys.exit(main())
