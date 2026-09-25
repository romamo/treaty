"""Parsing, registration, and schema contracts that once failed silently"""

import io
import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, RegistrationError
from treaty._cli import cli
from treaty._scaffold import ProjectName, render
from treaty._scalars import ScalarRegistry
from treaty._schema import schema_for


@dataclass(frozen=True, slots=True)
class Name:
    name: str = Flag(default="a", pattern=r"[a-z]+", description="Lowercase name")


@dataclass(frozen=True, slots=True)
class Shift:
    n: int = Arg(description="Offset")


@dataclass(frozen=True, slots=True)
class Remove:
    target: str = Arg(description="Target")
    dry_run: bool = Flag(default=False, description="Preview")


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="Token")


def review_app() -> App:
    app = App("revctl", version="1")

    @app.command("name", description="Echo a name", supports_raw_payload=True)
    def name(args: Name, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    @app.command("shift", description="Echo an offset")
    def shift(args: Shift, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    @app.command(
        "rm",
        description="Remove",
        danger_level="destructive",
        supports_raw_payload=True,
        has_network_io=True,
    )
    def rm(args: Remove, ctx: Ctx) -> dict[str, object]:
        effect = "would_delete" if args.dry_run else "deleted"
        return {"effect": effect, "timeout_ms": ctx.timeout.milliseconds}

    @app.command("login", description="Log in")
    def login(args: Login, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def run(app: App, argv: list[str], stdin: str = "") -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = app.run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.parametrize("argv", [["bogus", "--help"], ["bogus", "--schema"]])
def test_unknown_command_with_help_or_schema_is_arg_error(argv: list[str]) -> None:
    code, [env] = run(review_app(), argv)
    assert code == 2 and env["error"]["context"]["argument"] == "bogus"


def test_pattern_applies_to_every_input_path() -> None:
    app = review_app()
    assert run(app, ["name", "--name", "ABC"])[0] == 2
    assert run(app, ["name", "--raw-payload", '{"name": "ABC"}'])[0] == 2
    _, [line] = run(app, ["exec"], '{"_cmd": "name", "name": "ABC"}\n')
    assert line["error"]["code"] == "ARG_ERROR"
    assert app.call("name", {"name": "ABC"}, env={}).exit_code == 2


def test_raw_payload_keeps_timeout_and_confirmation() -> None:
    payload = '{"target": "a", "timeout": 5, "confirm_destructive": true}'
    code, [env] = run(review_app(), ["rm", "--raw-payload", payload])
    assert code == 0 and env["data"] == {"effect": "deleted", "timeout_ms": 5000}


def test_negative_number_is_a_positional_value() -> None:
    code, [env] = run(review_app(), ["shift", "-5"])
    assert code == 0 and env["data"] == {"n": -5}


def test_failed_secret_source_is_reported_once() -> None:
    code, [env] = run(review_app(), ["login", "--api-token-from-env", "UNSET_VAR"])
    assert code == 2 and len(env["error"]["errors"]) == 1


@dataclass(frozen=True, slots=True)
class TimeoutField:
    timeout: int = Flag(default=1, description="Collides with --timeout")


@dataclass(frozen=True, slots=True)
class RawPayloadField:
    raw_payload: str = Flag(default="", description="Collides with --raw-payload")


@dataclass(frozen=True, slots=True)
class NoStreamField:
    no_stream: bool = Flag(default=False, description="Collides with --no-stream")


def test_field_shadowed_by_a_framework_flag_is_rejected() -> None:
    app = App("t", version="1")
    with pytest.raises(RegistrationError, match="supplied by the framework"):

        @app.command("a", description="A", has_network_io=True)
        def a(args: TimeoutField, ctx: Ctx) -> dict[str, int]:
            return {}

    with pytest.raises(RegistrationError, match="supplied by the framework"):

        @app.command("b", description="B", supports_raw_payload=True)
        def b(args: RawPayloadField, ctx: Ctx) -> dict[str, int]:
            return {}

    with pytest.raises(RegistrationError, match="supplied by the framework"):

        @app.command("c", description="C", streaming=True)
        def c(args: NoStreamField, ctx: Ctx) -> Iterator[dict[str, int]]:
            yield {}


def test_exit_factory_behaves_like_an_object() -> None:
    assert not hasattr(Exit, "__wrapped__")
    with pytest.raises(AttributeError):
        _ = Exit.not_upper_case


def test_output_schema_types_match_emitted_values() -> None:
    class Level(IntEnum):
        LOW = 1
        HIGH = 2

    scalars = ScalarRegistry()
    assert schema_for(Level, scalars) == {"type": "integer", "enum": [1, 2]}
    pair = schema_for(tuple[int, str], scalars)
    assert pair["items"] == [{"type": "integer"}, {"type": "string"}] and pair["maxItems"] == 2


@pytest.mark.parametrize("source", [r"C:\Users\me\treaty", 'odd"path'])
def test_scaffold_pyproject_is_valid_toml_for_any_source(source: str) -> None:
    files = render(ProjectName("demo"), source)
    parsed = tomllib.loads(files["pyproject.toml"])
    assert parsed["tool"]["uv"]["sources"]["treaty"]["path"] == source


def test_init_into_a_file_is_a_conflict(tmp_path: Path) -> None:
    target = tmp_path / "afile"
    target.write_text("x")
    code, [env] = run(cli, ["init", "demo", "--directory", str(target), "--dry-run"])
    assert code != 0 and env["error"]["code"] == "CONFLICT"
