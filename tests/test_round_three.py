"""Third review round: input limits, user-code hooks, signals around exec, registration"""

import io
import json
import os
import signal
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import needs_posix_signals

from treaty import App, Arg, Ctx, Flag, NoArgs, ParseError, RegistrationError, SchemaError
from treaty._help import render_command
from treaty._profile import probes_for
from treaty._scaffold import ProjectName


def strict_json(text: str) -> object:
    def no_constant(name: str) -> object:
        raise AssertionError(f"{name} in output")

    return json.loads(text, parse_constant=no_constant)


def run(
    app: App, argv: list[str], *, stdin: io.TextIOBase | None = None, isatty: bool = False
) -> tuple[int, list[dict], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdin=stdin, stdout=out, stderr=err, env={}, isatty=isatty)
    lines = [] if isatty else [strict_json(line) for line in out.getvalue().splitlines()]
    return code, lines, (out.getvalue() + err.getvalue()) if isatty else err.getvalue()


@dataclass(frozen=True, slots=True)
class Numbers:
    count: int = Flag(default=1, description="Count")
    ratio: float = Flag(default=0.5, description="Ratio")


@dataclass(frozen=True, slots=True)
class Weird:
    value: str


def weird_parse(raw: str) -> Weird:
    raise KeyError(raw)


