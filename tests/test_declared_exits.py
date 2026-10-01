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


def findings(app: App) -> list[Finding]:
    rule = next(r for r in audit(app, "x:app", limit=3).rules if r.id == "declared-exits")
    return list(rule.findings)


def test_each_undeclared_code_is_found_with_its_line() -> None:
    found = findings(make_app())
    assert [(f.command, f.fix) for f in found] == [
        ("render", 'add "NOT_FOUND" to exit_codes='),
        ("render", 'add "PERMISSION_DENIED" to exit_codes='),
        ("render", 'add "STORE_DOWN" to exit_codes='),
        ("render", 'add "UNSAFE_TARGET" to exit_codes='),
    ]
    messages = {f.message.split()[1]: f.message for f in found}
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

    # Target's __post_init__ still raises UNSAFE_TARGET, which this app never registered
    [left] = findings(app)
    assert left.fix.startswith('app.exit_code("UNSAFE_TARGET", <79-125>, description=')
    assert left.fix.endswith('then add "UNSAFE_TARGET" to exit_codes=')
