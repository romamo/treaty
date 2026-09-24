"""Wall-clock timeout enforcement for handler execution.

The handler runs on a daemon thread and the caller waits with a deadline. On
expiry the framework emits the TIMEOUT envelope and returns; the thread is
abandoned and dies with the interpreter when ``main()`` exits. This works on
every platform and even when the handler is blocked inside a C call, which a
signal-based approach cannot guarantee.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from ._errors import ParseError
from ._values import InvalidValue


@dataclass(frozen=True, slots=True)
class Timeout:
    """Seconds a handler may run; ``None`` disables the limit"""

    seconds: float | None

    def __post_init__(self) -> None:
        if self.seconds is not None and not self.seconds > 0:
            raise InvalidValue("timeout seconds must be positive or None")

    @classmethod
    def parse(cls, raw: object) -> Timeout:
        """Parse a flag or dispatch value; ``0`` means no limit (REQ-C-012)"""
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ParseError("'timeout' expects a number of seconds", context={"flag": "timeout"})
        try:
            value = float(raw)
        except ValueError:
            raise ParseError(
                "'timeout' expects a number of seconds",
                context={"flag": "timeout", "value": raw},
            ) from None
        if value < 0 or value != value:
            raise ParseError(
                "'timeout' must be zero or positive",
                context={"flag": "timeout", "value": raw},
            )
        return cls(None) if value == 0 else cls(value)

    @property
    def milliseconds(self) -> int | None:
        return None if self.seconds is None else int(self.seconds * 1000)


class TimeoutExpired(Exception):
    """The handler did not finish within its timeout"""

    def __init__(self, timeout: Timeout) -> None:
        super().__init__(f"handler exceeded {timeout.seconds}s")
        self.timeout = timeout


@dataclass(slots=True)
class _Slot:
    result: object = None
    exc: BaseException | None = None


def call_with_timeout[T](fn: Callable[[], T], timeout: Timeout) -> T:
    """Run ``fn`` under ``timeout``; re-raise its exception or ``TimeoutExpired``"""
    if timeout.seconds is None:
        return fn()
    slot = _Slot()

    def target() -> None:
        try:
            slot.result = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            slot.exc = exc

    worker = threading.Thread(target=target, name="treaty-handler", daemon=True)
    worker.start()
    worker.join(timeout.seconds)
    if worker.is_alive():
        raise TimeoutExpired(timeout)
    if slot.exc is not None:
        raise slot.exc
    return slot.result  # type: ignore[return-value]
