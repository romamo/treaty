"""Async generator streaming handlers, stepped on the run's event loop (#347, REQ-F-049)"""

from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_signals, spec_validator
from fixture_async_stream_app import EVENTS, app
from test_streaming import read_lines

from treaty import App, Ctx, NoArgs, RegistrationError
from treaty._aio import AsyncEvents, Loop
from treaty._mcp import call_tool, tool_entries

TICKCTL = Path(__file__).resolve().parent / "fixture_async_stream_app.py"


def run(argv: list[str]) -> tuple[int, list[dict[str, Any]]]:
    EVENTS.clear()
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    for line in lines:
        spec_validator("response-envelope").validate(line)
    return code, lines


def test_an_async_generator_streams_one_envelope_per_event_with_its_context() -> None:
    code, lines = run(["ticks", "--count", "3", "--interval", "0"])
    assert code == 0
    # TENANT was set once inside the generator: one context serves every step
    assert [line["data"] for line in lines[:-1]] == [{"n": n, "tenant": "acme"} for n in (1, 2, 3)]
    assert [line["meta"]["seq"] for line in lines[:-1]] == [1, 2, 3]
    assert lines[-1]["meta"]["end"] is True and lines[-1]["meta"]["total"] == 3
    # the async resource opens on the stream's loop and closes after the source's finally
    assert EVENTS == ["feed open", "source finally", "feed closed"]


@pytest.mark.parametrize("path", ["ticks", "watch"])
def test_an_async_stream_past_its_idle_limit_is_cancelled_and_answers_timeout(path: str) -> None:
    code, lines = run([path, "--stall-at", "2", "--interval", "0", "--timeout", "0.3"])
    assert code == 10
    last = lines[-1]
    assert last["error"]["code"] == "TIMEOUT"
    assert last["meta"]["seq"] == 1 and last["meta"]["partial"] is True
    assert EVENTS == ["feed open", "source finally", "feed closed"]


def test_no_stream_buffers_an_async_stream() -> None:
    code, [envelope] = run(["ticks", "--count", "2", "--interval", "0", "--no-stream"])
    assert code == 0
    assert envelope["data"] == [{"n": 1, "tenant": "acme"}, {"n": 2, "tenant": "acme"}]
    assert envelope["meta"]["total"] == 2


def test_app_call_collects_an_async_stream_even_inside_a_host_event_loop() -> None:
    async def host() -> dict[str, Any]:
        await asyncio.sleep(0)
        return app.call("watch", {"count": 2, "interval": 0}).to_json()

    EVENTS.clear()
    envelope = asyncio.run(host())
    assert envelope["ok"] and envelope["data"] == [
        {"n": 1, "tenant": "acme"},
        {"n": 2, "tenant": "acme"},
    ]
    assert EVENTS == ["feed open", "source finally", "feed closed"]


def test_app_call_bounds_an_async_stream_that_stalls() -> None:
    EVENTS.clear()
    envelope = app.call("watch", {"stall_at": 2, "interval": 0, "timeout": 0.3})
    assert envelope.error is not None and envelope.error.code == "TIMEOUT"
    assert envelope.data == [{"n": 1, "tenant": "acme"}]
    assert EVENTS == ["feed open", "source finally", "feed closed"]


def test_an_mcp_tool_call_collects_an_async_stream() -> None:
    pytest.importorskip("mcp")
    entries = {e.name: e for e in tool_entries(app)}
    envelope = call_tool(app, entries, "ticks", {"count": 2, "interval": 0, "timeout": 5})
    assert envelope.data == [{"n": 1, "tenant": "acme"}, {"n": 2, "tenant": "acme"}]


def test_a_mutating_async_stream_counts_its_effects() -> None:
    code, lines = run(["apply", "--count", "3"])
    assert code == 0
    assert [line["data"]["effect"] for line in lines[:-1]] == ["created", "noop", "created"]
    assert lines[-1]["meta"]["effects"] == {"created": 2, "noop": 1}


