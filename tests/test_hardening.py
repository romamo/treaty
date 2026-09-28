"""Regressions from the pre-1.0 review: cleanup's reach, ctx.http's redirects and
retries, the background pid file's home, version order, async handlers and signals,
stdin for --flag -, late idempotency records, heartbeats, and handler source scans."""

import base64
import contextvars
import errno
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from conftest import needs_posix_signals
from test_more_declarations import SLEEPER, Watching, stop
from test_network_and_fs import Recording, serving

from treaty import App, Background, Batch, Ctx, Flag, Item, NoArgs, Retry, SideEffect
from treaty._changelog import version_key
from treaty._scan import ctx_calls
from treaty._update import available

WINDOWS = sys.platform == "win32"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def run(app: App, argv: list[str], env: dict[str, str]) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, **env},
    )
    return code, json.loads(out.getvalue())


def data_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


# cleanup


def declaring(*effects: SideEffect) -> App:
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=list(effects),
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_cleanup_does_not_follow_an_out_directory_that_is_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "precious").mkdir(parents=True)
    root = tmp_path / "temp" / f"fx-{os.getuid()}"
    root.mkdir(parents=True, mode=0o700)
    (root / "out").symlink_to(victim)
    code, envelope = run(
        declaring(), ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path / "temp")}
    )
    assert code == 0, envelope
    assert data_of(envelope)["effect"] == "noop"
    assert (victim / "precious").is_dir()


def test_cleanup_keeps_a_credential_a_wider_cache_glob_also_matches(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("secret")
    (tmp_path / "scratch").write_text("x")
    app = declaring(
        SideEffect(f"{tmp_path}/token", "credential"), SideEffect(f"{tmp_path}/*", "cache")
    )
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    assert (tmp_path / "token").read_text() == "secret"
    assert not (tmp_path / "scratch").exists()
    [warning] = [w for w in envelope["warnings"] if w["code"] == "CLEANUP_KEPT"]  # type: ignore[attr-defined]
    assert warning["context"]["paths"] == [str(tmp_path / "token")]


def test_cleanup_keeps_a_cache_directory_that_holds_a_config_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "settings.toml").write_text("region = 'eu'")
    app = declaring(
        SideEffect(f"{cache}/", "cache"), SideEffect(f"{cache}/settings.toml", "config")
    )
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    assert (cache / "settings.toml").exists()
    assert data_of(envelope)["cleaned"] == []


# ctx.http


