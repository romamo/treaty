"""File writes a crash cannot leave half done, and advisory locks around them.

``write_atomic`` writes a temporary file in the target's directory and renames it over
the target, so a reader sees the old file or the new one, never a mix (REQ-F-070). The
locks are advisory, whole-file, and exclusive: ``fcntl.flock`` on POSIX, ``msvcrt`` on
Windows.

On Windows a file another process has open cannot be replaced, and a file being replaced
cannot be opened: both raise ``PermissionError``. ``retry_sharing_violation`` retries such
an operation for a moment there, so parallel runs reading and writing one file don't fail.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
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


SHARING_RETRY_SECONDS = 2.0
"""How long a Windows sharing violation is retried before the PermissionError stands"""
_SHARING_POLL_SECONDS = 0.02


def retry_sharing_violation[T](
    operation: Callable[[], T],
    *,
    retry: bool = sys.platform == "win32",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    limit: float = SHARING_RETRY_SECONDS,
) -> T:
    """``operation()``, retried on ``PermissionError`` for up to ``limit`` seconds when
    ``retry`` is set, on Windows by default: another process replacing or holding the
    file is gone in milliseconds. After the deadline the error is raised, so a real
    permission problem still fails. Elsewhere the error is never a sharing violation and
    is raised at once."""
    if not retry:
        return operation()
    deadline = clock() + limit
    while True:
        try:
            return operation()
        except PermissionError:
            if clock() >= deadline:
                raise
        sleep(_SHARING_POLL_SECONDS)


def write_atomic(path: Path, text: str, *, new_mode: int = 0o600) -> None:
    """Replace ``path`` with ``text`` in UTF-8, byte for byte: no ``\\r\\n`` translation on
    Windows. See ``write_atomic_bytes``."""
    write_atomic_bytes(path, text.encode("utf-8"), new_mode=new_mode)


def write_atomic_bytes(path: Path, data: bytes, *, new_mode: int = 0o600) -> None:
    """Replace ``path`` with ``data``: a new file gets ``new_mode``, owner-only by default,
    and an existing one keeps its mode. Any failure before the rename leaves the old file
    as it was, and the temporary file is removed. A symlink stays a symlink: its target
    is what gets replaced."""
    path = path.resolve()
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else new_mode
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        # A reader holding the target open on Windows blocks the rename for a moment
        retry_sharing_violation(lambda: os.replace(tmp, path))
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)
    if sys.platform != "win32":
        # The rename itself is durable only once the directory holding it is on disk
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


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
