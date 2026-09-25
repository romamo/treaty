import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from conftest import spec_validator

from treaty import App


def run_exec(app: App, lines: list[str], *flags: str) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = app.run(
        ["exec", *flags],
        stdin=io.StringIO("\n".join(lines) + "\n"),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    envelopes = [json.loads(line) for line in out.getvalue().splitlines()]
    validator = spec_validator("response-envelope")
    for e in envelopes:
        validator.validate(e)
    return code, envelopes


def test_exec_dispatches_each_line_in_process(app: App) -> None:
    code, out = run_exec(
        app,
        [
            '{"_cmd": "deploy.status", "service": "api"}',
            '{"_cmd": "deploy.rollback", "_opts": {"dry-run": true, "replicas": 2}, '
            '"service": "web", "tags": ["a", "b"]}',
        ],
    )
    assert code == 0
    assert [e["meta"]["_line"] for e in out] == [1, 2]
    assert [e["meta"]["_cmd"] for e in out] == ["deploy.status", "deploy.rollback"]
    assert out[0]["data"] == [{"service": "api", "release": "1.4.0"}]
    assert out[1]["data"]["dry_run"] is True and out[1]["data"]["replicas"] == 2
    assert out[1]["data"]["tags"] == ["a", "b"]


def test_exec_stops_at_first_failure_by_default(app: App) -> None:
    code, out = run_exec(
        app,
        [
            '{"_cmd": "deploy.rollback", "service": "locked"}',
            '{"_cmd": "deploy.status", "service": "api"}',
        ],
    )
    assert code == 1 and len(out) == 1
    assert out[0]["meta"]["exit_code"] == 79 and out[0]["error"]["code"] == "DEPLOY_CONFLICT"


def test_exec_ignore_errors_continues_and_still_exits_1(app: App) -> None:
    code, out = run_exec(
        app,
        [
            '{"_cmd": "deploy.rollback", "service": "locked"}',
            "not json at all",
            '{"_cmd": "nope.nothing"}',
            '{"_cmd": "deploy.status", "service": "api"}',
        ],
        "--ignore-errors",
    )
    assert code == 1 and len(out) == 4
    assert out[1]["error"]["code"] == "DISPATCH_PARSE_ERROR"
    assert out[1]["error"]["phase"] == "validation" and out[1]["meta"]["exit_code"] == 2
    assert "_cmd" not in out[1]["meta"]
    assert out[2]["error"]["code"] == "UNKNOWN_COMMAND"
    assert out[3]["ok"] is True


def test_exec_fully_malformed_stream_exits_2(app: App) -> None:
    code, out = run_exec(app, ["{", "[]"], "--ignore-errors")
    assert code == 2 and all(e["error"]["code"] == "DISPATCH_PARSE_ERROR" for e in out)


def test_exec_dry_run_forwarded_only_to_non_safe(app: App) -> None:
    code, out = run_exec(
        app,
        [
            '{"_cmd": "deploy.rollback", "service": "web", "_opts": {"confirm-destructive": true}}',
            '{"_cmd": "deploy.status", "service": "api"}',
        ],
        "--dry-run",
    )
    assert code == 0
    assert out[0]["data"]["dry_run"] is True
    assert out[1]["ok"] is True


def test_exec_typed_payload_errors_are_arg_error(app: App) -> None:
    code, out = run_exec(
        app,
        [
            '{"_cmd": "deploy.rollback", "service": "web", "replicas": "two"}',
            '{"_cmd": "deploy.rollback", "service": "web", "bogus": 1}',
            '{"_cmd": "deploy.rollback"}',
        ],
        "--ignore-errors",
    )
    assert code == 1
    assert [e["meta"]["exit_code"] for e in out] == [2, 2, 2]
    assert out[0]["error"]["context"]["field"] == "replicas"
    assert out[1]["error"]["context"]["field"] == "bogus"
    assert out[2]["error"]["context"]["missing"] == ["service"]


def test_exec_cannot_dispatch_itself(app: App) -> None:
    code, out = run_exec(app, ['{"_cmd": "exec"}'])
    assert code == 1 and out[0]["error"]["code"] == "UNKNOWN_COMMAND"


def test_exec_empty_stdin_emits_error_envelope(app: App) -> None:
    code, out = run_exec(app, [])
    assert code == 2 and len(out) == 1
    assert out[0]["error"]["code"] == "EMPTY_STREAM"
    assert out[0]["error"]["phase"] == "validation"
    assert out[0]["meta"]["exit_code"] == 2


def test_exec_blank_lines_only_is_empty_stream(app: App) -> None:
    code, out = run_exec(app, ["", "   "])
    assert code == 2 and out[0]["error"]["code"] == "EMPTY_STREAM"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_exec_refuses_tty_stdin(app: App) -> None:
    out = io.StringIO()
    code = app.run(
        ["exec"],
        stdin=_Terminal('{"_cmd": "version"}\n'),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    assert code == 2 and envelope["error"]["code"] == "STDIN_IS_TTY"
    assert envelope["error"]["phase"] == "validation"


def test_exec_drains_stdin_before_writing() -> None:
    """A caller that writes the whole plan before reading must not deadlock on the stdout pipe"""
    plan = b'{"_cmd": "version"}\n' * 12000  # 240KB in, ~2MB out: both past the 64KB pipe buffer
    proc = subprocess.Popen(
        [sys.executable, "-c", "from treaty._cli import main; main()", "exec"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "TREATY_MAX_STDIN_BYTES": str(len(plan))},
    )
    assert proc.stdin is not None and proc.stdout is not None

    def write_all() -> None:
        assert proc.stdin is not None
        proc.stdin.write(plan)
        proc.stdin.close()

    writer = threading.Thread(target=write_all, daemon=True)
    writer.start()
    writer.join(timeout=10)
    try:
        assert not writer.is_alive(), "exec stopped reading stdin while its stdout was full"
    finally:
        out = proc.stdout.read()
        proc.wait(timeout=10)
    assert proc.returncode == 0 and out.count(b"\n") == 12000


def run_exec_raw(app: App, argv: list[str], stdin: str, env: dict[str, str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env=env)
    envelope = json.loads(out.getvalue().splitlines()[0])
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


LINE = '{"_cmd": "deploy.status", "service": "api"}\n'


def test_stdin_over_the_cap_is_rejected_before_dispatch(app: App) -> None:
    exact = LINE * 2
    env = {"TREATY_MAX_STDIN_BYTES": str(len(exact))}
    code, _ = run_exec_raw(app, ["exec"], exact, env)
    assert code == 0
    code, envelope = run_exec_raw(app, ["exec"], exact + "\n", env)
    error = envelope["error"]
    assert code == 2 and error["code"] == "STDIN_TOO_LARGE"
    assert error["context"] == {"limit_bytes": len(exact)}
    assert "--input-file" in error["fix_required"]


def test_default_stdin_cap_is_64_kib(app: App) -> None:
    code, envelope = run_exec_raw(app, ["exec"], LINE * 2000, {})
    assert code == 2 and envelope["error"]["context"]["limit_bytes"] == 65_536


def test_input_file_has_no_cap_and_dash_means_stdin(app: App, tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text(LINE * 2000)
    out = io.StringIO()
    code = app.run(["exec", "--input-file", str(plan)], stdout=out, env={}, isatty=False)
    assert code == 0 and len(out.getvalue().splitlines()) == 2000
    code, _ = run_exec_raw(app, ["exec", "--input-file", "-"], LINE * 2000, {})
    assert code == 2


def test_unreadable_input_file_is_arg_error(app: App, tmp_path: Path) -> None:
    code, envelope = run_exec_raw(app, ["exec", "--input-file", str(tmp_path / "no")], "", {})
    assert code == 2 and envelope["error"]["code"] == "INPUT_FILE_UNREADABLE"


def test_invalid_stdin_cap_env_is_arg_error(app: App) -> None:
    code, envelope = run_exec_raw(app, ["exec"], LINE, {"TREATY_MAX_STDIN_BYTES": "lots"})
    assert code == 2 and envelope["error"]["context"]["source"] == "TREATY_MAX_STDIN_BYTES"


def test_oversized_pipe_fails_fast_for_a_write_everything_caller() -> None:
    """Past the cap exec exits instead of reading on, so the writer gets EPIPE, not a hang"""
    plan = b'{"_cmd": "version"}\n' * 12000
    proc = subprocess.Popen(
        [sys.executable, "-c", "from treaty._cli import main; main()", "exec"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(plan)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    out = proc.stdout.read()
    assert proc.wait(timeout=10) == 2 and json.loads(out)["error"]["code"] == "STDIN_TOO_LARGE"
