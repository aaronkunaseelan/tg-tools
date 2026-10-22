import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "materialize_tg_tools_release.py"


class MaterializeTgToolsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.releases = self.base / "releases"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Disposable Test")
        (self.repo / "nested").mkdir()
        (self.repo / "plain.txt").write_bytes(b"plain release data\n")
        executable = self.repo / "nested" / "run.sh"
        executable.write_bytes(b"#!/bin/sh\necho local\n")
        executable.chmod(0o755)
        os.symlink("../plain.txt", self.repo / "nested" / "plain-link")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "initial")
        self.commit = self.git("rev-parse", "HEAD").stdout.decode().strip()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", os.fspath(self.repo), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def invoke(self, commit=None, promote=False):
        command = [
            sys.executable,
            os.fspath(SCRIPT),
            "--repo",
            os.fspath(self.repo),
            "--commit",
            commit or self.commit,
            "--releases-root",
            os.fspath(self.releases),
        ]
        if promote:
            command.append("--promote")
        return subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def receipt(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_materializes_hash_checked_immutable_tree(self):
        receipt = self.receipt(self.invoke())
        release = self.releases / self.commit
        self.assertEqual(receipt["commit"], self.commit)
        self.assertEqual(receipt["release"], os.fspath(release))
        self.assertEqual(receipt["tracked_file_count"], 3)
        self.assertFalse(receipt["promoted"])
        self.assertEqual((release / "plain.txt").read_bytes(), b"plain release data\n")
        self.assertEqual(os.readlink(release / "nested" / "plain-link"), "../plain.txt")
        self.assertEqual(
            hashlib.sha256(b"plain release data\n").hexdigest(),
            hashlib.sha256((release / "plain.txt").read_bytes()).hexdigest(),
        )
        for root, directories, files in os.walk(release):
            for name in directories + files:
                path = Path(root) / name
                if not path.is_symlink():
                    self.assertEqual(os.lstat(path).st_mode & 0o222, 0, os.fspath(path))
        self.assertEqual(os.lstat(release).st_mode & 0o222, 0)

    def test_exact_rerun_is_idempotent(self):
        first = self.receipt(self.invoke())
        second = self.receipt(self.invoke())
        self.assertEqual(first, second)
        self.assertEqual(sorted(item.name for item in self.releases.iterdir()), [self.commit])

    def test_dirty_repository_is_rejected_without_release(self):
        (self.repo / "plain.txt").write_bytes(b"dirty\n")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not clean", result.stderr)
        self.assertFalse(self.releases.exists())

    def test_non_commit_object_is_rejected(self):
        blob = self.git("hash-object", "plain.txt").stdout.decode().strip()
        result = self.invoke(blob)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.releases / blob).exists())

    def test_promote_replaces_sibling_current_symlink(self):
        self.releases.mkdir()
        current = self.releases.parent / "current"
        os.symlink("old-release", current)
        receipt = self.receipt(self.invoke(promote=True))
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current),
                         os.fspath((self.releases / self.commit).resolve()))
        self.assertTrue(receipt["promoted"])
        self.assertTrue((self.releases / self.commit).is_dir())


if __name__ == "__main__":
    unittest.main()