@needs_posix_signals
@pytest.mark.parametrize("path", ["ticks", "watch"])
def test_sigint_ends_an_async_stream_cancelled_after_its_finally(path: str) -> None:
    """``ticks`` has no idle limit, so its steps run on the main thread; ``watch`` has one,
    so they run on a worker: either way the pending step is cancelled, not left running
    under the source's ``aclose()`` (#347)"""
    proc = subprocess.Popen(
        [sys.executable, str(TICKCTL), path, "--interval", "0.05", "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    head = read_lines(proc, 2, deadline=time.monotonic() + 10)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 130, err
    lines = [json.loads(line) for line in (head + out).splitlines()]
    last = lines[-1]
    spec_validator("response-envelope").validate(last)
    assert last["error"]["code"] == "CANCELLED", last
    assert last["meta"]["partial"] is True
    assert last["meta"]["seq"] == len(lines) - 1
    assert "already running" not in err
    assert err.index("tickctl: source finally") < err.index("tickctl: feed closed")


@needs_posix_signals
def test_a_source_that_ignores_cancellation_delays_the_end_only_by_the_grace() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(TICKCTL), "stubborn", "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    read_lines(proc, 1, deadline=time.monotonic() + 10)
    # A signal that lands between steps, with no await pending, closes the source at its
    # yield: nothing then ignores the cancellation, so wait until the source awaits
    assert proc.stderr is not None
    seen = ""
    while "tickctl: waiting" not in seen:
        line = proc.stderr.readline()
        assert line, seen
        seen += line
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=15)
    err = seen + err
    assert proc.returncode == 130, err
    assert json.loads(out.splitlines()[-1])["error"]["code"] == "CANCELLED"
    assert "tickctl: cancellation ignored" in err
    assert "still running 2.0s after its cancellation" in err


def test_a_loop_job_is_cancelled_once_so_its_finally_can_await() -> None:
    """A signal cancels the step where it waits, and the stream's stop cancels it again:
    the second must not land in the source's ``finally`` (#347)"""
    loop = Loop()
    started = threading.Event()
    done: list[str] = []

    async def job() -> None:
        try:
            started.set()
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.1)
            done.append("finally")

    pending = loop.submit(job())
    assert started.wait(5)
    loop.cancel()
    time.sleep(0.02)
    loop.cancel()
    assert pending.wait(5)
    loop.close()
    assert done == ["finally"]
    assert isinstance(pending.exc, asyncio.CancelledError)


# Registration


def test_an_async_generator_without_streaming_is_refused() -> None:
    other = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="declare streaming=True"):

        @other.command("g", description="G", danger_level="safe", exit_codes=())
        async def g(args: NoArgs, ctx: Ctx) -> AsyncIterator[dict[str, int]]:
            yield {"n": 1}


def test_an_async_def_that_returns_still_cannot_stream() -> None:
    other = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="an async def that returns cannot yield"):

        @other.command("s", description="S", danger_level="safe", exit_codes=(), streaming=True)
        async def s(args: NoArgs, ctx: Ctx) -> AsyncIterator[dict[str, int]]:  # type: ignore[misc]
            return iter([])  # type: ignore[return-value]


def test_a_streaming_handler_is_annotated_for_its_own_kind() -> None:
    other = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"annotated AsyncIterator\[T\]"):

        @other.command("a", description="A", danger_level="safe", exit_codes=(), streaming=True)
        async def a(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:  # type: ignore[misc]
            yield {"n": 1}

    with pytest.raises(RegistrationError, match=r"annotated Iterator\[T\]"):

        @other.command("s", description="S", danger_level="safe", exit_codes=(), streaming=True)
        def s(args: NoArgs, ctx: Ctx) -> AsyncIterator[dict[str, int]]:  # type: ignore[misc]
            yield {"n": 1}


def test_stop_cancels_a_step_still_queued_on_the_loop() -> None:
    """A signal that lands after a step was queued but before the loop started it found
    no running task to cancel: the step then ran uncancelled and its ``aclose()`` never
    did, so the stream ended only at the grace, with the source's ``finally`` skipped"""
    loop = Loop()
    gate, entered = threading.Event(), threading.Event()
    seen: list[str] = []

    async def source() -> AsyncIterator[int]:
        try:
            yield 1
            await asyncio.sleep(3600)
            yield 2
        finally:
            seen.append("finally")

    async def blocker() -> None:
        entered.set()
        gate.wait(5)  # holds the loop's thread, so the next step stays queued

    events = AsyncEvents(source(), loop, grace=2, owns_loop=True)
    assert next(events) == 1
    loop.submit(blocker())
    assert entered.wait(5)
    stepped: list[object] = []
    stepping = threading.Thread(target=lambda: stepped.append(next(events, "end")), daemon=True)
    stepping.start()
    time.sleep(0.05)  # the step is queued behind the blocker
    events.stop()
    gate.set()
    events.close()
    stepping.join(5)
    assert stepped == ["end"]
    assert seen == ["finally"]
