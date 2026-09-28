"""REQ-F-049: async def handlers and async resources, on one event loop per run"""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Exit, NoArgs, RegistrationError


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


def test_async_streaming_is_refused_at_registration() -> None:
    app = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="streaming handler is a plain generator"):

        @app.command("s", description="S", danger_level="safe", exit_codes=(), streaming=True)
        async def s(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:  # type: ignore[misc]
            return iter([])

    with pytest.raises(RegistrationError, match="async generator"):

        @app.command("g", description="G", danger_level="safe", exit_codes=(), streaming=True)
        async def g(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:  # type: ignore[misc]
            yield {"n": 1}


def test_importing_treaty_does_not_import_asyncio() -> None:
    """On Windows asyncio loads _overlapped, which fails without SYSTEMROOT"""
    probe = "import sys, treaty; print('asyncio' in sys.modules)"
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "False", proc.stderr
