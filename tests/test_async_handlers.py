"""REQ-F-049: async def handlers and async resources, on one event loop per run"""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, Ctx, Exit, NoArgs, RegistrationError
from treaty._aio import Loop, within
from treaty._subprocess import GRACE_SECONDS
from treaty._timeout import Timeout

LINGERCTL = Path(__file__).resolve().parent / "fixture_async_app.py"


def run(app: App, argv: list[str]) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def test_a_command_that_performs_async_io_completes_all_writes_before_exiting() -> None:
    app = App("aio", version="1.0.0")
    written: list[int] = []

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        for i in range(3):
            await asyncio.sleep(0.01)
            written.append(i)
        return {"written": len(written)}

    code, envelope = run(app, ["go"])
    assert code == 0 and envelope["data"] == {"written": 3} and written == [0, 1, 2]


def test_an_exit_raised_in_an_async_handler_is_the_declared_envelope() -> None:
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=("NOT_FOUND",))
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        await asyncio.sleep(0)
        raise Exit.NOT_FOUND("no such thing")

    code, envelope = run(app, ["go"])
    assert envelope["error"]["code"] == "NOT_FOUND" and code != 0


def test_a_crash_in_an_async_handler_is_handler_crashed() -> None:
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    code, envelope = run(app, ["go"])
    assert code == 1 and envelope["error"]["code"] == "HANDLER_CRASHED"


class Pool:
    """Tied to the loop it was opened on, like an aiohttp session or a DB pool"""

    events: list[str] = []

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()

    @classmethod
    async def acquire(cls, args: object, ctx: Ctx) -> Pool:
        await asyncio.sleep(0)
        cls.events.append("pool open")
        return cls()

    async def release(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        await asyncio.sleep(0)
        self.events.append("pool closed")


class Repo:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool

    @classmethod
    async def acquire(cls, args: object, ctx: Ctx, pool: Pool) -> Repo:
        Pool.events.append("repo open")
        return cls(pool)

    async def release(self) -> None:
        Pool.events.append("repo closed")


def test_async_resources_share_the_handlers_loop_and_release_newest_first() -> None:
    Pool.events = []
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx, repo: Repo) -> dict[str, bool]:
        Pool.events.append("handler")
        return {"same_loop": asyncio.get_running_loop() is repo.pool.loop}

    code, envelope = run(app, ["go"])
    assert code == 0 and envelope["data"] == {"same_loop": True}
    assert Pool.events == ["pool open", "repo open", "handler", "repo closed", "pool closed"]


class Cache:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx, pool: Pool) -> Cache:
        return cls()


def test_a_sync_resource_cannot_depend_on_an_async_one() -> None:
    app = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="Cache is sync but needs Pool"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        async def go(args: NoArgs, ctx: Ctx, cache: Cache) -> dict[str, str]:
            return {}


def test_an_async_handler_past_its_timeout_is_cancelled_and_answers_timeout() -> None:
    app = App("aio", version="1.0.0")
    cancelled = threading.Event()

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), timeout=0.3)
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
        return {}

    started = time.monotonic()
    code, envelope = run(app, ["go"])
    assert envelope["error"]["code"] == "TIMEOUT" and code == 10
    assert cancelled.wait(5) and time.monotonic() - started < 5


def test_a_timed_out_async_handler_finishes_its_finally_before_the_answer() -> None:
    """The cancellation lands a reserve before the hard limit; a finally that outlasts
    the reserve still gets the grace, not abandoned at the deadline (#355)"""
    app = App("aio", version="1.0.0")
    cleaned = threading.Event()

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), timeout=0.3)
    async def go(args: NoArgs, ctx: Ctx) -> None:
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.3)
            cleaned.set()

    code, envelope = run(app, ["go"])
    assert envelope["error"]["code"] == "TIMEOUT" and code == 10
    assert cleaned.is_set() and envelope["warnings"] == []


def _cleaning(started: threading.Event, cleaning: threading.Event, done: list[str]) -> Any:
    async def handler(resources: list[Any]) -> None:
        try:
            started.set()
            await asyncio.sleep(3600)
        finally:
            cleaning.set()
            await asyncio.sleep(0.4)
            done.append("finally")

    return handler


async def _no_resources() -> list[Any]:
    return []


def _warn(code: str, message: str, context: dict[str, object]) -> None:
    raise AssertionError(code)


@pytest.mark.parametrize("deadline", [0.2, 0.01])
def test_a_handler_is_cancelled_once_by_a_signal_and_its_deadline_together(
    deadline: float,
) -> None:
    """A signal's cancel turns the deadline off, so it does not land in the ``finally``
    too; when the deadline fired first, the signal's cancel is skipped instead (#355)"""
    loop = Loop()
    started, cleaning = threading.Event(), threading.Event()
    done: list[str] = []
    job = loop.submit(
        within(deadline, Timeout(1), _no_resources, _cleaning(started, cleaning, done), _warn, loop)
    )
    assert started.wait(5)
    if deadline < 0.2:
        assert cleaning.wait(5)  # the deadline fired: the finally is running
    loop.cancel(job)
    assert job.wait(5)
    loop.close()
    assert done == ["finally"], job.exc