class Hop(BaseHTTPRequestHandler):
    """Records each request; ``/to?<url>`` redirects there with a 302, and ``/busy``
    answers 503 with the server's ``retry_after``"""

    server: Recording

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.path.startswith("/to?"):
            self.send_response(302)
            self.send_header("Location", self.path.removeprefix("/to?"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 503 if self.path == "/busy" else 200
        self.send_response(status)
        if status == 503 and self.server.retry_after is not None:
            self.send_header("Retry-After", self.server.retry_after)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def hops() -> Iterator[tuple[Recording, Recording]]:
    with serving(Hop) as first, serving(Hop) as second:
        first.retry_after = second.retry_after = None  # type: ignore[attr-defined]
        yield first, second


@dataclass(frozen=True, slots=True)
class Call:
    url: str = Flag(description="URL")
    method: str = Flag(default="GET", description="HTTP method")


def http_app() -> App:
    app = App("netctl", version="1.0.0")

    @app.command(
        "call",
        description="Call a URL with credentials",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        retry=Retry(retries=3, delay_ms=0),
    )
    def call(args: Call, ctx: Ctx) -> dict[str, int]:
        headers = {"Authorization": "Bearer SECRET", "X-Api-Key": "KEY", "Accept": "text/plain"}
        return {"status": ctx.http.request(args.method, args.url, headers=headers).status}

    return app


def test_a_redirect_to_another_origin_drops_the_callers_credentials(
    hops: tuple[Recording, Recording],
) -> None:
    first, second = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/to?{second.url}/x"], {})
    assert code == 0, envelope
    [(_, _, headers)] = second.seen
    assert "Authorization" not in headers and "X-Api-Key" not in headers
    assert headers["Accept"] == "text/plain"


def test_a_redirect_within_the_origin_keeps_the_callers_credentials(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/to?{first.url}/x"], {})
    assert code == 0, envelope
    assert first.seen[1][2]["Authorization"] == "Bearer SECRET"


def test_a_post_that_reached_the_server_is_not_retried(hops: tuple[Recording, Recording]) -> None:
    first, _ = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy", "--method", "POST"], {})
    assert code == 12, envelope
    assert len(first.seen) == 1


def test_a_get_is_retried_after_the_delay_retry_after_asks_for(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    first.retry_after = "1"  # type: ignore[attr-defined]
    started = time.monotonic()
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy", "--retries", "1"], {})
    assert code == 12, envelope
    assert len(first.seen) == 2 and time.monotonic() - started >= 1


def test_a_retry_after_too_long_to_sleep_ends_the_run_with_it(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    first.retry_after = "600"  # type: ignore[attr-defined]
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy"], {})
    assert code == 12, envelope
    assert len(first.seen) == 1
    error = envelope["error"]
    assert isinstance(error, dict) and error["retry_after_ms"] == 600_000


# ctx.spawn without a state directory


def stateless_watcher() -> App:
    app = App("watch", version="1.0.0")

    @app.command(
        "start-watcher",
        description="Start a watcher",
        danger_level="safe",
        exit_codes=(),
        background=Background("watch stop-watcher", max_lifetime_seconds=60),
    )
    def start(args: NoArgs, ctx: Ctx) -> Watching:
        spawned = ctx.spawn(SLEEPER)
        return Watching(spawned.pid, "watch stop-watcher", str(spawned.pid_file))

    @app.command("stop-watcher", description="Stop the watcher", danger_level="safe", exit_codes=())
    def stop_watcher(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_without_a_state_directory_pid_files_live_in_the_users_private_temp_root(
    tmp_path: Path,
) -> None:
    code, envelope = run(stateless_watcher(), ["start-watcher"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    data = data_of(envelope)
    pid = data["background_pid"]
    assert isinstance(pid, int)
    try:
        background = tmp_path / f"watch-{os.getuid()}" / "background"
        assert Path(str(data["pid_file"])) == background / "start-watcher.pids"
        assert background.stat().st_mode & 0o777 == 0o700
    finally:
        stop(pid)


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_a_planted_background_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / f"watch-{os.getuid()}"
    root.mkdir(mode=0o700)
    (root / "background").symlink_to(elsewhere)
    code, envelope = run(stateless_watcher(), ["start-watcher"], {"TMPDIR": str(tmp_path)})
    error = envelope["error"]
    assert isinstance(error, dict)
    assert code == 4 and error["code"] == "TEMP_DIR_UNSAFE"
    assert list(elsewhere.iterdir()) == []


# Changelog order


def test_changelog_versions_follow_semver_precedence() -> None:
    ordered = [
        "0.9.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0-rc.9",
        "1.0.0-rc.10",
        "1.0.0",
    ]
    assert sorted(reversed(ordered), key=version_key) == ordered


class NoCheck:
    def latest(self, current: str, timeout: float) -> str | None:
        raise AssertionError("a fresh cache needs no check")


def test_an_update_notice_names_a_later_pre_release(tmp_path: Path) -> None:
    cache = {"checked_at": time.time(), "latest": "1.0.0-rc.10"}
    (tmp_path / "update.json").write_text(json.dumps(cache))
    assert available(NoCheck(), "1.0.0-rc.9", tmp_path) == "1.0.0-rc.10"
    assert available(NoCheck(), "1.0.0-rc.10", tmp_path) is None


# Signals: an async handler, and --flag - waiting on stdin

HARDCTL = Path(__file__).resolve().parent / "fixture_hardening_app.py"


@needs_posix_signals
@pytest.mark.parametrize("command", ["wait", "wait-timed"])
def test_a_signal_cancels_an_async_handler_and_releases_its_resources(command: str) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(HARDCTL), command, "--format", "json"],
        env=BASE_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    assert proc.stderr.readline() == "started\n"
    proc.send_signal(signal.SIGINT)
    started = time.monotonic()
    out, err = proc.communicate(timeout=10)
    assert time.monotonic() - started < 1.5, err  # well inside the 2 s grace: not waited out
    assert proc.returncode == 130, err
    assert json.loads(out)["error"]["code"] == "CANCELLED"
    assert err.index("handler finally") < err.index("released")


class SignallingStdin(io.StringIO):
    """A pipe whose writer never finishes: SIGINT arrives while the read waits"""

    def read(self, size: int | None = -1) -> str:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)
        raise AssertionError("the signal did not end the read")


@needs_posix_signals
def test_a_signal_while_flag_dash_reads_stdin_answers_cancelled() -> None:
    from fixture_hardening_app import app

    out = io.StringIO()
    code = app.run(
        ["greet", "--name", "-", "--format", "json"],
        stdin=SignallingStdin(),
        stdout=out,
        stderr=io.StringIO(),
        env=BASE_ENV,
    )
    assert code == 130
    assert json.loads(out.getvalue())["error"]["code"] == "CANCELLED"


def test_flag_dash_refuses_stdin_that_is_not_utf8_under_surrogateescape() -> None:
    from fixture_hardening_app import app

    stdin = io.TextIOWrapper(io.BytesIO(b"ab\xffcd"), encoding="utf-8", errors="surrogateescape")
    out = io.StringIO()
    code = app.run(
        ["greet", "--name", "-", "--format", "json"],
        stdin=stdin,
        stdout=out,
        stderr=io.StringIO(),
        env=BASE_ENV,
    )
    assert code == 2
    assert json.loads(out.getvalue())["error"]["code"] == "STDIN_NOT_UTF8"


def test_an_async_command_fails_rather_than_hangs_when_asyncio_cannot_load(
    tmp_path: Path,
) -> None:
    # A shadowing module stands in for Windows without SYSTEMROOT (WinError 10106)
    (tmp_path / "asyncio.py").write_text("raise OSError(10106, 'the provider failed to load')\n")
    script = tmp_path / "aioctl.py"
    script.write_text(
        "import sys\n"
        "from treaty import App, Ctx, NoArgs\n"
        "app = App('aioctl', version='1.0.0')\n"
        "@app.command('go', description='Go', danger_level='safe', exit_codes=())\n"
        "async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:\n"
        "    return {}\n"
        "sys.exit(app.run(sys.argv[1:]))\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script), "go", "--format", "json"],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0, proc.stdout
    error = json.loads(proc.stdout)["error"]
    assert "10106" in error["message"]


# Where a handler runs


def test_a_handler_without_a_timeout_cannot_change_the_callers_contextvars() -> None:
    seen: contextvars.ContextVar[str] = contextvars.ContextVar("seen", default="unset")
    app = App("ctxctl", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), timeout=None)
    def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        before = seen.get()
        seen.set("from the handler")
        return {"before": before}

    for _ in range(2):
        code, envelope = run(app, ["go"], {})
        assert code == 0 and data_of(envelope)["before"] == "unset"
    assert seen.get() == "unset"


@dataclass(frozen=True, slots=True)
class Sent:
    effect: str


def test_a_timed_out_batch_is_recorded_so_a_retry_replays_it(tmp_path: Path) -> None:
    calls: list[int] = []
    app = App("batchctl", version="1.0.0", state_dir=tmp_path)

    @app.command("send", description="Send", danger_level="mutating", exit_codes=(), timeout=0.2)
    def send(args: NoArgs, ctx: Ctx) -> Batch[Sent]:
        calls.append(1)
        time.sleep(0.5)
        return Batch([Item(1, Sent("created"))])

    argv = ["send", "--idempotency-key", "k-12345678"]
    assert run(app, argv, {})[1]["error"]["code"] == "TIMEOUT"  # type: ignore[index]
    # The key is busy until the late result is recorded; then a retry replays it
    deadline = time.monotonic() + 10
    while True:
        code, envelope = run(app, argv, {})
        busy = code != 0 and envelope["error"]["code"] == "IDEMPOTENCY_KEY_BUSY"  # type: ignore[index]
        if not busy or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert code == 0, envelope
    assert len(calls) == 1


class FailingHeartbeats(io.StringIO):
    """A stdout that fails every heartbeat line with EIO, and takes the envelope"""

    def write(self, s: str) -> int:
        if '"heartbeat"' in s:
            raise OSError(errno.EIO, "I/O error")
        return super().write(s)


def test_a_failed_heartbeat_write_neither_crashes_the_run_nor_frees_its_key(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    app = App("beatctl", version="1.0.0", state_dir=tmp_path)

    @app.command("go", description="Go", danger_level="mutating", exit_codes=(), heartbeat=True)
    def go(args: NoArgs, ctx: Ctx) -> Sent:
        calls.append(threading.current_thread().name)
        time.sleep(0.4)
        return Sent("created")

    for _ in range(2):
        out = FailingHeartbeats()
        code = app.run(
            ["go", "--heartbeat-ms", "50", "--idempotency-key", "k-12345678", "--format", "json"],
            stdout=out,
            stderr=io.StringIO(),
            env=BASE_ENV,
        )
        assert code == 0, out.getvalue()
    assert len(calls) == 1


# Registration and parsing


def test_a_nested_handler_with_a_column_zero_string_is_scanned() -> None:
    def factory() -> object:
        def handler(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            text = """
line at column zero
"""
            ctx.run(["git", "status"])
            return {"text": text}

        return handler

    [call] = ctx_calls(factory())  # type: ignore[arg-type]
    assert call.method == "run" and call.line == 5


def test_a_deeply_nested_cursor_is_an_invalid_cursor() -> None:
    app = App("pagectl", version="1.0.0")

    @app.command("items", description="Items", danger_level="safe", exit_codes=(), paginated=True)
    def items(args: NoArgs, ctx: Ctx) -> list[dict[str, str]]:
        return [{"id": "a"}]

    token = base64.urlsafe_b64encode(b"[" * 50_000 + b"]" * 50_000).decode().rstrip("=")
    code, envelope = run(app, ["items", "--cursor", token], {})
    assert code == 2
    assert envelope["error"]["code"] == "INVALID_CURSOR"  # type: ignore[index]
