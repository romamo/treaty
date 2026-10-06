"""A missing required argument at a terminal names it as typed, with usage (#358)."""

import io
import json
from dataclasses import dataclass
from typing import Literal

from treaty import App, Arg, Ctx, Flag


@dataclass(frozen=True, slots=True)
class CreateArgs:
    name: str = Flag(short="n", description="Account name to create")
    currency: str = Flag(default="USD", description="Currency")


@dataclass(frozen=True, slots=True)
class LoginArgs:
    user: str = Arg(description="User to log in")
    password: str = Flag(description="Password")
    count: int = Flag(description="Attempts")
    api_token: str = Flag(default="", description="Token")
    mode: Literal["fast", "slow"] = Flag(default="fast", description="Mode")


def make_app() -> App:
    app = App("bean", version="1.0.0")

    @app.command("account.create", description="Create", danger_level="safe", exit_codes=())
    def create(args: CreateArgs, ctx: Ctx) -> None:
        return None

    @app.command("login", description="Log in", danger_level="safe", exit_codes=())
    def login(args: LoginArgs, ctx: Ctx) -> None:
        return None

    return app


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(
        argv, stdin=io.StringIO(), stdout=out, stderr=err, env=env or {}, isatty=True
    )
    return code, out.getvalue(), err.getvalue()


def test_a_missing_flag_names_its_spelling_help_row_usage_and_help() -> None:
    code, _, err = run(["account", "create"])
    assert code == 2
    assert err.splitlines() == [
        "bean: ARG_ERROR: Missing required option --name/-n",
        "  --name, -n  Account name to create (required)",
        "usage: bean account create --name <name> [options]",
        "Run 'bean account create --help' for all options.",
    ]


def test_the_row_is_the_one_help_prints() -> None:
    _, help_text, _ = run(["account", "create", "--help"])
    assert "  --name, -n" in help_text and "Account name to create (required)" in help_text
    _, _, err = run(["account", "create"])
    row = err.splitlines()[1].split(maxsplit=3)
    assert row[:3] == ["--name,", "-n", "Account"]


def test_a_positional_is_named_in_angle_brackets_and_a_secret_by_its_sources() -> None:
    code, _, err = run(["login"], env={"BEAN_API_TOKEN": "s3cret-token-value-1"})
    assert code == 2
    lines = err.splitlines()
    assert lines[0] == (
        "bean: ARG_ERROR: Missing required arguments <user>, "
        "--password-from-env/--password-from-file, --count"
    )
    assert "  user                       User to log in" in lines
    assert "  --count                    Attempts (required)" in lines
    assert lines[-2:] == [
        "usage: bean login <user> --password-from-env <var> --count <count> [options]",
        "Run 'bean login --help' for all options.",
    ]
    assert "s3cret-token-value-1" not in err
    assert "[" not in lines[0] and "'" not in lines[0]


def test_several_errors_keep_their_list_then_rows_hint_and_usage() -> None:
    code, _, err = run(["login", "alice", "--count", "x"])
    assert code == 2
    lines = err.splitlines()
    assert lines[:4] == [
        "bean: ARG_ERROR: Validation failed: 2 errors.",
        "  - count: 'count' expects an integer.",
        "  - Missing required option --password-from-env/--password-from-file",
        "  --password-from-env VAR    Password: read from $VAR (required)",
    ]
    assert lines[-3:] == [
        "hint: fix every entry in errors, then reissue once",
        "usage: bean login <user> --password-from-env <var> --count <count> [options]",
        "Run 'bean login --help' for all options.",
    ]


def test_json_keeps_the_field_names_code_and_exit_code() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(
        ["login", "--format", "json"], stdin=io.StringIO(), stdout=out, stderr=err, env={}
    )
    assert code == 2
    error = json.loads(out.getvalue())["error"]
    assert error["code"] == "ARG_ERROR"
    assert error["context"] == {
        "missing": ["user", "password-from-env", "count"],
        "command": "login",
    }
    assert "usage:" not in out.getvalue() + err.getvalue()


def test_context_values_print_as_text_not_a_python_repr() -> None:
    argv = ["login", "alice", "--password-from-env", "PW", "--count", "1", "--mode", "x"]
    code, _, err = run(argv, env={"PW": "pw-value-1234"})
    assert code == 2
    assert "  allowed: fast, slow" in err.splitlines()
    assert "['" not in err
