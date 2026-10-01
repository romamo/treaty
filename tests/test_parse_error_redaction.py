"""A phase 1 error written by user code never carries a secret value it quotes (#165).

The args ``__post_init__`` sees every secret in the clear, and its ``ParseError`` or
``InvalidValue`` may quote one; the envelope is redacted of the run's secrets wherever it
goes, as a handler's error is.
"""

import io
import json
from dataclasses import dataclass
from pathlib import Path

from treaty import App, AuditLog, Ctx, Flag, ParseError
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
