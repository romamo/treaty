import io
import json

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
