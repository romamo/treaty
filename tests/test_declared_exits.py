"""The declared-exits audit rule: an exit code a handler raises but its command does not
declare is found before the run exits 1 with UNDECLARED_EXIT_CODE (#211)"""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import treaty
from treaty import App, CliExit, Ctx, Exit, Flag, NoArgs
from treaty import Exit as Bail
from treaty._audit import Finding, audit
from treaty._values import ExitCodeName

from examples.tutorial import todo_exit_codes

HERE = Path(__file__).name


def line_of(text: str) -> int:
    """The line of this file that holds ``text``, once"""
    [found] = [
        n
        for n, line in enumerate(Path(__file__).read_text().splitlines(), 1)
        if line.endswith(text)
    ]
    return found


def write_report(path: Path) -> None:
    try:
        path.write_text("report")
    except OSError:
        raise Exit.PERMISSION_DENIED("the output directory is read-only") from None  # helper


@dataclass(frozen=True, slots=True)
class Target:
    out: Path = Flag(default=Path("report.txt"), description="Where the report goes")

    def __post_init__(self) -> None:
        if self.out.suffix == ".exe":
            raise Bail.UNSAFE_TARGET("refusing to write an executable")  # post-init


class Store:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx) -> Store:
        if ctx.env.get("STORE_DOWN"):
            raise CliExit(ExitCodeName("STORE_DOWN"), "the store is down")  # acquire
        return cls()


def make_app() -> App:
    app = App("renderctl", version="1.0.0")
    for name, code in (("STALE", 79), ("UNSAFE_TARGET", 80), ("STORE_DOWN", 81), ("UNUSED", 82)):
        app.exit_code(name, code, description=name.lower(), retryable=False, side_effects="none")

    @app.command(
        "render",
        description="Render the report",
        danger_level="mutating",
        exit_codes=["STALE", "UNUSED"],
    )
    def render(args: Target, ctx: Ctx, store: Store) -> dict[str, str]:
        if ctx.env.get("STALE"):
            raise Exit.STALE("the input is stale")
        if ctx.env.get("GONE"):
            raise treaty.Exit.NOT_FOUND("no such report")  # framework
        if ctx.env.get("LATE"):
            raise Exit.PRECONDITION("too late")  # implicit
        write_report(args.out)
        return {"effect": "created"}

    @app.command(
        "clean", description="Raise only declared codes", danger_level="safe", exit_codes=()
    )
    def clean(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        if ctx.env.get("X"):
            raise Exit.TIMEOUT("slow")
        return {}

    return app


POST_INIT_FIX = (
    "raise treaty.ParseError there instead of Exit.UNSAFE_TARGET, which exits 2 before "
    "anything runs"
)


def findings(app: App) -> list[Finding]:
    rule = next(r for r in audit(app, "x:app", limit=3).rules if r.id == "declared-exits")
    return list(rule.findings)


def test_each_undeclared_code_is_found_with_its_line() -> None:
    found = findings(make_app())
    assert [(f.command, f.fix) for f in found] == [
        ("render", POST_INIT_FIX),
        ("render", 'add "NOT_FOUND" to exit_codes='),
        ("render", 'add "PERMISSION_DENIED" to exit_codes='),
        ("render", 'add "STORE_DOWN" to exit_codes='),
    ]
    messages = {f.message.split()[1]: f.message for f in found[1:]}
    messages["UNSAFE_TARGET"] = found[0].message
    assert f"({HERE}:{line_of('# framework')})" in messages["NOT_FOUND"]
    helper = messages["PERMISSION_DENIED"]
    assert f"({HERE}:{line_of('# helper')})" in helper
    assert "found via test_declared_exits.write_report" in helper
    assert f"({HERE}:{line_of('# acquire')})" in messages["STORE_DOWN"]
    assert f"({HERE}:{line_of('# post-init')})" in messages["UNSAFE_TARGET"]
    assert "UNDECLARED_EXIT_CODE" in helper
    assert all(f.severity.value == "warning" for f in found)


def test_a_declared_code_never_raised_and_the_implicit_ones_stay_silent() -> None:
    messages = " ".join(f.message for f in findings(make_app()))
    for name in ("STALE", "UNUSED", "PRECONDITION", "TIMEOUT"):
        assert name not in messages


def test_what_the_rule_finds_is_what_the_run_refuses(tmp_path) -> None:
    app = make_app()
    out = io.StringIO()
    code = app.run(
        ["render", "--format", "json", "--out", str(tmp_path / "r.txt")],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"GONE": "1"},
    )
    error = json.loads(out.getvalue().splitlines()[0])["error"]
    assert code == 1 and error["code"] == "UNDECLARED_EXIT_CODE"
    assert error["context"]["raised"] == "NOT_FOUND"


def test_declaring_every_code_clears_the_rule() -> None:
    app = App("renderctl", version="1.0.0")
    app.exit_code("STALE", 79, description="stale", retryable=False, side_effects="none")

    @app.command(
        "render",
        description="Render the report",
        danger_level="mutating",
        exit_codes=["STALE", "NOT_FOUND", "PERMISSION_DENIED"],
    )
    def render(args: Target, ctx: Ctx) -> dict[str, str]:
        if ctx.env.get("STALE"):
            raise Exit.STALE("the input is stale")
        if ctx.env.get("GONE"):
            raise treaty.Exit.NOT_FOUND("no such report")
        write_report(args.out)
        return {"effect": "created"}

    # Target's __post_init__ still raises UNSAFE_TARGET, which no declaration clears
    [left] = findings(app)
    assert left.fix == POST_INIT_FIX