def _interrupted(path: str, *flags: str) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run ``path`` until its handler awaits, SIGINT it; the result, and seconds to exit"""
    proc = subprocess.Popen(
        [sys.executable, str(LINGERCTL), path, *flags, "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    seen = ""
    while "lingerctl: waiting" not in seen:
        line = proc.stderr.readline()
        assert line, seen
        seen += line
    proc.send_signal(signal.SIGINT)
    sent = time.monotonic()
    out, err = proc.communicate(timeout=30)
    ended = time.monotonic() - sent
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, seen + err), ended


@needs_posix_signals
@pytest.mark.parametrize("path", ["linger", "linger-bounded"])
def test_sigint_lets_an_async_handlers_finally_await_before_the_run_ends(path: str) -> None:
    """``linger`` has no timeout, so the handler runs on the main thread, which the
    signal interrupts; ``linger-bounded`` runs on a worker (#355)"""
    proc, _ = _interrupted(path, "--cleanup", "0.2")
    assert proc.returncode == 130, proc.stderr
    envelope = json.loads(proc.stdout.splitlines()[-1])
    spec_validator("response-envelope").validate(envelope)
    assert envelope["error"]["code"] == "CANCELLED" and envelope["warnings"] == []
    assert "lingerctl: cleaned up" in proc.stderr


@needs_posix_signals
@pytest.mark.parametrize("path", ["linger", "linger-bounded"])
def test_a_finally_that_outlasts_the_grace_ends_the_run_cancelled_anyway(path: str) -> None:
    proc, ended = _interrupted(path, "--cleanup", "30")
    assert proc.returncode == 130, proc.stderr
    assert ended < GRACE_SECONDS + 3, ended
    envelope = json.loads(proc.stdout.splitlines()[-1])
    assert envelope["error"]["code"] == "CANCELLED"
    assert envelope["error"]["context"]["cleanup_failed"] == "RuntimeError"
    [warning] = envelope["warnings"]
    assert warning["code"] == "CLEANUP_FAILED" and warning["context"]["hook"] == "async handler"
    assert f"still running {GRACE_SECONDS}s after its cancellation" in proc.stderr
    assert "lingerctl: cleaned up" not in proc.stderr


def test_an_async_operation_started_but_not_awaited_is_detected() -> None:
    app = App("aio", version="1.0.0")
    stopped = threading.Event()

    async def forgotten() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        asyncio.create_task(forgotten(), name="forgotten")
        await asyncio.sleep(0)
        return {}

    code, envelope = run(app, ["go"])
    assert code == 0 and stopped.is_set()
    [warning] = [w for w in envelope["warnings"] if w["code"] == "UNAWAITED_TASKS"]
    assert warning["context"]["tasks"] == ["forgotten"]


def test_the_process_exits_only_after_async_teardown_hooks_have_resolved() -> None:
    Pool.events = []
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=("NOT_FOUND",))
    async def go(args: NoArgs, ctx: Ctx, pool: Pool) -> dict[str, str]:
        raise Exit.NOT_FOUND("gone")

    run(app, ["go"])
    assert Pool.events == ["pool open", "pool closed"]


def test_app_call_works_inside_a_host_event_loop() -> None:
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        await asyncio.sleep(0)
        return {"n": 1}

    async def host() -> dict[str, Any]:
        return app.call("go", {}).to_json()

    envelope = asyncio.run(host())
    assert envelope["ok"] and envelope["data"] == {"n": 1}


def test_an_async_handler_sees_the_callers_context_variables() -> None:
    tenant: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="none")
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"tenant": tenant.get()}

    token = tenant.set("acme")
    try:
        assert app.call("go", {}).to_json()["data"] == {"tenant": "acme"}
    finally:
        tenant.reset(token)


def test_an_async_handler_without_a_timeout_runs_on_the_callers_thread() -> None:
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), timeout=None)
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, float | None]:
        await asyncio.sleep(0)
        return {"remaining": ctx.remaining}

    code, envelope = run(app, ["go"])
    assert code == 0 and envelope["data"] == {"remaining": None}


def test_an_async_def_that_returns_is_refused_as_a_streaming_handler() -> None:
    """An async generator streams (tests/test_async_streaming.py); a coroutine cannot"""
    app = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="an async def that returns cannot yield"):

        @app.command("s", description="S", danger_level="safe", exit_codes=(), streaming=True)
        async def s(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:  # type: ignore[misc]
            return iter([])


def test_importing_treaty_does_not_import_asyncio() -> None:
    """On Windows asyncio loads _overlapped, which fails without SYSTEMROOT"""
    probe = "import sys, treaty; print('asyncio' in sys.modules)"
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "False", proc.stderr
