"""A CLI moving to treaty a command at a time (#28): ``app.resolves`` splits a half-migrated
group for a routing shim, and ``App(exec_fallback=)`` runs a plan that mixes migrated and
unmigrated commands in one ``exec``"""

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, NoArgs, ParseError, RegistrationError
from treaty._signals import Cancelled, CancelSignal


@dataclass(frozen=True)
class ListArgs:
    account: str = Arg(description="Account to list")


def make_app(**kw: object) -> App:
    app = App("bean", version="1.0.0", **kw)  # type: ignore[arg-type]
    txn = app.group("transaction", description="Transactions")

    @txn.command("list", danger_level="safe", exit_codes=(), description="List transactions")
    def list_(args: ListArgs, ctx: Ctx) -> list[dict[str, str]]:
        return [{"account": args.account, "amount": "1.00"}]

    @app.command("check", danger_level="safe", exit_codes=(), description="Check the ledger")
    def check(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    app.redirect("verify", to="check")
    return app


# app.resolves


@pytest.mark.parametrize(
    "argv",
    [
        ["transaction", "list", "Assets"],
        ["transaction", "list", "--help"],
        ["--format", "json", "transaction", "list", "Assets"],
        ["transaction", "--format=json", "list"],
        ["-v", "check"],
        ["verify"],  # a retired path answers with REDIRECTED
        ["manifest"],
        ["version"],
        ["exec"],
    ],
)
def test_resolves_a_registered_command_path(argv: list[str]) -> None:
    assert make_app().resolves(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["transaction", "add", "--posting", "x"],  # same group, not migrated
        ["transaction"],  # a group: the old CLI's help lists every command in it
        ["transaction", "--help"],
        [],
        ["--help"],
        ["--version"],
        ["report"],
        ["--format", "json", "report"],
        ["--db", "ledger.beancount", "transaction", "list"],  # an old root option first
    ],
)
def test_does_not_resolve_what_the_old_cli_still_answers(argv: list[str]) -> None:
    assert make_app().resolves(argv) is False


def test_resolves_refuses_a_string() -> None:
    with pytest.raises(TypeError, match="sequence of words"):
        make_app().resolves("transaction list")


def test_resolves_agrees_with_run() -> None:
    """What resolves says treaty runs is what run() routes to a command"""
    app = make_app()
    out = io.StringIO()
    code = app.run(["transaction", "list", "Assets"], stdout=out, env={}, isatty=False)
    assert code == 0 and json.loads(out.getvalue())["meta"]["command"] == "transaction.list"
    out = io.StringIO()
    err = io.StringIO()
    code = app.run(["transaction", "add"], stdout=out, stderr=err, env={}, isatty=False)
    assert code == 2 and json.loads(out.getvalue())["error"]["code"] == "ARG_ERROR"


# exec_fallback


def run_exec(
    app: App, lines: list[str], *flags: str, env: Mapping[str, str] | None = None
) -> tuple[int, list[dict], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        ["exec", *flags],
        stdin=io.StringIO("\n".join(lines) + "\n"),
        stdout=out,
        stderr=err,
        env=dict(env or {}),
        isatty=False,
    )
    envelopes = [json.loads(line) for line in out.getvalue().splitlines()]
    validator = spec_validator("response-envelope")
    for e in envelopes:
        validator.validate(e)
    return code, envelopes, err.getvalue()


BLOB = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODkwYWJjZGVm"


