"""A missing required argument at a terminal names it as typed, with usage (#358)."""

import io
import json
from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True, slots=True)
class StrictArgs:
    count: int = Flag(description="Attempts")
    child: tuple[str, ...] = Arg(description="Passed to the child")


def test_a_strict_command_usage_puts_the_options_before_the_positionals() -> None:
    app = App("bean", version="1.0.0")

    @app.command(
        "wrap",
        description="Wrap",
        danger_level="safe",
        exit_codes=(),
        option_placement="strict",
    )
    def wrap(args: StrictArgs, ctx: Ctx) -> None:
        return None

    err = io.StringIO()
    code = app.run(
        ["wrap"], stdin=io.StringIO(), stdout=io.StringIO(), stderr=err, env={}, isatty=True
    )
    assert code == 2
    usage = err.getvalue().splitlines()[-2]
    assert usage == "usage: bean wrap --count <count> [options] <child>"
    # The usage line, followed, runs: strict takes every token from <child> on verbatim
    code = app.run(
        ["wrap", "--count", "1", "x"],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        env={},
        isatty=True,
    )
    assert code == 0


@dataclass(frozen=True, slots=True)
class StrictOptionalArgs:
    count: int = Flag(default=1, description="Attempts")
    child: tuple[str, ...] = Arg(default=(), description="Passed to the child")


def help_usage(app: App, command: str) -> list[str]:
    out = io.StringIO()
    code = app.run([command, "--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert code == 0
    return out.getvalue().splitlines()[0].split()


def runs(app: App, argv: list[str]) -> int:
    return app.run(
        argv, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(), env={}, isatty=True
    )


def test_a_strict_command_help_usage_puts_the_flags_before_the_positionals() -> None:
    app = App("bean", version="1.0.0")

    @app.command(
        "wrap",
        description="Wrap",
        danger_level="safe",
        exit_codes=(),
        option_placement="strict",
    )
    def wrap(args: StrictArgs, ctx: Ctx) -> None:
        return None

    @app.command(
        "pick",
        description="Pick",
        danger_level="safe",
        exit_codes=(),
        option_placement="strict",
    )
    def pick(args: StrictOptionalArgs, ctx: Ctx) -> None:
        return None

    usage = help_usage(app, "wrap")
    assert usage[:3] == ["bean", "wrap", "[flags]"]
    # The usage line, typed with a real flag for [flags] and values for the rest, runs
    typed = [{"[flags]": "--count=1"}.get(t, "x" if t.startswith("<") else t) for t in usage]
    assert runs(app, typed[1:]) == 0
    assert help_usage(app, "pick") == ["bean", "pick", "[flags]", "[child]"]
    assert runs(app, ["pick", "--count", "2", "x"]) == 0


def test_a_command_without_strict_placement_keeps_the_flags_last() -> None:
    _, help_text, _ = run(["login", "--help"])
    assert help_text.splitlines()[0] == "bean login <user> [flags]"


def test_a_strict_command_skill_puts_the_flags_before_the_positionals(tmp_path: Path) -> None:
    app = App("bean", version="1.0.0")

    @app.command(
        "wrap",
        description="Wrap",
        danger_level="safe",
        exit_codes=(),
        option_placement="strict",
    )
    def wrap(args: StrictArgs, ctx: Ctx) -> None:
        return None

    code = runs(app, ["generate-skills", "--output-dir", str(tmp_path)])
    assert code == 0
    skill = (tmp_path / "SKILL-wrap.md").read_text()
    lines = skill.splitlines()
    minimal = lines[lines.index("```bash") + 1]
    assert minimal == "bean wrap --count <count> <child>"
    # The minimal call, typed with values for the placeholders, runs
    typed = ["1" if t == "<count>" else "x" if t.startswith("<") else t for t in minimal.split()]
    assert runs(app, typed[1:]) == 0
    assert "`bean wrap --validate-only ...`" in skill
