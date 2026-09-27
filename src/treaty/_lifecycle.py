"""Teardown of one handler run: resource ``release`` hooks, then ``cleanup=`` (REQ-C-017).

Every way a run ends calls ``Teardown.run``: the worker once the handler returns or
raises, the timeout and signal paths after the handler's grace, a stream once its
generator is done or closed, and a closed stdout. Only the first call does anything, so
the hooks see one teardown whichever paths race.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

Hook = Callable[[], None]
Failed = Callable[[str, Exception], None]
"""Told the hook's name and what it raised; the next hooks still run"""

CLEANUP_HOOK = "cleanup"


class Teardown:
    """What one handler run holds: released newest first, then the command's cleanup"""

    def __init__(self, cleanup: Hook | None, failed: Failed) -> None:
        self._hooks: list[tuple[str, Hook]] = []
        self._cleanup = cleanup
        self._failed = failed
        self._lock = threading.Lock()
        self._began = False
        self._done = False
        self._finished = threading.Event()
        self.failures: list[tuple[str, Exception]] = []

    def begin(self) -> None:
        """The handler's run starts; before it, there is nothing to clean up"""
        self._began = True

    def add(self, name: str, release: Hook) -> None:
        """Release ``release`` when the run ends; framework resources register here too"""
        with self._lock:
            self._hooks.append((name, release))

    @property
    def pending(self) -> bool:
        """Whether ``run`` has hooks left to call"""
        with self._lock:
            return not self._done and (bool(self._hooks) or self._began and bool(self._cleanup))

    def run(self, wait: float | None = None) -> None:
        """Call every hook once, the first time only; a failing hook does not stop the rest.
        A call while another thread's is under way returns once that one has finished, or
        after ``wait`` seconds: a hook that hangs on the handler's thread must not hold
        up the answer to a timeout or a signal."""
        with self._lock:
            if not self._began:
                return
            first, self._done = not self._done, True
            hooks = [*reversed(self._hooks)]
        if not first:
            self._finished.wait(wait)
            return
        if self._cleanup is not None:
            hooks.append((CLEANUP_HOOK, self._cleanup))
        try:
            for name, hook in hooks:
                try:
                    hook()
                except Exception as exc:  # noqa: BLE001 - release and cleanup= are user code
                    self.failures.append((name, exc))
                    self._failed(name, exc)
        finally:
            self._finished.set()
