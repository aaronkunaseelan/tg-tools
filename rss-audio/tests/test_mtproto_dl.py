"""mtproto_dl session ownership over a real OS boundary: two spawned processes, one session file.

| step                              | expected                                              |
| first process downloads           | holds the flock while the client exists               |
| second process starts             | waits; never constructs a client without the flock    |
| first finishes, stays alive idle  | lock released -> second proceeds                      |
| both finish                       | "ok" twice, lock free, clean exit                     |
Telegram itself is a stand-in client; the lock, processes and session file are real.
"""
import asyncio
import fcntl
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mtproto_dl as dl


def lock_free(session):
    fd = os.open(session + ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def process_download(session, output, ready, constructed, release, idle_release, results):
    dl.SESSION_PATH, dl.SESSION_LOCK_TIMEOUT, dl.SESSION_LOCK_POLL_INTERVAL = Path(session), 10, 0.01

    class Client:
        def __init__(self, *args):
            assert not lock_free(session), "client constructed without ownership"
            constructed.set()

        async def start(self, **kwargs):
            pass

        async def get_messages(self, *args, **kwargs):
            return SimpleNamespace(media=True)

        async def download_media(self, message, *, file, progress_callback):
            while not release.is_set():
                await asyncio.sleep(0.01)
            Path(file).write_bytes(b"fixture")
            return file

        async def disconnect(self):
            pass

    async def run():
        try:
            with mock.patch.object(dl, "TelegramClient", Client), \
                    mock.patch.object(dl, "_mtproto_credentials", return_value=(1, "fixture", "fixture")):
                ready.set()
                await dl.download_file(1, 1, output)
            results.put("ok")
            while not idle_release.is_set():  # stay alive: an idle worker must not keep ownership
                await asyncio.sleep(0.01)
        except BaseException as exc:
            results.put(f"{type(exc).__name__}: {exc}")
    asyncio.run(run())


def test_two_processes_share_one_session(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    session = str(tmp_path / "bot")
    ready, opened, release = [[ctx.Event(), ctx.Event()] for _ in range(3)]
    idle_release, results = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=process_download, args=(session, str(tmp_path / f"out{i}"), ready[i], opened[i],
                                                         release[i], idle_release, results)) for i in (0, 1)]
    try:
        procs[0].start()
        assert opened[0].wait(30)
        procs[1].start()
        assert ready[1].wait(30)
        assert not opened[1].wait(0.3)
        release[0].set()
        assert opened[1].wait(30) and procs[0].is_alive()
        release[1].set()
        assert [results.get(True, 30) for _ in procs] == ["ok", "ok"]
        assert lock_free(session)
        idle_release.set()
        for proc in procs:
            proc.join(10)
            assert proc.exitcode == 0
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
