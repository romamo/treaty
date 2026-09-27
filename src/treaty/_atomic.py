"""File writes a crash cannot leave half done, and advisory locks around them.

``write_atomic`` writes a temporary file in the target's directory and renames it over
the target, so a reader sees the old file or the new one, never a mix (REQ-F-070). The
locks are advisory, whole-file, and exclusive: ``fcntl.flock`` on POSIX, ``msvcrt`` on
Windows.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt

    def try_lock(handle: IO[str]) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True

    def lock(handle: IO[str]) -> None:
        # LK_LOCK gives up after ten one-second attempts; a retry must wait as long as it takes
        while not try_lock(handle):
            time.sleep(0.05)

    def unlock(handle: IO[str]) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def lock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def unlock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def write_atomic(path: Path, text: str, *, new_mode: int = 0o600) -> None:
    """Replace ``path`` with ``text``: a new file gets ``new_mode``, owner-only by default,
    and an existing one keeps its mode. Any failure before the rename leaves the old file
    as it was, and the temporary file is removed."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else new_mode
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold the advisory lock on ``path``, created if missing, for the block"""
    handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
    try:
        lock(handle)
        try:
            yield
        finally:
            unlock(handle)
    finally:
        handle.close()
