import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "materialize_tg_tools_release.py"

# A release that is not the one being promoted: the units that still run it must be restarted.
OLD = "be1ef8e" + "0" * 33


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

        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        # A stub release-guard first on PATH: the real one admits only the supervisor's agent.
        self.guard = self.stubs / "release-guard"
        self.stub_guard(0)

        # The runtime controller client: prints the jobs this test says are there, and records its arguments.
        self.jobs_file = self.base / "jobs.json"
        self.jobs_call = self.base / "jobs-call"
        self.jobs([])
        self.jobs_view = self.base / "rss-audio-jobs"
        self.jobs_view.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" > {call}\n'
            "cat {jobs}\n".format(call=os.fspath(self.jobs_call), jobs=os.fspath(self.jobs_file))
        )
        self.jobs_view.chmod(0o755)

        # systemctl: `show` prints the running units this test describes; `restart` records its arguments
        # and, when the test prepared one, swaps in the units as they are after the restart.
        self.units_file = self.base / "units.txt"
        self.units_after = self.base / "units-after.txt"
        self.restart_log = self.base / "restart.log"
        self.units([])
        systemctl = self.stubs / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  --user) shift ;;\n'
            "esac\n"
            'case "$1" in\n'
            "  show) cat {units} ;;\n"
            "  restart) shift; printf '%s\\n' \"$*\" >> {log}\n"
            "    if [ -f {after} ]; then mv {after} {units}; fi ;;\n"
            "  *) exit 9 ;;\n"
            "esac\n"
            "exit 0\n".format(units=os.fspath(self.units_file), log=os.fspath(self.restart_log),
                               after=os.fspath(self.units_after))
        )
        systemctl.chmod(0o755)

        self.proc = self.base / "proc"
        self.proc.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def stub_guard(self, code):
        self.guard.write_text("#!/bin/sh\nexit %d\n" % code)
        self.guard.chmod(0o755)

    def jobs(self, jobs):
        self.jobs_file.write_text(json.dumps({"jobs": jobs}) + "\n")

    def units(self, units):
        """units: [(unit, pid)] as one `systemctl show` prints them."""
        self.units_file.write_text("".join("Id=%s\nMainPID=%d\n\n" % (unit, pid) for unit, pid in units))

    def units_after_restart(self, units):
        self.units_after.write_text("".join("Id=%s\nMainPID=%d\n\n" % (unit, pid) for unit, pid in units))

    def process(self, pid, cwd, exe=None):
        """A fake /proc entry: the cwd the process was started in and the executable it was started from."""
        directory = self.proc / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        os.symlink(cwd, directory / "cwd")
        os.symlink(exe or os.fspath(self.base / "runtime" / "bin" / "python"), directory / "exe")

    def release_path(self, release, product):
        return os.fspath(self.releases / release / product)

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", os.fspath(self.repo), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def invoke(self, *extra, commit=None, promote=False):
        command = [
            sys.executable,
            os.fspath(SCRIPT),
            "--repo",
            os.fspath(self.repo),
            "--commit",
            commit or self.commit,
            "--releases-root",
            os.fspath(self.releases),
            *extra,
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
            env=dict(
                os.environ,
                PATH=os.fspath(self.stubs) + os.pathsep + os.environ["PATH"],
                TG_TOOLS_JOBS_VIEW=os.fspath(self.jobs_view),
                TG_TOOLS_PROC=os.fspath(self.proc),
            ),
        )

    def receipt(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def restarts(self):
        if not self.restart_log.exists():
            return []
        return [line.split() for line in self.restart_log.read_text().splitlines()]

    def test_materializes_hash_checked_immutable_tree(self):
        receipt = self.receipt(self.invoke())
        release = self.releases / self.commit
        self.assertEqual(receipt["commit"], self.commit)
        self.assertEqual(receipt["release"], os.fspath(release))
        self.assertEqual(receipt["tracked_file_count"], 3)
        self.assertFalse(receipt["promoted"])
        self.assertEqual(receipt["restarted"], [])
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
        result = self.invoke(commit=blob)
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

    def test_refusing_release_guard_stops_promote_before_any_change(self):
        self.stub_guard(3)
        result = self.invoke(promote=True)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self.releases.exists())
        self.assertFalse((self.releases.parent / "current").exists())
        self.assertEqual(self.invoke().returncode, 0)  # no promote: no guard

    def test_promote_restarts_every_unit_running_old_release(self):
        self.units([("rss-audio-schedule.service", 0),
                    ("some-other.service", 104),
                    ("tg-gateway-ms.service", 102),
                    ("tg-gateway-rss.service", 101),
                    ("tts-result-projector.service", 103)])
        for pid, product in ((101, "tg-gateway"), (102, "tg-gateway"), (103, "tts")):
            self.process(pid, self.release_path(OLD, product))
        self.process(104, os.fspath(self.base / "elsewhere"))
        self.units_after_restart([("rss-audio-schedule.service", 0),
                                  ("some-other.service", 104),
                                  ("tg-gateway-ms.service", 202),
                                  ("tg-gateway-rss.service", 201),
                                  ("tts-result-projector.service", 203)])
        for pid, product in ((201, "tg-gateway"), (202, "tg-gateway"), (203, "tts")):
            self.process(pid, self.release_path(self.commit, product))

        result = self.invoke(promote=True)
        receipt = self.receipt(result)

        self.assertEqual(self.restarts(), [["tg-gateway-ms.service",
                                           "tg-gateway-rss.service", "tts-result-projector.service"]])
        self.assertEqual(sorted(receipt["restarted"]), ["tg-gateway-ms.service",
                                                       "tg-gateway-rss.service",
                                                       "tts-result-projector.service"])
        for unit in ("tg-gateway-rss", "tg-gateway-ms", "tts-result-projector"):
            self.assertIn("restarted %s.service be1ef8e -> %s" % (unit, self.commit[:7]), result.stderr)
        self.assertNotIn("some-other.service", result.stderr)
        self.assertEqual(os.readlink(self.releases.parent / "current"),
                         os.fspath((self.releases / self.commit).resolve()))

    def test_promote_refuses_while_job_pending_or_leased(self):
        self.releases.mkdir()
        current = self.releases.parent / "current"
        os.symlink(os.fspath(self.releases / OLD), current)
        self.units([("tg-gateway-rss.service", 101)])
        self.process(101, self.release_path(OLD, "tg-gateway"))
        self.jobs([{"jobId": "3f8a1c22-0000-4000-8000-000000000001", "status": "pending",
                    "idempotencyKey": "rss-audio:publish:surveillance-20261029"},
                   {"jobId": "3f8a1c22-0000-4000-8000-000000000002", "status": "leased",
                    "workloadType": "transcribe_audio"}])

        result = self.invoke(promote=True)

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("3f8a1c22-0000-4000-8000-000000000001  publish  pending", result.stderr)
        self.assertIn("3f8a1c22-0000-4000-8000-000000000002  transcribe  leased", result.stderr)
        self.assertIn("pending,leased,retryable", self.jobs_call.read_text())
        self.assertEqual(os.readlink(current), os.fspath(self.releases / OLD))
        self.assertFalse((self.releases / self.commit).exists())
        self.assertEqual(self.restarts(), [])

    def test_force_promotes_with_jobs_in_flight(self):
        self.jobs([{"jobId": "3f8a1c22-0000-4000-8000-000000000003", "status": "retryable",
                    "workloadType": "rss_publish"}])

        result = self.invoke("--force", promote=True)
        receipt = self.receipt(result)

        self.assertTrue(receipt["promoted"])
        self.assertIn("--force", result.stderr)
        self.assertIn("3f8a1c22-0000-4000-8000-000000000003  publish  retryable", result.stderr)
        self.assertEqual(os.readlink(self.releases.parent / "current"),
                         os.fspath((self.releases / self.commit).resolve()))

    def test_dry_run_prints_the_check_and_the_units_and_changes_nothing(self):
        self.units([("tg-gateway-rss.service", 101)])
        self.process(101, self.release_path(OLD, "tg-gateway"))

        result = self.invoke("--dry-run", "--promote")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runtime jobs: none pending, leased or retryable", result.stdout)
        self.assertIn("would restart tg-gateway-rss.service be1ef8e -> " + self.commit[:7], result.stdout)
        self.assertEqual(self.restarts(), [])
        self.assertFalse(self.releases.exists())
        self.assertFalse((self.releases.parent / "current").exists())

    def test_unreadable_controller_refuses_unless_forced(self):
        self.jobs_view.write_text("#!/bin/sh\necho 'unknown: settings unreadable'\nexit 1\n")

        result = self.invoke(promote=True)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("unknown: settings unreadable", result.stderr)
        self.assertFalse(self.releases.exists())
        self.assertFalse((self.releases.parent / "current").exists())
        self.assertEqual(self.restarts(), [])

    def test_restart_failure_fails_the_release(self):
        unit = "tg-gateway-rss.service"
        self.units([(unit, 301)])
        self.process(301, self.release_path(OLD, "tg-gateway"))
        self.units_after_restart([(unit, 302)])
        self.process(302, self.release_path(OLD, "tg-gateway"))   # still the old release after the restart

        result = self.invoke(promote=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(unit, result.stderr)
        self.assertIn("restart did not move", result.stderr)
        self.assertEqual(os.readlink(self.releases.parent / "current"),
                         os.fspath((self.releases / self.commit).resolve()))


if __name__ == "__main__":
    unittest.main()