class OldDispatcher:
    """The old CLI's exec, keyed by _cmd"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, cmd: str, payload: Mapping[str, object]) -> object:
        self.calls.append((cmd, dict(payload)))
        if cmd == "transaction.add":
            return {"added": payload["postings"]}
        if cmd == "boom":
            raise RuntimeError(f"ledger locked by {payload.get('api_token')}")
        if cmd == "bad.output":
            return {1, 2}
        if cmd == "scalar":
            return "done"
        if cmd == "leak":
            return {"echo": f"token is {payload['api_token']}", "id": BLOB}
        if cmd == "exits":
            raise SystemExit(3)
        raise ParseError(f"unknown command {cmd}", code="UNKNOWN_COMMAND")


def test_mixed_plan_runs_in_one_exec() -> None:
    old = OldDispatcher()
    code, out, _ = run_exec(
        make_app(exec_fallback=old),
        [
            '{"_cmd": "transaction.list", "account": "Assets"}',
            '{"_cmd": "transaction.add", "postings": [{"account": "Assets", "amount": 5}], '
            '"_opts": {"dry": false}}',
        ],
    )
    assert code == 0
    assert out[0]["data"] == [{"account": "Assets", "amount": "1.00"}]
    assert "exec_fallback" not in out[0]["meta"]
    assert out[1]["ok"] is True and out[1]["data"] == {
        "added": [{"account": "Assets", "amount": 5}]
    }
    assert out[1]["meta"]["command"] == "transaction.add"
    assert out[1]["meta"]["_cmd"] == "transaction.add" and out[1]["meta"]["_line"] == 2
    assert out[1]["meta"]["exec_fallback"] is True
    # The fallback gets the line without _cmd; _opts stays for the old dispatcher
    assert old.calls == [
        (
            "transaction.add",
            {"postings": [{"account": "Assets", "amount": 5}], "_opts": {"dry": False}},
        )
    ]


def test_registered_and_redirected_commands_never_reach_the_fallback() -> None:
    old = OldDispatcher()
    code, out, _ = run_exec(
        make_app(exec_fallback=old), ['{"_cmd": "check"}', '{"_cmd": "verify"}'], "--ignore-errors"
    )
    assert out[0]["ok"] is True and out[1]["error"]["code"] == "REDIRECTED"
    assert old.calls == [] and code == 1


def test_unknown_command_without_a_fallback_is_unchanged() -> None:
    code, out, _ = run_exec(make_app(), ['{"_cmd": "transaction.add"}'])
    assert code == 1 and out[0]["error"]["code"] == "UNKNOWN_COMMAND"
    assert out[0]["meta"]["exit_code"] == 2 and "exec_fallback" not in out[0]["meta"]


def test_a_parse_error_from_the_fallback_answers_exit_2() -> None:
    code, out, _ = run_exec(make_app(exec_fallback=OldDispatcher()), ['{"_cmd": "nope"}'])
    assert code == 1
    error = out[0]["error"]
    assert error["code"] == "UNKNOWN_COMMAND" and error["phase"] == "validation"
    assert out[0]["meta"]["exit_code"] == 2


def test_an_exception_from_the_fallback_is_fallback_failed_and_redacted() -> None:
    code, out, err = run_exec(
        make_app(exec_fallback=OldDispatcher()),
        ['{"_cmd": "boom", "api_token": "s3cr3t-value"}', '{"_cmd": "exits"}'],
        "--ignore-errors",
    )
    assert code == 1
    error = out[0]["error"]
    assert error["code"] == "FALLBACK_FAILED" and out[0]["meta"]["exit_code"] == 1
    assert error["context"] == {"command": "boom", "exception": "RuntimeError"}
    assert "s3cr3t-value" not in error["message"] and "[REDACTED]" in error["message"]
    assert "s3cr3t-value" not in err and "RuntimeError" in err
    assert out[1]["error"]["code"] == "FALLBACK_FAILED"
    assert out[1]["error"]["context"]["exception"] == "SystemExit"


@pytest.mark.parametrize(
    ("raised", "signal_name", "exit_code"),
    [
        (KeyboardInterrupt(), "SIGINT", 130),
        (Cancelled(CancelSignal("SIGTERM", 143)), "SIGTERM", 143),
    ],
)
def test_a_cancellation_in_the_fallback_cancels_as_in_a_handler(
    raised: BaseException, signal_name: str, exit_code: int
) -> None:
    """Not FALLBACK_FAILED: the same CANCELLED line a migrated handler raising it gets"""

    def old(cmd: str, payload: Mapping[str, object]) -> object:
        raise raised

    app = make_app(exec_fallback=old)

    @app.command("interrupted", danger_level="safe", exit_codes=(), description="Interrupt")
    def interrupted(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        raise raised

    lines = ['{"_cmd": "legacy"}', '{"_cmd": "interrupted"}']
    _, out, _ = run_exec(app, lines, "--ignore-errors")
    fallback, handler = out[0], out[1]
    assert fallback["error"]["code"] == handler["error"]["code"] == "CANCELLED"
    assert fallback["error"]["context"] == {"signal": signal_name, "command": "legacy"}
    assert fallback["meta"]["exit_code"] == handler["meta"]["exit_code"] == exit_code
    assert fallback["meta"]["exec_fallback"] is True


@pytest.mark.parametrize("cmd", ["bad.output", "scalar"])
def test_a_result_that_is_not_json_data_is_invalid_output(cmd: str) -> None:
    code, out, _ = run_exec(make_app(exec_fallback=OldDispatcher()), [f'{{"_cmd": "{cmd}"}}'])
    assert code == 1 and out[0]["error"]["code"] == "INVALID_OUTPUT"
    assert out[0]["meta"]["exit_code"] == 1


def test_fallback_data_is_redacted_and_masked() -> None:
    code, out, _ = run_exec(
        make_app(exec_fallback=OldDispatcher()),
        ['{"_cmd": "leak", "api_token": "s3cr3t-value"}'],
    )
    assert code == 0
    data = out[0]["data"]
    assert data["echo"] == "token is [REDACTED]"
    assert data["id"] != BLOB
    assert [w["code"] for w in out[0]["warnings"]] == ["HIGH_ENTROPY_MASKED"]


def test_fallback_output_is_capped() -> None:
    def big(cmd: str, payload: Mapping[str, object]) -> object:
        return [{"n": "x" * 200} for _ in range(200)]

    code, out, _ = run_exec(
        make_app(exec_fallback=big), ['{"_cmd": "old.dump"}'], env={"BEAN_MAX_OUTPUT_BYTES": "8192"}
    )
    assert code == 0
    assert any(w["code"] == "FIELD_TRUNCATED" for w in out[0]["warnings"])
    assert len(out[0]["data"]) < 200


def test_fallback_lines_are_audit_logged_with_secrets_redacted(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    code, _, _ = run_exec(
        make_app(exec_fallback=OldDispatcher()),
        ['{"_cmd": "transaction.add", "postings": [], "password": "hunter22"}'],
        env={"BEAN_AUDIT_LOG": str(log)},
    )
    assert code == 0
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    spec_validator("audit-log-entry").validate(entry)
    assert entry["command"] == "transaction.add"
    assert entry["args"] == {"postings": [], "password": "[REDACTED]"}


def test_fallback_runs_every_time_in_a_session() -> None:
    """Treaty cannot tell what an unmigrated command changes, so it never dedups one"""
    old = OldDispatcher()
    line = '{"_cmd": "transaction.add", "postings": []}'
    run_exec(make_app(exec_fallback=old), [line, line], env={"BEAN_SESSION": "s1"})
    assert len(old.calls) == 2


def test_dry_run_plan_refuses_a_fallback_line() -> None:
    old = OldDispatcher()
    code, out, _ = run_exec(
        make_app(exec_fallback=old), ['{"_cmd": "transaction.add", "postings": []}'], "--dry-run"
    )
    assert code == 1 and out[0]["meta"]["exit_code"] == 2
    assert "cannot honor --dry-run" in out[0]["error"]["message"]
    assert old.calls == []


def test_exec_fallback_is_validated_at_registration() -> None:
    with pytest.raises(RegistrationError, match="callable"):
        App("bean", version="1.0.0", exec_fallback="old.exec")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="enable_exec"):
        App("bean", version="1.0.0", enable_exec=False, exec_fallback=OldDispatcher())


def test_manifest_lists_only_migrated_commands() -> None:
    commands = make_app(exec_fallback=OldDispatcher()).manifest()["commands"]
    assert isinstance(commands, dict)
    assert "transaction.list" in commands and "transaction.add" not in commands
