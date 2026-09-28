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
from typing import TYPE_CHECKING, Any

from ._timeout import Timeout, TimeoutExpired

if TYPE_CHECKING:
    import asyncio

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None
        self._thread = threading.Thread(target=self._serve, name="treaty-loop", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            import asyncio
        except BaseException as exc:  # noqa: BLE001 - re-raised on each caller's thread
            self._refuse(exc)
            return
        with asyncio.Runner() as runner:
            self._loop = runner.get_loop()
            while (job := self._jobs.get()) is not None:
                try:
                    job.result = runner.run(self._tracked(job.coro), context=job.context)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                    job.exc = exc
                job.done.set()

    def _refuse(self, exc: BaseException) -> None:
        """The loop could not start (on Windows, ``asyncio`` without ``SYSTEMROOT``):
        every job fails with why, rather than waiting for a loop that never runs"""
        while (job := self._jobs.get()) is not None:
            job.coro.close()
            job.exc = exc
            job.done.set()

    async def _tracked(self, coro: Coroutine[Any, Any, object]) -> object:
        import asyncio

        self._task = asyncio.current_task()
        try:
            return await coro
        finally:
            self._task = None

    def cancel(self) -> None:
        """Cancel the job running on the loop, so its ``finally`` blocks run now and the
        jobs queued behind it, such as the async releases, do not wait for it"""
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion on the loop, in a copy of the caller's context; a
        signal raised while this waits cancels it"""
        job = _Job(coro, contextvars.copy_context())
        self._jobs.put(job)
        try:
            job.done.wait()
        except BaseException:  # noqa: BLE001 - Cancelled or KeyboardInterrupt, re-raised
            self.cancel()
            raise
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
    cancelled, so they never outlive it into its resources' release: on a return they are
    reported as ``UNAWAITED_TASKS``, as their work would otherwise stop silently when the
    loop closes. A resource's own tasks, such as a pool's, stay."""
    import asyncio

    current = asyncio.current_task()
    before: set[asyncio.Task[Any]] | None = None
    try:
        async with asyncio.timeout(remaining) as scope:
            resources = await acquire()
            before = asyncio.all_tasks()
            result = await handler(resources)
    except BaseException as exc:  # noqa: BLE001 - re-raised once the handler's tasks stop
        if before is not None:
            await _stop(_strays(before, current))
        if isinstance(exc, TimeoutError) and scope.expired():
            raise TimeoutExpired(timeout) from None
        raise
    stray = _strays(before, current)
    if stray:
        await _stop(stray)
        names = sorted(t.get_name() for t in stray)
        warn(
            UNAWAITED_TASKS,
            f"the handler returned with {len(stray)} task(s) still running; they were "
            "cancelled, so their work did not finish: await them before returning",
            {"tasks": names},
        )
    return result


def _strays(
    before: set[asyncio.Task[Any]], current: asyncio.Task[Any] | None
) -> list[asyncio.Task[Any]]:
    """The tasks started since ``before`` that still run, the handler's own left out"""
    import asyncio

    return [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]


async def _stop(tasks: list[asyncio.Task[Any]]) -> None:
    import asyncio

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
