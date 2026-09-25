"""Idempotency keys for mutating and destructive commands (REQ-C-007).

A successful live run stores its response ``data`` under the key; a repeat with
the same key and arguments returns that data with ``effect: "noop"`` instead of
running the handler again, and a repeat with different arguments is a CONFLICT.
Failures and dry runs are never stored, so they stay retryable.

Each key gets its own record and lock file in the app's state directory. The
lock is held while the handler runs, so a concurrent retry waits for the first
call and then replays its result. A handler that outlives its timeout keeps the
lock until it finishes, and its result is recorded if it succeeds. Records expire
after 24 hours; an expired lock file is removed only while nobody holds it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ._errors import ParseError
from ._scalars import ScalarRegistry
from ._schema import to_jsonable
from ._values import CommandPath

if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: IO[str]) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True

    def _lock(handle: IO[str]) -> None:
        # LK_LOCK gives up after ten one-second attempts; a retry must wait as long as it takes
        while not _try_lock(handle):
            time.sleep(0.05)

    def _unlock(handle: IO[str]) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


STATE_ENV = "TREATY_STATE_DIR"
TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """Caller-chosen token naming one logical operation"""

    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= 255 or not self.value.isprintable():
            raise ParseError(
                "--idempotency-key must be 1 to 255 printable characters",
                context={"flag": "idempotency-key"},
            )


@dataclass(frozen=True, slots=True)
class Record:
    fingerprint: str
    command: str
    data: object
    created_at: float


def fingerprint(command: CommandPath, args: object, scalars: ScalarRegistry) -> str:
    """Hash of the command and its arguments; the same key must always mean the same call"""
    payload = {"command": command.value, "args": to_jsonable(args, scalars)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def state_dir(app_name: str, explicit: Path | None, env: Mapping[str, str]) -> Path | None:
    """``App(state_dir=)``, then ``$TREATY_STATE_DIR/<app>``, then the XDG state home"""
    if explicit is not None:
        return explicit
    if root := env.get(STATE_ENV):
        return Path(root) / app_name
    if xdg := env.get("XDG_STATE_HOME"):
        return Path(xdg) / "treaty" / app_name
    if home := env.get("HOME"):
        return Path(home) / ".local" / "state" / "treaty" / app_name
    return None


@dataclass(slots=True)
class Slot:
    """The record for one key, readable and writable while its lock is held"""

    path: Path
    record: Record | None
    _finish: Callable[[Slot], None] | None = field(default=None, repr=False)

    def hand_off(self, finish: Callable[[Slot], None]) -> None:
        """Keep the lock after the ``claim`` block and run ``finish`` on a daemon thread;
        the lock is released when it returns (a handler that outlived its timeout)"""
        self._finish = finish

    def save(self, record: Record) -> None:
        body = json.dumps(
            {
                "fingerprint": record.fingerprint,
                "command": record.command,
                "data": record.data,
                "created_at": record.created_at,
            }
        )
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, self.path)
        _prune(self.path.parent, record.created_at)


@contextmanager
def claim(directory: Path, key: IdempotencyKey) -> Iterator[Slot]:
    """Lock the key's record for the duration of the block, or until a hand-off finishes"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.value.encode()).hexdigest()[:32]
    record_path = directory / f"{name}.json"
    lock = _acquire(directory / f"{name}.lock")
    slot = Slot(record_path, None)
    try:
        slot.record = _load(record_path, time.time())
        yield slot
    finally:
        finish = slot._finish
        if finish is None:
            _release(lock)
        else:

            def finish_then_release() -> None:
                try:
                    finish(slot)
                finally:
                    _release(lock)

            threading.Thread(
                target=finish_then_release, name="treaty-idempotency", daemon=True
            ).start()


def _acquire(path: Path) -> IO[str]:
    """Lock the file now at ``path``: a prune may unlink it between open and lock"""
    while True:
        lock = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
        _lock(lock)
        if _is_current(lock, path):
            return lock
        _release(lock)


def _is_current(handle: IO[str], path: Path) -> bool:
    try:
        on_disk = path.stat()
    except FileNotFoundError:
        return False
    held = os.fstat(handle.fileno())
    return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def _release(handle: IO[str]) -> None:
    _unlock(handle)
    handle.close()


def _load(path: Path, now: float) -> Record | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    record = Record(raw["fingerprint"], raw["command"], raw["data"], raw["created_at"])
    return None if now - record.created_at > TTL_SECONDS else record


def _prune(directory: Path, now: float) -> None:
    for path in directory.iterdir():
        if path.suffix not in (".json", ".lock"):
            continue
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue  # another process pruned it after iterdir()
        if age <= TTL_SECONDS:
            continue
        if path.suffix == ".json":
            path.unlink(missing_ok=True)
        elif sys.platform != "win32":
            # Windows cannot unlink a file another process has open, so its locks stay
            _unlink_idle_lock(path)


def _unlink_idle_lock(path: Path) -> None:
    """A lock file's mtime never changes, so age alone says nothing about a holder"""
    try:
        handle = os.fdopen(os.open(path, os.O_WRONLY), "w")
    except FileNotFoundError:
        return
    with handle:
        if _try_lock(handle):
            if _is_current(handle, path):
                path.unlink()
            _unlock(handle)
