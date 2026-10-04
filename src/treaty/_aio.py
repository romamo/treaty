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
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._timeout import SIGNAL_POLL_SECONDS, Timeout, TimeoutExpired

if TYPE_CHECKING:
    import asyncio

UNAWAITED_TASKS = "UNAWAITED_TASKS"


def _held() -> threading.Lock:
    lock = threading.Lock()
    lock.acquire()
    return lock


@dataclass(slots=True)
class _Job:
    coro: Coroutine[Any, Any, object]
    context: contextvars.Context
    done: threading.Lock = field(default_factory=_held)
    """Released once the job ended. A lock, not an ``Event``: a signal raised inside
    ``Event.wait``'s Python code can release its condition's lock twice, which turns the
    ``Cancelled`` into a ``RuntimeError``; ``acquire`` is one call (#304)"""
    result: object = None
    exc: BaseException | None = None

    def wait(self, seconds: float) -> bool:
        """Whether the job ended within ``seconds``; sliced as ``Loop.run``'s wait is"""
        deadline = time.monotonic() + seconds
        while not self.done.acquire(timeout=min(SIGNAL_POLL_SECONDS, seconds)):
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                return False
        self.done.release()  # a later wait sees it ended too
        return True


class Loop:
    """One run's event loop on its own thread; ``close`` cancels what is left on it"""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None
        self._closed = False
        """Set before the ``None`` that stops the loop; no lock, as a signal raised while
        one is held could leave it held"""
        self._cancelled: asyncio.Task[object] | None = None
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
                job.done.release()
        self._drain()

    def _drain(self) -> None:
        """Fail the jobs queued behind the ``None``: the loop is gone, so none would run"""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                job.coro.close()
                job.exc = RuntimeError("the run's event loop is closed")
                job.done.release()

    def _refuse(self, exc: BaseException) -> None:
        """The loop could not start (on Windows, ``asyncio`` without ``SYSTEMROOT``):
        every job fails with why, rather than waiting for a loop that never runs"""
        while (job := self._jobs.get()) is not None:
            job.coro.close()
            job.exc = exc
            job.done.release()

    async def _tracked(self, coro: Coroutine[Any, Any, object]) -> object:
        import asyncio

        self._task = asyncio.current_task()
        try:
            return await coro
        finally:
            self._task = None

    def cancel(self) -> None:
        """Cancel the job running on the loop, so its ``finally`` blocks run now and the
        jobs queued behind it, such as the async releases, do not wait for it. Once per
        job: a second cancel would land in those ``finally`` blocks' own awaits (#347)"""
        loop, task = self._loop, self._task
        if loop is None or task is None or task is self._cancelled:
            return
        self._cancelled = task
        loop.call_soon_threadsafe(task.cancel)

    def submit(
        self, coro: Coroutine[Any, Any, object], context: contextvars.Context | None = None
    ) -> _Job:
        """Queue ``coro`` behind the jobs before it, without waiting; once the loop was
        closed, the job fails with ``coro`` unrun"""
        job = _Job(coro, contextvars.copy_context() if context is None else context)
        self._jobs.put(job)
        if self._closed:
            self._drain()  # the loop may have drained before this job landed
        return job

    def run[T](self, coro: Coroutine[Any, Any, T], context: contextvars.Context | None = None) -> T:
        """Run ``coro`` to completion on the loop, in ``context`` or a copy of the caller's;
        a signal raised while this waits cancels it. The wait is sliced, so a signal no
        lock wait wakes for still raises here within ``SIGNAL_POLL_SECONDS`` (#304)"""
        job = self.submit(coro, context)
        try:
            while not job.done.acquire(timeout=SIGNAL_POLL_SECONDS):
                pass
        except BaseException:  # noqa: BLE001 - Cancelled or KeyboardInterrupt, re-raised
            self.cancel()
            raise
        if job.exc is not None:
            raise job.exc
        return job.result  # type: ignore[return-value]

    def close(self) -> None:
        """Stop the loop once the jobs queued before this one are done"""
        if not self._closed:
            self._closed = True
            self._jobs.put(None)


class AsyncEvents(Iterator[object]):
    """A streaming handler's async generator, stepped on the run's loop (#347)

    Each ``next()`` awaits one ``__anext__()`` there, all in one context, so what the
    generator sets survives between events as a plain generator's does. ``stop`` cancels
    the pending step and queues ``aclose()`` behind it without waiting: a signal, a
    timeout, or a reader that stops ends the stream at once, and the source's ``finally``
    still runs, before the run's async releases and the loop's close, which queue behind
    it. ``close`` stops it, then waits up to ``grace`` seconds for ``aclose()``."""

    def __init__(
        self, source: AsyncGenerator[object, Any], loop: Loop, *, grace: float, owns_loop: bool
    ) -> None:
        self._source = source
        self._loop = loop
        self._grace = grace
        self._owns_loop = owns_loop
        self._context = contextvars.copy_context()
        self._stopped = False
        """A flag, not a lock: the stream's thread may be interrupted anywhere by a signal"""
        self._closing: _Job | None = None

    def __next__(self) -> object:
        import asyncio

        if self._stopped:
            raise StopIteration
        try:
            return self._loop.run(self._source.__anext__(), self._context)
        except StopAsyncIteration:
            raise StopIteration from None
        except asyncio.CancelledError:
            if self._stopped:
                raise StopIteration from None  # the step stop() cancelled: the stream ended
            raise

    def stop(self) -> None:
        """Cancel the pending step, then ``aclose()`` the source once it ended; once only"""
        if self._stopped:
            return
        self._stopped = True
        self._loop.cancel()
        self._closing = self._loop.submit(self._source.aclose(), self._context)

    def close(self) -> None:
        """Stop the source and wait for its ``finally`` blocks; what they raised, or a
        source still running past the grace, is raised here"""
        self.stop()
        job = self._closing
        try:
            if job is None:
                return
            if not job.wait(self._grace):
                raise RuntimeError(
                    f"the stream's async generator was still running {self._grace}s after "
                    "its cancellation; its finally blocks may not run: let CancelledError "
                    "propagate from its awaits"
                )
            if job.exc is not None:
                raise job.exc
        finally:
            if self._owns_loop:
                self._loop.close()


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
