"""A phase 1 error written by user code never carries a secret value it quotes (#165).

The args ``__post_init__`` sees every secret in the clear, and its ``ParseError`` or
``InvalidValue`` may quote one; the envelope is redacted of the run's secrets wherever it
goes, as a handler's error is.
"""

import io
import json
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Arg, AuditLog, Ctx, Flag, ParseError
from treaty._mcp import call_tool, tool_entries
from treaty._values import InvalidValue

SECRET = "Zq7-supersecret-value-91"
ENV = {"PW": SECRET}


@dataclass(frozen=True, slots=True)
class GoArgs:
    password: str = Flag(description="Password", secret=True)
    mode: str = Flag(default="plain", description="How the check fails")

    def __post_init__(self) -> None:
        if self.password.startswith("ok-"):
            return
        if self.mode == "invalid":
            raise InvalidValue(f"password {self.password} is not a valid password")
        if self.mode == "many":
            raise ParseError.combine(
                [
                    ParseError(f"first {self.password}", context={"field": "password"}),
                    ParseError(
                        "second",
                        context={"field": "mode", "seen": [{"deep": (self.password,)}]},
                        suggestion=f"not {self.password}",
                    ),
                ]
            )
        if self.mode == "object":
            raise ParseError("refused", context={"args": self})
        if self.mode == "deep":
            nested: object = self.password
            for _ in range(5000):
                nested = [nested]
            raise ParseError("too deep", context={"nested": nested})
        raise ParseError(
            f"password {self.password} must start with ok-",
            context={"field": "password", "given": self.password},
            suggestion=f"use ok-{self.password}",
        )


@dataclass(frozen=True, slots=True)
class NamedArgs:
    api_token: str = Flag(description="Inferred a secret from its name")

    def __post_init__(self) -> None:
        raise ParseError(f"token {self.api_token} is revoked", context={"field": "api_token"})


def probe_app(*, audit_log: AuditLog | None = None) -> App:
    app = App("probe", version="1.0.0", audit_log=audit_log)

    @app.command(
        "go", description="Go", danger_level="safe", exit_codes=(), supports_raw_payload=True
    )
    def go(args: GoArgs, ctx: Ctx) -> dict[str, str]:
        return {"ok": "yes"}

    @app.command("named", description="Named", danger_level="safe", exit_codes=())
    def named(args: NamedArgs, ctx: Ctx) -> dict[str, str]:
        return {"ok": "yes"}

    return app


def run(
    argv: list[str], *, app: App | None = None, stdin: str = "", env: dict[str, str] = ENV
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = (app or probe_app()).run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=env, isatty=False
    )
    return code, out.getvalue(), err.getvalue()


def test_the_post_init_message_errors_context_and_suggestion_are_redacted() -> None:
    code, out, err = run(["go", "--password-from-env", "PW", "--format", "json"])
    assert code == 2 and SECRET not in out + err
    error = json.loads(out)["error"]
    assert error["message"] == "Password [REDACTED] must start with ok-"
    assert error["context"] == {"field": "password", "given": "[REDACTED]"}
    assert error["suggestion"] == "use ok-[REDACTED]"
    assert error["errors"][0]["message"] == "Password [REDACTED] must start with ok-"
    assert error["errors"][0]["context"]["given"] == "[REDACTED]"


def test_every_collected_error_is_redacted() -> None:
    argv = ["go", "--password-from-env", "PW", "--mode", "many", "--format", "json"]
    code, out, _ = run(argv)
    assert code == 2 and SECRET not in out
    errors = json.loads(out)["error"]["errors"]
    assert [e["message"].rstrip(".") for e in errors] == ["First [REDACTED]", "Second"]
    assert errors[1]["context"]["seen"] == [{"deep": ["[REDACTED]"]}]
    assert errors[1]["suggestion"] == "not [REDACTED]"


def test_an_invalid_value_from_post_init_is_redacted() -> None:
    argv = ["go", "--password-from-env", "PW", "--mode", "invalid", "--format", "json"]
    code, out, _ = run(argv)
    assert code == 2 and SECRET not in out
    assert json.loads(out)["error"]["message"].startswith("Password [REDACTED] is not a valid")


def test_an_object_in_context_is_redacted_as_its_text() -> None:
    argv = ["go", "--password-from-env", "PW", "--mode", "object", "--format", "json"]
    code, out, _ = run(argv)
    assert code == 2 and SECRET not in out


def test_a_deeply_nested_context_is_redacted_without_recursing_into_it() -> None:
    argv = ["go", "--password-from-env", "PW", "--mode", "deep", "--format", "json"]
    code, out, _ = run(argv)
    assert code == 2 and SECRET not in out
    assert json.loads(out)["error"]["message"].startswith("Too deep")


