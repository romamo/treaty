"""Teardown on every exit: resource release and cleanup= (REQ-C-017)"""

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import fixture_lifecycle_app
import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, Ctx, Exit, NoArgs, RegistrationError
from treaty._audit import audit
from treaty._lifecycle import Teardown

LIFECTL = Path(__file__).resolve().parent / "fixture_lifecycle_app.py"


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope, err.getvalue()


def lifectl(state: Path, *argv: str) -> subprocess.Popen[str]:
    env = {**os.environ, "LIFECTL_STATE_DIR": str(state), "LIFECTL_LOG": str(state / "log")}
    return subprocess.Popen(
        [sys.executable, str(LIFECTL), *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def free(state: Path) -> bool:
    """Whether another run can take the lock at once"""
    env = {"LIFECTL_STATE_DIR": str(state), "LIFECTL_LOG": str(state / "log")}
    code, _, _ = run(fixture_lifecycle_app.app, ["hold"], env)
    return code == 0


def log(state: Path) -> list[str]:
    return (state / "log").read_text().split()


def wait_for(state: Path, line: str) -> None:
    deadline = time.monotonic() + 30
    while not ((state / "log").exists() and line in log(state)):
        assert time.monotonic() < deadline, f"{line!r} never logged"
        time.sleep(0.02)


def recording_app(events: list[str], body: str = "ok", cleanup_fails: bool = False) -> App:
    class First:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            events.append("acquire First")
            return cls()

        def release(self) -> None:
            events.append("release First")

    class Second:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx, first: First) -> Self:
            events.append("acquire Second")
            return cls()

        def release(self) -> None:
            events.append("release Second")

    app = App("rec", version="1.0.0", default_timeout=None)

    def cleanup() -> None:
        events.append("cleanup")
        if cleanup_fails:
            raise OSError("gone")

    @app.command(
        "go",
        description="Go",
        danger_level="safe",
        exit_codes=("NOT_FOUND",),
        cleanup=cleanup,
    )
    def go(args: NoArgs, ctx: Ctx, second: Second, first: First) -> dict[str, str]:
        events.append("handler")
        if body == "exit":
            raise Exit.NOT_FOUND("missing")
        if body == "crash":
            raise RuntimeError("boom")
        return {}

    return app


# Acceptance criteria


@pytest.mark.parametrize("how", ["normal", "sigterm", "timeout"])
@needs_posix_signals
def test_a_command_that_acquires_a_lock_has_the_lock_released_on_normal_exit_sigterm_and_timeout(
    tmp_path: Path, how: str
) -> None:
    seconds = {"normal": "0", "sigterm": "30", "timeout": "30"}[how]
    extra = ["--timeout", "0.5"] if how == "timeout" else []
    proc = lifectl(tmp_path, "hold", "--seconds", seconds, *extra)
    if how == "sigterm":
        wait_for(tmp_path, "acquired")
        proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    expected = {"normal": 0, "sigterm": 143, "timeout": 10}[how]
    assert proc.returncode == expected, err
    assert log(tmp_path) == ["acquired", "released", "cleanup"]
    assert free(tmp_path)


def test_after_a_timeout_the_lock_is_released_while_the_handler_still_runs(
    tmp_path: Path,
) -> None:
    env = {"LIFECTL_STATE_DIR": str(tmp_path), "LIFECTL_LOG": str(tmp_path / "log")}
    started = time.monotonic()
    code, envelope, _ = run(
        fixture_lifecycle_app.app, ["hold", "--seconds", "30", "--timeout", "0.2"], env
    )
    assert code == 10 and envelope["error"]["code"] == "TIMEOUT"
    assert time.monotonic() - started < 10  # the grace, not the handler's 30 s
    assert log(tmp_path) == ["acquired", "released", "cleanup"]
    assert free(tmp_path)


def test_a_cleanup_hook_called_twice_produces_the_same_end_state_as_calling_it_once() -> None:
    calls: list[str] = []
    teardown = Teardown(lambda: calls.append("cleanup"), lambda name, exc: None)
    teardown.add("r.release", lambda: calls.append("release"))
    teardown.begin()
    teardown.run()
    teardown.run()
    assert calls == ["release", "cleanup"] and not teardown.pending


def test_a_command_that_does_not_acquire_any_resources_may_register_a_no_op_cleanup_hook() -> None:
    app = App("noop", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), cleanup=lambda: None)
    def go(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    code, envelope, _ = run(app, ["go"])
    assert code == 0 and envelope["warnings"] == []


def test_the_framework_warns_at_development_time_if_a_command_acquires_a_tracked_resource_without_a_cleanup_hook() -> (  # noqa: E501
    None
):
    class Connection:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            return cls()

        def close(self) -> None:
            pass

    class Released(Connection):
        def release(self) -> None:
            self.close()

    app = App("aud", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: NoArgs, ctx: Ctx, conn: Connection) -> dict[str, bool]:
        return {}

    @app.command("fine", description="Fine", danger_level="safe", exit_codes=())
    def fine(args: NoArgs, ctx: Ctx, conn: Released) -> dict[str, bool]:
        return {}

    [rule] = [r for r in audit(app, "aud", limit=3).rules if r.id == "resource-release"]
    assert [f.command for f in rule.findings] == ["go"]
    assert "def release(self) -> None: self.close()" in rule.findings[0].fix


# The teardown path by path


@pytest.mark.parametrize(
    ("body", "code"), [("ok", 0), ("exit", 5), ("crash", 1)], ids=["result", "exit", "crash"]
)
def test_resources_are_released_newest_first_then_cleanup_runs_on_every_exit(
    body: str, code: int
) -> None:
    events: list[str] = []
    got, _, _ = run(recording_app(events, body), ["go"])
    assert got == code
    assert events == [
        "acquire First",
        "acquire Second",
        "handler",
        "release Second",
        "release First",
        "cleanup",
    ]


def test_a_failing_cleanup_warns_cleanup_failed_and_keeps_the_exit_code() -> None:
    events: list[str] = []
    code, envelope, err = run(recording_app(events, cleanup_fails=True), ["go"])
    assert code == 0 and envelope["ok"] is True
    [warning] = envelope["warnings"]
    assert warning["code"] == "CLEANUP_FAILED"
    assert warning["context"] == {"hook": "cleanup", "exception": "OSError"}
    assert "OSError: gone" in err


def test_a_failing_release_does_not_stop_the_next_one() -> None:
    events: list[str] = []

    class Flaky:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            return cls()

        def release(self) -> None:
            raise RuntimeError("stuck")

    app = App("fl", version="1.0.0")

    @app.command(
        "go",
        description="Go",
        danger_level="safe",
        exit_codes=(),
        cleanup=lambda: events.append("cleanup"),
    )
    def go(args: NoArgs, ctx: Ctx, flaky: Flaky) -> dict[str, bool]:
        return {}

    code, envelope, _ = run(app, ["go"])
    assert code == 0 and events == ["cleanup"]
    assert envelope["warnings"][0]["context"]["hook"].endswith("Flaky.release")


def test_a_failing_acquire_releases_what_was_already_acquired() -> None:
    events: list[str] = []

    class Held:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            return cls()

        def release(self) -> None:
            events.append("release Held")

    class Missing:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx, held: Held) -> Self:
            raise Exit.NOT_FOUND("no such thing")

    app = App("af", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=("NOT_FOUND",))
    def go(args: NoArgs, ctx: Ctx, missing: Missing) -> dict[str, bool]:
        return {}

    code, _, _ = run(app, ["go"])
    assert code == 5 and events == ["release Held"]


