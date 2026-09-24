import io
import json
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError


def run(app: App, argv: list[str], *, isatty: bool = False, env: dict[str, str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=isatty)
    return code, out.getvalue(), err.getvalue()


def run_json(app: App, argv: list[str]) -> tuple[int, dict]:
    code, out, err = run(app, argv)
    assert err == ""
    envelope = json.loads(out)
    spec_validator("response-envelope").validate(envelope)
    assert envelope["meta"]["exit_code"] == code
    return code, envelope


def test_success_envelope(app: App) -> None:
    code, env = run_json(
        app, ["deploy", "rollback", "api", "-t", "1.3.9", "--tags", "a", "--tags=b", "--dry-run"]
    )
    assert code == 0 and env["ok"] is True and env["error"] is None
    assert env["data"] == {
        "service": "api",
        "release": "1.3.9",
        "strategy": "safe",
        "replicas": 1,
        "tags": ["a", "b"],
        "dry_run": True,
    }


def test_positional_may_be_given_as_flag(app: App) -> None:
    code, env = run_json(
        app,
        [
            "deploy",
            "rollback",
            "--service=api",
            "--replicas",
            "3",
            "--strategy",
            "fast",
            "--confirm-destructive",
        ],
    )
    assert code == 0 and env["data"]["replicas"] == 3 and env["data"]["strategy"] == "fast"


def test_list_output(app: App) -> None:
    code, env = run_json(app, ["deploy", "status", "api"])
    assert code == 0 and env["data"] == [{"service": "api", "release": "1.4.0"}]


def test_declared_exit_code(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "locked"])
    assert code == 79 and env["ok"] is False and env["data"] is None
    assert env["error"]["code"] == "DEPLOY_CONFLICT" and env["error"]["retryable"] is False
    assert env["error"]["context"] == {"active": "1.4.0"} and env["error"]["phase"] == "execution"


def test_retryable_exit_carries_retry_after(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "slow"])
    assert (
        code == 80 and env["error"]["retryable"] is True and env["error"]["retry_after_ms"] == 500
    )


def test_undeclared_exit_becomes_general_error(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "buggy"])
    assert code == 1 and env["error"]["code"] == "UNDECLARED_EXIT_CODE"
    assert env["error"]["context"]["raised"] == "NOT_DECLARED"


@pytest.mark.parametrize(
    ("argv", "missing_or_flag"),
    [
        (["deploy", "rollback"], "missing"),
        (["deploy", "rollback", "api", "--nope"], "flag"),
        (["deploy", "rollback", "api", "--replicas", "many"], "flag"),
        (["deploy", "rollback", "api", "--strategy", "yolo"], "flag"),
        (["deploy", "rollback", "api", "extra"], "argument"),
    ],
)
def test_parse_errors_are_arg_error(app: App, argv: list[str], missing_or_flag: str) -> None:
    code, env = run_json(app, argv)
    assert (
        code == 2 and env["error"]["code"] == "ARG_ERROR" and env["error"]["phase"] == "validation"
    )
    assert missing_or_flag in env["error"]["context"]


def test_unknown_command_lists_available(app: App) -> None:
    code, env = run_json(app, ["deploy", "explode"])
    assert code == 2 and "deploy.rollback" in env["error"]["context"]["available"]


def test_no_args_json_mode_returns_manifest(app: App) -> None:
    code, env = run_json(app, [])
    assert code == 0 and "commands" in env["data"]


def test_group_help_json_scopes_to_subtree(app: App) -> None:
    code, env = run_json(app, ["deploy", "--help"])
    assert code == 0
    assert set(env["data"]["commands"]) == {"deploy.rollback", "deploy.status"}
    code, env = run_json(app, ["deploy"])
    assert code == 0 and "manifest" not in env["data"]["commands"]


def test_human_mode_help_and_errors(app: App) -> None:
    code, out, err = run(app, [], isatty=True)
    assert code == 0 and "Command groups" in out and "deploy" in out
    code, out, err = run(app, ["deploy", "rollback", "--help"], isatty=True)
    assert code == 0 and "--dry-run" in out and "Danger level: destructive" in out
    code, out, err = run(app, ["deploy", "rollback", "locked"], isatty=True)
    assert code == 79 and out == "" and "DEPLOY_CONFLICT" in err


