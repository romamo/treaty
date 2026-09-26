"""Sixth review round: user-code BaseExceptions, closed pipes, contexts, registration"""

import asyncio
import contextvars
import io
import json
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Arg, Ctx, Envelope, ErrorDetail, Flag, NoArgs, RegistrationError

SLOWCTL = Path(__file__).resolve().parents[1] / "examples" / "slowctl.py"
TENANT: contextvars.ContextVar[str] = contextvars.ContextVar("tenant", default="unset")


def run(app: App, argv: list[str], *, isatty: bool = False) -> tuple[int, list[dict], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env={}, isatty=isatty)
    lines = [] if isatty else [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines, (out.getvalue() + err.getvalue()) if isatty else err.getvalue()


def test_asyncio_cancelled_error_in_a_handler_still_writes_an_envelope() -> None:
    app = App("aio", version="1")

    async def cancelled() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    @app.command("one", description="Awaits a cancelled task")
    def one(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        asyncio.run(cancelled())
        return {}

    code, [env], _ = run(app, ["one"])
    assert code == 1 and env["error"]["context"]["exception"] == "CancelledError"


def test_closed_stdout_exits_141_without_a_traceback() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(SLOWCTL), "serve", "--interval", "0.01"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.readline()
    proc.stdout.close()  # the reader goes away, as `| head -n 1` does
    code = proc.wait(timeout=10)
    err = proc.stderr.read().decode()
    assert code == 141 and "Traceback" not in err, err


def test_handler_and_stream_see_the_callers_contextvars() -> None:
    app = App("ctx", version="1", default_timeout=5)

    @app.command("who", description="Reads a contextvar on the worker")
    def who(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"tenant": TENANT.get()}

    @app.command("events", description="Sets a contextvar between events", streaming=True)
    def events(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, str]]:
        TENANT.set("acme")
        yield {"tenant": TENANT.get()}
        yield {"tenant": TENANT.get()}

    token = TENANT.set("host")
    try:
        envelope = app.call("who", {}, env={})
    finally:
        TENANT.reset(token)
    assert isinstance(envelope, Envelope) and envelope.data == {"tenant": "host"}
    code, lines, _ = run(app, ["events", "--timeout", "5"])
    assert [line["data"] for line in lines[:2]] == [{"tenant": "acme"}, {"tenant": "acme"}]


def test_failure_before_any_event_is_not_partial() -> None:
    app = App("early", version="1")
    app.exit_code("NOPE", 80, description="No", retryable=False, side_effects="none")

    @app.command("s", description="Fails first", streaming=True, exit_codes=["NOPE"])
    def s(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        from treaty import Exit

        raise Exit.NOPE("no")
        yield {}

    code, lines, _ = run(app, ["s"])
    assert code == 80 and lines[-1]["meta"]["partial"] is False


def test_in_process_streams_are_bounded_by_the_default_timeout() -> None:
    app = App("inf", version="1", default_timeout=0.3)

    @app.command("forever", description="Never ends", streaming=True)
    def forever(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        n = 0
        while True:
            n += 1
            yield {"n": n}

    envelope = app.call("forever", {}, env={})
    assert isinstance(envelope.error, ErrorDetail) and envelope.error.code == "TIMEOUT"
    bounded = app.call("forever", {"timeout": 0.1}, env={})
    assert bounded.error is not None and bounded.error.code == "TIMEOUT"


def test_integer_fields_accept_integral_floats_from_json() -> None:
    app = App("n", version="1")

    @dataclass(frozen=True, slots=True)
    class N:
        n: int = Flag(default=1, description="N")

    @app.command("n", description="N")
    def n(args: N, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    assert app.call("n", {"n": 2.0}, env={}).data == {"n": 2}
    assert app.call("n", {"n": 2.5}, env={}).exit_code == 2


def test_cap_leaves_an_envelope_whose_error_alone_is_too_big() -> None:
    from treaty import Exit

    app = App("cap", version="1", max_output_bytes=4096)
    app.exit_code("BIG", 80, description="Big", retryable=False, side_effects="none")

    @app.command("big", description="Big error", exit_codes=["BIG"])
    def big(args: NoArgs, ctx: Ctx) -> dict[str, list[int]]:
        raise Exit.BIG("big", detail="x" * 6000, data={"items": [1, 2, 3]})

    code, [env], _ = run(app, ["big"])
    assert code == 80 and env["data"] == {"items": [1, 2, 3]}


def test_human_help_lists_groups_implied_by_dotted_paths() -> None:
    app = App("a1", version="1", description="A1")

    @app.command("db.migrate.up", description="Apply migrations")
    def up(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    _, _, root = run(app, ["--help"], isatty=True)
    _, _, db = run(app, ["db", "--help"], isatty=True)
    assert "db" in root and "migrate" in db


def test_framework_flag_errors_are_collected_with_the_rest() -> None:
    app = App("cp", version="1")

    @dataclass(frozen=True, slots=True)
    class Copy:
        src: str = Arg(description="Source")
        count: int = Flag(default=1, description="Count")

    @app.command("copy", description="Copy", has_network_io=True)
    def copy(args: Copy, ctx: Ctx) -> dict[str, str]:
        return {}

    code, [env], _ = run(app, ["copy", "--bogus", "1", "--count", "x", "--timeout", "abc"])
    fields = {e.get("field") for e in env["error"]["errors"]}
    assert code == 2 and {"bogus", "count", "timeout"} <= fields


def test_resource_reading_foreign_args_is_rejected_at_registration() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class ProjectArgs:
        project: str = Flag(default="p", description="Project")

    @dataclass(frozen=True, slots=True)
    class OtherArgs:
        name: str = Flag(default="n", description="Name")

    class Project:
        @classmethod
        def acquire(cls, args: ProjectArgs, ctx: Ctx) -> Project:
            return cls()

    app = App("res", version="1")
    with pytest.raises(RegistrationError, match=r"reads .*ProjectArgs, but .*OtherArgs"):

        @app.command("x", description="X")
        def x(args: OtherArgs, ctx: Ctx, project: Project) -> dict[str, str]:
            return {}


type Port = int
type Names = tuple[str, ...]


def test_pep_695_aliases_are_accepted() -> None:
    app = App("alias", version="1")

    @dataclass(frozen=True, slots=True)
    class Serve:
        port: Port = Flag(default=80, description="Port")
        names: Names = Flag(default=(), description="Names")

    @app.command("serve", description="Serve")
    def serve(args: Serve, ctx: Ctx) -> dict[str, int]:
        return {"port": args.port, "names": len(args.names)}

    code, [env], _ = run(app, ["serve", "--port", "8080", "--names", "a"])
    assert code == 0 and env["data"] == {"port": 8080, "names": 1}


def test_defaults_must_match_the_field_type() -> None:
    from enum import StrEnum

    class Color(StrEnum):
        RED = "red"
        BLUE = "blue"

    @dataclass(frozen=True, slots=True)
    class Bad:
        n: int = Flag(default="x", description="N")

    @dataclass(frozen=True, slots=True)
    class Paint:
        color: Color = Flag(default="red", description="Color")

    app = App("def", version="1")
    with pytest.raises(RegistrationError, match="does not match"):

        @app.command("bad", description="Bad")
        def bad(args: Bad, ctx: Ctx) -> dict[str, str]:
            return {}

    @app.command("paint", description="Paint")
    def paint(args: Paint, ctx: Ctx) -> dict[str, bool]:
        return {"member": isinstance(args.color, Color)}

    assert run(app, ["paint"])[1][0]["data"] == {"member": True}


def test_init_rejects_py_and_a_missing_treaty_source(tmp_path: Path) -> None:
    from treaty import ParseError
    from treaty._cli import cli
    from treaty._scaffold import ProjectName

    with pytest.raises(ParseError):
        ProjectName("py")
    target = tmp_path / "x"
    code, [env], _ = run(
        cli, ["init", "demo", "--directory", str(target), "--treaty-source", str(tmp_path)]
    )
    assert code == 2 and "not a treaty checkout" in env["error"]["message"]
