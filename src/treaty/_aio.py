"""Async handlers: an ``async def`` handler and its async resources share one event loop
per run (REQ-F-049).

The loop runs on a daemon thread of its own, so a host that calls ``App.call`` from inside
its own running loop never clashes with it, a release queued from the timeout path waits
behind the handler instead of racing it, and a handler that ignores cancellation cannot
keep the process from exiting. The handler runs under ``ctx.remaining``: at the deadline
it is cancelled, so its ``finally`` blocks and ``async with`` exits run, and the run
answers ``TIMEOUT`` as for a sync handler.

``asyncio`` is imported only once an async command runs: on Windows it loads
``_overlapped``, which fails without ``SYSTEMROOT`` (``WinError 10106``), so importing it
with treaty would break every app started with a stripped environment.
"""

from __future__ import annotations

import contextvars
import queue
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from ._timeout import Timeout, TimeoutExpired

UNAWAITED_TASKS = "UNAWAITED_TASKS"


@dataclass(slots=True)
class _Job:
    coro: Coroutine[Any, Any, object]
    context: contextvars.Context
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    exc: BaseException | None = None


class Loop:
    """One run's event loop on its own thread; ``close`` cancels what is left on it"""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._serve, name="treaty-loop", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        import asyncio

        with asyncio.Runner() as runner:
            while (job := self._jobs.get()) is not None:
                try:
                    job.result = runner.run(job.coro, context=job.context)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                    job.exc = exc
                job.done.set()

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion on the loop, in a copy of the caller's context"""
        job = _Job(coro, contextvars.copy_context())
        self._jobs.put(job)
        job.done.wait()
        if job.exc is not None:
            raise job.exc
        return job.result  # type: ignore[return-value]

    def close(self) -> None:
        """Stop the loop once the jobs queued before this one are done"""
        self._jobs.put(None)


async def within[T](
    remaining: float | None,
    timeout: Timeout,
    acquire: Callable[[], Awaitable[list[Any]]],
    handler: Callable[[list[Any]], Awaitable[T]],
    warn: Callable[[str, str, dict[str, object]], None],
) -> T:
    """Acquire the resources, then await the handler, until the deadline; then cancel
    and raise ``TimeoutExpired``. Tasks the handler started and left running are
    cancelled and reported as ``UNAWAITED_TASKS``: their work would otherwise stop
    silently when the loop closes. A resource's own tasks, such as a pool's, stay."""
    import asyncio

    current = asyncio.current_task()
    try:
        async with asyncio.timeout(remaining) as scope:
            resources = await acquire()
            before = asyncio.all_tasks()
            result = await handler(resources)
    except TimeoutError:
        if scope.expired():
            raise TimeoutExpired(timeout) from None
        raise
    stray = [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]
    if stray:
        for task in stray:
            task.cancel()
        await asyncio.gather(*stray, return_exceptions=True)
        names = sorted(t.get_name() for t in stray)
        warn(
            UNAWAITED_TASKS,
            f"the handler returned with {len(stray)} task(s) still running; they were "
            "cancelled, so their work did not finish: await them before returning",
            {"tasks": names},
        )
    return result
