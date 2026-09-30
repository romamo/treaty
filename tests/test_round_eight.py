"""Eighth review round: phase 1 crashes, digit limits, cursors bound to their listing"""

import io
import json
import shlex
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag, NoArgs, Page, ParseError, RegistrationError
from treaty._values import InvalidValue


@dataclass(frozen=True, slots=True)
class Checked:
    n: int = Flag(default=1, description="A number")

    def __post_init__(self) -> None:
        if self.n == 7:
            raise InvalidValue("n cannot be 7")
        if self.n > 7:
            raise ValueError("a bug in the check")


@dataclass(frozen=True, slots=True)
class Query:
    q: str = Flag(default="a", description="Filter")


def make_app() -> App:
    app = App("r8", version="1.0.0", max_output_bytes=4096)

    @app.command(
        "go", description="Go", danger_level="safe", exit_codes=(), supports_raw_payload=True
    )
    def go(args: Checked, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    @app.command(
        "items",
        description="Items matching a filter",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
        supports_raw_payload=True,
        heartbeat=True,
    )
    def items(args: Query, ctx: Ctx) -> list[dict[str, str]]:
        return [{"id": f"{args.q}-{i}", "pad": "x" * 60} for i in range(200)]

    def numeric(cursor: str) -> None:
        if not cursor.isdigit():
            raise ParseError("the cursor is an offset")

    @app.command(
        "offsets",
        description="Items from a source with offset cursors",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        cursor_check=numeric,
    )
    def offsets(args: NoArgs, ctx: Ctx) -> Page[dict[str, int]]:
        assert ctx.page is not None
        start = int(ctx.page.cursor or 0)  # safe: cursor_check ran first
        return Page(items=[{"i": start}], next_cursor=str(start + 1))

    return app


def run(argv: list[str], stdin: str = "") -> tuple[int, dict]:
    out = io.StringIO()
    code = make_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue().splitlines()[-1])


# 1: the args __post_init__


def test_invalid_value_in_post_init_is_an_argument_error() -> None:
    code, env = run(["go", "--n", "7"])
    assert code == 2 and env["error"]["message"] == "N cannot be 7"


@pytest.mark.parametrize(
    "argv, stdin",
    [
        (["go", "--n", "9"], ""),
        (["go", "--raw-payload", '{"n": 9}'], ""),
        (["exec"], json.dumps({"_cmd": "go", "n": 9}) + "\n"),
    ],
)
def test_other_post_init_errors_are_handler_crashes(argv: list[str], stdin: str) -> None:
    _, env = run(argv, stdin)
    assert env["meta"]["exit_code"] == 1 and env["error"]["code"] == "HANDLER_CRASHED"
    assert env["error"]["context"]["exception"] == "ValueError"


def test_post_init_crash_through_app_call() -> None:
    envelope = make_app().call("go", {"n": 9})
    assert envelope.exit_code == 1 and envelope.error is not None
    assert envelope.error.code == "HANDLER_CRASHED"


# 2: numbers past the int() digit limit


@pytest.mark.parametrize("flag", ["--limit", "--heartbeat-ms"])
def test_a_five_thousand_digit_number_is_an_argument_error(flag: str) -> None:
    code, env = run(["items", flag, "9" * 5000])
    assert code == 2 and env["error"]["context"]["flag"] == flag[2:]


# 3 and 8: argv pagination flags over a payload's; JSON limits are integers


def test_truncation_hint_after_raw_payload_runs() -> None:
    code, env = run(["items", "--raw-payload", json.dumps({"q": "b", "limit": 100})])
    assert code == 0 and env["meta"]["truncated"] is True
    code, rerun = run(shlex.split(env["meta"]["truncation_hint"])[1:])
    assert code == 0 and rerun["data"][0]["id"] == f"b-{len(env['data'])}"


