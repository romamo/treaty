"""Seventh review round: stable fingerprints, closed pipes everywhere, exit fields"""

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, NoArgs, RegistrationError
from treaty._idempotency import fingerprint
from treaty._values import CommandPath

SLOWCTL = Path(__file__).resolve().parents[1] / "examples" / "slowctl.py"


class Dead(io.StringIO):
    """A stream whose reader is gone after ``live`` writes"""

    def __init__(self, live: int = 0) -> None:
        super().__init__()
        self.live = live

    def write(self, text: str) -> int:
        if self.live <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.live -= 1
        return super().write(text)


class Money:
    """A plain class: no __eq__, no __repr__, so its default repr has an address"""

    def __init__(self, amount: Decimal) -> None:
        self.amount = amount


def test_fingerprint_is_stable_for_plain_objects_and_sets() -> None:
    app = App("fp", version="1")
    app.scalar(
        Money, base=float, parse=lambda v: Money(Decimal(str(v))), serialize=lambda m: m.amount
    )

    @dataclass(frozen=True, slots=True)
    class Pay:
        amount: Money
        tags: frozenset[str]

    path = CommandPath("pay")
    first = fingerprint(path, Pay(Money(Decimal("1.5")), frozenset({"a", "b"})), app.scalars)
    again = fingerprint(path, Pay(Money(Decimal("1.5")), frozenset({"b", "a"})), app.scalars)
    other = fingerprint(path, Pay(Money(Decimal("2")), frozenset({"a", "b"})), app.scalars)
    assert first == again != other


def test_set_fingerprint_does_not_depend_on_the_hash_seed() -> None:
    code = (
        "from treaty._idempotency import fingerprint; from treaty._scalars import EMPTY;"
        "from treaty._values import CommandPath;"
        "print(fingerprint(CommandPath('x'), {'tags': frozenset(map(str, range(50)))}, EMPTY))"
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        ).stdout
        for seed in ("1", "2", "3")
    }
    assert len(hashes) == 1


class HasRegion(Protocol):
    @property
    def region(self) -> str: ...


class Client:
    def __init__(self, region: str) -> None:
        self.region = region

    @classmethod
    def acquire(cls, args: HasRegion, ctx: Ctx) -> Client:
        return cls(args.region)


def test_protocol_typed_resources_register_and_check_members() -> None:
    @dataclass(frozen=True, slots=True)
    class Where:
        region: str = Flag(default="eu", description="Region")

    @dataclass(frozen=True, slots=True)
    class Nowhere:
        name: str = Flag(default="n", description="Name")

    app = App("proto", version="1")

    @app.command("where", description="Where")
    def where(args: Where, ctx: Ctx, client: Client) -> dict[str, str]:
        return {"region": client.region}

    assert app.call("where", {}, env={}).data == {"region": "eu"}
    with pytest.raises(RegistrationError, match="lack \\['region'\\]"):

        @app.command("nowhere", description="Nowhere")
        def nowhere(args: Nowhere, ctx: Ctx, client: Client) -> dict[str, str]:
            return {}


@pytest.mark.parametrize("argv", [["--schema"], ["--help"], ["--bogus"], ["nope"]])
def test_closed_reader_on_builtins_and_errors_exits_141(argv: list[str]) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(SLOWCTL), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()  # gone before the child writes anything
    code = proc.wait(timeout=10)
    err = proc.stderr.read().decode()
    assert code == 141 and "Traceback" not in err, err