def test_a_secret_inferred_from_its_name_is_redacted() -> None:
    code, out, _ = run(["named", "--api-token-from-env", "PW", "--format", "json"])
    assert code == 2 and SECRET not in out
    assert json.loads(out)["error"]["message"].startswith("Token [REDACTED] is revoked")


def test_the_plain_rendering_on_stderr_is_redacted() -> None:
    code, out, err = run(["go", "--password-from-env", "PW", "--format", "plain"])
    assert code == 2 and SECRET not in out + err
    assert "[REDACTED]" in out + err


def test_validate_only_is_redacted() -> None:
    argv = ["go", "--password-from-env", "PW", "--validate-only", "--format", "json"]
    code, out, _ = run(argv)
    assert code == 2 and SECRET not in out


def test_raw_payload_is_redacted() -> None:
    payload = json.dumps({"password_from_env": "PW"})
    code, out, _ = run(["go", "--raw-payload", payload, "--format", "json"])
    assert code == 2 and SECRET not in out


def test_an_exec_line_is_redacted() -> None:
    line = json.dumps({"_cmd": "go", "password_from_env": "PW"})
    code, out, err = run(["exec"], stdin=line + "\n")
    assert code != 0 and SECRET not in out + err
    assert "Password [REDACTED] must start with ok-" in out


def test_app_call_is_redacted() -> None:
    envelope = probe_app().call("go", {"password_from_env": "PW"}, env=ENV)
    assert envelope.error is not None and envelope.exit_code == 2
    assert envelope.error.message == "Password [REDACTED] must start with ok-"
    assert SECRET not in json.dumps(envelope.to_json())


def test_an_mcp_tool_call_is_redacted() -> None:
    app = probe_app()
    entries = {e.name: e for e in tool_entries(app)}
    envelope = call_tool(app, entries, "go", {"password_from_env": "PW"}, env=ENV)
    assert envelope.error is not None and envelope.exit_code == 2
    assert SECRET not in json.dumps(envelope.to_json())


