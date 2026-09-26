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

import math
import threading
from collections.abc import Callable
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
) -> T:
    """Run ``fn`` under ``timeout``; re-raise its exception or ``TimeoutExpired``

    ``running`` receives the worker as it starts, so a caller interrupted while it
    waits (a signal) can still wait for the handler or hold its locks until it ends.
    Only the handler itself, or the wait for its worker, runs inside ``interruptible()``:
    an exception raised while ``Thread.start`` holds its internal locks corrupts them.
    """
    if timeout.seconds is None:
        with interruptible():
            return fn()
    slot = Outcome()

    def target() -> None:
        try:
            slot.result = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            slot.exc = exc

    worker = threading.Thread(target=target, name="treaty-handler", daemon=True)
    pending = Pending(worker, slot)
    worker.start()
    if running is not None:
        running(pending)
    with interruptible():
        worker.join(timeout.seconds)
    if worker.is_alive():
        raise TimeoutExpired(timeout, pending)
    if slot.exc is not None:
        raise slot.exc
    return slot.result  # type: ignore[return-value]
