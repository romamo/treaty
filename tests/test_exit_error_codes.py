"""ExitCodeEntry 1.1 ``error_codes`` on exit 6 (ManifestResponse 3.19, #362): the manifest
names every ``error.code`` a command's CONFLICT carries, or nothing when it cannot be sure"""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

import treaty
from treaty import App, Arg, Ctx, Exit, NoArgs, already_exists
from treaty._audit import audit
from treaty._manifest import SCHEMA_VERSION
from treaty._skills import render


@dataclass(frozen=True, slots=True)
class NameArgs:
    name: str = Arg(description="Resource name")


@dataclass(frozen=True, slots=True)
class Made:
    effect: str
    name: str


STORE: dict[str, Made] = {}


def existing_or_none(name: str) -> None:
    """A helper beside the handler: the scan follows it"""
    if name in STORE:
        raise already_exists(STORE[name], conflict_id=name)


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, Any]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def manifest(app: App) -> dict[str, Any]:
    built = app.manifest()
    spec_validator("manifest-response").validate(built)
    return built


def conflict(app: App, path: str) -> dict[str, Any] | None:
    entry: dict[str, Any] | None = manifest(app)["commands"][path]["exit_codes"].get("6")
    return entry


def make_app() -> App:
    app = App("res", version="1.0.0")

    @app.command("create", description="Create", danger_level="mutating", exit_codes=["CONFLICT"])
    def create(args: NameArgs, ctx: Ctx) -> Made:
        if args.name in STORE:
            raise treaty.already_exists(STORE[args.name], conflict_id=args.name)
        STORE[args.name] = Made("created", args.name)
        return STORE[args.name]

    @app.command("put", description="Put", danger_level="mutating", exit_codes=["CONFLICT"])
    def put(args: NameArgs, ctx: Ctx) -> Made:
        existing_or_none(args.name)
        if args.name == "locked":
            raise Exit.CONFLICT("locked", code="VERSION_MISMATCH")
        if args.name == "held":
            raise Exit.CONFLICT("held")
        return Made("created", args.name)

    @app.command("lookup", description="Look up", danger_level="safe", exit_codes=["CONFLICT"])
    def lookup(args: NameArgs, ctx: Ctx) -> Made:
        if args.name in STORE:
            raise already_exists(STORE[args.name], conflict_id=args.name)
        return Made("noop", args.name)

    @app.command("touch", description="Touch", danger_level="mutating", exit_codes=())
    def touch(args: NameArgs, ctx: Ctx) -> Made:
        return Made("updated", args.name)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NameArgs, ctx: Ctx) -> Made:
        return Made("noop", args.name)

    @app.command(
        "watch", description="Watch", danger_level="mutating", exit_codes=(), streaming=True
    )
    def watch(args: NoArgs, ctx: Ctx) -> Iterator[Made]:
        yield Made("created", "x")

    return app


@pytest.fixture(autouse=True)
def empty_store() -> Iterator[None]:
    STORE.clear()
    yield
    STORE.clear()


def test_the_manifest_declares_3_19() -> None:
    assert SCHEMA_VERSION == "3.19"
    assert manifest(make_app())["schema_version"] == "3.19"


def test_already_exists_is_listed_with_the_reused_key_of_a_mutating_command() -> None:
    entry = conflict(make_app(), "create")
    assert entry is not None and entry["name"] == "CONFLICT"
    assert entry["error_codes"] == ["ALREADY_EXISTS", "IDEMPOTENCY_KEY_REUSED"]


def test_every_code_the_command_answers_under_exit_6_is_listed(tmp_path: Any) -> None:
    """The issue's repro: what the manifest lists is what the runs answer"""
    app = make_app()
    listed = conflict(app, "create")
    assert listed is not None
    env = {"RES_STATE_DIR": str(tmp_path)}
    run(app, ["create", "a"])
    code, again = run(app, ["create", "a"])
    assert code == 6 and again["error"]["code"] in listed["error_codes"]
    run(app, ["create", "b", "--idempotency-key", "k1"], env)
    code, reused = run(app, ["create", "c", "--idempotency-key", "k1"], env)
    assert code == 6 and reused["error"]["code"] in listed["error_codes"]


def test_a_helper_a_literal_code_and_the_default_code_are_listed() -> None:
    app = make_app()
    entry = conflict(app, "put")
    assert entry is not None
    assert entry["error_codes"] == [
        "ALREADY_EXISTS",
        "CONFLICT",
        "IDEMPOTENCY_KEY_REUSED",
        "VERSION_MISMATCH",
    ]
    assert run(app, ["put", "locked"])[1]["error"]["code"] == "VERSION_MISMATCH"
    assert run(app, ["put", "held"])[1]["error"]["code"] == "CONFLICT"


def test_a_safe_command_lists_only_what_its_handler_raises() -> None:
    """No --idempotency-key on a safe command, so no IDEMPOTENCY_KEY_REUSED"""
    entry = conflict(make_app(), "lookup")
    assert entry is not None and entry["error_codes"] == ["ALREADY_EXISTS"]


def test_the_implicit_conflict_of_a_mutating_command_lists_the_reused_key() -> None:
    entry = conflict(make_app(), "touch")
    assert entry is not None and entry["error_codes"] == ["IDEMPOTENCY_KEY_REUSED"]


def test_commands_without_exit_6_are_unchanged() -> None:
    app = make_app()
    assert conflict(app, "show") is None
    assert conflict(app, "watch") is None  # a stream takes no --idempotency-key
    shared = manifest(app)["exit_codes"]
    assert all("error_codes" not in e for e in shared.values())


