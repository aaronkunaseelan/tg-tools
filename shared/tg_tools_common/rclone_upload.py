"""One immutable upload to an exact object key."""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def immutable_copy_argv(rclone_bin: str, local: Path, remote_key: str) -> Iterator[list[str]]:
    """Yield the rclone argv that uploads ``local`` to ``remote_key`` without ever rewriting it.

    ``rclone copyto --immutable`` silently overwrites a differing object
    (measured on rclone 1.60 and 1.73); ``copy --immutable`` refuses. So the
    file is hard-linked under the key's basename into a private directory and
    that directory is copied. ``--checksum`` lets a retry with identical bytes
    but a newer mtime pass, while different bytes still fail.
    """
    parent, name = remote_key.rsplit("/", 1)
    with tempfile.TemporaryDirectory(dir=local.parent, prefix=".upload-") as staging:
        os.link(local, Path(staging) / name)
        yield [rclone_bin, "copy", "--immutable", "--checksum", staging, parent]
