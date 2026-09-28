"""Verbosity, --warnings-as-errors, and the audit log (REQ-F-038, REQ-O-008, REQ-O-025,
REQ-F-026, REQ-F-042, REQ-O-030, REQ-F-034, REQ-O-023, REQ-F-025, REQ-F-060)"""

import datetime as dt
import io
import json
import logging
import os
import stat
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_permissions

from treaty import App, AuditLog, Ctx, Exit, Flag, NoArgs, RegistrationError
from treaty._audit import audit
from treaty._cli import cli

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
    """Run with the audit log under ``tmp_path``; the envelope"""
    code, out, _ = run(make_app(), [*argv, "--format", "json"], data_env(tmp_path, **env))
    return code, envelope_of(out)


def data_env(tmp_path: Path, **env: str) -> dict[str, str]:
    return {"XDG_DATA_HOME": str(tmp_path / "data"), **env}


def log_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / BASE / "audit.jsonl"


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
    assert not [h for h in root.handlers if type(h).__name__ == "_TraceHandler"]
    _, _, err = run(make_app(), ["lib", "--verbose"])
    assert "pool size" not in err


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
    assert code == 1 and [line["ok"] for line in lines] == [True, True, False]
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


# REQ-F-026: the audit log


def test_after_any_command_invocation_the_audit_log_contains_a_new_entry(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"])
    logged(tmp_path, ["warn", "--n", "1"])
    found = entries(tmp_path)
    assert [e["command"] for e in found] == ["warn", "warn"]
    assert found[1]["parameters"] == {"n": 1} and found[1]["warnings"] == ["SOMETHING_ODD"]
    assert set(found[0]) >= {
        "timestamp",
        "command",
        "parameters",
        "exit_code",
        "duration_ms",
        "trace_id",
        "request_id",
        "operator",
    }


def test_the_entry_for_a_command_invoked_with_a_secret_argument_omits_the_secret(
    tmp_path: Path,
) -> None:
    code, _ = logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="sk-live-98765")
    assert code == 0
    assert "sk-live-98765" not in log_file(tmp_path).read_text()
    assert entries(tmp_path)[0]["parameters"] == {"api_token": "[REDACTED]", "user": "me"}


def test_the_audit_log_is_valid_jsonl(tmp_path: Path) -> None:
    for argv in (["warn"], ["missing"], ["login", "--api-token-from-env", "T"], ["nope"]):
        logged(tmp_path, argv, T="token-value")
    lines = log_file(tmp_path).read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 5
    assert all(isinstance(json.loads(line), dict) for line in lines[:-1])


def test_the_audit_log_is_written_even_when_the_command_exits_non_zero(tmp_path: Path) -> None:
    assert logged(tmp_path, ["missing"])[0] == 79
    assert logged(tmp_path, ["warn", "--n", "x"])[0] == 2
    failed, bad_args = entries(tmp_path)
    assert failed["exit_code"] == 79 and failed["error_code"] == "GONE"
    assert bad_args["exit_code"] == 2 and bad_args["parameters"] == {}  # never raw argv


def test_meta_audit_log_path_names_the_log_file(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["warn"])
    assert envelope["meta"]["audit_log_path"] == str(log_file(tmp_path))
    home = {"HOME": str(tmp_path / "home")}
    _, out, _ = run(make_app(), ["warn"], home)
    expected = tmp_path / "home" / ".local" / "share" / BASE / "audit.jsonl"
    assert json.loads(out)["meta"]["audit_log_path"] == str(expected) and expected.exists()


def test_help_and_schema_are_not_logged(tmp_path: Path) -> None:
    for argv in (["warn", "--help"], ["warn", "--schema"], ["--help"]):
        logged(tmp_path, argv)
    assert not log_file(tmp_path).exists()


def test_the_audit_log_variable_turns_it_off_or_moves_it(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG="off")
    assert "audit_log_path" not in envelope["meta"] and not log_file(tmp_path).exists()
    elsewhere = tmp_path / "custom" / "audit.jsonl"
    _, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG=str(elsewhere))
    assert envelope["meta"]["audit_log_path"] == str(elsewhere) and elsewhere.exists()


def test_a_relative_audit_log_variable_exits_2_before_anything_runs(tmp_path: Path) -> None:
    code, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG="audit.jsonl")
    assert code == 2 and envelope["error"]["context"]["variable"] == "LOGCTL_AUDIT_LOG"
    code, _ = logged(tmp_path, ["version"], LOGCTL_AUDIT_LOG="audit.jsonl")
    assert code == 0  # REQ-F-068: the pure built-ins still answer


def test_audit_log_none_keeps_no_log_and_no_builtin(tmp_path: Path) -> None:
    app = make_app(audit_log=None)
    _, out, _ = run(app, ["warn"], data_env(tmp_path))
    assert "audit_log_path" not in json.loads(out)["meta"] and not log_file(tmp_path).exists()
    assert "audit-log" not in app.manifest()["commands"]  # type: ignore[operator]


