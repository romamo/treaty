"""Verbosity, --warnings-as-errors, and the opt-in audit log (REQ-F-038, REQ-O-008,
REQ-O-025, REQ-O-030, REQ-F-034, REQ-O-023, REQ-F-025, REQ-F-060)"""

import datetime as dt
import io
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_permissions, spec_validator

from treaty import (
    App,
    Arg,
    AuditLog,
    Ctx,
    Deprecated,
    Exit,
    Flag,
    Format,
    NoArgs,
    RegistrationError,
)
from treaty._app import _RECORDS, _LateStream
from treaty._atomic import exclusive
from treaty._audit import audit
from treaty._cli import cli
from treaty._journal import Journal

BASE = "logctl"


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)
    user: str = Flag(default="me", description="User name")


@dataclass(frozen=True, slots=True)
class Count:
    n: int = Flag(default=0, description="How many warnings")


def make_app(**kwargs: Any) -> App:
    app = App(BASE, version="1.0.0", **kwargs)
    app.exit_code("GONE", 79, description="No such thing", retryable=False, side_effects="none")

    @app.command("chat", description="Log at every level", danger_level="safe", exit_codes=())
    def chat(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.log("connecting", host="db")
        ctx.progress("copying", done=1, total=2)
        ctx.debug("internals", step=1)
        ctx.log_error("disk nearly full", free_mb=5)
        return {"status": "ok"}

    @app.command("warn", description="Warn n times", danger_level="safe", exit_codes=())
    def warn(args: Count, ctx: Ctx) -> dict[str, int]:
        for i in range(args.n):
            ctx.warn("SOMETHING_ODD", f"odd {i}", index=i)
        return {"n": args.n}

    @app.command("chatty", description="Print", danger_level="safe", exit_codes=())
    def chatty(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        print("initialized")
        return {"status": "ok"}

    @app.command("login", description="Log in", danger_level="safe", exit_codes=())
    def login(args: Login, ctx: Ctx) -> dict[str, object]:
        return {"length": len(args.api_token), "PassWord": "hunter2", "user": args.user}

    @app.command("missing", description="Fail", danger_level="safe", exit_codes=["GONE"])
    def missing(args: NoArgs, ctx: Ctx) -> None:
        raise Exit.GONE("no such thing")

    @app.command("child", description="Run a child", danger_level="safe", exit_codes=())
    def child(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"out": ctx.run([sys.executable, "-c", "print('hi')"]).stdout.strip()}

    @app.command(
        "fetch",
        description="Fetch a URL",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
    )
    def fetch(args: Url, ctx: Ctx) -> dict[str, int]:
        response = ctx.http.get(args.url, headers={"Authorization": "Bearer sk-secret-1"})
        return {"status": response.status}

    @app.command("lib", description="Log through a library", danger_level="safe", exit_codes=())
    def lib(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        logging.getLogger("somelib").debug("pool size %d", 4)
        return {"ok": True}

    @app.command("tail", description="Stream", danger_level="safe", exit_codes=(), streaming=True)
    def tail(args: Count, ctx: Ctx) -> Iterator[dict[str, int]]:
        for i in range(2):
            if i == 1 and args.n:
                ctx.warn("SOMETHING_ODD", "odd")
            yield {"i": i}

    return app


@dataclass(frozen=True, slots=True)
class Url:
    url: str = Flag(description="URL")


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None, *, isatty: bool = False
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=isatty)
    return code, out.getvalue(), err.getvalue()


def envelope_of(out: str) -> dict[str, Any]:
    return json.loads(out.splitlines()[-1])


def logged(tmp_path: Path, argv: list[str], **env: str) -> tuple[int, dict[str, Any]]:
    """Run with the audit log turned on under ``tmp_path``; the envelope"""
    code, out, _ = run(make_app(), [*argv, "--format", "json"], data_env(tmp_path, **env))
    return code, envelope_of(out)


def data_env(tmp_path: Path, **env: str) -> dict[str, str]:
    """The operator turned the log on; its default home is under ``tmp_path``"""
    return {"XDG_STATE_HOME": str(tmp_path / "state"), "LOGCTL_AUDIT_LOG": "1", **env}


def log_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / BASE / "audit.jsonl"


def entries(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_file(tmp_path).read_text().splitlines()]


# REQ-F-038: auto-quiet off a terminal


def test_in_a_non_tty_context_progress_calls_produce_no_output() -> None:
    code, _, err = run(make_app(), ["chat"])
    assert code == 0 and "copying" not in err


def test_in_a_non_tty_context_log_calls_at_info_and_below_produce_no_stderr_output() -> None:
    _, _, err = run(make_app(), ["chat"])
    assert "connecting" not in err and "internals" not in err


def test_error_level_log_calls_are_always_emitted_regardless_of_tty_state() -> None:
    for isatty, env in ((False, {}), (True, {}), (True, {"CI": "true"})):
        _, _, err = run(make_app(), ["chat", "--format", "json"], env, isatty=isatty)
        records = [json.loads(line) for line in err.splitlines()]
        error = {"level": "error", "message": "disk nearly full", "fields": {"free_mb": 5}}
        assert records[-1] == error


def test_explicitly_passing_verbose_overrides_auto_quiet_mode() -> None:
    _, _, err = run(make_app(), ["chat", "--verbose", "--format", "json"])
    levels = [json.loads(line)["level"] for line in err.splitlines()]
    assert levels == ["info", "progress", "error"]


def test_a_terminal_shows_info_and_progress_but_ci_quiets_it() -> None:
    _, _, err = run(make_app(), ["chat"], isatty=True)
    assert err.splitlines() == [
        "connecting host=db",
        "progress: copying done=1 total=2",
        "error: disk nearly full free_mb=5",
    ]
    _, _, err = run(make_app(), ["chat", "--format", "plain"], {"CI": "1"}, isatty=True)
    assert err.splitlines() == ["error: disk nearly full free_mb=5"]


def test_stray_print_text_is_dropped_off_a_terminal_but_still_reported() -> None:
    code, out, err = run(make_app(), ["chatty"])
    warning = envelope_of(out)["warnings"][0]
    assert code == 0 and err == "" and warning["context"]["text"] == "initialized"


# REQ-O-008: --quiet, --verbose, --debug


def test_quiet_produces_zero_bytes_on_stderr_even_for_warnings() -> None:
    for argv in (["chat"], ["chatty"], ["warn", "--n", "1"], ["missing", "--format", "plain"]):
        _, out, err = run(make_app(), [*argv, "--quiet"], isatty=True)
        assert err == "", argv
    _, out, err = run(make_app(), ["--quiet", "--help", "--format", "json"])
    assert err == "" and json.loads(out)["meta"]["help"] is True


def test_verbose_produces_progress_messages_on_stderr_and_the_json_result_on_stdout() -> None:
    code, out, err = run(make_app(), ["chat", "--verbose", "--format", "json"])
    assert code == 0 and json.loads(out)["data"] == {"status": "ok"}
    progress = [json.loads(line) for line in err.splitlines()][1]
    assert progress == {
        "level": "progress",
        "message": "copying",
        "fields": {"done": 1, "total": 2},
    }


def test_passing_verbose_with_ci_true_overrides_the_auto_quiet_mode() -> None:
    argv = ["chat", "--verbose", "--format", "plain"]
    _, _, err = run(make_app(), argv, {"CI": "true"}, isatty=True)
    assert "progress: copying done=1 total=2" in err.splitlines()


def test_the_verbosity_flags_are_mutually_exclusive() -> None:
    code, out, _ = run(make_app(), ["chat", "--quiet", "--debug"])
    error = json.loads(out)["error"]
    assert code == 2 and error["code"] == "ARG_ERROR"
    assert error["context"]["flags"] == ["debug", "quiet"]


def levels(err: str) -> set[str]:
    return {json.loads(line)["level"] for line in err.splitlines()}


def test_v_is_short_for_verbose_and_vv_for_debug_in_any_position() -> None:
    for argv in (["chat", "-v"], ["-v", "chat"]):
        code, _, err = run(make_app(), argv)
        assert code == 0 and levels(err) == {"info", "progress", "error"}, argv
    for argv in (["chat", "-vv"], ["chat", "-v", "-v"], ["-v", "chat", "-v"]):
        code, _, err = run(make_app(), argv)
        assert code == 0 and "debug" in levels(err), argv
    code, out, _ = run(make_app(), ["chat", "-v", "--quiet"])
    assert code == 2 and json.loads(out)["error"]["context"]["flags"] == ["quiet", "verbose"]


@dataclass(frozen=True, slots=True)
class Grep:
    invert: bool = Flag(default=False, short="v", description="Select non-matching lines")


def test_a_command_with_its_own_short_v_keeps_it() -> None:
    app = make_app()

    @app.command("grep", description="Match lines", danger_level="safe", exit_codes=())
    def grep(args: Grep, ctx: Ctx) -> dict[str, bool]:
        ctx.log("matching")
        return {"invert": args.invert}

    code, out, err = run(app, ["grep", "-v"])
    assert code == 0 and json.loads(out)["data"] == {"invert": True} and err == ""
    code, out, err = run(app, ["grep", "--verbose"])
    assert json.loads(out)["data"] == {"invert": False} and levels(err) == {"info"}
    # Only that command: -v is still --verbose on the others
    _, _, err = run(app, ["chat", "-v"])
    assert "progress" in levels(err)


class _Origin(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the http.server API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/items"
    finally:
        server.shutdown()
        server.server_close()


def debug_lines(err: str) -> list[dict[str, Any]]:
    return [r for r in map(json.loads, err.splitlines()) if r["level"] == "debug"]


def test_debug_produces_full_diagnostic_trace_including_http_requests_and_config(
    origin: str, tmp_path: Path
) -> None:
    code, _, err = run(
        make_app(),
        ["fetch", "--url", origin, "--debug", "--format", "json"],
        data_env(tmp_path),
    )
    assert code == 0
    trace = {r["message"]: r["fields"] for r in debug_lines(err)}
    assert trace["run"]["verbosity"] == "debug"
    assert trace["config resolved"]["read"] == []
    assert "searched" in trace["config resolved"]
    assert trace["command started"] == {"command": "fetch", "timeout_ms": 60000}
    request = trace["http request"]
    assert request["method"] == "GET" and request["url"] == origin and request["status"] == 200
    assert request["headers"]["Authorization"] == "[REDACTED]" and "sk-secret-1" not in err
    assert trace["audit entry written"]["path"] == str(log_file(tmp_path))


def test_debug_traces_a_request_that_never_got_an_answer(tmp_path: Path) -> None:
    """A refused connection has no status: the trace names the failure instead"""
    url = "http://127.0.0.1:9/items"
    code, _, err = run(make_app(), ["fetch", "--url", url, "--debug"], data_env(tmp_path))
    assert code == 12
    request = next(r["fields"] for r in debug_lines(err) if r["message"] == "http request")
    assert request["url"] == url and request["error"] == "CONNECTION_FAILED"
    assert "status" not in request and request["headers"]["Authorization"] == "[REDACTED]"


def test_debug_traces_each_child_process() -> None:
    _, _, err = run(make_app(), ["child", "--debug", "--format", "json"])
    exited = next(r["fields"] for r in debug_lines(err) if r["message"] == "child exited")
    assert exited["argv"][1:] == ["-c", "print('hi')"] and exited["returncode"] == 0


def test_debug_routes_library_log_records_and_restores_the_root_logger() -> None:
    before = logging.getLogger().level
    _, _, err = run(make_app(), ["lib", "--debug", "--format", "plain"])
    assert "debug: pool size 4 logger=somelib" in err.splitlines()
    root = logging.getLogger()
    assert root.level == before
    assert not records_left_on_root()
    _, _, err = run(make_app(), ["lib", "--verbose"])
    assert "pool size" not in err


# Library log records by level (#31)

LIB = logging.getLogger("somelib")
TOKEN = "sk-live-abcdef123456"


def library_app() -> App:
    app = App("libctl", version="1.0.0")

    @app.command("levels", description="Log at each level", danger_level="safe", exit_codes=())
    def levels(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        LIB.debug("pool size %d", 4)
        LIB.info("retrying in %ds", 60)
        LIB.warning("slow response")
        LIB.error("gave up")
        return {"ok": True}

    @app.command("leak", description="Log a secret", danger_level="safe", exit_codes=())
    def leak(args: Login, ctx: Ctx) -> dict[str, bool]:
        LIB.warning("auth failed for %s", args.api_token)
        return {"ok": True}

    @app.command("nested", description="Call another app", danger_level="safe", exit_codes=())
    def nested(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        LIB.info("before")
        inner = library_app().call("levels", {}, env={})
        LIB.info("after")
        return {"ok": inner.ok}

    return app


def records_left_on_root() -> bool:
    """Whether the framework's handler stayed on the root logger with no run attached and
    no held handler thread alive to keep it there: one an earlier test abandoned, still
    sleeping, keeps it on the root until it ends (#118)"""
    on_root = any(h is _RECORDS for h in logging.getLogger().handlers)
    return on_root and not any(worker.is_alive() for worker, *_ in _RECORDS._workers)


def library_lines(err: str) -> list[tuple[str, str]]:
    """(level, message) of each stderr line the library's logger wrote"""
    records = map(json.loads, err.splitlines())
    return [(r["level"], r["message"]) for r in records if r["fields"].get("logger") == "somelib"]


@pytest.mark.parametrize(
    ("flags", "isatty", "levels"),
    [
        ([], False, ["warn", "error"]),
        ([], True, ["info", "warn", "error"]),
        (["--verbose"], False, ["info", "warn", "error"]),
        (["--debug"], False, ["debug", "info", "warn", "error"]),
        (["--quiet"], False, []),
    ],
)
def test_library_log_records_are_shown_by_their_own_level(
    flags: list[str], isatty: bool, levels: list[str]
) -> None:
    root = logging.getLogger()
    before = root.level
    env: dict[str, str] = {}
    code, _, err = run(library_app(), ["levels", *flags, "--format", "json"], env, isatty=isatty)
    assert code == 0
    every = {
        "debug": "pool size 4",
        "info": "retrying in 60s",
        "warn": "slow response",
        "error": "gave up",
    }
    assert library_lines(err) == [(level, every[level]) for level in levels]
    assert root.level == before
    assert not records_left_on_root()


def test_a_library_warning_reads_as_a_warn_line_in_plain_output() -> None:
    _, _, err = run(library_app(), ["levels", "--format", "plain"], {})
    assert err.splitlines() == [
        "warn: slow response logger=somelib",
        "error: gave up logger=somelib",
    ]


def test_a_secret_in_a_library_log_message_is_redacted() -> None:
    env = {"LIBCTL_API_TOKEN": TOKEN}
    code, _, err = run(library_app(), ["leak", "--format", "json"], env)
    assert code == 0 and TOKEN not in err
    assert library_lines(err) == [("warn", "auth failed for [REDACTED]")]


def test_app_call_and_a_nested_run_route_records_once_and_restore_the_root_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    before = root.level
    code, _, err = run(library_app(), ["nested", "--verbose", "--format", "json"], {})
    assert code == 0
    # The outer run shows its own records; the inner App.call's go to its stderr, once
    assert library_lines(err) == [("info", "before"), ("info", "after")]
    inner = capsys.readouterr().err
    assert library_lines(inner) == [("warn", "slow response"), ("error", "gave up")]
    assert root.level == before
    assert not records_left_on_root()


@pytest.mark.parametrize(("flags", "lines"), [([], 1), (["--quiet"], 0)])
def test_a_library_warning_never_falls_through_to_logging_last_resort(
    flags: list[str], lines: int
) -> None:
    """Without a handler on the root logger, a WARNING goes to ``logging.lastResort``,
    which writes it to ``sys.stderr`` raw, secret included, even under ``--quiet``. A
    subprocess, since pytest's own capture handlers sit on the root logger"""
    script = (
        "import logging\n"
        "from dataclasses import dataclass\n"
        "from treaty import App, Ctx, Flag\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class Login:\n"
        "    api_token: str = Flag(description='API token', secret=True)\n"
        "app = App('libctl', version='1.0.0')\n"
        "@app.command('leak', description='Leak', danger_level='safe', exit_codes=())\n"
        "def leak(args: Login, ctx: Ctx) -> dict[str, bool]:\n"
        "    logging.getLogger('somelib').warning('auth failed for %s', args.api_token)\n"
        "    return {'ok': True}\n"
        f"raise SystemExit(app.run(['leak', *{flags!r}]))\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "LIBCTL_API_TOKEN": TOKEN,
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert TOKEN not in proc.stderr
    assert len(proc.stderr.splitlines()) == lines, proc.stderr
    if lines:
        assert library_lines(proc.stderr) == [("warn", "auth failed for [REDACTED]")]


@pytest.mark.parametrize("host_handler", [False, True])
def test_a_handler_that_logs_after_its_app_call_returned_leaks_no_secret(
    host_handler: bool,
) -> None:
    """A handler abandoned at its timeout that logs after ``App.call`` returned: with no
    run attached, the record is written where ``logging.lastResort`` would write it,
    redacted of the handler's secrets, and not at all when the host's own handler takes
    it; the root logger is left as it was once the thread ends (#118). A subprocess,
    since pytest's own capture handlers sit on the root logger"""
    script = (
        "import logging, threading\n"
        "from dataclasses import dataclass\n"
        "from treaty import App, Ctx, Flag\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class Login:\n"
        "    api_token: str = Flag(description='API token', secret=True)\n"
        "taken = []\n"
        "class Host(logging.Handler):\n"
        "    def emit(self, record):\n"
        "        taken.append(record.name)\n"
        f"if {host_handler!r}:\n"
        "    logging.getLogger().addHandler(Host())\n"
        "before = list(logging.getLogger().handlers)\n"
        "go, logged, workers = threading.Event(), threading.Event(), []\n"
        "app = App('libctl', version='1.0.0')\n"
        "@app.command('slow', description='Log late', timeout=0.05, danger_level='safe',\n"
        "             exit_codes=())\n"
        "def slow(args: Login, ctx: Ctx) -> dict[str, bool]:\n"
        "    workers.append(threading.current_thread())\n"
        "    go.wait(timeout=10)  # released only after the call answered TIMEOUT\n"
        "    logging.getLogger('somelib').warning('auth failed for %s', args.api_token)\n"
        "    logged.set()\n"
        "    return {'ok': True}\n"
        f"env = {{'LIBCTL_API_TOKEN': {TOKEN!r}, 'LIBCTL_AUDIT_LOG': '0'}}\n"
        "print(app.call('slow', {}, env=env).error.code)\n"
        "go.set()\n"
        "assert logged.wait(timeout=10)\n"
        "workers[0].join(timeout=10)\n"
        "assert not workers[0].is_alive()\n"
        "print(logging.getLogger().handlers == before, taken)\n"
    )
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert TOKEN not in proc.stderr and TOKEN not in proc.stdout
    taken = ["somelib"] if host_handler else []
    assert proc.stdout.splitlines() == ["TIMEOUT", f"True {taken}"], proc.stdout
    written = [] if host_handler else ["auth failed for [REDACTED]"]
    assert proc.stderr.splitlines() == written, proc.stderr


LATE_PRINT_SCRIPT = """\
import io, json, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
go, written, finish = threading.Event(), threading.Event(), threading.Event()
workers = []
app = App('libctl', version='1.0.0')
@app.command('slow', description='Print late', timeout=0.05, danger_level='safe',
             exit_codes=())
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    workers.append(threading.current_thread())
    go.wait(timeout=10)  # released only after the call answered TIMEOUT
    print('out', args.api_token)
    sys.stdout.writelines(['lines ', args.api_token, '\\n'])
    sys.stderr.write('err ' + args.api_token + '\\n')
    print('esc', args.api_token[:5] + '\\x1b[0m' + args.api_token[5:], file=sys.stderr)
    written.set()
    finish.wait(timeout=10)
    return {{'ok': True}}
env = {{'LIBCTL_API_TOKEN': {token!r}, 'LIBCTL_AUDIT_LOG': '0'}}
before = (sys.stdout, sys.stderr)
if {entry!r} == 'call':
    print(app.call('slow', {{}}, env=env).error.code)
else:
    app.run(['slow', '--format', 'json'], env=env)
go.set()
assert written.wait(timeout=10)
print('host', {token!r})  # the host's own write, while the late thread lives
other = threading.Thread(target=lambda: sys.stderr.write('other {token}\\n'))
other.start()
other.join()
mine = io.StringIO()
if {host_swaps!r}:
    sys.stdout = mine
finish.set()
workers[0].join(timeout=10)
assert not workers[0].is_alive()
kept = sys.stdout is mine if {host_swaps!r} else sys.stdout is before[0]
sys.stdout = before[0]
print(kept, sys.stderr is before[1])
"""


@pytest.mark.parametrize(("entry", "host_swaps"), [("call", False), ("call", True), ("run", False)])
def test_a_handler_that_prints_after_its_run_returned_leaks_no_secret(
    entry: str, host_swaps: bool
) -> None:
    """A handler abandoned at its timeout that prints and writes to stderr after its run
    returned, with no run attached: what it writes is redacted, and its stdout text goes
    to stderr, never into the host's stdout. The host's and other threads' writes pass
    through untouched, and the standard streams are restored once the thread ends, unless
    the host replaced one meanwhile, which it keeps (#135). A subprocess, since pytest
    captures the standard streams"""
    script = LATE_PRINT_SCRIPT.format(token=TOKEN, entry=entry, host_swaps=host_swaps)
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    first, *rest = proc.stdout.splitlines()
    code = first if entry == "call" else json.loads(first)["error"]["code"]
    assert code == "TIMEOUT", proc.stdout
    assert rest == [f"host {TOKEN}", "True True"], proc.stdout
    late = [line for line in proc.stderr.splitlines() if not line.startswith("other ")]
    assert TOKEN not in "".join(late), proc.stderr
    assert late == [
        "out [REDACTED]",
        "lines [REDACTED]",
        "err [REDACTED]",
        "esc [REDACTED]",
    ], proc.stderr
    assert f"other {TOKEN}" in proc.stderr.splitlines(), proc.stderr


SWAPPED_CALL_SCRIPT = """\
import io, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
finish = threading.Event()
workers = []
app = App('libctl', version='1.0.0')
@app.command('slow', description='Outlive its timeout', timeout=0.05, danger_level='safe',
             exit_codes=())
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    workers.append(threading.current_thread())
    finish.wait(timeout=10)
    return {{'ok': True}}
@app.command('leak', description='Print the token', danger_level='safe', exit_codes=())
def leak(args: Login, ctx: Ctx) -> dict[str, bool]:
    print('out', args.api_token)
    sys.stderr.write('err ' + args.api_token + '\\n')
    return {{'ok': True}}
env = {{'LIBCTL_API_TOKEN': {token!r}, 'LIBCTL_AUDIT_LOG': '0'}}
before = (sys.stdout, sys.stderr)
print(app.call('slow', {{}}, env=env).error.code)
out, err = io.StringIO(), io.StringIO()
sys.stdout, sys.stderr = out, err  # a capture, put in place while the handler lives
print(app.call('leak', {{}}, env=env).ok, file=before[0])
finish.set()
workers[0].join(timeout=10)
sys.stdout, sys.stderr = before
print(repr(out.getvalue()), repr(err.getvalue()))
"""


def test_a_call_after_a_timed_out_one_redacts_a_stream_the_host_put_in_place() -> None:
    """While a handler abandoned at its timeout lives, the host replaces ``sys.stdout`` and
    ``sys.stderr``, as a capture does: the next call's prints into them are redacted, not
    passed through because a wrapper from the first call still stands (#300). A
    subprocess, since pytest captures the standard streams"""
    script = SWAPPED_CALL_SCRIPT.format(token=TOKEN)
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "TIMEOUT",
        "True",
        "'out [REDACTED]\\n' 'err [REDACTED]\\n'",
    ], proc.stdout


LATE_MAIN_SCRIPT = """\
import atexit, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
go, written = threading.Event(), threading.Event()
app = App('libctl', version='1.0.0')
@app.command('slow', description='Print late', timeout=0.05, danger_level='safe',
             exit_codes=())
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    go.wait(timeout=10)  # released only once App.main restored the streams
    print('out', args.api_token)
    sys.stderr.write('err ' + args.api_token + '\\n')
    written.set()
    return {{'ok': True}}
def late() -> None:
    go.set()
    assert written.wait(timeout=10)
    print('host')
atexit.register(late)
app.main()
"""


def test_a_handler_that_prints_after_app_main_returned_leaks_no_secret() -> None:
    """A handler abandoned at its timeout that prints after ``App.main`` put its own
    ``sys.stdout`` back, as at the program's exit: its text is redacted on stderr, never on
    stdout, which carries the envelope and the host's own write alone (#135)"""
    env = {
        "PATH": os.environ["PATH"],
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "LIBCTL_API_TOKEN": TOKEN,
        "LIBCTL_AUDIT_LOG": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-c", LATE_MAIN_SCRIPT.format(), "slow", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    first, *rest = proc.stdout.splitlines()
    assert json.loads(first)["error"]["code"] == "TIMEOUT", proc.stdout
    assert rest == ["host"], proc.stdout
    assert TOKEN not in proc.stderr, proc.stderr
    assert ["out [REDACTED]", "err [REDACTED]"] == [
        line for line in proc.stderr.splitlines() if line.startswith(("out ", "err "))
    ], proc.stderr


def run_script(script: str) -> subprocess.CompletedProcess[str]:
    """``script`` in a fresh interpreter, whose standard streams pytest does not capture"""
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


CALL_PRINT_SCRIPT = """\
import io, sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
app = App('libctl', version='1.0.0')
before = (sys.stdout, sys.stderr)
mine = io.StringIO()
def other(token: str) -> None:
    print('other', token)  # a thread no call runs: raw, in its place
    if {host_swaps!r}:
        sys.stdout = mine
@app.command('show', description='Print', timeout={timeout!r}, danger_level='safe',
             exit_codes=())
def show(args: Login, ctx: Ctx) -> dict[str, bool]:
    print('out', args.api_token)
    thread = threading.Thread(target=other, args=(args.api_token,))
    thread.start()
    thread.join()
    sys.stdout.writelines(['lines ', args.api_token, '\\n'])
    sys.stderr.write('err ' + args.api_token + '\\n')
    print('esc', args.api_token[:5] + '\\x1b[0m' + args.api_token[5:], file=sys.stderr)
    return {{'ok': True}}
env = {{'LIBCTL_API_TOKEN': {token!r}, 'LIBCTL_AUDIT_LOG': '0'}}
envelope = app.call('show', {{}}, env=env)
kept = sys.stdout is mine if {host_swaps!r} else sys.stdout is before[0]
sys.stdout = before[0]
print(envelope.ok, envelope.warnings, kept, sys.stderr is before[1])
"""


@pytest.mark.parametrize(
    ("timeout", "host_swaps"), [(None, False), (30.0, False), (None, True), (30.0, True)]
)
def test_a_handler_that_prints_during_app_call_leaks_no_secret(
    timeout: float | None, host_swaps: bool
) -> None:
    """A handler that prints and writes to stderr during ``App.call``, on the calling
    thread or, under a timeout, on a worker: its text is redacted on the stream it was
    written to, a secret an escape splits too, and the envelope carries no warning. A
    thread no call runs writes raw and in order; the streams are restored once the call
    returns, unless the host replaced one meanwhile, which it keeps (#141). A subprocess,
    since pytest captures the standard streams"""
    proc = run_script(CALL_PRINT_SCRIPT.format(token=TOKEN, timeout=timeout, host_swaps=host_swaps))
    assert proc.returncode == 0, proc.stderr
    printed = ["out [REDACTED]", f"other {TOKEN}"]
    if not host_swaps:
        printed.append("lines [REDACTED]")
    assert proc.stdout.splitlines() == [*printed, "True () True True"], proc.stdout
    assert proc.stderr.splitlines() == ["err [REDACTED]", "esc [REDACTED]"], proc.stderr


CALL_TIMEOUT_SCRIPT = """\
import sys, threading, time
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
app = App('libctl', version='1.0.0')
before = (sys.stdout, sys.stderr)
returned, workers = threading.Event(), []
@app.command('slow', description='Print on', timeout=0.05, danger_level='safe',
             exit_codes=())
def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
    workers.append(threading.current_thread())
    while not returned.is_set():
        sys.stdout.write('tick ' + args.api_token + '\\n')
        time.sleep(0.001)
    sys.stdout.write('tick ' + args.api_token + '\\n')
    return {{'ok': True}}
env = {{'LIBCTL_API_TOKEN': {token!r}, 'LIBCTL_AUDIT_LOG': '0'}}
code = app.call('slow', {{}}, env=env).error.code
returned.set()
workers[0].join(timeout=10)
assert not workers[0].is_alive()
print(code, sys.stdout is before[0], sys.stderr is before[1])
"""


def test_a_handler_printing_through_its_timeout_leaks_no_secret() -> None:
    """A handler that prints on and on through its timeout, while ``App.call`` answers
    ``TIMEOUT``, then after it returned: every line is redacted, on stdout while its run is
    attached, the window between the timeout and the run's detach too, and on stderr once
    the run detached, with no raw line at the handover (#141, #135)"""
    proc = run_script(CALL_TIMEOUT_SCRIPT.format(token=TOKEN))
    assert proc.returncode == 0, proc.stderr
    *ticks, last = proc.stdout.splitlines()
    assert last == "TIMEOUT True True", proc.stdout
    assert ticks and set(ticks) == {"tick [REDACTED]"}, proc.stdout
    late = proc.stderr.splitlines()
    assert late and set(late) == {"tick [REDACTED]"}, proc.stderr


CONCURRENT_CALLS_SCRIPT = """\
import sys, threading
from dataclasses import dataclass
from treaty import App, Ctx, Flag
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
app = App('libctl', version='1.0.0')
before = (sys.stdout, sys.stderr)
both, printed = threading.Barrier(3, timeout=10), threading.Barrier(3, timeout=10)
tokens = {{'a': {token!r} + 'a', 'b': {token!r} + 'b'}}
@app.command('show', description='Print', timeout={timeout!r}, danger_level='safe',
             exit_codes=())
def show(args: Login, ctx: Ctx) -> dict[str, bool]:
    both.wait()  # both calls attached
    name = 'a' if args.api_token == tokens['a'] else 'b'
    other = tokens['b' if name == 'a' else 'a']
    sys.stdout.write(f'{{name}} {{args.api_token}} {{other}}\\n')
    printed.wait()
    return {{'ok': True}}
def call(name: str) -> None:
    env = {{'LIBCTL_API_TOKEN': tokens[name], 'LIBCTL_AUDIT_LOG': '0'}}
    assert app.call('show', {{}}, env=env).ok
callers = [threading.Thread(target=call, args=(n,)) for n in 'ab']
for caller in callers:
    caller.start()
both.wait()
sys.stdout.write('host ' + tokens['a'] + '\\n')  # the host, while both calls run
printed.wait()
for caller in callers:
    caller.join(timeout=10)
print(sys.stdout is before[0], sys.stderr is before[1])
"""


@pytest.mark.parametrize("timeout", [None, 30.0])
def test_concurrent_app_calls_redact_each_others_secrets_in_place(timeout: float | None) -> None:
    """Two ``App.call``s on two threads at once: each handler's line is redacted of both
    calls' secrets on stdout, the host's own write in between stays raw, and the streams
    are restored once the last call returns (#141)"""
    proc = run_script(CONCURRENT_CALLS_SCRIPT.format(token=TOKEN, timeout=timeout))
    assert proc.returncode == 0, proc.stderr
    *lines, last = proc.stdout.splitlines()
    assert last == "True True", proc.stdout
    assert sorted(lines) == [
        "a [REDACTED] [REDACTED]",
        "b [REDACTED] [REDACTED]",
        f"host {TOKEN}a",
    ], proc.stdout
    assert proc.stderr == "", proc.stderr


NESTED_CALL_SCRIPT = """\
import json, sys
from dataclasses import dataclass
from treaty import App, Ctx, Flag, NoArgs
@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description='API token', secret=True)
app = App('libctl', version='1.0.0')
before = (sys.stdout, sys.stderr)
seen = []
@app.command('inner', description='Print', danger_level='safe', exit_codes=())
def inner(args: Login, ctx: Ctx) -> dict[str, bool]:
    seen.append(type(sys.stdout).__name__)
    print('out', args.api_token)
    sys.stderr.write('err ' + args.api_token + '\\n')
    return {{'ok': True}}
@app.command('outer', description='Call', timeout={timeout!r}, danger_level='safe',
             exit_codes=())
def outer(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    env = {{'LIBCTL_API_TOKEN': {token!r}, 'LIBCTL_AUDIT_LOG': '0'}}
    return {{'ok': app.call('inner', {{}}, env=env).ok}}
code = app.run(['outer', '--format', 'json'], env={{'LIBCTL_AUDIT_LOG': '0'}})
print(code, seen, sys.stdout is before[0], sys.stderr is before[1])
"""


@pytest.mark.parametrize("timeout", [None, 30.0])
def test_an_app_call_in_a_run_handler_leaves_stdout_to_the_run(timeout: float | None) -> None:
    """An ``App.call`` inside an ``app.run`` handler: ``sys.stdout`` stays the run's
    stand-in, which takes the inner handler's print as ever, redacted and reported as
    ``THIRD_PARTY_STDOUT``; its stderr write is redacted in place, and both streams are
    restored once the run returns (#141)"""
    proc = run_script(NESTED_CALL_SCRIPT.format(token=TOKEN, timeout=timeout))
    assert proc.returncode == 0, proc.stderr
    first, last = proc.stdout.splitlines()
    envelope = json.loads(first)
    assert envelope["data"] == {"ok": True}, first
    assert [w["code"] for w in envelope["warnings"]] == ["THIRD_PARTY_STDOUT"], first
    assert last == "0 ['_StrayStdout'] True True", proc.stdout
    assert TOKEN not in proc.stdout and TOKEN not in proc.stderr, proc.stderr
    assert "err [REDACTED]" in proc.stderr.splitlines(), proc.stderr


class _PausingThread(threading.Thread):
    """A thread that, once armed, pauses in its own first hash, the one a stream write
    takes to look itself up, until released"""

    def __init__(self, target: Callable[[], None]) -> None:
        super().__init__(target=target, daemon=True)
        self.armed = threading.Event()
        self.paused = threading.Event()
        self.release = threading.Event()

    def __hash__(self) -> int:
        if threading.current_thread() is self and self.armed.is_set():
            self.armed.clear()
            self.paused.set()
            assert self.release.wait(10)
        return id(self) >> 4


def test_a_write_racing_its_calls_detach_stays_redacted() -> None:
    """A handler thread writing as its ``App.call`` detaches leaves the call threads and
    joins the late ones in one step: a write that looked itself up in the late threads
    before the detach and in the call threads after it found itself in neither, and
    passed through raw, as on free-threaded CPython (#141)"""
    inner = io.StringIO()
    stream = _LateStream(inner, stdout=False)

    def write(_: logging.LogRecord) -> None:
        return None

    def redact(text: str) -> str:
        return text.replace(TOKEN, "[REDACTED]")

    go = threading.Event()

    def handler() -> None:
        assert go.wait(10)
        stream.write(TOKEN + "\n")

    worker = _PausingThread(handler)
    _RECORDS.attach(write, redact, None, threading.current_thread())
    try:
        worker.start()
        _RECORDS.hold(worker, redact, write)
        worker.armed.set()
        go.set()
        assert worker.paused.wait(10)
    finally:
        _RECORDS.detach(write)
    worker.release.set()
    worker.join(10)
    _RECORDS.forget(worker)
    assert inner.getvalue() == "[REDACTED]\n"


@pytest.mark.parametrize("before_first_event", [True, False])
def test_a_stream_abandoned_at_its_timeout_closes_on_its_worker_and_leaks_no_secret(
    before_first_event: bool,
) -> None:
    """A stream that times out waiting for an event: its generator is closed on the
    worker as ``next()`` returns, while the worker is still held, so a secret logged in
    its ``finally`` is redacted, rather than closed at garbage collection once the worker
    was forgotten and ``_Records`` had left the root, which wrote it raw through
    ``logging.lastResort`` (#128). A subprocess, since pytest's own capture handlers sit
    on the root logger.

    The 0.05 s timeout can run out before the worker reaches the wait it is meant to cut
    short, as on a loaded free-threaded runner: before the generator's body began, when
    it never started and has no ``finally`` to run, or before its first event. The call
    is repeated, once every handler worker ended, until the timeout caught the generator
    in that wait, rather than trusting 0.05 s to be enough (#308)"""
    script = (
        "import logging, threading\n"
        "from collections.abc import Iterator\n"
        "from dataclasses import dataclass\n"
        "from treaty import App, Ctx, Flag\n"
        "from treaty._app import _RECORDS\n"
        "@dataclass(frozen=True, slots=True)\n"
        "class Login:\n"
        "    api_token: str = Flag(description='API token', secret=True)\n"
        "go, closed, waiting = threading.Event(), threading.Event(), threading.Event()\n"
        "workers, closers = [], []\n"
        "app = App('libctl', version='1.0.0')\n"
        "@app.command('tail', description='Stream', timeout=0.05, danger_level='safe',\n"
        "             exit_codes=(), streaming=True)\n"
        "def tail(args: Login, ctx: Ctx) -> Iterator[dict[str, int]]:\n"
        "    try:\n"
        f"        if not {before_first_event!r}:\n"
        "            yield {'n': 0}\n"
        "        workers.append(threading.current_thread())\n"
        "        waiting.set()\n"
        "        go.wait(timeout=10)  # released only after the call answered TIMEOUT\n"
        "        yield {'n': 1}\n"
        "        yield {'n': 2}\n"
        "    finally:\n"
        "        closers.append(threading.current_thread())\n"
        "        logging.getLogger('somelib').warning('closing for %s', args.api_token)\n"
        "        closed.set()\n"
        f"env = {{'LIBCTL_API_TOKEN': {TOKEN!r}, 'LIBCTL_AUDIT_LOG': '0'}}\n"
        "for _ in range(100):\n"
        "    for event in (go, closed, waiting):\n"
        "        event.clear()\n"
        "    workers.clear()\n"
        "    closers.clear()\n"
        "    code = app.call('tail', {}, env=env).error.code\n"
        "    go.set()\n"
        "    for thread in threading.enumerate():\n"
        "        if thread.name == 'treaty-handler':\n"
        "            thread.join(timeout=10)\n"
        "    if waiting.is_set():\n"
        "        break\n"
        "print(code)\n"
        "assert waiting.is_set(), 'the timeout ran out before the wait on every call'\n"
        "assert closed.wait(timeout=10)\n"
        "workers[-1].join(timeout=10)\n"
        "assert not workers[-1].is_alive()\n"
        "on_root = any(h is _RECORDS for h in logging.getLogger().handlers)\n"
        "print(closers == workers[-1:], on_root)\n"
    )
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert TOKEN not in proc.stderr and TOKEN not in proc.stdout
    assert proc.stdout.splitlines() == ["TIMEOUT", "True False"], proc.stdout
    # A repeated call's generator, closed before its first event, logged redacted too
    lines = proc.stderr.splitlines()
    assert lines[-1] == "closing for [REDACTED]", proc.stderr
    assert all("closing for [REDACTED]" in line for line in lines), proc.stderr


def test_a_handler_that_ends_at_once_under_a_timeout_leaves_the_root_logger() -> None:
    """A handler on a worker thread that returns before the caller registered it: its
    ``forget`` must not come before its ``hold``, which would keep ``_Records`` on the
    root logger, and ``logging.basicConfig()`` a no-op, with no run attached (#128)"""
    app = App("libctl", version="1.0.0")

    @app.command("now", description="Return", timeout=30, danger_level="safe", exit_codes=())
    def now(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    for _ in range(50):
        assert app.call("now", {}, env={"LIBCTL_AUDIT_LOG": "0"}).ok
        assert not records_left_on_root()


def test_a_record_written_as_last_resort_never_takes_the_logging_module_lock() -> None:
    """A record written where ``logging.lastResort`` would, with a held thread ended
    unforgotten, as when its ``hold`` came after its ``forget``: emitting holds the
    handler's lock, and leaving the root logger there would take logging's module lock,
    the reverse of ``logging.config.dictConfig``'s order, which deadlocks it. A
    subprocess, since pytest's own capture handlers sit on the root logger"""
    script = (
        "import logging, threading\n"
        "from treaty._app import _RECORDS\n"
        "def write(record):\n"
        "    pass\n"
        "release = threading.Event()\n"
        "worker = threading.Thread(target=release.wait)\n"
        "worker.start()\n"
        "_RECORDS.attach(write, lambda text: text, None)\n"
        "_RECORDS.hold(worker, lambda text: text, write)\n"
        "_RECORDS.detach(write)\n"
        "release.set()\n"
        "worker.join()\n"
        "late = logging.getLogger('somelib')\n"
        "late.isEnabledFor(logging.WARNING)  # its level cached, which takes the lock\n"
        "with logging._lock:  # dictConfig's order: the module lock, then each handler's\n"
        "    emitting = threading.Thread(target=late.warning, args=('late',), daemon=True)\n"
        "    emitting.start()\n"
        "    emitting.join(timeout=10)\n"
        "    print(emitting.is_alive())\n"
    )
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["False"], proc.stdout
    assert proc.stderr.splitlines() == ["late"], proc.stderr


def test_concurrent_calls_redact_each_others_secrets_in_library_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Records go to the innermost attached run, which may be another thread's
    ``App.call``, as with the MCP adapter's concurrent tool calls: every attached run's
    secrets are redacted from them, not only the receiving run's"""
    both = threading.Barrier(2)
    app = App("libctl", version="1.0.0")

    @app.command("leak", description="Log a secret", danger_level="safe", exit_codes=())
    def leak(args: Login, ctx: Ctx) -> dict[str, bool]:
        both.wait(timeout=10)
        LIB.warning("auth failed for %s", args.api_token)
        both.wait(timeout=10)
        return {"ok": True}

    tokens = ["sk-live-first0000000001", "sk-live-second000000002"]
    oks: list[bool] = []

    def call(token: str) -> None:
        env = {"LIBCTL_API_TOKEN": token}
        oks.append(app.call("leak", {}, env=env).ok)

    threads = [threading.Thread(target=call, args=(t,)) for t in tokens]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert oks == [True, True]
    err = capsys.readouterr().err
    assert not [t for t in tokens if t in err], err
    assert library_lines(err) == [("warn", "auth failed for [REDACTED]")] * 2


def test_concurrent_runs_redact_each_others_secrets_in_printed_text() -> None:
    """While runs overlap on threads, ``sys.stdout`` is the last run's, so a handler's
    ``print()`` reaches another run's stderr and ``THIRD_PARTY_STDOUT`` warning: every
    attached run's secrets are redacted from it, not only the receiving run's (#92)"""
    runs = 4
    together = threading.Barrier(runs)
    app = App("libctl", version="1.0.0")

    @app.command("leak", description="Print a secret", danger_level="safe", exit_codes=())
    def leak(args: Login, ctx: Ctx) -> dict[str, bool]:
        together.wait(timeout=10)  # every run has swapped sys.stdout before any prints
        print(f"token is {args.api_token}")
        together.wait(timeout=10)  # and none has restored it yet
        return {"ok": True}

    tokens = [f"sk-live-{n:020d}" for n in range(runs)]
    results: dict[str, tuple[int, str, str]] = {}

    def one(token: str) -> None:
        env = {"LIBCTL_API_TOKEN": token, "LIBCTL_AUDIT_LOG": "0"}
        results[token] = run(app, ["leak", "--verbose", "--format", "json"], env)

    threads = [threading.Thread(target=one, args=(t,)) for t in tokens]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(results) == tokens
    printed = 0
    for code, out, err in results.values():
        assert code == 0, err
        assert not [t for t in tokens if t in out or t in err], (out, err)
        printed += err.count("token is [REDACTED]")
    assert printed == runs  # every print reached some run's stderr, redacted


@pytest.mark.parametrize("verbosity", ["--verbose", "--debug"])
def test_a_timed_out_handler_that_prints_later_leaks_no_secret_into_the_next_run(
    verbosity: str,
) -> None:
    """A handler that outlives its timeout keeps running on its worker thread after its
    run returned: what it prints then reaches the stderr and ``THIRD_PARTY_STDOUT``
    warning of whichever run holds ``sys.stdout``, still redacted of its own run's
    secrets while the thread lives, and those are released once it ends (#104)"""
    go, printed, holding = threading.Event(), threading.Event(), threading.Event()
    workers: list[threading.Thread] = []
    app = App("libctl", version="1.0.0")

    @app.command(
        "slow", description="Print a secret late", timeout=0.05, danger_level="safe", exit_codes=()
    )
    def slow(args: Login, ctx: Ctx) -> dict[str, bool]:
        workers.append(threading.current_thread())
        go.wait(timeout=10)  # released only after this run answered TIMEOUT
        print(f"token is {args.api_token}")
        printed.set()
        return {"ok": True}

    @app.command("hold", description="Hold sys.stdout", danger_level="safe", exit_codes=())
    def hold(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        holding.set()
        printed.wait(timeout=10)
        return {"ok": True}

    token = "sk-live-abandoned0000000001"
    code, out, _ = run(
        app,
        ["slow", "--format", "json"],
        {"LIBCTL_API_TOKEN": token, "LIBCTL_AUDIT_LOG": "0"},
    )
    assert envelope_of(out)["error"]["code"] == "TIMEOUT", out
    later: list[tuple[int, str, str]] = []
    second = threading.Thread(
        target=lambda: later.append(
            run(app, ["hold", verbosity, "--format", "json"], {"LIBCTL_AUDIT_LOG": "0"})
        )
    )
    second.start()
    assert holding.wait(timeout=10)
    go.set()
    second.join(timeout=30)
    assert printed.is_set()
    [(code, out, err)] = later
    assert code == 0, err
    assert token not in out and token not in err, (out, err)
    [warning] = envelope_of(out)["warnings"]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    assert warning["context"]["text"] == "token is [REDACTED]"
    [worker] = workers
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert _RECORDS.redact(token) == token  # released with the thread that printed it


# REQ-F-060: --debug attributes stray stdout


def test_in_debug_mode_intercepted_stdout_is_emitted_to_stderr_with_source_attribution() -> None:
    code, out, err = run(make_app(), ["chatty", "--debug", "--format", "json"])
    write = next(r["fields"] for r in debug_lines(err) if r["message"] == "stdout write")
    assert write["text"] == "initialized"
    assert write["source"].startswith(f"{__file__}:")
    assert envelope_of(out)["warnings"][0]["code"] == "THIRD_PARTY_STDOUT"


# REQ-O-025: --warnings-as-errors


def test_a_command_that_emits_no_warnings_exits_0_even_with_warnings_as_errors() -> None:
    code, out, _ = run(make_app(), ["warn", "--warnings-as-errors"])
    assert code == 0 and json.loads(out)["ok"] is True


def test_a_command_that_emits_one_warning_exits_1_when_warnings_as_errors_is_passed() -> None:
    code, out, _ = run(make_app(), ["warn", "--n", "1", "--warnings-as-errors"])
    envelope = json.loads(out)
    assert code == 1 and envelope["meta"]["exit_code"] == 1
    assert envelope["error"]["code"] == "WARNINGS_AS_ERRORS"
    assert envelope["error"]["context"] == {"count": 1, "codes": ["SOMETHING_ODD"]}
    assert envelope["data"] == {"n": 1}  # 11-D3: what already happened stays


def test_the_warnings_array_contains_the_warning_that_triggered_the_exit() -> None:
    _, out, _ = run(make_app(), ["--warnings-as-errors", "warn", "--n", "1"])
    assert [w["code"] for w in json.loads(out)["warnings"]] == ["SOMETHING_ODD"]


def test_without_warnings_as_errors_warnings_do_not_affect_the_exit_code() -> None:
    code, out, _ = run(make_app(), ["warn", "--n", "2"])
    assert code == 0 and len(json.loads(out)["warnings"]) == 2


def test_framework_warnings_count_toward_warnings_as_errors() -> None:
    code, out, _ = run(make_app(), ["chatty", "--warnings-as-errors"])
    assert code == 1 and json.loads(out)["error"]["context"]["codes"] == ["THIRD_PARTY_STDOUT"]


def test_warnings_as_errors_applies_to_a_stream_terminal_and_to_exec_lines() -> None:
    code, out, _ = run(make_app(), ["tail", "--n", "1", "--warnings-as-errors"])
    lines = [json.loads(line) for line in out.splitlines()]
    # Two item lines, then the error envelope in place of the summary line
    assert code == 1 and [line.get("ok") for line in lines] == [None, None, False]
    assert lines[-1]["error"]["code"] == "WARNINGS_AS_ERRORS"
    plan = '{"_cmd":"warn","n":1}\n{"_cmd":"warn","n":0}\n'
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(
        ["exec", "--warnings-as-errors", "--ignore-errors"],
        stdin=io.StringIO(plan),
        stdout=out,
        stderr=err,
        env={},
    )
    assert code == 1 and [json.loads(line)["ok"] for line in out.getvalue().splitlines()] == [
        False,
        True,
    ]


# #152: a text format's stdout has no room for warnings, so they go to stderr


def run_merged(app: App, argv: list[str]) -> tuple[int, str]:
    """stdout and stderr in one buffer, so the order they were written in shows"""
    both = io.StringIO()
    code = app.run(argv, stdout=both, stderr=both, env={})
    return code, both.getvalue()


@pytest.mark.parametrize(
    ("mode", "text"),
    [("plain", "n: 2\n"), ("tsv", "n\n2\n"), ("csv", "n,2\n"), ("markdown", "**n** 2\n")],
)
def test_a_text_format_writes_each_warning_to_stderr_after_the_result(mode: str, text: str) -> None:
    app = make_app()
    app.format(Format.CSV, render=lambda data: f"n,{data['n']}\n")
    app.format(Format.MARKDOWN, render=lambda data: f"**n** {data['n']}\n")
    code, out, err = run(app, ["warn", "--n", "2", "--format", mode])
    assert (code, out) == (0, text)
    assert err == "warning: SOMETHING_ODD: odd 0\nwarning: SOMETHING_ODD: odd 1\n"
    _, both = run_merged(app, ["warn", "--n", "2", "--format", mode])
    assert both == text + err


def test_a_streams_warning_is_written_once_after_the_event_it_came_with() -> None:
    code, both = run_merged(make_app(), ["tail", "--n", "1", "--format", "plain"])
    assert code == 0
    assert both == "i: 0\n\ni: 1\n\nwarning: SOMETHING_ODD: odd\n"


def test_warnings_as_errors_in_text_lists_the_warning_before_the_error() -> None:
    code, out, err = run(
        make_app(), ["warn", "--n", "1", "--format", "plain", "--warnings-as-errors"]
    )
    assert code == 1 and out == "n: 1\n"
    first, second = err.splitlines()[:2]
    assert first == "warning: SOMETHING_ODD: odd 0"
    assert second.startswith(f"{BASE}: WARNINGS_AS_ERRORS: ")


def test_quiet_keeps_text_warnings_off_stderr() -> None:
    _, out, err = run(make_app(), ["warn", "--n", "1", "--format", "plain", "--quiet"])
    assert (out, err) == ("n: 1\n", "")


def test_a_text_warning_is_redacted_and_loses_its_escapes() -> None:
    app = App(BASE, version="1.0.0")

    @app.command("sign", description="Sign", danger_level="safe", exit_codes=())
    def sign(args: Login, ctx: Ctx) -> dict[str, bool]:
        ctx.warn("ODD_TOKEN", f"\x1b]0;title\x07token {args.api_token} looks \x1b[31mold")
        return {"ok": True}

    argv = ["sign", "--api-token-from-env", "TOK", "--format", "plain"]
    _, out, err = run(app, argv, {"TOK": "s3cr3t-value"})
    assert out == "ok: true\n"
    assert err == "warning: ODD_TOKEN: token [REDACTED] looks old\n"


def test_a_deprecated_flag_warns_once_in_text() -> None:
    app = App(BASE, version="1.1.0")

    @dataclass(frozen=True, slots=True)
    class Named:
        name: str = Flag(default="x", description="Name")
        label: str | None = Flag(
            default=None,
            description="Old spelling of --name",
            deprecated=Deprecated("1.1.0", replacement="name"),
        )

    @app.command("greet", description="Greet", danger_level="safe", exit_codes=())
    def greet(args: Named, ctx: Ctx) -> dict[str, str]:
        return {"name": args.label or args.name}

    code, out, err = run(app, ["greet", "--label", "y", "--format", "plain"])
    assert (code, out) == (0, "name: y\n")
    assert [json.loads(line)["code"] for line in err.splitlines()] == ["DEPRECATED_FLAG"]


# REQ-O-030: the audit log is off until the app or the operator turns it on


def test_the_audit_log_is_off_by_default_and_creates_nothing(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "state"), "HOME": str(tmp_path / "home")}
    app = make_app()
    _, out, _ = run(app, ["warn"], env)
    assert "audit_log_path" not in json.loads(out)["meta"]
    assert not (tmp_path / "state").exists() and not (tmp_path / "home").exists()
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert all("filesystem_side_effects" not in c for c in commands.values())


def test_audit_log_1_appends_one_entry_to_the_default_path(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["warn"])
    assert envelope["meta"]["audit_log_path"] == str(log_file(tmp_path))
    assert [e["command"] for e in entries(tmp_path)] == ["warn"]


def test_the_default_path_is_the_xdg_state_home_else_local_state(tmp_path: Path) -> None:
    home = {"HOME": str(tmp_path / "home"), "LOGCTL_AUDIT_LOG": "1"}
    _, out, _ = run(make_app(), ["warn"], home)
    expected = tmp_path / "home" / ".local" / "state" / BASE / "audit.jsonl"
    assert json.loads(out)["meta"]["audit_log_path"] == str(expected) and expected.exists()
    relative = {**home, "XDG_STATE_HOME": "state"}  # ignored, as the XDG spec says
    _, out, _ = run(make_app(), ["warn"], relative)
    assert json.loads(out)["meta"]["audit_log_path"] == str(expected)


def test_audit_log_0_turns_off_a_log_the_app_turned_on(tmp_path: Path) -> None:
    app = make_app(audit_log=AuditLog(path=tmp_path / "log" / "audit.jsonl"))
    _, out, _ = run(app, ["warn"])
    assert json.loads(out)["meta"]["audit_log_path"] == str(tmp_path / "log" / "audit.jsonl")
    (tmp_path / "log" / "audit.jsonl").unlink()
    _, out, _ = run(app, ["warn"], {"LOGCTL_AUDIT_LOG": "0"})
    assert "audit_log_path" not in json.loads(out)["meta"]
    assert not (tmp_path / "log" / "audit.jsonl").exists()


def test_the_app_turns_it_on_at_the_default_path(tmp_path: Path) -> None:
    app = make_app(audit_log=AuditLog())
    _, out, _ = run(app, ["warn"], {"XDG_STATE_HOME": str(tmp_path / "state")})
    assert json.loads(out)["meta"]["audit_log_path"] == str(log_file(tmp_path))
    assert log_file(tmp_path).exists()


def test_the_operators_path_wins_over_the_apps_and_1_keeps_the_apps(tmp_path: Path) -> None:
    mine = tmp_path / "app" / "audit.jsonl"
    app = make_app(audit_log=AuditLog(path=mine))
    _, out, _ = run(app, ["warn"], {"LOGCTL_AUDIT_LOG": "1"})
    assert json.loads(out)["meta"]["audit_log_path"] == str(mine) and mine.exists()
    elsewhere = tmp_path / "a" / "audit.jsonl"
    _, out, _ = run(app, ["warn"], {"LOGCTL_AUDIT_LOG": str(elsewhere)})
    assert json.loads(out)["meta"]["audit_log_path"] == str(elsewhere) and elsewhere.exists()
    assert len(mine.read_text().splitlines()) == 1


@pytest.mark.parametrize("value", ["true", "yes", "", "logs/audit.jsonl", "2", "on", "off", "OFF"])
def test_any_other_audit_log_value_exits_2_and_writes_nothing(tmp_path: Path, value: str) -> None:
    code, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG=value)
    assert code == 2 and envelope["error"]["code"] == "INVALID_AUDIT_LOG_SETTING"
    assert envelope["error"]["context"]["variable"] == "LOGCTL_AUDIT_LOG"
    assert "1, 0, or an absolute file path" in envelope["error"]["message"]
    assert not (tmp_path / "state").exists()


def test_a_bad_audit_log_value_fails_everything_but_help_and_version(tmp_path: Path) -> None:
    for argv in (["manifest"], ["warn", "--schema"], ["audit-log"], ["status"]):
        code, envelope = logged(tmp_path, argv, LOGCTL_AUDIT_LOG="true")
        assert code == 2 and envelope["error"]["code"] == "INVALID_AUDIT_LOG_SETTING", argv
    for argv in (["--version"], ["--help"], ["warn", "--help"]):
        assert logged(tmp_path, argv, LOGCTL_AUDIT_LOG="true")[0] == 0, argv
    envelope = make_app().call("warn", {}, env={"LOGCTL_AUDIT_LOG": "true"})
    assert envelope.error is not None and envelope.error.code == "INVALID_AUDIT_LOG_SETTING"


def test_a_bad_audit_log_value_keeps_the_trace_id_in_meta(tmp_path: Path) -> None:
    code, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG="off", TOOL_TRACE_ID="t-1")
    assert code == 2 and envelope["error"]["code"] == "INVALID_AUDIT_LOG_SETTING"
    assert envelope["meta"]["trace_id"] == "t-1"


def test_while_on_the_manifest_lists_the_log_as_a_log_side_effect(tmp_path: Path) -> None:
    code, envelope = logged(tmp_path, ["manifest"])
    assert code == 0
    commands = envelope["data"]["commands"]
    effect = {"path": str(log_file(tmp_path)), "type": "log"}
    assert commands["warn"]["filesystem_side_effects"] == [effect]
    assert commands["cleanup"]["filesystem_side_effects"] == [effect]
    for unlogged in ("manifest", "version", "completion", "audit-log"):
        assert "filesystem_side_effects" not in commands[unlogged]
    spec_validator("manifest-response").validate(envelope["data"])


def test_every_entry_validates_against_the_spec_schema(tmp_path: Path) -> None:
    logged(tmp_path, ["warn", "--n", "1"], TOOL_TRACE_ID="span-7", LOGCTL_SESSION="s-1")
    logged(tmp_path, ["missing"])
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="sk-live-98765")
    validator = spec_validator("audit-log-entry")
    for entry in entries(tmp_path):
        validator.validate(entry)
    first = entries(tmp_path)[0]
    assert first["args"] == {"n": 1} and first["warnings"] == ["SOMETHING_ODD"]
    assert first["trace_id"] == "span-7" and first["session_id"] == "s-1"
    assert "trace_id" not in entries(tmp_path)[1] and "session_id" not in entries(tmp_path)[1]


def test_the_entry_for_a_command_invoked_with_a_secret_argument_omits_the_secret(
    tmp_path: Path,
) -> None:
    code, _ = logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="sk-live-98765")
    assert code == 0
    assert "sk-live-98765" not in log_file(tmp_path).read_text()
    assert entries(tmp_path)[0]["args"] == {"api_token": "[REDACTED]", "user": "me"}
    assert "hunter2" not in log_file(tmp_path).read_text()  # data is never logged


def test_the_audit_log_is_valid_jsonl(tmp_path: Path) -> None:
    for argv in (["warn"], ["missing"], ["login", "--api-token-from-env", "T"]):
        logged(tmp_path, argv, T="token-value")
    lines = log_file(tmp_path).read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 4
    assert all(isinstance(json.loads(line), dict) for line in lines[:-1])


def test_failures_validation_and_refusals_are_logged(tmp_path: Path) -> None:
    for argv in (
        ["missing"],
        ["warn", "--n", "x"],
        ["warn", "--validate-only"],
        ["cleanup", "--dry-run"],
        ["cleanup"],  # destructive, refused without --confirm-destructive
        ["cleanup", "--confirm-destructive"],
    ):
        logged(tmp_path, argv)
    found = entries(tmp_path)
    assert [(e["command"], e["exit_code"]) for e in found] == [
        ("missing", 79),
        ("warn", 2),
        ("warn", 0),
        ("cleanup", 0),
        ("cleanup", 2),
        ("cleanup", 0),
    ]
    assert found[1]["args"] == {}  # never raw argv
    assert found[2]["args"] == {"n": 0, "validate_only": True}
    assert found[3]["args"]["dry_run"] is True
    assert "confirm_destructive" not in found[4]["args"]
    assert found[5]["args"]["confirm_destructive"] is True


def test_invocations_that_do_no_work_or_resolve_no_command_are_not_logged(
    tmp_path: Path,
) -> None:
    for argv in (
        ["nope"],
        ["warn", "--help"],
        ["warn", "--schema"],
        ["--help"],
        ["--version"],
        ["version"],
        ["manifest"],
        ["completion", "bash"],
        ["audit-log"],
    ):
        logged(tmp_path, argv)
    assert not log_file(tmp_path).exists()


def test_an_entry_over_16_kib_truncates_the_largest_arguments(tmp_path: Path) -> None:
    app = make_app()

    @dataclass(frozen=True, slots=True)
    class Big:
        body: str = Flag(description="Body", max_bytes=60_000_000)
        note: str = Flag(default="short", description="Note")

    @app.command("big", description="Big", danger_level="safe", exit_codes=())
    def big(args: Big, ctx: Ctx) -> dict[str, int]:
        return {"n": len(args.body)}

    body = "x" * (50 * 2**20)
    code, _, _ = run(app, ["big", "--body", body], data_env(tmp_path))
    assert code == 0
    line = log_file(tmp_path).read_bytes()
    assert len(line) <= 16 * 1024 and line.endswith(b"\n")
    entry = json.loads(line)
    assert entry["truncated"] is True and entry["args"] == {"body": "[TRUNCATED]", "note": "short"}
    spec_validator("audit-log-entry").validate(entry)


@needs_posix_permissions
def test_an_unwritable_audit_log_warns_and_never_fails_the_command(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    code, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG=str(blocker / "audit.jsonl"))
    assert code == 0
    assert [w["code"] for w in envelope["warnings"]] == ["AUDIT_LOG_UNAVAILABLE"]
    assert envelope["warnings"][0]["context"]["path"] == str(blocker / "audit.jsonl")
    env = data_env(tmp_path, LOGCTL_AUDIT_LOG=str(blocker / "audit.jsonl"))
    code, out, err = run(make_app(), ["warn", "--format", "plain"], env)
    assert code == 0 and "AUDIT_LOG_UNAVAILABLE" not in out
    warning = json.loads(err.splitlines()[-1])
    assert warning["code"] == "AUDIT_LOG_UNAVAILABLE" and set(warning) == {
        "code",
        "message",
        "context",
    }


@needs_posix_permissions
def test_the_log_and_its_directories_are_owner_only_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        logged(tmp_path, ["warn"])
    finally:
        os.umask(previous)
    assert stat.S_IMODE(log_file(tmp_path).stat().st_mode) == 0o600
    assert stat.S_IMODE(log_file(tmp_path).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


@needs_posix_permissions
def test_an_existing_log_and_directory_keep_their_modes(tmp_path: Path) -> None:
    folder = tmp_path / "state" / BASE
    folder.mkdir(parents=True, mode=0o755)
    os.chmod(folder, 0o755)
    log_file(tmp_path).write_text("")
    os.chmod(log_file(tmp_path), 0o640)
    logged(tmp_path, ["warn"])
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
    assert stat.S_IMODE(log_file(tmp_path).stat().st_mode) == 0o640


def test_app_call_appends_an_entry_too(tmp_path: Path) -> None:
    envelope = make_app().call("warn", {"n": 1}, env=data_env(tmp_path))
    assert envelope.extra_meta["audit_log_path"] == str(log_file(tmp_path))
    assert entries(tmp_path)[0]["args"] == {"n": 1}


def test_status_lists_the_log_only_while_it_is_on_and_cleanup_keeps_it(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["status", "--show-state-files"])
    purposes = {f["purpose"]: f["path"] for f in envelope["data"]["state_files"]}
    assert purposes["audit log"] == str(log_file(tmp_path).resolve())
    code, _ = logged(tmp_path, ["cleanup", "--confirm-destructive"])
    assert code == 0 and log_file(tmp_path).exists()
    _, envelope = logged(tmp_path, ["status", "--show-state-files"], LOGCTL_AUDIT_LOG="0")
    assert "audit log" not in {f["purpose"] for f in envelope["data"]["state_files"]}


# REQ-F-034: redaction in the audit log


def test_an_argument_api_token_appears_as_redacted_in_the_audit_log(tmp_path: Path) -> None:
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    assert entries(tmp_path)[0]["args"]["api_token"] == "[REDACTED]"


def test_the_actual_command_execution_is_not_affected_by_redaction(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    assert envelope["data"]["length"] == len("abc123-value")


# audit=False: a value kept out of the audit log, not out of the command


@dataclass(frozen=True, slots=True)
class Note:
    to: str = Arg(description="Recipient", audit=False)
    body: str = Flag(description="Message body", audit=False)
    subject: str = Flag(default="hi", description="Subject")
    password: str | None = Flag(default=None, description="Password", audit=False)


@dataclass(frozen=True, slots=True)
class Sent:
    ref: str
    chars: int
    effect: str = "created"


def note_app() -> App:
    app = App(BASE, version="1.0.0")

    @app.command(
        "send",
        description="Send a note",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def send(args: Note, ctx: Ctx) -> Sent:
        return Sent(f"note:{args.subject}", len(args.to) + len(args.body))

    return app


OMITTED_ARGS = {"to": "[OMITTED]", "body": "[OMITTED]", "subject": "hi"}


def note_entry(tmp_path: Path) -> dict[str, Any]:
    [entry] = entries(tmp_path)
    assert "private" not in log_file(tmp_path).read_text()
    return entry


def test_an_audit_false_argument_is_omitted_from_the_log_but_reaches_the_handler(
    tmp_path: Path,
) -> None:
    argv = ["send", "private-bob", "--body", "private text", "--format", "json"]
    code, out, _ = run(note_app(), argv, data_env(tmp_path))
    assert code == 0
    data = envelope_of(out)["data"]
    assert data == {"ref": "note:hi", "chars": 23, "effect": "created"}
    entry = note_entry(tmp_path)
    assert entry["args"] == {**OMITTED_ARGS, "password": "[REDACTED]"}


def test_audit_false_is_omitted_on_exec_raw_payload_and_app_call(tmp_path: Path) -> None:
    line = {"_cmd": "send", "to": "private-bob", "body": "private text"}
    for route in ("exec", "raw", "call"):
        home = tmp_path / route
        if route == "exec":
            stdin = io.StringIO(json.dumps(line) + "\n")
            out = io.StringIO()
            note_app().run(
                ["exec"], stdin=stdin, stdout=out, stderr=io.StringIO(), env=data_env(home)
            )
            data = json.loads(out.getvalue().splitlines()[0])["data"]
        elif route == "raw":
            payload = json.dumps({k: v for k, v in line.items() if k != "_cmd"})
            _, out_text, _ = run(note_app(), ["send", "--raw-payload", payload], data_env(home))
            data = envelope_of(out_text)["data"]
        else:
            envelope = note_app().call(
                "send", {"to": "private-bob", "body": "private text"}, env=data_env(home)
            )
            data = envelope.data
        assert data["chars"] == 23, route
        assert note_entry(home)["args"] == {**OMITTED_ARGS, "password": "[REDACTED]"}


def test_the_manifest_and_schemas_mark_audit_false_fields() -> None:
    app = note_app()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["send"]  # type: ignore[index]
    assert entry["flags"]["body"]["description"] == "Message body (omitted from the audit log)"
    assert entry["flags"]["subject"]["description"] == "Subject"
    assert entry["positionals"][0]["description"] == "Recipient (omitted from the audit log)"
    assert "x-audited" not in json.dumps(entry["output_schema"])
    _, out, _ = run(app, ["send", "--schema"])
    schema = json.loads(out)["data"]
    assert schema["raw_payload_schema"]["properties"]["body"]["x-audited"] is False
    assert "x-audited" not in schema["raw_payload_schema"]["properties"]["subject"]


def test_audit_is_a_boolean() -> None:
    with pytest.raises(RegistrationError, match="audit is True or False"):
        Flag(description="x", audit="no")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="audit is True or False"):
        Arg(description="x", audit=0)  # type: ignore[arg-type]


# REQ-O-023: --no-injection-protection in the audit trail


def test_use_of_no_injection_protection_is_recorded_in_the_audit_log_with_a_warning(
    tmp_path: Path,
) -> None:
    logged(tmp_path, ["warn", "--no-injection-protection"])
    entry = entries(tmp_path)[0]
    assert entry["warnings"] == ["INJECTION_PROTECTION_DISABLED"]
    assert entry["args"]["no_injection_protection"] is True


# Bounds: rotation and retention


def small(tmp_path: Path, **kw: int) -> App:
    return make_app(audit_log=AuditLog(path=tmp_path / "log" / "audit.jsonl", **kw))


def test_the_default_bounds_are_10_mib_5_rotated_files_and_30_days() -> None:
    bounds = AuditLog()
    assert (bounds.max_bytes, bounds.keep, bounds.max_age_days) == (10 * 2**20, 5, 30)
    assert bounds.max_bytes * (bounds.keep + 1) <= 60 * 2**20


def test_a_log_file_that_exceeds_the_size_limit_is_rotated_and_a_new_file_started(
    tmp_path: Path,
) -> None:
    app = small(tmp_path, max_bytes=400)
    for _ in range(3):
        run(app, ["warn"])
    live, first = tmp_path / "log" / "audit.jsonl", tmp_path / "log" / "audit.1.jsonl"
    assert first.exists() and live.exists()
    assert first.stat().st_size <= 400 and live.stat().st_size <= 400


def test_rotated_files_beyond_the_retention_count_are_deleted_automatically(
    tmp_path: Path,
) -> None:
    app = small(tmp_path, max_bytes=200, keep=2)
    for _ in range(8):
        run(app, ["warn"])
    names = sorted(p.name for p in (tmp_path / "log").glob("audit*.jsonl"))
    assert names == ["audit.1.jsonl", "audit.2.jsonl", "audit.jsonl"]


def test_rotated_files_older_than_the_maximum_age_are_deleted(tmp_path: Path) -> None:
    folder = tmp_path / "log"
    folder.mkdir()
    old, recent = folder / "audit.2.jsonl", folder / "audit.1.jsonl"
    old.write_text("{}\n")
    recent.write_text("{}\n")
    month_ago = (dt.datetime.now() - dt.timedelta(days=31)).timestamp()
    os.utime(old, (month_ago, month_ago))
    run(small(tmp_path, max_age_days=30), ["warn"])
    assert not old.exists() and recent.exists()


def test_pruning_waits_for_a_concurrent_rotation_and_keeps_the_file_it_moved_in(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "log"
    folder.mkdir()
    slot = folder / "audit.1.jsonl"
    slot.write_text('{"old":true}\n')
    month_ago = (dt.datetime.now() - dt.timedelta(days=31)).timestamp()
    os.utime(slot, (month_ago, month_ago))
    journal = Journal(folder / "audit.jsonl", AuditLog(max_age_days=30))
    entry = {"timestamp": "2026-01-01T00:00:00.000Z", "command": "warn"}
    appending = threading.Thread(target=journal.append, args=(entry,))
    with exclusive(journal.lock_path):  # another run is rotating
        appending.start()
        appending.join(timeout=0.5)
        # The append waits for the lock rather than pruning the slot mid-rotation
        assert appending.is_alive() and slot.exists()
        slot.write_text('{"moved":true}\n')  # the rotation moves a fresh file into it
    appending.join()
    assert json.loads(slot.read_text()) == {"moved": True}


def test_an_active_file_whose_first_entry_is_too_old_is_rotated_on_the_next_write(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "log"
    folder.mkdir()
    live = folder / "audit.jsonl"
    stale = {"timestamp": "2020-01-01T00:00:00.000Z", "command": "warn"}
    live.write_text(json.dumps(stale) + "\n")  # written just now, but its entry is old
    run(small(tmp_path, max_age_days=30), ["warn"])
    assert json.loads((folder / "audit.1.jsonl").read_text()) == stale
    assert [json.loads(line)["command"] for line in live.read_text().splitlines()] == ["warn"]
    run(small(tmp_path, max_age_days=30), ["warn"])
    assert not (folder / "audit.2.jsonl").exists()  # the new first entry is recent


def test_disk_usage_from_the_audit_log_is_bounded_even_across_unlimited_invocations(
    tmp_path: Path,
) -> None:
    app = small(tmp_path, max_bytes=1000, keep=2)
    for _ in range(60):
        run(app, ["warn", "--n", "1"])
    used = sum(p.stat().st_size for p in (tmp_path / "log").glob("audit*.jsonl"))
    assert used <= 3 * 1000


def test_audit_log_settings_are_checked_at_registration() -> None:
    for bad in ({"max_bytes": 0}, {"keep": 0}, {"max_age_days": -1}, {"path": "rel/audit.jsonl"}):
        with pytest.raises(RegistrationError, match="AuditLog"):
            AuditLog(**bad)  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="audit_log"):
        App("x", version="1.0.0", audit_log="yes")  # type: ignore[arg-type]


# REQ-O-030: the audit-log built-in


def query(tmp_path: Path, argv: list[str]) -> tuple[int, list[dict[str, Any]]]:
    code, out, _ = run(make_app(), ["audit-log", *argv, "--format", "jsonl"], data_env(tmp_path))
    return code, [json.loads(line) for line in out.splitlines()]


def events(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The item lines of the audit-log stream, before its terminal line"""
    return [line for line in lines if "_summary" not in line and "ok" not in line]


def old_entry(tmp_path: Path, **fields: object) -> None:
    path = log_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": "2099-01-01T00:00:00.000Z", "command": "warn"}
    with path.open("a") as handle:
        handle.write(json.dumps(entry | fields) + "\n")


def test_audit_log_since_1h_format_jsonl_returns_invocations_of_the_past_hour_one_per_line(
    tmp_path: Path,
) -> None:
    logged(tmp_path, ["warn"])
    logged(tmp_path, ["missing"])
    # Queried entries are read from the file whatever it holds: one from long ago
    with log_file(tmp_path).open("r+") as handle:
        lines = handle.read().splitlines()
        handle.seek(0)
        old = json.dumps({"timestamp": "2020-01-01T00:00:00.000Z", "command": "warn"})
        handle.write("\n".join([old, *lines]) + "\n")
    code, found = query(tmp_path, ["--since", "1h"])
    assert code == 0 and [e["command"] for e in events(found)] == ["warn", "missing"]
    assert found[-1]["_summary"] is True and found[-1]["total"] == 2


def test_audit_log_since_takes_an_iso_time_with_an_offset(tmp_path: Path) -> None:
    old_entry(tmp_path, timestamp="2026-03-17T13:59:59Z", n=1)
    old_entry(tmp_path, timestamp="2026-03-17T14:00:00Z", n=2)
    old_entry(tmp_path, timestamp="2026-03-17T15:30:00+02:00", n=3)
    _, found = query(tmp_path, ["--since", "2026-03-17T14:00:00Z"])
    assert [e["n"] for e in events(found)] == [2]


def test_audit_log_trace_id_returns_only_entries_with_that_trace_id(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"], TOOL_TRACE_ID="abc123")
    logged(tmp_path, ["warn"], TOOL_TRACE_ID="other")
    logged(tmp_path, ["missing"], TOOL_TRACE_ID="abc123")
    _, lines = query(tmp_path, ["--trace-id", "abc123"])
    assert [(e["command"], e["trace_id"]) for e in events(lines)] == [
        ("warn", "abc123"),
        ("missing", "abc123"),
    ]


def test_secret_field_values_are_redacted_in_all_audit_log_query_results(tmp_path: Path) -> None:
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    old_entry(tmp_path, args={"password": "plain"})
    code, out, _ = run(make_app(), ["audit-log", "--format", "jsonl"], data_env(tmp_path))
    assert code == 0 and "abc123-value" not in out and "plain" not in out
    found = events([json.loads(line) for line in out.splitlines()])
    assert found[0]["args"]["api_token"] == "[REDACTED]"
    assert found[1]["args"] == {"password": "[REDACTED]"}


def test_limit_100_returns_the_newest_100_oldest_first(tmp_path: Path) -> None:
    for n in range(120):
        old_entry(tmp_path, timestamp=f"2099-01-01T00:{n // 60:02d}:{n % 60:02d}Z", n=n)
    _, lines = query(tmp_path, ["--limit", "100"])
    found = events(lines)
    assert [e["n"] for e in found] == list(range(20, 120))


def test_audit_log_command_matches_whole_words_of_the_path(tmp_path: Path) -> None:
    for command in ("config.set", "config.get", "configure", "warn"):
        old_entry(tmp_path, command=command)
    for wanted in ("config", "config set", "config.set"):
        _, lines = query(tmp_path, ["--command", wanted])
        expected = ["config.set", "config.get"] if wanted == "config" else ["config.set"]
        assert [e["command"] for e in events(lines)] == expected, wanted


def test_audit_log_skips_unreadable_lines_with_a_warning(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"])
    logged(tmp_path, ["missing"])
    with log_file(tmp_path).open("a") as handle:
        handle.write("not json\n")
    _, lines = query(tmp_path, ["--command", "missing"])
    assert [e["command"] for e in events(lines)] == ["missing"]
    assert lines[-1]["warnings"][0]["code"] == "AUDIT_LINES_UNREADABLE"


@pytest.mark.parametrize(
    "argv",
    [
        ["--since", "yesterday"],
        ["--since", "1w"],
        ["--since", "0h"],
        ["--since", "2026-03-17T14:00:00"],
        ["--limit", "0"],
    ],
)
def test_audit_log_rejects_a_bad_since_or_limit_before_running(
    tmp_path: Path, argv: list[str]
) -> None:
    code, lines = query(tmp_path, argv)
    assert code == 2 and lines[-1]["error"]["phase"] == "validation"


def test_audit_log_reads_rotated_files_oldest_first(tmp_path: Path) -> None:
    app = make_app(audit_log=AuditLog(path=tmp_path / "log" / "audit.jsonl", max_bytes=400))
    for n in range(4):
        run(app, ["warn", "--n", str(n)])
    assert (tmp_path / "log" / "audit.1.jsonl").exists()
    _, out, _ = run(app, ["audit-log", "--command", "warn"])
    ns = [e["args"]["n"] for e in events([json.loads(line) for line in out.splitlines()])]
    assert ns == [0, 1, 2, 3]


def test_audit_log_while_the_log_is_disabled_exits_4(tmp_path: Path) -> None:
    code, lines = query(tmp_path, [])
    assert code == 0
    for env in ({}, {"LOGCTL_AUDIT_LOG": "0"}):
        code, out, _ = run(make_app(), ["audit-log"], env)
        error = json.loads(out.splitlines()[-1])["error"]
        assert code == 4 and error["code"] == "AUDIT_LOG_DISABLED"
        assert "LOGCTL_AUDIT_LOG=1" in error["fix_required"]


def test_audit_log_is_on_every_app_and_yields_to_an_app_command_of_that_name() -> None:
    assert "audit-log" in make_app().manifest()["commands"]  # type: ignore[operator]
    app = make_app()

    @app.command("audit-log", description="Mine", danger_level="safe", exit_codes=())
    def mine(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"mine": "yes"}

    assert json.loads(run(app, ["audit-log"])[1])["data"] == {"mine": "yes"}
    assert "audit-log" in [p.value for p in app.shadowed_builtins]


def test_the_treaty_cli_has_the_audit_log_builtin_too() -> None:
    assert "audit-log" in cli.manifest()["commands"]  # type: ignore[operator]


# The linter


def test_a_handler_that_prints_is_flagged_by_log_not_print() -> None:
    report = audit(make_app(), "logctl", limit=50)
    rule = next(r for r in report.rules if r.id == "log-not-print")
    assert [f.command for f in rule.findings] == ["chatty"]
    assert "ctx.log(" in rule.findings[0].fix
