"""SIGINT and SIGTERM handling (REQ-F-069, REQ-F-013).

Handlers are installed only on the main thread for the duration of one run. The
first signal raises ``Cancelled`` where the main thread is waiting; the second
flushes stdout and exits immediately so the envelope is never written twice.
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


class Cancelled(Exception):
    """Raised on the main thread when a cancellation signal arrives"""

    def __init__(self, sig: CancelSignal) -> None:
        super().__init__(f"cancelled by {sig.name}")
        self.signal = sig


@contextmanager
def cancellation_handlers(stdout: IO[str]) -> Iterator[None]:
    """Install handlers for the run; restore the previous ones afterwards"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    state = {"cancelling": False}

    def handle(signum: int, _frame: object) -> None:
        sig = CancelSignal(signal.Signals(signum).name, SIGNAL_EXIT_CODES[signal.Signals(signum)])
        if state["cancelling"]:
            stdout.flush()
            os._exit(sig.exit_code)
        state["cancelling"] = True
        raise Cancelled(sig)

    previous = {sig: signal.signal(sig, handle) for sig in SIGNAL_EXIT_CODES}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