@needs_posix_permissions
def test_an_unwritable_audit_log_warns_and_never_fails_the_command(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    code, envelope = logged(tmp_path, ["warn"], LOGCTL_AUDIT_LOG=str(blocker / "audit.jsonl"))
    assert code == 0
    assert [w["code"] for w in envelope["warnings"]] == ["AUDIT_LOG_UNAVAILABLE"]
    assert envelope["warnings"][0]["context"]["path"] == str(blocker / "audit.jsonl")


@needs_posix_permissions
def test_the_audit_log_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"])
    assert stat.S_IMODE(log_file(tmp_path).stat().st_mode) == 0o600
    assert stat.S_IMODE(log_file(tmp_path).parent.stat().st_mode) == 0o700


def test_app_call_appends_an_entry_too(tmp_path: Path) -> None:
    envelope = make_app().call("warn", {"n": 1}, env=data_env(tmp_path))
    assert envelope.extra_meta["audit_log_path"] == str(log_file(tmp_path))
    assert entries(tmp_path)[0]["parameters"] == {"n": 1}


# REQ-F-025: audit entries carry the trace; REQ-O-030's operator is the session


def test_audit_log_entries_include_the_trace_id(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"], TOOL_TRACE_ID="span-7", LOGCTL_SESSION="agent-1")
    entry = entries(tmp_path)[0]
    assert entry["trace_id"] == "span-7" and entry["operator"] == "agent-1"
    assert len(entry["request_id"]) == 12


# REQ-F-034: redaction in the audit log


def test_an_argument_api_token_appears_as_redacted_in_the_audit_log(tmp_path: Path) -> None:
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    assert entries(tmp_path)[0]["parameters"]["api_token"] == "[REDACTED]"


def test_a_response_field_password_appears_as_redacted_in_the_audit_log(tmp_path: Path) -> None:
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    assert entries(tmp_path)[0]["data"]["PassWord"] == "[REDACTED]"
    assert "hunter2" not in log_file(tmp_path).read_text()


def test_the_actual_command_execution_is_not_affected_by_redaction(tmp_path: Path) -> None:
    _, envelope = logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    assert envelope["data"]["length"] == len("abc123-value")


def test_redaction_applies_to_field_names_matched_case_insensitively(tmp_path: Path) -> None:
    logged(tmp_path, ["login", "--api-token-from-env", "TOK"], TOK="abc123-value")
    data = entries(tmp_path)[0]["data"]
    assert data == {"length": 12, "PassWord": "[REDACTED]", "user": "me"}


def test_large_data_is_left_out_of_the_entry_by_size(tmp_path: Path) -> None:
    app = make_app()

    @app.command("big", description="Big", danger_level="safe", exit_codes=())
    def big(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"text": "x" * 5000}

    run(app, ["big"], data_env(tmp_path))
    entry = entries(tmp_path)[0]
    assert entry["data"] is None and entry["data_bytes"] > 5000


# REQ-O-023: --no-injection-protection in the audit trail


def test_use_of_no_injection_protection_is_recorded_in_the_audit_log_with_a_warning(
    tmp_path: Path,
) -> None:
    logged(tmp_path, ["warn", "--no-injection-protection"])
    assert entries(tmp_path)[0]["warnings"] == ["INJECTION_PROTECTION_DISABLED"]


# REQ-F-042: rotation and retention


def small(tmp_path: Path, **kw: int) -> App:
    return make_app(audit_log=AuditLog(path=tmp_path / "log" / "audit.jsonl", **kw))


def test_a_log_file_that_exceeds_the_size_limit_is_rotated_and_a_new_file_started(
    tmp_path: Path,
) -> None:
    app = small(tmp_path, max_bytes=600)
    for _ in range(3):
        run(app, ["warn"])
    live, first = tmp_path / "log" / "audit.jsonl", tmp_path / "log" / "audit.1.jsonl"
    assert first.exists() and live.exists()
    assert first.stat().st_size <= 600 and live.stat().st_size <= 600


def test_rotated_files_beyond_the_retention_count_are_deleted_automatically(
    tmp_path: Path,
) -> None:
    app = small(tmp_path, max_bytes=300, keep=2)
    for _ in range(8):
        run(app, ["warn"])
    names = sorted(p.name for p in (tmp_path / "log").glob("audit*.jsonl"))
    assert names == ["audit.1.jsonl", "audit.2.jsonl", "audit.jsonl"]


def test_log_files_older_than_the_maximum_age_are_deleted_on_framework_startup(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "log"
    folder.mkdir()
    old, recent = folder / "audit.2.jsonl", folder / "audit.1.jsonl"
    old.write_text("{}\n")
    recent.write_text("{}\n")
    month_ago = (dt.datetime.now() - dt.timedelta(days=31)).timestamp()
    os.utime(old, (month_ago, month_ago))
    run(small(tmp_path, max_age_days=30), ["warn"])
    assert not old.exists() and recent.exists()


def test_disk_usage_from_framework_logs_is_bounded_even_across_unlimited_invocations(
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
    return [line["data"] for line in lines if "seq" in line["meta"] and not line["meta"].get("end")]


def old_entry(tmp_path: Path, **fields: object) -> None:
    path = log_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": "2020-01-01T00:00:00.000Z", "command": "warn", "trace_id": None}
    with path.open("a") as handle:
        handle.write(json.dumps(entry | fields) + "\n")


def test_audit_log_since_1h_format_jsonl_returns_invocations_of_the_past_hour_one_per_line(
    tmp_path: Path,
) -> None:
    old_entry(tmp_path)
    logged(tmp_path, ["warn"])
    logged(tmp_path, ["missing"])
    code, lines = query(tmp_path, ["--since", "1h"])
    assert code == 0 and [e["command"] for e in events(lines)] == ["warn", "missing"]
    assert lines[-1]["meta"]["end"] is True and lines[-1]["meta"]["total"] == 2


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
    old_entry(tmp_path, timestamp="2099-01-01T00:00:00Z", parameters={"password": "plain"})
    code, out, _ = run(make_app(), ["audit-log", "--format", "jsonl"], data_env(tmp_path))
    assert code == 0 and "abc123-value" not in out and "plain" not in out
    found = events([json.loads(line) for line in out.splitlines()])
    assert found[0]["parameters"]["api_token"] == "[REDACTED]"
    assert found[1]["parameters"] == {"password": "[REDACTED]"}


def test_limit_100_returns_at_most_100_entries(tmp_path: Path) -> None:
    for n in range(120):
        old_entry(tmp_path, timestamp=f"2099-01-01T00:00:{n % 60:02d}Z", n=n)
    _, lines = query(tmp_path, ["--limit", "100"])
    found = events(lines)
    assert len(found) == 100 and found[-1]["n"] == 119  # the newest, oldest first


def test_audit_log_filters_by_command_and_skips_unreadable_lines(tmp_path: Path) -> None:
    logged(tmp_path, ["warn"])
    logged(tmp_path, ["missing"])
    with log_file(tmp_path).open("a") as handle:
        handle.write("not json\n")
    _, lines = query(tmp_path, ["--command", "missing"])
    assert [e["command"] for e in events(lines)] == ["missing"]
    assert lines[-1]["warnings"][0]["code"] == "AUDIT_LINES_UNREADABLE"


def test_audit_log_rejects_a_bad_since_or_limit_before_running(tmp_path: Path) -> None:
    for argv in (["--since", "yesterday"], ["--limit", "0"]):
        code, lines = query(tmp_path, argv)
        assert code == 2 and lines[-1]["error"]["phase"] == "validation"


def test_audit_log_reads_rotated_files_oldest_first(tmp_path: Path) -> None:
    app = make_app(audit_log=AuditLog(path=tmp_path / "log" / "audit.jsonl", max_bytes=600))
    for n in range(4):
        run(app, ["warn", "--n", str(n)])
    assert (tmp_path / "log" / "audit.1.jsonl").exists()
    _, out, _ = run(app, ["audit-log", "--command", "warn"])
    ns = [e["parameters"]["n"] for e in events([json.loads(line) for line in out.splitlines()])]
    assert ns == [0, 1, 2, 3]


def test_audit_log_off_exits_4(tmp_path: Path) -> None:
    code, lines = query(tmp_path, [])
    assert code == 0
    code, out, _ = run(make_app(), ["audit-log"], {"LOGCTL_AUDIT_LOG": "off"})
    assert code == 4 and json.loads(out.splitlines()[-1])["error"]["code"] == "AUDIT_LOG_OFF"


def test_audit_log_yields_to_an_app_command_of_that_name() -> None:
    app = make_app()

    @app.command("audit-log", description="Mine", danger_level="safe", exit_codes=())
    def mine(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"mine": "yes"}

    assert json.loads(run(app, ["audit-log"])[1])["data"] == {"mine": "yes"}
    assert "audit-log" in [p.value for p in app.shadowed_builtins]


def test_the_treaty_cli_has_no_audit_log_builtin() -> None:
    assert "audit-log" not in cli.manifest()["commands"]  # type: ignore[operator]


# The linter


def test_a_handler_that_prints_is_flagged_by_log_not_print() -> None:
    report = audit(make_app(), "logctl", limit=50)
    rule = next(r for r in report.rules if r.id == "log-not-print")
    assert [f.command for f in rule.findings] == ["chatty"]
    assert "ctx.log(" in rule.findings[0].fix


def test_the_audit_log_variable_off_wins_over_an_explicit_path(tmp_path: Path) -> None:
    app = small(tmp_path)
    _, out, _ = run(app, ["warn"], {"LOGCTL_AUDIT_LOG": "off"})
    assert "audit_log_path" not in json.loads(out)["meta"]
    assert not (tmp_path / "log").exists()
