"""Parsing, registration, and schema contracts that once failed silently"""

import io
import json
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from pathlib import Path

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, RegistrationError
from treaty._cli import cli
from treaty._profile import argument_order_for
from treaty._scaffold import ProjectName, render
from treaty._scalars import ScalarRegistry
from treaty._schema import schema_for, to_jsonable


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


# Second review round


@dataclass(frozen=True, slots=True)
class Copy:
    src: str = Arg(description="Source")
    dst: str = Arg(description="Destination")


@dataclass(frozen=True, slots=True)
class Ratio:
    r: float = Flag(default=0.5, description="Ratio")


@dataclass(frozen=True, slots=True)
class Count:
    n: int = Flag(default=1, pattern=r"[1-9]\d*", description="Positive count")


def round_two_app() -> App:
    app = App("r2", version="1")

    @dataclass(frozen=True, slots=True)
    class Out:
        out: Path = Flag(default=Path("out.json"), description="Output file")

    @app.command("write", description="Path default")
    def write(args: Out, ctx: Ctx) -> dict[str, str]:
        return {"out": str(args.out)}

    @app.command("cp", description="Copy")
    def cp(args: Copy, ctx: Ctx) -> dict[str, str]:
        return {"src": args.src, "dst": args.dst}

    @app.command("ratio", description="Ratio")
    def ratio(args: Ratio, ctx: Ctx) -> dict[str, float]:
        return {"r": args.r}

    @app.command("nan", description="Returns NaN")
    def nan(args: Ratio, ctx: Ctx) -> dict[str, float]:
        return {"r": float("nan")}

    @app.command("quit", description="Calls sys.exit")
    def quit_(args: Ratio, ctx: Ctx) -> dict[str, float]:
        raise SystemExit(3)

    @app.command("count", description="Count")
    def count(args: Count, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    return app


def test_path_default_is_listed_in_the_manifest() -> None:
    code, [env] = run(round_two_app(), ["manifest"])
    assert code == 0
    assert env["data"]["commands"]["write"]["flags"]["out"]["default"] == "out.json"
    assert run(round_two_app(), ["write", "--schema"])[0] == 0


def test_unlistable_default_fails_registration() -> None:
    app = App("t", version="1")

    @dataclass(frozen=True, slots=True)
    class Odd:
        when: float = Flag(default=float("inf"), description="Never")

    with pytest.raises(RegistrationError, match="finite"):

        @app.command("odd", description="Odd")
        def odd(args: Odd, ctx: Ctx) -> dict[str, str]:
            return {}


def test_positional_given_by_flag_leaves_the_rest_in_order() -> None:
    for argv in (["cp", "--src", "a", "b"], ["cp", "b", "--src", "a"]):
        code, [env] = run(round_two_app(), argv)
        assert code == 0 and env["data"] == {"src": "a", "dst": "b"}


@pytest.mark.parametrize("value", ["nan", "inf", "-Infinity"])
def test_non_finite_numbers_are_rejected(value: str) -> None:
    assert run(round_two_app(), ["ratio", "--r", value])[0] == 2


def test_non_finite_output_is_invalid_output() -> None:
    code, [env] = run(round_two_app(), ["nan"])
    assert code == 1 and env["error"]["code"] == "INVALID_OUTPUT"


def test_sys_exit_in_a_handler_still_writes_an_envelope() -> None:
    code, [env] = run(round_two_app(), ["quit"])
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"


def test_pattern_applies_to_json_integers() -> None:
    envelope = round_two_app().call("count", {"n": 0}, env={})
    assert envelope.exit_code == 2


def test_exec_line_conflicts_and_plan_dry_run() -> None:
    app = round_two_app()
    _, [line] = run(app, ["exec"], '{"_cmd": "count", "n": 3, "_opts": {"n": 5}}\n')
    assert line["error"]["code"] == "ARG_ERROR"
    code, [line] = run(
        review_app(), ["exec", "--dry-run"], '{"_cmd": "rm", "target": "a", "dry-run": false}\n'
    )
    assert code == 0 and line["data"]["effect"] == "would_delete"


def test_registration_rejects_positional_layouts_the_parser_cannot_serve() -> None:
    @dataclass(frozen=True, slots=True)
    class Greedy:
        files: tuple[str, ...] = Arg(description="Files")
        dest: str = Arg(description="Destination")

    @dataclass(frozen=True, slots=True)
    class Capital:
        Name: str = Arg(description="Name")

    app = App("t", version="1")
    for args_type, match in ((Greedy, "only the last positional"), (Capital, "lowercase")):
        with pytest.raises(RegistrationError, match=match):
            app.command(f"c{len(match)}", description="C")(
                _handler_for(args_type)  # type: ignore[arg-type]
            )


def _handler_for(args_type: type) -> object:
    def handler(args: args_type, ctx: Ctx) -> dict[str, str]:  # type: ignore[valid-type]
        return {}

    return handler


def test_boolean_named_stream_is_rejected_on_streaming_commands() -> None:
    @dataclass(frozen=True, slots=True)
    class Tail:
        stream: bool = Flag(default=True, description="Follow")

    app = App("t", version="1")
    with pytest.raises(RegistrationError, match="supplied by the framework"):

        @app.command("tail", description="Tail", streaming=True)
        def tail(args: Tail, ctx: Ctx) -> Iterator[dict[str, int]]:
            yield {}


def test_same_secret_source_twice_is_accepted() -> None:
    app = review_app()
    out = io.StringIO()
    code = app.run(
        ["login", "--api-token-from-env", "T", "--api-token-from-env", "T"],
        stdout=out,
        stderr=io.StringIO(),
        env={"T": "x"},
        isatty=False,
    )
    assert code == 0, out.getvalue()


def test_flag_enums_and_str_subclass_scalars_serialize_as_declared() -> None:
    class Perm(IntFlag):
        READ = 1
        WRITE = 2

    class Slug(str):
        pass

    scalars = ScalarRegistry()
    assert schema_for(Perm, scalars) == {"type": "integer", "minimum": 0}
    app = App("t", version="1")
    app.scalar(Slug, parse=Slug, serialize=lambda s: f"slug:{s}")
    assert to_jsonable(Slug("abc"), app.scalars) == "slug:abc"


def test_argument_order_ignores_example_globals_and_streams() -> None:
    app = App("fmtapp", version="1")

    @dataclass(frozen=True, slots=True)
    class Show:
        item: str = Arg(description="Item")
        limit: int = Flag(default=1, description="Limit")

    @app.command(
        "show", description="Show", examples=[("x", "fmtapp show x --format json --limit 3")]
    )
    def show(args: Show, ctx: Ctx) -> dict[str, str]:
        return {}

    order = argument_order_for(app)
    assert order is not None and order["local_args"] == ["--limit", "3"]
