"""Wall-clock timeout enforcement for handler execution.

The handler runs on a daemon thread and the caller waits with a deadline. On
expiry the framework emits the TIMEOUT envelope and returns; the thread is
abandoned and dies with the interpreter when ``main()`` exits. This works on
every platform and even when the handler is blocked inside a C call, which a
signal-based approach cannot guarantee.

In a long-lived process (``App.call``, the MCP adapter) an abandoned handler runs
to completion after TIMEOUT was reported. ``TimeoutExpired.pending`` lets the
caller wait for it; the idempotency layer holds the key's lock until then.
"""

from __future__ import annotations

import contextvars
import math
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

from ._errors import ParseError
from ._values import InvalidValue

# One year: finite (NaN and inf fail the range check) and far below threading.TIMEOUT_MAX
MAX_SECONDS = 365 * 24 * 3600.0


@dataclass(frozen=True, slots=True)
class Timeout:
    """Seconds a handler may run; ``None`` disables the limit"""

    seconds: float | None

    def __post_init__(self) -> None:
        if self.seconds is not None and not 0 < self.seconds <= MAX_SECONDS:
            raise InvalidValue(f"timeout seconds must be in (0, {MAX_SECONDS:g}] or None")

    @classmethod
    def parse(cls, raw: object) -> Timeout:
        """Parse a flag or dispatch value; ``0`` means no limit (REQ-C-012)"""
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ParseError("'timeout' expects a number of seconds", context={"flag": "timeout"})
        try:
            value = float(raw)
        except OverflowError:
            value = math.inf  # a huge integer: out of range below
        except ValueError:
            raise ParseError(
                "'timeout' expects a number of seconds",
                context={"flag": "timeout", "value": raw},
            ) from None
        if not 0 <= value <= MAX_SECONDS:
            raise ParseError(
                f"'timeout' must be between 0 and {MAX_SECONDS:g} seconds",
                context={"flag": "timeout", "value": _shown(raw), "maximum": MAX_SECONDS},
                suggestion="pass 0 to disable the limit",
            )
        return cls(None) if value == 0 else cls(value)

    @property
    def milliseconds(self) -> int | None:
        return None if self.seconds is None else int(self.seconds * 1000)


def _shown(raw: int | float | str) -> str:
    """The rejected value as text: NaN is invalid JSON and a huge int refuses str()"""
    if isinstance(raw, int):
        return f"an integer of {raw.bit_length()} bits" if raw.bit_length() > 64 else str(raw)
    return str(raw)[:32]


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """``tick`` runs on the waiting thread every ``seconds`` while the handler runs"""

    seconds: float
    tick: Callable[[], None]

    def __post_init__(self) -> None:
        if not 0 < self.seconds <= MAX_SECONDS:
            raise InvalidValue(f"heartbeat seconds must be in (0, {MAX_SECONDS:g}]")


class TimeoutExpired(Exception):
    """The handler did not finish within its timeout"""

    def __init__(self, timeout: Timeout, pending: Pending | None = None) -> None:
        super().__init__(f"handler exceeded {timeout.seconds}s")
        self.timeout = timeout
        self.pending = pending


@dataclass(slots=True)
class Outcome:
    result: object = None
    exc: BaseException | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """A handler still running on its abandoned worker thread"""

    worker: threading.Thread
    outcome: Outcome

    def wait(self) -> Outcome:
        self.worker.join()
        return self.outcome


def call_with_timeout[T](
    fn: Callable[[], T],
    timeout: Timeout,
    running: Callable[[Pending], None] | None = None,
    interruptible: Callable[[], AbstractContextManager[None]] = nullcontext,
    context: contextvars.Context | None = None,
    heartbeats: Sequence[Heartbeat] = (),
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Run ``fn`` under ``timeout``; re-raise its exception or ``TimeoutExpired``

    ``running`` receives the worker as it starts, so a caller interrupted while it
    waits (a signal) can still wait for the handler or hold its locks until it ends.
    Only the handler itself, or the wait for its worker, runs inside ``interruptible()``:
    an exception raised while ``Thread.start`` holds its internal locks corrupts them.
    The worker runs in ``context``, or a copy of the caller's: contextvars the host set
    reach the handler, and a stream passing one context keeps what its generator set.
    Each of ``heartbeats`` ticks on its own interval between waits, outside
    ``interruptible()``. A beat is scheduled from the time it ticked, so a wait that
    wakes late skips the beats it missed rather than ticking them all at once, and two
    beats are never closer than their interval. ``clock`` reads the time in seconds.
    """
    run_in = context if context is not None else contextvars.copy_context()
    if timeout.seconds is None and not heartbeats:
        with interruptible():
            return run_in.run(fn)
    slot = Outcome()

    def target() -> None:
        try:
            slot.result = run_in.run(fn)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            slot.exc = exc

    worker = threading.Thread(target=target, name="treaty-handler", daemon=True)
    pending = Pending(worker, slot)
    worker.start()
    if running is not None:
        running(pending)
    start = clock()
    deadline = None if timeout.seconds is None else start + timeout.seconds
    due = [start + h.seconds for h in heartbeats]
    while True:
        until = min(due, default=None) if deadline is None else min([deadline, *due])
        wait = None if until is None else max(0.0, until - clock())
        with interruptible():
            worker.join(wait)
        if not worker.is_alive():
            break
        now = clock()
        if deadline is not None and now >= deadline:
            raise TimeoutExpired(timeout, pending)
        for i, heartbeat in enumerate(heartbeats):
            if now >= due[i]:
                heartbeat.tick()
                due[i] = now + heartbeat.seconds
    if slot.exc is not None:
        raise slot.exc
    return slot.result  # type: ignore[return-value]
