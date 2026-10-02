"""Named advisory locks for handlers: ``ctx.lock(name)`` (REQ-F-033).

A lock is an exclusive ``flock`` (``msvcrt`` on Windows) on a file under the state
directory's ``locks/``. The kernel drops it when the holder exits for any reason,
SIGTERM and SIGKILL included, so a crashed holder never blocks the next run. The holder
writes its pid and start time next to the lock file, for the ``LOCK_HELD`` error of
whoever waits too long.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ._atomic import try_lock, unlock, write_atomic
from ._errors import CliExit, RegistrationError
from ._values import ExitCodeName
from ._verbosity import trace

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_POLL_SECONDS = 0.05
_DEADLINE_MARGIN = 0.25
"""A wait bounded by the timeout gives up this much before it, so the run answers
``LOCK_HELD``, retryable, rather than ``TIMEOUT``"""
LOCK_HELD = "LOCK_HELD"


class LockHeld(CliExit):
    """``ctx.lock`` waited its limit: exit 4 ``LOCK_HELD``, retryable because nothing ran
    under the lock (03-D1), with ``retry_after_ms`` and the holder"""

    def __init__(self, name: str, path: Path, waited: float, retry_after_ms: int) -> None:
        holder = _holder(path)
        context: dict[str, object] = {"lock": name, "lock_file": str(path)}
        if holder is not None:
            context["holder_pid"] = holder.pid
            context["holder_age_ms"] = max(0, round((time.time() - holder.since) * 1000))
        super().__init__(
            ExitCodeName("PRECONDITION"),
            f"lock {name!r} is held by another run after waiting {waited:g}s",
            code=LOCK_HELD,
            context=context,
            retry_after_ms=retry_after_ms,
            retry_strategy="linear_backoff",
            suggestion="retry after retry_after_ms, once the holder has finished",
        )


@dataclass(frozen=True, slots=True)
class _Holder:
    pid: int
    since: float


def _holder_path(path: Path) -> Path:
    return path.with_suffix(".holder")


def _holder(path: Path) -> _Holder | None:
    """Who holds the lock, as the holder wrote it; None when unknown or unreadable"""
    try:
        data = json.loads(_holder_path(path).read_text())
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid, since = data.get("pid"), data.get("since")
    if not isinstance(pid, int) or not isinstance(since, (int, float)):
        return None
    return _Holder(pid, float(since))


@dataclass(frozen=True, slots=True)
class Locks:
    """The locks one command run may take"""

    directory: Path | None
    """``<state dir>/locks``; None when there is no state directory"""
    deadline: float | None
    """``time.monotonic()`` when ``ctx.remaining`` runs out; the default limit of a wait"""

    @contextmanager
    def hold(self, name: str, *, wait: float | None, retry_after_ms: int) -> Iterator[None]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RegistrationError(
                f"ctx.lock({name!r}): a lock name is letters, digits, '.', '_', and '-'"
            )
        if isinstance(retry_after_ms, bool) or not isinstance(retry_after_ms, int):
            raise RegistrationError("ctx.lock: retry_after_ms is an int of milliseconds")
        if retry_after_ms <= 0:
            raise RegistrationError("ctx.lock: retry_after_ms must be positive")
        if wait is not None and (
            isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0
        ):
            raise RegistrationError("ctx.lock: wait is seconds, 0 or more, or None")
        if self.directory is None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                "no directory to keep locks in",
                code="STATE_DIR_UNKNOWN",
                fix_required="set the tool's state directory variable, XDG_STATE_HOME, or HOME",
            )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{name}.lock"
        limit = wait
        if limit is None and self.deadline is not None:
            limit = max(0.0, self.deadline - time.monotonic() - _DEADLINE_MARGIN)
        handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
        try:
            started = time.monotonic()
            while not try_lock(handle):
                waited = time.monotonic() - started
                if limit is not None and waited >= limit:
                    raise LockHeld(name, path, limit, retry_after_ms)
                time.sleep(_POLL_SECONDS)
            trace("lock acquired", name=name, path=str(path))
            try:
                write_atomic(
                    _holder_path(path), json.dumps({"pid": os.getpid(), "since": time.time()})
                )
                yield
            finally:
                _holder_path(path).unlink(missing_ok=True)
                unlock(handle)
        finally:
            handle.close()