@pytest.mark.parametrize(
    "argv, stdin",
    [
        (["items", "--raw-payload", '{"limit": "5"}'], ""),
        (["exec"], json.dumps({"_cmd": "items", "limit": "5"}) + "\n"),
    ],
)
def test_json_limit_must_be_an_integer(argv: list[str], stdin: str) -> None:
    _, env = run(argv, stdin)
    assert env["meta"]["exit_code"] == 2 and env["error"]["context"]["flag"] == "limit"


def test_app_call_limit_must_be_an_integer() -> None:
    envelope = make_app().call("items", {"limit": "5"})
    assert envelope.exit_code == 2


# 4: cursors bind to their arguments; cursor_check guards the handler's own


def test_cursor_from_another_filter_is_invalid() -> None:
    _, first = run(["items", "--q", "a", "--limit", "5"])
    token = first["meta"]["pagination"]["next_cursor"]
    code, env = run(["items", "--q", "a", "--limit", "5", "--cursor", token])
    assert code == 0 and env["data"][0]["id"] == "a-5"
    code, env = run(["items", "--q", "b", "--limit", "5", "--cursor", token])
    assert code == 2 and env["error"]["code"] == "INVALID_CURSOR"


def test_cursor_check_refuses_a_forged_handler_cursor_before_the_handler() -> None:
    _, first = run(["offsets", "--limit", "1"])
    token = first["meta"]["pagination"]["next_cursor"]
    code, env = run(["offsets", "--cursor", token])
    assert code == 0 and env["data"] == [{"i": 1}]
    from treaty._page import Position
    from treaty._values import CommandPath

    decoded = Position.decode(token, CommandPath("offsets"))
    forged = Position("abc", 0, decoded.args).encode(CommandPath("offsets"))
    code, env = run(["offsets", "--cursor", forged])
    assert code == 2 and env["error"]["code"] == "INVALID_CURSOR"


def test_cursor_check_needs_a_paginated_command() -> None:
    app = App("r8", version="1.0.0")
    with pytest.raises(RegistrationError, match="cursor_check"):

        @app.command(
            "x", description="X", danger_level="safe", exit_codes=(), cursor_check=lambda c: None
        )
        def x(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            return {}


# 7: registration errors name the fix


@pytest.mark.parametrize(
    "kw, fix",
    [
        ({"exit_codes": "NOT_FOUND", "danger_level": "safe"}, "exit_codes=('NOT_FOUND',)"),
        ({"exit_codes": "", "danger_level": "safe"}, "exit_codes=()"),
        ({"exit_codes": None, "danger_level": "safe"}, "exit_codes=()"),
        ({"exit_codes": (), "danger_level": "Safe"}, "safe, mutating, destructive"),
    ],
)
def test_malformed_declarations_are_registration_errors(kw: dict[str, object], fix: str) -> None:
    app = App("r8", version="1.0.0")
    with pytest.raises(RegistrationError) as info:

        @app.command("x", description="X", **kw)  # type: ignore[arg-type]
        def x(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            return {}

    assert fix in str(info.value)


# 15: one table of framework flags feeds the parser, manifest, payload schema, and help


def test_every_framework_flag_surface_agrees() -> None:
    from treaty._framework import framework_flags
    from treaty._help import render_command
    from treaty._manifest import command_entry, payload_schema
    from treaty._parse import known_flags
    from treaty._values import CommandPath

    app = make_app()
    command = app.commands[CommandPath("items")]
    names = [f.name for f in framework_flags(command)]
    assert names == [
        "raw-payload",
        "limit",
        "cursor",
        "heartbeat-ms",
        "heartbeat-interval",
        "validate-only",
    ]
    entry = command_entry(command, app.exits, app.commands)
    assert set(names) <= set(entry["flags"])  # type: ignore[arg-type]
    assert known_flags(command)[-6:] == names
    json_keys = {n.replace("-", "_") for n in known_flags(command, argv=False)}
    assert json_keys == set(payload_schema(command)["properties"])  # type: ignore[arg-type]
    help_text = render_command("r8", command, [])
    assert all(f"--{n}" in help_text for n in names)
