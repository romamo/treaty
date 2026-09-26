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

    @app.command("show", description="Show", examples=[("x", "t show widget --format human")])
    def show(args: Show, ctx: Ctx) -> dict[str, str]:
        return {}

    probe = next(p for p in probes_for(app) if p.name == "show")
    assert probe.argv == ("show", "widget")


@pytest.mark.parametrize("name", ["class", "treaty", "json"])
def test_init_rejects_names_that_cannot_be_a_package(name: str) -> None:
    with pytest.raises(ParseError):
        ProjectName(name)