def limits_app() -> App:
    app = App("lim", version="1")
    app.scalar(Weird, parse=weird_parse)

    @app.command("num", description="Numbers", supports_raw_payload=True)
    def num(args: Numbers, ctx: Ctx) -> dict[str, float]:
        return {"count": args.count, "ratio": args.ratio}

    @dataclass(frozen=True, slots=True)
    class WeirdArgs:
        w: Weird = Flag(description="Weird")

    @app.command("weird", description="Parser raises KeyError")
    def weird(args: WeirdArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.parametrize(
    "payload",
    [
        '{"count": ' + "9" * 5000 + "}",
        '{"ratio": 1' + "0" * 400 + "}",
        '{"count": NaN}',
        '{"ratio": Infinity}',
    ],
)
def test_out_of_range_json_numbers_are_arg_errors_with_valid_json(payload: str) -> None:
    code, [env], _ = run(limits_app(), ["num", "--raw-payload", payload])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"


def test_exec_line_with_a_huge_number_does_not_end_the_plan() -> None:
    plan = io.StringIO('{"_cmd": "num", "ratio": 1' + "0" * 400 + '}\n{"_cmd": "version"}\n')
    code, lines, _ = run(limits_app(), ["exec", "--ignore-errors"], stdin=plan)
    assert [line["ok"] for line in lines] == [False, True]


def test_in_process_huge_int_is_an_arg_error() -> None:
    assert limits_app().call("num", {"ratio": 10**400}, env={}).exit_code == 2


def test_scalar_parser_that_raises_anything_is_an_arg_error() -> None:
    code, [env], _ = run(limits_app(), ["weird", "--w", "abc"])
    assert code == 2 and "KeyError" in env["error"]["message"]


@dataclass(frozen=True, slots=True)
class RunId:
    value: str


@dataclass(frozen=True, slots=True)
class Version:
    value: str


def test_scalar_pattern_type_is_enforced() -> None:
    app = App("pt", version="1")
    app.scalar(RunId, parse=RunId, pattern_type="uuid")
    app.scalar(Version, parse=Version, pattern_type="semver")

    @dataclass(frozen=True, slots=True)
    class Go:
        run: RunId = Flag(description="Run")
        ver: Version = Flag(description="Version")

    @app.command("go", description="Go")
    def go(args: Go, ctx: Ctx) -> dict[str, str]:
        return {"run": args.run.value}

    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert run(app, ["go", "--run", uuid, "--ver", "1.2.3-rc.1"])[0] == 0
    assert run(app, ["go", "--run", "not-a-uuid", "--ver", "1.2.3"])[0] == 2
    assert run(app, ["go", "--run", uuid, "--ver", "banana"])[0] == 2


def test_secret_path_is_not_rebuilt_in_the_suggestion() -> None:
    app = App("sp", version="1")

    @dataclass(frozen=True, slots=True)
    class KeyFile:
        key_file: Path = Flag(description="Key file")

    @app.command("use", description="Use a key file")
    def use(args: KeyFile, ctx: Ctx) -> dict[str, str]:
        return {}

    out = io.StringIO()
    env = {"K": "/very/s3cret%41path"}
    app.run(["use", "--key-file-from-env", "K"], stdout=out, stderr=io.StringIO(), env=env)
    assert "s3cret" not in out.getvalue()


def hooks_app(cleanup_fails: bool = False) -> App:
    app = App("hooks", version="1", default_timeout=None)

    def cleanup() -> None:
        if cleanup_fails:
            raise FileNotFoundError("gone")

    @app.command("kill", description="Signals itself", cleanup=cleanup)
    def kill(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        signal.raise_signal(signal.SIGTERM)
        return {}

    @app.command("tick", description="Events", streaming=True, human=lambda e: f"n={e['n']}\n")
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        yield {"n": 2}

    @app.command("closing", description="Its finally raises", streaming=True)
    def closing(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, object]]:
        try:
            yield {"x": object()}
        finally:
            raise RuntimeError("cleanup failed")

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no str")

    @app.command("unprintable", description="Raises an unprintable exception")
    def unprintable(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Unprintable()

    return app


def test_failing_cleanup_still_gives_the_cancelled_envelope() -> None:
    code, [env], err = run(hooks_app(cleanup_fails=True), ["kill"])
    assert code == 143 and env["error"]["context"]["cleanup_failed"] == "FileNotFoundError"
    assert "gone" in err


def test_no_stream_in_human_mode_renders_every_event() -> None:
    code, _, text = run(hooks_app(), ["tick", "--no-stream"], isatty=True)
    assert code == 0 and "n=1\nn=2\n" in text


def test_stream_finally_that_raises_leaves_the_terminal_envelope() -> None:
    code, lines, err = run(hooks_app(), ["closing"])
    assert code == 1 and lines[-1]["error"]["code"] == "INVALID_OUTPUT"
    assert "cleanup failed" in err


def test_unprintable_exception_still_crashes_into_an_envelope() -> None:
    code, [env], _ = run(hooks_app(), ["unprintable"])
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"


def test_signal_between_exec_lines_writes_a_cancelled_line() -> None:
    app = App("sig", version="1")

    @dataclass(frozen=True, slots=True)
    class Loud:
        value: str

    def loud(v: Loud) -> str:
        signal.raise_signal(signal.SIGTERM)  # lands while the result is serialized
        return v.value

    app.scalar(Loud, parse=Loud, serialize=loud)

    @app.command("mk", description="Result signals while serialized")
    def mk(args: NoArgs, ctx: Ctx) -> dict[str, Loud]:
        return {"v": Loud("x")}

    plan = io.StringIO('{"_cmd": "mk"}\n{"_cmd": "version"}\n')
    code, lines, _ = run(app, ["exec"], stdin=plan)
    assert code == 143 and lines[0]["ok"] is True
    assert lines[-1]["error"]["code"] == "CANCELLED" and lines[-1]["error"]["context"] == {
        "signal": "SIGTERM",
        "lines_run": 1,
    }


@needs_posix_signals
def test_signal_interrupts_exec_waiting_on_stdin() -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r")
    # A process-directed signal, as `kill` sends: raise_signal would target the timer thread
    timer = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        code, [env], _ = run(App("wait", version="1"), ["exec"], stdin=stdin)  # type: ignore[arg-type]
    finally:
        os.close(write_fd)
        stdin.close()
    assert code == 143 and env["error"]["code"] == "CANCELLED"


def test_crash_redaction_skips_defaults_and_short_values() -> None:
    app = App("red", version="1")

    @dataclass(frozen=True, slots=True)
    class Gen:
        max_tokens: int = Flag(default=1, description="Tokens")

    @app.command("gen", description="Generate")
    def gen(args: Gen, ctx: Ctx) -> dict[str, str]:
        raise RuntimeError("upstream returned 413 after 10 retries")

    code, [env], _ = run(app, ["gen"])
    assert (
        env["error"]["message"] == "gen raised RuntimeError: upstream returned 413 after 10 retries"
    )


def register(app: App, args_type: type, **meta: object) -> None:
    def handler(args: args_type, ctx: Ctx) -> dict[str, str]:  # type: ignore[valid-type]
        return {}

    app.command("c", description="C", **meta)(handler)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Node:
    children: tuple[Node, ...]


def test_registration_rejects_what_would_break_later() -> None:
    @dataclass(frozen=True, slots=True)
    class Priced:
        price: Money = Flag(default=Money(Decimal("1.5")), description="Price")

    @dataclass(frozen=True, slots=True)
    class Convert:
        format: str = Arg(description="Target format")

    @dataclass(frozen=True, slots=True)
    class Cache:
        cache: bool = Flag(default=True, description="Use the cache")
        no_cache: bool = Flag(default=False, description="Separate knob")

    @dataclass(frozen=True, slots=True)
    class Tokens:
        token: str = Flag(description="Token")
        token_from_env: str = Flag(default="", secret=False, description="Shadow")

    app = App("reg", version="1")
    app.scalar(
        Money, parse=lambda raw: Money(Decimal(raw)), base=float, serialize=lambda m: m.amount
    )
    for args_type in (Priced, Convert, Cache, Tokens):
        with pytest.raises(RegistrationError):
            register(app, args_type)
    with pytest.raises(SchemaError, match="refers to itself"):

        @app.command("tree", description="Tree")
        def tree(args: NoArgs, ctx: Ctx) -> Node:
            return Node(())


def test_confirm_given_on_argv_conflicts_with_false_in_the_payload() -> None:
    app = App("cf", version="1")

    @dataclass(frozen=True, slots=True)
    class Rm:
        target: str = Arg(description="Target")
        dry_run: bool = Flag(default=False, description="Preview")

    @app.command("rm", description="Remove", danger_level="destructive", supports_raw_payload=True)
    def rm(args: Rm, ctx: Ctx) -> dict[str, str]:
        return {"effect": "deleted"}

    payload = '{"target": "a", "confirm_destructive": false}'
    code, [env], _ = run(app, ["rm", "--confirm-destructive", "--raw-payload", payload])
    assert code == 2 and env["error"]["context"]["flag"] == "confirm-destructive"
    help_text = render_command(
        "cf", app.commands[next(iter(p for p in app.commands if p.value == "rm"))]
    )
    assert "--confirm-destructive" in help_text and "--raw-payload JSON" in help_text


def test_probes_drop_global_options_from_examples() -> None:
    app = App("t", version="1")

    @dataclass(frozen=True, slots=True)
    class Show:
        item: str = Arg(description="Item")

    @app.command("show", description="Show", examples=[("x", "t --format human show widget")])
    def show(args: Show, ctx: Ctx) -> dict[str, str]:
        return {}

    probe = next(p for p in probes_for(app) if p.name == "show")
    assert probe.argv == ("show", "widget")


@pytest.mark.parametrize("name", ["class", "treaty", "json", "shop-", "tests", "pytest"])
def test_init_rejects_names_that_cannot_be_a_package(name: str) -> None:
    with pytest.raises(ParseError):
        ProjectName(name)


# Fourth review round


@dataclass(frozen=True, slots=True)
class Region:
    value: str

    def __post_init__(self) -> None:
        if self.value not in ("eu", "us"):
            raise ParseError(f"unknown region {self.value}", suggestion="use eu or us")


def test_scalar_parser_parse_error_keeps_its_message_and_suggestion() -> None:
    app = App("rg", version="1")
    app.scalar(Region, parse=Region)

    @dataclass(frozen=True, slots=True)
    class Go:
        region: Region = Flag(description="Region")

    @app.command("go", description="Go")
    def go(args: Go, ctx: Ctx) -> dict[str, str]:
        return {"region": args.region.value}

    code, [env], _ = run(app, ["go", "--region", "xx"])
    assert code == 2 and env["error"]["message"] == "unknown region xx"
    assert env["error"]["suggestion"] == "use eu or us"


def test_failing_human_renderer_on_a_stream_exits_1_with_one_traceback() -> None:
    app = App("hr", version="1")

    def broken(event: object) -> str:
        raise KeyError("renderer bug")

    @app.command("tick", description="Events", streaming=True, human=broken)
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        yield {"n": 2}

    code, _, text = run(app, ["tick"], isatty=True)
    assert code == 1 and text.count("Traceback") == 1


def test_signal_held_before_the_handler_skips_cleanup() -> None:
    ran: list[str] = []
    app = App("held", version="1", default_timeout=None)

    @dataclass(frozen=True, slots=True)
    class Loud:
        value: str

    def loud(raw: str) -> Loud:
        signal.raise_signal(signal.SIGTERM)  # lands while the line is validated
        return Loud(raw)

    app.scalar(Loud, parse=loud)

    @dataclass(frozen=True, slots=True)
    class Args:
        v: Loud = Flag(description="V")

    @app.command("go", description="Go", cleanup=lambda: ran.append("cleanup"))
    def go(args: Args, ctx: Ctx) -> dict[str, str]:
        ran.append("handler")
        return {}

    code, [env], _ = run(app, ["exec"], stdin=io.StringIO('{"_cmd": "go", "v": "x"}\n'))
    assert code == 143 and env["error"]["code"] == "CANCELLED"
    assert ran == [] and "partial" not in env["meta"]


@pytest.mark.parametrize("name", ["match", "type", "shop-tool", "z9"])
def test_init_accepts_soft_keywords_and_plain_names(name: str) -> None:
    ProjectName(name)


@dataclass(frozen=True, slots=True)
class Url:
    value: str


def test_malformed_url_preset_input_is_an_arg_error() -> None:
    app = App("u", version="1")
    app.scalar(Url, parse=Url, pattern_type="url")

    @dataclass(frozen=True, slots=True)
    class Fetch:
        url: Url = Arg(description="URL")

    @app.command("fetch", description="Fetch")
    def fetch(args: Fetch, ctx: Ctx) -> dict[str, str]:
        return {}

    code, [env], _ = run(app, ["fetch", "http://[::1"])
    assert code == 2 and env["error"]["code"] == "ARG_ERROR"


def test_optional_positional_is_not_required_in_the_payload_schema() -> None:
    from treaty._manifest import payload_schema

    app = App("o", version="1")

    @dataclass(frozen=True, slots=True)
    class Hello:
        name: str | None = Arg(description="Name")

    @app.command("hi", description="Hi", supports_raw_payload=True)
    def hi(args: Hello, ctx: Ctx) -> dict[str, str]:
        return {}

    command = next(c for p, c in app.commands.items() if p.value == "hi")
    assert "required" not in payload_schema(command)


def test_undeclared_framework_exit_is_rejected_like_a_custom_one() -> None:
    from treaty import Exit

    app = App("nf", version="1")

    @app.command("get", description="Get")
    def get(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.NOT_FOUND("missing")

    code, [env], _ = run(app, ["get"])
    assert code == 1 and env["error"]["code"] == "UNDECLARED_EXIT_CODE"


def test_replay_schema_and_failed_data_validate_as_an_mcp_client_would() -> None:
    from typing import Literal

    from jsonschema.validators import validator_for

    from treaty._mcp import tool_entries

    @dataclass(frozen=True, slots=True)
    class Up:
        effect: Literal["updated"] | None

    app = App("up", version="1")
    app.exit_code("TAKEN", 80, description="Taken", retryable=False, side_effects="none")

    @app.command("up", description="Up", danger_level="mutating", exit_codes=["TAKEN"])
    def up(args: NoArgs, ctx: Ctx) -> Up:
        return Up("updated")

    schema = next(e for e in tool_entries(app) if e.name == "up").output_schema
    validator = validator_for(schema)(schema)
    base = {"warnings": [], "meta": {}}
    validator.validate({**base, "ok": True, "data": {"effect": "noop"}, "error": None})
    failed = {"code": "TAKEN", "message": "taken", "retryable": False}
    validator.validate({**base, "ok": False, "data": {"holder": "bob"}, "error": failed})


def test_huge_ints_are_errors_not_crashes_in_process_and_in_output() -> None:
    app = App("hg", version="1")

    @dataclass(frozen=True, slots=True)
    class Size:
        size: int = Flag(default=1, pattern=r"\d+", description="Size")

    @app.command("size", description="Size")
    def size(args: Size, ctx: Ctx) -> dict[str, int]:
        return {"n": 10**5000}

    assert app.call("size", {"size": 10**5000}, env={}).exit_code == 2
    code, [env], _ = run(app, ["size"])
    assert code == 1 and env["error"]["code"] == "INVALID_OUTPUT"


def test_prune_skips_foreign_entries_and_keeps_going(tmp_path: Path) -> None:
    import time as clock

    from treaty._idempotency import TTL_SECONDS, IdempotencyKey, Record, claim

    stale = clock.time() - TTL_SECONDS - 3600
    (tmp_path / "stray.json").mkdir()
    os.utime(tmp_path / "stray.json", (stale, stale))
    with claim(tmp_path, IdempotencyKey("old")) as slot:
        slot.save(Record("fp", "c", {"effect": "created"}, stale))
        old = slot.path
    os.utime(old, (stale, stale))
    with claim(tmp_path, IdempotencyKey("new")) as slot:
        slot.save(Record("fp", "c", {"effect": "created"}, clock.time()))
        slot.prune(clock.time())
    assert not old.exists() and (tmp_path / "stray.json").is_dir()


# Fifth review round


def echo_app() -> App:
    from collections.abc import Iterable

    app = App("echo", version="1")

    @dataclass(frozen=True, slots=True)
    class Text:
        text: str = Flag(description="Text")

    @app.command("echo", description="Echo", supports_raw_payload=True, exit_codes=["NOT_FOUND"])
    def echo(args: Text, ctx: Ctx) -> dict[str, str]:
        if args.text == "where":
            raise ParseError("bad place", context={"path": Path("/srv"), "n": Decimal("1.5")})
        return {"text": args.text}

    @app.command("items", description="Items", streaming=True)
    def items(args: NoArgs, ctx: Ctx) -> Iterable[dict[str, int]]:
        return [{"a": 1}, {"a": 2}]

    return app


def test_parse_error_context_with_objects_still_writes_an_envelope() -> None:
    code, [env], _ = run(echo_app(), ["echo", "--text", "where"])
    assert code == 2 and env["error"]["context"] == {"path": str(Path("/srv")), "n": "1.5"}


def test_exec_keeps_unicode_line_separators_inside_json_strings() -> None:
    plan = io.StringIO('{"_cmd": "echo", "text": "a b\u0085c"}\n')
    code, [line], _ = run(echo_app(), ["exec"], stdin=plan)
    assert code == 0 and line["data"]["text"] == "a b\u0085c"


def test_deeply_nested_input_is_an_arg_error() -> None:
    nested = "[" * 1000 + "]" * 1000
    code, lines, _ = run(
        echo_app(), ["exec"], stdin=io.StringIO(f'{{"_cmd": "echo", "text": {nested}}}\n')
    )
    assert lines[0]["error"]["code"] == "ARG_ERROR"  # exec exits 1 when any line fails
    deeper = "[" * 100000 + "]" * 100000
    code, [env], _ = run(echo_app(), ["echo", "--raw-payload", f'{{"text": {deeper}}}'])
    assert code == 2


def test_single_command_help_carries_its_full_exit_table() -> None:
    code, [env], _ = run(echo_app(), ["echo", "--help"])
    assert {"0", "1", "2", "5", "10", "130", "143"} <= set(env["data"]["echo"]["exit_codes"])


def test_exec_file_with_a_bom_and_physical_line_numbers(tmp_path: Path) -> None:
    plan = tmp_path / "plan.jsonl"
    plan.write_text('{"_cmd": "version"}\n\n\n{"_cmd": "nope"}\n', encoding="utf-8-sig")
    code, lines, _ = run(echo_app(), ["exec", "--input-file", str(plan), "--ignore-errors"])
    assert [line["meta"]["_line"] for line in lines] == [1, 4] and lines[0]["ok"] is True


def test_streaming_handler_may_return_an_iterable() -> None:
    code, lines, _ = run(echo_app(), ["items"])
    assert code == 0 and [line["data"] for line in lines[:2]] == [{"a": 1}, {"a": 2}]


def test_profile_keeps_explicit_relative_commands_absolute_without_resolving_symlinks(
    tmp_path: Path,
) -> None:
    from treaty._profile import build_profile

    app = App("deployctl", version="1")
    venv_python = tmp_path / "bin" / "python"
    venv_python.parent.mkdir()
    venv_python.symlink_to(Path(os.__file__))
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        explicit = build_profile(app, ["./deployctl"], [])["command"]
        linked = build_profile(app, ["bin/python"], [])["command"]
        found = build_profile(app, ["./deployctl"], [], beside_profile=True)["command"]
    finally:
        os.chdir(cwd)
    assert explicit == [str(tmp_path / "deployctl")]
    assert linked == [str(tmp_path / "bin" / "python")]
    assert found == ["./deployctl"]


def test_audit_does_not_ask_to_redeclare_framework_retryable_codes() -> None:
    from treaty._audit import audit

    app = App("rl", version="1")

    @app.command(
        "push",
        description="Push",
        danger_level="mutating",
        exit_codes=["RATE_LIMITED"],
        examples=[("x", "rl push")],
    )
    def push(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    by_rule = {r.id: r for r in audit(app, "rl:app", limit=10).rules}
    assert by_rule["retryable"].findings == ()


@pytest.mark.parametrize("name", ["dist", "build", "pluggy", "packaging"])
def test_init_rejects_names_the_scaffold_or_pytest_use(name: str) -> None:
    with pytest.raises(ParseError):
        ProjectName(name)


def test_leading_inline_flag_pattern_is_rejected_at_registration() -> None:
    with pytest.raises(RegistrationError, match="scope inline flags"):
        Flag(default="a", pattern="(?i)[a-z]+", description="Name")
    Flag(default="a", pattern="(?i:[a-z]+)", description="Name")


def test_mcp_structured_content_escapes_lone_surrogates() -> None:
    from treaty._mcp import without_surrogates

    # A POSIX filename that is not UTF-8, as os.fsdecode returns it there
    name = b"caf\xe9.txt".decode("utf-8", "surrogateescape")
    cleaned = without_surrogates({"files": [name], name: 1})
    json.dumps(cleaned, ensure_ascii=False).encode("utf-8")  # strict UTF-8 must succeed
    assert cleaned == {"files": ["caf\\udce9.txt"], "caf\\udce9.txt": 1}


def test_idempotency_key_works_when_args_serialize_to_non_json(tmp_path: Path) -> None:
    app = App("pay", version="1", state_dir=tmp_path)

    @dataclass(frozen=True, slots=True)
    class Money:
        amount: Decimal

    app.scalar(Money, parse=lambda s: Money(Decimal(s)), serialize=lambda m: m.amount)

    @dataclass(frozen=True, slots=True)
    class Charge:
        amount: Money = Arg(description="Amount")

    @app.command("charge", description="Charge", danger_level="mutating")
    def charge(args: Charge, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    code, [env], _ = run(app, ["charge", "9.99", "--idempotency-key", "k"])
    assert code == 0 and env["data"]["effect"] == "created"
    code, [env], _ = run(app, ["charge", "9.99", "--idempotency-key", "k"])
    assert code == 0 and env["data"]["effect"] == "noop"
    code, [env], _ = run(app, ["charge", "1.00", "--idempotency-key", "k"])
    assert code == 6 and env["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