def test_the_audit_log_entry_is_redacted(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    code, out, _ = run(
        ["go", "--password-from-env", "PW", "--format", "json"],
        app=probe_app(audit_log=AuditLog(path=log)),
    )
    assert code == 2 and SECRET not in out
    # The entry records the failure without its message, and the secret field redacted
    entry = json.loads(log.read_text())
    assert entry["exit_code"] == 2 and SECRET not in json.dumps(entry)


def test_treaty_s_own_errors_are_unchanged() -> None:
    code, out, _ = run(["go", "--format", "json"], env={})
    assert code == 2
    assert json.loads(out)["error"]["message"] == "Missing required: password-from-env"


def test_a_non_secret_value_stays_quoted() -> None:
    code, out, _ = run(["go", "--password-from-env", "PW", "--mode", "x", "--format", "json"])
    assert code == 2
    assert json.loads(out)["error"]["context"]["field"] == "password"
    good = run(["go", "--password-from-env", "PW", "--format", "json"], env={"PW": "ok-1234"})
    assert good[0] == 0


@dataclass(frozen=True, slots=True)
class WipeArgs:
    password: str = Flag(description="Password", secret=True)
    dry_run: bool = Flag(default=False, description="Preview only")

    def __post_init__(self) -> None:
        if self.dry_run:
            raise ParseError(f"cannot preview with {self.password}", context={"pw": self.password})


@dataclass(frozen=True, slots=True)
class PruneArgs:
    target: Path = Arg(description="What to prune, relative to the project")
    password: str = Flag(description="Password", secret=True)

    def __post_init__(self) -> None:
        if self.target.is_absolute():
            raise ParseError(f"{self.target} is outside the project for {self.password}")


@dataclass(frozen=True, slots=True)
class Leaf:
    text: str

    def __post_init__(self) -> None:
        if self.text.startswith("crash-"):
            raise ValueError(f"cannot read {self.text.removeprefix('crash-')}")
        if self.text.startswith("Zq7"):
            raise ParseError(f"note {self.text} looks like a credential")


@dataclass(frozen=True, slots=True)
class Branch:
    leaf: Leaf


@dataclass(frozen=True, slots=True)
class NoteArgs:
    password: str = Flag(description="Password", secret=True)
    note: Branch | None = Flag(default=None, description="A nested note")


def rebuild_app() -> App:
    app = App("probe", version="1.0.0")

    @app.command("wipe", description="Wipe", danger_level="destructive", exit_codes=())
    def wipe(args: WipeArgs, ctx: Ctx) -> dict[str, str]:
        return {"ok": "yes"}

    @app.command("prune", description="Prune", danger_level="safe", exit_codes=())
    def prune(args: PruneArgs, ctx: Ctx) -> dict[str, str]:
        return {"ok": "yes"}

    @app.command("note", description="Note", danger_level="safe", exit_codes=())
    def note(args: NoteArgs, ctx: Ctx) -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_issue_repro_a_forced_dry_run_rebuild_is_redacted() -> None:
    for fmt in ("json", "plain"):
        code, out, err = run(
            ["wipe", "--password-from-env", "PW", "--format", fmt], app=rebuild_app()
        )
        assert code == 2 and SECRET not in out + err
        assert "[REDACTED]" in out + err
    code, out, _ = run(["wipe", "--password-from-env", "PW", "--format", "json"], app=rebuild_app())
    error = json.loads(out)["error"]
    assert error["message"].startswith("Cannot preview with [REDACTED]")
    assert error["errors"][0]["context"] == {"pw": "[REDACTED]"}
    assert "--confirm-destructive" in error["suggestion"]


def test_a_forced_dry_run_rebuild_under_validate_only_is_redacted() -> None:
    argv = ["wipe", "--password-from-env", "PW", "--validate-only", "--format", "json"]
    code, out, _ = run(argv, app=rebuild_app())
    assert code == 2 and SECRET not in out and "[REDACTED]" in out


def test_a_forced_dry_run_rebuild_in_exec_call_and_mcp_is_redacted() -> None:
    line = json.dumps({"_cmd": "wipe", "password_from_env": "PW"})
    code, out, err = run(["exec"], app=rebuild_app(), stdin=line + "\n")
    assert code != 0 and SECRET not in out + err and "[REDACTED]" in out
    envelope = rebuild_app().call("wipe", {"password_from_env": "PW"}, env=ENV)
    assert envelope.error is not None and envelope.exit_code == 2
    assert envelope.error.message.startswith("Cannot preview with [REDACTED]")
    assert SECRET not in json.dumps(envelope.to_json())
    app = rebuild_app()
    entries = {e.name: e for e in tool_entries(app)}
    envelope = call_tool(app, entries, "wipe", {"password_from_env": "PW"}, env=ENV)
    assert envelope.exit_code == 2 and SECRET not in json.dumps(envelope.to_json())


def test_the_cwd_rebuild_is_redacted(tmp_path: Path) -> None:
    argv = ["prune", "cache", "--password-from-env", "PW", "--cwd", str(tmp_path)]
    code, out, err = run([*argv, "--format", "json"], app=rebuild_app())
    assert code == 2 and SECRET not in out + err
    assert json.loads(out)["error"]["message"].rstrip(".").endswith("for [REDACTED]")
    line = json.dumps({"_cmd": "prune", "target": "cache", "password_from_env": "PW"})
    code, out, err = run(["exec", "--cwd", str(tmp_path)], app=rebuild_app(), stdin=line + "\n")
    assert code != 0 and SECRET not in out + err and "[REDACTED]" in out


def test_a_nested_object_post_init_quoting_a_secret_is_redacted() -> None:
    note = json.dumps({"leaf": {"text": SECRET}})
    argv = ["note", "--password-from-env", "PW", "--note", note, "--format", "json"]
    code, out, err = run(argv, app=rebuild_app())
    assert code == 2 and SECRET not in out + err
    item = json.loads(out)["error"]["errors"][0]
    assert item["message"].startswith("Note [REDACTED] looks like")
    assert item["field"] == "note.leaf"
    envelope = rebuild_app().call(
        "note", {"password_from_env": "PW", "note": {"leaf": {"text": SECRET}}}, env=ENV
    )
    assert envelope.exit_code == 2 and SECRET not in json.dumps(envelope.to_json())


def test_a_nested_object_crash_quoting_a_secret_is_redacted() -> None:
    """An object's __post_init__ bug is reported once the secrets are read, whichever
    order the flags came in, so the crash report is redacted of them"""
    note = json.dumps({"leaf": {"text": f"crash-{SECRET}"}})
    for argv in (
        ["note", "--password-from-env", "PW", "--note", note],
        ["note", "--note", note, "--password-from-env", "PW"],
    ):
        code, out, err = run([*argv, "--format", "json"], app=rebuild_app())
        assert code == 1 and SECRET not in out + err
        error = json.loads(out)["error"]
        assert error["code"] == "HANDLER_CRASHED" and "[REDACTED]" in error["message"]
    arguments = {"password_from_env": "PW", "note": {"leaf": {"text": f"crash-{SECRET}"}}}
    envelope = rebuild_app().call("note", arguments, env=ENV)
    assert envelope.exit_code == 1 and SECRET not in json.dumps(envelope.to_json())
    line = json.dumps({"_cmd": "note", **arguments})
    code, out, err = run(["exec"], app=rebuild_app(), stdin=line + "\n")
    assert code != 0 and SECRET not in out + err and "HANDLER_CRASHED" in out