def test_a_code_the_scan_cannot_read_leaves_the_list_out() -> None:
    """A present list is read as complete: a guess would be a partial list"""
    app = App("dyn", version="1.0.0")
    reason = "BUSY"

    @app.command("var", description="Var", danger_level="mutating", exit_codes=["CONFLICT"])
    def var(args: NameArgs, ctx: Ctx) -> Made:
        raise Exit.CONFLICT("taken", code=reason)

    @app.command("kw", description="Kw", danger_level="mutating", exit_codes=["CONFLICT"])
    def kw(args: NameArgs, ctx: Ctx) -> Made:
        extra: dict[str, Any] = {"code": reason}
        raise Exit.CONFLICT("taken", **extra)

    @app.command("ok", description="Ok", danger_level="mutating", exit_codes=["CONFLICT"])
    def ok(args: NameArgs, ctx: Ctx) -> Made:
        if args.name == "busy":
            raise Exit.CONFLICT("taken", code="BUSY")
        raise Exit.CONFLICT("taken", code="TAKEN")

    for path in ("var", "kw"):
        entry = conflict(app, path)
        assert entry is not None and "error_codes" not in entry, path
    assert run(app, ["var", "x"])[1]["error"]["code"] == "BUSY"
    entry = conflict(app, "ok")
    assert entry is not None
    assert entry["error_codes"] == ["BUSY", "IDEMPOTENCY_KEY_REUSED", "TAKEN"]


def test_exit_conflict_not_called_in_place_leaves_the_list_out() -> None:
    app = App("dyn", version="1.0.0")

    @app.command("alias", description="Alias", danger_level="mutating", exit_codes=["CONFLICT"])
    def alias(args: NameArgs, ctx: Ctx) -> Made:
        make = Exit.CONFLICT
        raise make("taken", code="TAKEN")

    entry = conflict(app, "alias")
    assert entry is not None and "error_codes" not in entry


CONFLICT_NAME = treaty.ExitCodeName("CONFLICT")


def test_an_exit_name_the_scan_cannot_read_leaves_the_list_out() -> None:
    """Each answers exit 6 with LOCKED, which a list of the reused key alone would miss"""
    app = App("dyn", version="1.0.0")

    @app.command("const", description="Const", danger_level="mutating", exit_codes=["CONFLICT"])
    def const(args: NameArgs, ctx: Ctx) -> Made:
        raise treaty.CliExit(CONFLICT_NAME, "taken", code="LOCKED")

    @app.command("built", description="Built", danger_level="mutating", exit_codes=["CONFLICT"])
    def built(args: NameArgs, ctx: Ctx) -> Made:
        which = "CONFLICT"
        raise treaty.CliExit(treaty.ExitCodeName(which), "taken", code="LOCKED")

    @app.command("by-name", description="By", danger_level="mutating", exit_codes=["CONFLICT"])
    def by_name(args: NameArgs, ctx: Ctx) -> Made:
        which = "CONFLICT"
        raise getattr(Exit, which)("taken", code="LOCKED")

    for path in ("const", "built", "by-name"):
        assert run(app, [path, "x"])[1]["error"]["code"] == "LOCKED", path
        entry = conflict(app, path)
        assert entry is not None and "error_codes" not in entry, path


def test_a_handler_without_source_leaves_the_list_out() -> None:
    """The reused key alone would be partial when the handler cannot be read"""
    app = App("gen", version="1.0.0")
    namespace: dict[str, Any] = {"NameArgs": NameArgs, "Ctx": Ctx, "Made": Made}
    source = "def handler(args: NameArgs, ctx: Ctx) -> Made:\n    return Made('created', 'x')\n"
    exec(compile(source, "<generated>", "exec"), namespace)
    app.command("gen", description="Gen", danger_level="mutating", exit_codes=())(
        namespace["handler"]
    )
    entry = conflict(app, "gen")
    assert entry is not None and "error_codes" not in entry


def test_a_passthrough_command_leaves_the_list_out() -> None:
    """Its tool owns its exit codes, 6 among them"""
    app = App("pt", version="1.0.0")

    @app.command(
        "tool", description="Tool", danger_level="mutating", exit_codes=(), passthrough=True
    )
    def tool(args: NoArgs, ctx: Ctx) -> int:
        return 0

    entry = conflict(app, "tool")
    assert entry is not None and "error_codes" not in entry


def test_schema_and_skills_carry_the_list() -> None:
    app = make_app()
    code, envelope = run(app, ["--schema", "create"])
    entry = envelope["data"]["exit_codes"]["6"]
    assert code == 0 and entry["error_codes"] == ["ALREADY_EXISTS", "IDEMPOTENCY_KEY_REUSED"]
    skill = render(app)["SKILL-create.md"]
    assert "error.code is one of ALREADY_EXISTS, IDEMPOTENCY_KEY_REUSED" in skill


def test_declared_exits_reads_already_exists_as_conflict() -> None:
    """A safe command without CONFLICT that answers already_exists exits 1 with
    UNDECLARED_EXIT_CODE; the audit says so before it runs"""
    app = App("aud", version="1.0.0")

    @app.command("find", description="Find", danger_level="safe", exit_codes=())
    def find(args: NameArgs, ctx: Ctx) -> Made:
        raise already_exists(Made("noop", args.name), conflict_id=args.name)

    rules = {r.id: r for r in audit(app, "aud", limit=3).rules}
    findings = rules["declared-exits"].findings
    assert [f.command for f in findings] == ["find"]
    assert "raises CONFLICT" in findings[0].message
    assert run(app, ["find", "x"])[1]["error"]["code"] == "UNDECLARED_EXIT_CODE"