def test_a_finished_stream_is_torn_down_before_its_terminal_envelope() -> None:
    order: list[str] = []
    out = io.StringIO()
    app = App("st", version="1.0.0")

    @app.command(
        "tick",
        description="Tick",
        streaming=True,
        danger_level="safe",
        exit_codes=(),
        cleanup=lambda: order.append(f"cleanup after {out.getvalue().count(chr(10))} lines"),
    )
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}

    assert app.run(["tick"], stdout=out, stderr=io.StringIO(), env={}) == 0
    assert order == ["cleanup after 1 lines"]


def test_release_takes_only_self() -> None:
    class Odd:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            return cls()

        def release(self, force: bool) -> None:
            pass

    app = App("odd", version="1.0.0")
    with pytest.raises(RegistrationError, match="release takes only self"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: NoArgs, ctx: Ctx, odd: Odd) -> dict[str, bool]:
            return {}


def test_a_second_teardown_call_waits_at_most_its_bound_for_a_hanging_hook() -> None:
    release = threading.Event()
    teardown = Teardown(None, lambda hook, exc: None)
    teardown.begin()
    teardown.add("Conn.release", lambda: release.wait(30))
    worker = threading.Thread(target=teardown.run)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    teardown.run(0.1)
    assert time.monotonic() - started < 5
    release.set()
    worker.join()


def test_a_release_that_hangs_on_the_handler_thread_does_not_hold_up_the_timeout() -> None:
    unblock = threading.Event()

    class Conn:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Self:
            return cls()

        def release(self) -> None:
            unblock.wait(60)

    app = App("hangctl", version="1.0.0", description="Hangs")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), timeout=0.2)
    def go(args: NoArgs, ctx: Ctx, conn: Conn) -> dict[str, int]:
        time.sleep(0.4)
        return {"n": 1}

    started = time.monotonic()
    try:
        code, envelope, _ = run(app, ["go"])
    finally:
        unblock.set()
    assert code == 10 and envelope["error"]["code"] == "TIMEOUT"
    assert time.monotonic() - started < 15