def human_app(events: list[str]) -> App:
    app = App("hum", version="1")

    @app.command("show", description="Show", human=lambda d: f"{d['n']}\n")
    def show(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": 1}

    @app.command(
        "tick", description="Tick", streaming=True, cleanup=lambda: events.append("cleanup")
    )
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        try:
            yield {"n": 1}
            yield {"n": 2}
        finally:
            events.append("finally")

    return app


def test_closed_stdout_in_human_mode_is_not_a_renderer_crash() -> None:
    err = io.StringIO()
    code = human_app([]).run(["show"], stdout=Dead(), stderr=err, env={}, isatty=True)
    assert code == 141 and "renderer" not in err.getvalue()


def test_closed_stdout_during_exec_closes_and_cleans_up_the_step() -> None:
    events: list[str] = []
    plan = io.StringIO('{"_cmd": "tick"}\n')
    code = human_app(events).run(
        ["exec"], stdin=plan, stdout=Dead(live=2), stderr=io.StringIO(), env={}
    )
    assert code == 141 and events == ["finally", "cleanup"]


def test_closed_stderr_keeps_the_crash_envelope() -> None:
    app = App("err", version="1")

    @app.command("boom", description="Crashes")
    def boom(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise RuntimeError("bug")

    out = io.StringIO()
    code = app.run(["boom"], stdout=out, stderr=Dead(), env={}, isatty=False)
    assert code == 1 and json.loads(out.getvalue())["error"]["code"] == "HANDLER_CRASHED"


def test_oversized_error_still_bounds_data() -> None:
    app = App("cap", version="1", max_output_bytes=4096)
    app.exit_code("PART", 80, description="Part", retryable=False, side_effects="none")

    @app.command("batch", description="Batch", exit_codes=["PART"])
    def batch(args: NoArgs, ctx: Ctx) -> dict[str, list[int]]:
        raise Exit.PART("partial", detail="x" * 6000, data={"rows": list(range(100_000))})

    out = io.StringIO()
    app.run(["batch"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    assert len(out.getvalue().encode()) < 6000 + 4096 + 2048


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"code": "quota.exceeded"}, "INVALID_EXIT"),
        ({"retry_after_ms": -5}, 0),
        ({"retry_after_ms": 1.5}, 2),
    ],
)
def test_exit_fields_are_checked_or_normalized(kwargs: dict, expected: object) -> None:
    app = App("q", version="1")
    app.exit_code("QUOTA", 80, description="Quota", retryable=True, side_effects="none")

    @app.command("q", description="Q", exit_codes=["QUOTA"])
    def q(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.QUOTA("over quota", **kwargs)

    out = io.StringIO()
    app.run(["q"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    error = json.loads(out.getvalue())["error"]
    got = error["code"] if expected == "INVALID_EXIT" else error["retry_after_ms"]
    assert got == expected


def test_buffered_stream_refuses_timeout_zero_in_process() -> None:
    app = App("st", version="1")

    @app.command("ev", description="Events", streaming=True)
    def ev(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}

    envelope = app.call("ev", {"timeout": 0}, env={})
    assert envelope.exit_code == 2 and envelope.error is not None


@dataclass(frozen=True, slots=True)
class Row:
    n: int


type Events = Iterator[Row]
type Vec[T] = list[T]


def test_stream_return_type_may_be_an_alias() -> None:
    app = App("al", version="1")

    @app.command("rows", description="Rows", streaming=True)
    def rows(args: NoArgs, ctx: Ctx) -> Events:
        yield Row(1)

    out = io.StringIO()
    app.run(["rows"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    assert json.loads(out.getvalue().splitlines()[0])["data"] == {"n": 1}


def test_a_command_cannot_also_be_a_group() -> None:
    app = App("nest", version="1")

    @app.command("db", description="DB")
    def db(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    with pytest.raises(RegistrationError, match="overlap"):

        @app.command("db.migrate.up", description="Up")
        def up(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


def test_empty_declared_group_is_not_listed() -> None:
    app = App("grp", version="1", description="G")
    app.group("empty", description="Nothing yet")
    out = io.StringIO()
    app.run(["--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert "empty" not in out.getvalue()


def test_generic_alias_resolves_for_a_positional() -> None:
    app = App("gv", version="1")

    @dataclass(frozen=True, slots=True)
    class Many:
        items: Vec[str] = Arg(description="Items")

    @app.command("many", description="Many")
    def many(args: Many, ctx: Ctx) -> dict[str, int]:
        return {"n": len(args.items)}

    assert app.call("many", {"items": ["a", "b"]}, env={}).data == {"n": 2}
