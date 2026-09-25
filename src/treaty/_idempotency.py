"""Idempotency keys for mutating and destructive commands (REQ-C-007).

A successful live run stores its response ``data`` under the key; a repeat with
the same key and arguments returns that data with ``effect: "noop"`` instead of
running the handler again, and a repeat with different arguments is a CONFLICT.
Failures and dry runs are never stored, so they stay retryable.

Each key gets its own record and lock file in the app's state directory. The
lock is held while the handler runs, so a concurrent retry waits for the first
call and then replays its result. Records expire after 24 hours.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ._errors import ParseError
from ._scalars import ScalarRegistry
from ._schema import to_jsonable
from ._values import CommandPath

if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle: IO[str]) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

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
    """Lock the key's record for the duration of the block"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.value.encode()).hexdigest()[:32]
    record_path = directory / f"{name}.json"
    fd = os.open(directory / f"{name}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        _lock(lock)
        try:
            yield Slot(record_path, _load(record_path, time.time()))
        finally:
            _unlock(lock)


def _load(path: Path, now: float) -> Record | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    record = Record(raw["fingerprint"], raw["command"], raw["data"], raw["created_at"])
    return None if now - record.created_at > TTL_SECONDS else record


def _prune(directory: Path, now: float) -> None:
    for path in directory.iterdir():
        if path.suffix in (".json", ".lock") and now - path.stat().st_mtime > TTL_SECONDS:
            path.unlink(missing_ok=True)