def test_format_flag_overrides_tty(app: App) -> None:
    code, out, _ = run(app, ["--format", "json", "version"], isatty=True)
    assert json.loads(out)["data"] == {"name": "deployctl", "version": "1.4.0"}
    code, out, _ = run(app, ["--format=yaml", "version"], isatty=True)
    assert code == 2 and json.loads(out)["error"]["context"]["value"] == "yaml"


def test_root_version_flag_aliases_version_command(app: App) -> None:
    code, env = run_json(app, ["--version"])
    assert code == 0 and env["data"] == {"name": "deployctl", "version": "1.4.0"}
    code, out, _ = run(app, ["--version"], isatty=True)
    assert code == 0 and '"version": "1.4.0"' in out


def test_version_flag_not_aliased_below_root(app: App) -> None:
    code, env = run_json(app, ["deploy", "--version"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"


def test_root_help_lists_version_flag(app: App) -> None:
    _, out, _ = run(app, ["--help"], isatty=True)
    assert "--version" in out


def test_ci_env_forces_json(app: App) -> None:
    code, out, _ = run(app, ["version"], isatty=True, env={"CI": "1"})
    assert json.loads(out)["ok"] is True


# Registration-time invariants


def test_retryable_requires_no_side_effects() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="side_effects"):
        app.exit_code("BOOM", 90, description="Boom", retryable=True, side_effects="partial")


def test_command_specific_codes_must_be_in_range() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="79..125"):
        app.exit_code("BOOM", 3, description="Boom", retryable=False, side_effects="none")


def test_destructive_requires_dry_run() -> None:
    app = App("x", version="1")

    @dataclass(frozen=True, slots=True)
    class Args:
        name: str = Arg(description="Name")

    with pytest.raises(RegistrationError, match="dry_run"):

        @app.command("nuke", description="Delete", danger_level="destructive")
        def nuke(args: Args, ctx: Ctx) -> dict[str, str]:
            return {}


def test_undeclared_exit_code_name_rejected_at_registration() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="not registered"):

        @app.command("go", description="Go", exit_codes=["NOPE"])
        def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


def test_handler_signature_checked() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="return annotation"):

        @app.command("go", description="Go")
        def go(args: NoArgs, ctx: Ctx):  # type: ignore[no-untyped-def]
            return {}

    with pytest.raises(RegistrationError, match="object, array, or null"):

        @app.command("go2", description="Go")
        def go2(args: NoArgs, ctx: Ctx) -> str:
            return ""


def test_duplicate_path_and_builtin_collision() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="already registered"):

        @app.command("version", description="Mine")
        def version(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


def test_field_without_marker_rejected() -> None:
    app = App("x", version="1")

    @dataclass(frozen=True, slots=True)
    class Args:
        name: str = Flag(description="ok")
        other: int = 3

    with pytest.raises(RegistrationError, match="Flag"):

        @app.command("go", description="Go")
        def go(args: Args, ctx: Ctx) -> dict[str, str]:
            return {}


# Destructive confirmation (REQ-O-021)


def test_destructive_without_confirmation_previews_and_exits_2(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "api"])
    assert code == 2 and env["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert env["error"]["phase"] == "validation" and env["data"]["dry_run"] is True


def test_destructive_with_confirmation_applies(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "api", "--confirm-destructive"])
    assert code == 0 and env["data"]["dry_run"] is False


def test_explicit_dry_run_is_success(app: App) -> None:
    code, env = run_json(app, ["deploy", "rollback", "api", "--dry-run"])
    assert code == 0 and env["data"]["dry_run"] is True


def test_confirm_flag_rejected_on_safe_command(app: App) -> None:
    code, env = run_json(app, ["deploy", "status", "api", "--confirm-destructive"])
    assert code == 2 and env["error"]["context"]["flag"] == "confirm-destructive"


def test_manifest_advertises_confirm_flag(app: App) -> None:
    flags = app.manifest()["commands"]["deploy.rollback"]["flags"]
    assert flags["confirm-destructive"]["type"] == "boolean"


def test_handler_raised_parse_error_is_validation_failure() -> None:
    from treaty import ParseError

    app = App("x", version="1")

    @app.command("check", description="Validates its own input")
    def check(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise ParseError("bad input", context={"field": "x"})

    out = io.StringIO()
    code = app.run(["check"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    env = json.loads(out.getvalue())
    assert code == 2 and env["error"]["phase"] == "validation"
    assert env["error"]["context"] == {"field": "x"}