def test_an_exit_from_the_args_post_init_crashes_declared_or_not(tmp_path) -> None:
    """Only a ParseError refuses the arguments there: declaring the code does not stop the
    run reporting HANDLER_CRASHED, so the rule says so instead of asking to declare it"""
    app = App("renderctl", version="1.0.0")
    app.exit_code("UNSAFE_TARGET", 80, description="u", retryable=False, side_effects="none")

    @app.command(
        "render", description="Render", danger_level="mutating", exit_codes=["UNSAFE_TARGET"]
    )
    def render(args: Target, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    out = io.StringIO()
    code = app.run(
        ["render", "--format", "json", "--out", str(tmp_path / "r.exe")],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={},
    )
    assert code == 1
    assert json.loads(out.getvalue().splitlines()[0])["error"]["code"] == "HANDLER_CRASHED"
    [found] = findings(app)
    assert found.fix == POST_INIT_FIX
    assert f"({HERE}:{line_of('# post-init')})" in found.message
    assert "HANDLER_CRASHED instead, declared or not" in found.message


def test_an_unregistered_code_is_registered_then_declared() -> None:
    app = App("renderctl", version="1.0.0")

    @app.command("render", description="Render", danger_level="safe", exit_codes=())
    def render(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.STALE("the input is stale")

    [found] = findings(app)
    assert found.fix == (
        'app.exit_code("STALE", <79-125>, description="<what failed>", retryable=False, '
        'side_effects="none"), then add "STALE" to exit_codes='
    )


# Methods called on a parameter whose class is known (#218)


def test_a_method_on_a_resource_is_followed_as_the_tutorial_calls_it() -> None:
    """The tutorial's list handler reads its items through store.load(), which names
    STORE_CORRUPT: registered without declaring it, the audit finds it there"""
    probe = App("todo", version="1.0.0")
    probe.exit_code("STORE_CORRUPT", 79, description="c", retryable=False, side_effects="none")
    probe.command("list", description="List items", danger_level="safe", exit_codes=())(
        todo_exit_codes.list_items
    )
    [found] = findings(probe)
    assert found.fix == 'add "STORE_CORRUPT" to exit_codes='
    assert "(examples/tutorial/todo_exit_codes.py:64)" in found.message
    assert "found via examples.tutorial.todo_exit_codes.Store.load" in found.message


def test_the_tutorial_declares_every_code_its_store_raises() -> None:
    assert findings(todo_exit_codes.app) == []


class Ledger:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx) -> Ledger:
        return cls()

    def post(self) -> None:
        self.ping()
        self._check()

    def _check(self) -> None:
        raise Exit.LEDGER_LOCKED("the ledger is locked")  # ledger

    # Methods that call each other: the audit reads each once and stops
    def ping(self) -> None:
        self.pong()

    def pong(self) -> None:
        self.ping()


class Archive(Ledger):
    def _check(self) -> None:
        raise Exit.ARCHIVE_SEALED("the archive is sealed")  # archive


@dataclass(frozen=True, slots=True)
class Batch:
    size: int = Flag(default=1, description="Items per batch")

    def checked(self) -> int:
        if self.size > 100:
            raise Exit.BATCH_TOO_LARGE("at most 100 items")  # batch
        return self.size


def ledger_app() -> App:
    app = App("ledgerctl", version="1.0.0")
    for name, code in (("LEDGER_LOCKED", 79), ("ARCHIVE_SEALED", 80), ("BATCH_TOO_LARGE", 81)):
        app.exit_code(name, code, description=name.lower(), retryable=False, side_effects="none")
    return app


def test_self_calls_in_a_reached_method_are_followed_through_cycles() -> None:
    app = ledger_app()

    @app.command("post", description="Post", danger_level="mutating", exit_codes=())
    def post(args: Batch, ctx: Ctx, ledger: Ledger) -> dict[str, int]:
        ledger.post()
        return {"size": args.checked()}

    found = {f.message.split()[1]: f.message for f in findings(app)}
    assert sorted(found) == ["BATCH_TOO_LARGE", "LEDGER_LOCKED"]
    assert f"({HERE}:{line_of('# ledger')})" in found["LEDGER_LOCKED"]
    assert "Ledger.post" in found["LEDGER_LOCKED"]
    assert f"({HERE}:{line_of('# batch')})" in found["BATCH_TOO_LARGE"]


def test_self_resolves_on_the_class_the_parameter_names() -> None:
    """An inherited method's self call reaches the subclass's override"""
    app = ledger_app()

    @app.command("seal", description="Seal", danger_level="mutating", exit_codes=())
    def seal(args: NoArgs, ctx: Ctx, archive: Archive) -> dict[str, str]:
        archive.post()
        return {}

    [found] = findings(app)
    assert f"({HERE}:{line_of('# archive')})" in found.message


def test_a_method_on_an_object_of_unknown_class_is_not_followed() -> None:
    app = ledger_app()

    @app.command("post", description="Post", danger_level="mutating", exit_codes=())
    def post(args: NoArgs, ctx: Ctx, ledger: Ledger) -> dict[str, str]:
        alias = ledger
        alias.post()
        return {}

    assert findings(app) == []
