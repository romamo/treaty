"""SIGINT and SIGTERM handling (REQ-F-069, REQ-F-013).

Handlers are installed only on the main thread for the duration of one run. The
first signal raises ``Cancelled`` where the main thread is waiting on a handler; the
second flushes stdout and exits immediately so the envelope is never written twice.

A signal that lands while the framework serializes or records a finished result is
held: the completed run still writes its envelope and exits with its code, because
the work it reports did happen. An ``exec`` plan stops at the first signal, even with
``--ignore-errors``, and exits with the signal's code.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

SIGNAL_EXIT_CODES: dict[signal.Signals, int] = {
    signal.SIGINT: 130,
    signal.SIGTERM: 143,
}


@dataclass(frozen=True, slots=True)
class CancelSignal:
    name: str
    exit_code: int


class Cancelled(BaseException):
    """Raised on the main thread when a cancellation signal arrives

    A ``BaseException``, like ``KeyboardInterrupt``, so a handler's ``except Exception``
    cannot swallow it.
    """

    def __init__(self, sig: CancelSignal) -> None:
        super().__init__(f"cancelled by {sig.name}")
        self.signal = sig


class Cancellation:
    """Where a received signal may interrupt the run: only inside ``armed()``"""

    def __init__(self) -> None:
        self._armed = False
        self._pending: CancelSignal | None = None
        self.received: CancelSignal | None = None
        """The first signal of the run, raised or held"""

    def check(self) -> None:
        """Raise a signal held since the last window, before a handler starts"""
        if self._pending is not None:
            sig, self._pending = self._pending, None
            raise Cancelled(sig)

    @contextmanager
    def armed(self) -> Iterator[None]:
        """Let a signal raise ``Cancelled`` here; one held since the last window raises now"""
        assert not self._armed, "armed() windows do not nest"
        self.check()
        self._armed = True
        try:
            yield
        finally:
            self._armed = False

    def handle(self, sig: CancelSignal, stdout: IO[str]) -> None:
        if self.received is not None:
            stdout.flush()
            os._exit(sig.exit_code)
        self.received = sig
        if not self._armed:
            self._pending = sig
            return
        raise Cancelled(sig)


@contextmanager
def cancellation_handlers(stdout: IO[str]) -> Iterator[Cancellation]:
    """Install handlers for the run; restore the previous ones afterwards"""
    cancellation = Cancellation()
    if threading.current_thread() is not threading.main_thread():
        yield cancellation
        return

    def handle(signum: int, _frame: object) -> None:
        received = signal.Signals(signum)
        cancellation.handle(CancelSignal(received.name, SIGNAL_EXIT_CODES[received]), stdout)

    previous = {sig: signal.signal(sig, handle) for sig in SIGNAL_EXIT_CODES}
    try:
        yield cancellation
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
