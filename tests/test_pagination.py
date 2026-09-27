"""Pagination: REQ-F-018, REQ-F-019, REQ-O-003, and the runnable hint of REQ-F-052."""

import io
import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, Page, RegistrationError
from treaty._audit import audit
from treaty._cap import MIN_BYTES
from treaty._page import Position
from treaty._values import CommandPath

ITEMS = [{"id": f"item-{i}"} for i in range(50)]


@dataclass(frozen=True, slots=True)
class Item:
    id: str


def make_app(max_output_bytes: int = 1_048_576) -> App:
    app = App("listctl", version="1.0.0", max_output_bytes=max_output_bytes)

    @app.command(
        "items",
        description="Every item",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
    )
    def items(args: NoArgs, ctx: Ctx) -> list[dict[str, str]]:
        return ITEMS

    @app.command(
        "paged",
        description="Items from a source with its own cursor",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
    )
    def paged(args: NoArgs, ctx: Ctx) -> Page[Item]:
        assert ctx.page is not None
        start = int(ctx.page.cursor or 0)
        end = len(ITEMS) if ctx.page.limit is None else start + ctx.page.limit
        rows = [Item(r["id"]) for r in ITEMS[start:end]]
        after = str(end) if end < len(ITEMS) else None
        return Page(items=rows, next_cursor=after, total=len(ITEMS))

    @app.command(
        "batches",
        description="Fixed batches of 30, whatever the limit",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
        default_limit=7,
    )
    def batches(args: NoArgs, ctx: Ctx) -> Page[Item]:
        assert ctx.page is not None
        start = int(ctx.page.cursor or 0)
        rows = [Item(r["id"]) for r in ITEMS[start : start + 30]]
        return Page(items=rows, next_cursor=str(start + 30) if start + 30 < 50 else None)

    @app.command(
        "many",
        description="Ten thousand items",
        danger_level="safe",
        exit_codes=(),
        paginated=True,
        ordered=True,
    )
    def many(args: NoArgs, ctx: Ctx) -> list[dict[str, object]]:
        return [{"id": i, "name": f"item-{i}"} for i in range(10_000)]

    return app


def call(
    app: App, argv: list[str], *, env: dict[str, str] | None = None, stdin: str = ""
) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def test_f018_every_list_response_has_pagination_even_when_complete() -> None:
    code, env = call(make_app(), ["items", "--limit", "0"])
    assert code == 0 and set(env) == {"ok", "data", "error", "warnings", "meta"}
    assert env["meta"]["pagination"] == {
        "total": 50,
        "returned": 50,
        "truncated": False,
        "has_more": False,
        "next_cursor": None,
    }


def test_f019_no_flags_returns_the_default_twenty() -> None:
    code, env = call(make_app(), ["items"])
    page = env["meta"]["pagination"]
    assert code == 0 and env["data"] == ITEMS[:20]
    assert page["returned"] == 20 and page["total"] == 50
    assert page["truncated"] is True and page["has_more"] is True
    assert isinstance(page["next_cursor"], str)


@pytest.mark.parametrize("command", ["items", "paged"])
def test_o003_following_next_cursor_returns_the_subsequent_pages(command: str) -> None:
    app = make_app()
    seen: list[object] = []
    sizes: list[int] = []
    argv = [command]
    while True:
        code, env = call(app, argv)
        assert code == 0
        seen.extend(env["data"])
        sizes.append(env["meta"]["pagination"]["returned"])
        cursor = env["meta"]["pagination"]["next_cursor"]
        if cursor is None:
            assert env["meta"]["pagination"]["truncated"] is False
            break
        argv = [command, "--cursor", cursor]
    assert sizes == [20, 20, 10] and seen == ITEMS


def test_f019_limit_overrides_the_default() -> None:
    app = make_app()
    assert len(call(app, ["items", "--limit", "5"])[1]["data"]) == 5
    code, env = call(app, ["items", "--limit", "100"])
    assert code == 0 and len(env["data"]) == 50
    assert env["meta"]["pagination"]["next_cursor"] is None


def test_default_limit_is_per_command_and_a_batch_is_sliced() -> None:
    """A handler returning more than asked is cut, and the cursor resumes inside its batch"""
    app = make_app()
    seen: list[object] = []
    argv = ["batches"]
    while True:
        code, env = call(app, argv)
        assert code == 0 and len(env["data"]) <= 7
        seen.extend(env["data"])
        cursor = env["meta"]["pagination"]["next_cursor"]
        if cursor is None:
            break
        argv = ["batches", "--cursor", cursor]
    assert seen == ITEMS
    assert env["meta"]["pagination"]["total"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor!",
        "eyJub3QiOiJvdXJzIn0",  # {"not":"ours"}
        Position("x").encode(CommandPath("paged")),  # another command's
    ],
)
def test_o003_invalid_cursor_is_a_structured_argument_error(cursor: str) -> None:
    code, env = call(make_app(), ["items", "--cursor", cursor])
    assert code == 2 and env["error"]["code"] == "INVALID_CURSOR"
    assert env["error"]["phase"] == "validation"
    assert "without --cursor" in env["error"]["suggestion"]


def test_o003_cursor_is_url_safe() -> None:
    _, env = call(make_app(), ["items"])
    cursor = env["meta"]["pagination"]["next_cursor"]
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize("limit", ["-1", "ten", "1.5"])
def test_bad_limit_exits_2(limit: str) -> None:
    code, env = call(make_app(), ["items", f"--limit={limit}"])
    assert code == 2 and env["error"]["errors"][0]["field"] == "limit"


def test_f019_default_limit_is_in_schema_and_manifest() -> None:
    app = make_app()
    _, env = call(app, ["items", "--schema"])
    assert env["data"]["default_limit"] == 20 and env["data"]["paginated"] is True
    assert env["data"]["flags"]["limit"]["default"] == 20
    _, env = call(app, ["batches", "--schema"])
    assert env["data"]["default_limit"] == 7
    _, env = call(app, ["manifest"])
    spec_validator("manifest-response").validate(env["data"])
    assert set(env["data"]["commands"]["items"]["flags"]) == {"limit", "cursor"}


def test_exec_and_call_take_limit_and_cursor() -> None:
    app = make_app()
    envelope = app.call("items", {"limit": 3})
    assert envelope.data == ITEMS[:3]
    cursor = envelope.extra_meta["pagination"]["next_cursor"]
    assert app.call("items", {"limit": 3, "cursor": cursor}).data == ITEMS[3:6]
    out = io.StringIO()
    plan = json.dumps({"_cmd": "paged", "_opts": {"limit": 2}}) + "\n"
    code = app.run(["exec"], stdin=io.StringIO(plan), stdout=out, env={}, isatty=False)
    line = json.loads(out.getvalue())
    assert code == 0 and [r["id"] for r in line["data"]] == ["item-0", "item-1"]


def test_f052_cut_page_hint_runs_and_continues_where_the_cut_ended() -> None:
    app = make_app(max_output_bytes=MIN_BYTES)
    code, env = call(app, ["many", "--limit", "0"])
    meta = env["meta"]
    kept = len(env["data"])
    assert code == 0 and meta["truncated"] is True and 1 < kept < 10_000
    assert meta["pagination"]["returned"] == kept and meta["pagination"]["has_more"] is True
    assert meta["total_count"] == 10_000 and meta["returned_count"] == kept
    argv = shlex.split(meta["truncation_hint"])
    assert argv[:2] == ["listctl", "many"] and argv.count("--limit") == 1
    code, env = call(app, argv[1:])
    assert code == 0 and env["data"][0]["id"] == kept
    # The page cursor in meta and the hint agree
    assert meta["pagination"]["next_cursor"] == argv[argv.index("--cursor") + 1]


def test_f052_tool_env_var_sets_the_cap() -> None:
    app = make_app(max_output_bytes=MIN_BYTES)
    _, env = call(app, ["many", "--limit", "0"], env={"LISTCTL_MAX_OUTPUT_BYTES": "5242880"})
    assert "truncated" not in env["meta"] and len(env["data"]) == 10_000


def test_registration_checks() -> None:
    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="paginated=False"):

        @app.command("a", description="a", danger_level="safe", exit_codes=(), paginated=False)
        def a(args: NoArgs, ctx: Ctx) -> Page[Item]:
            return Page(items=[])

    with pytest.raises(RegistrationError, match="list"):

        @app.command("b", description="b", danger_level="safe", exit_codes=(), paginated=True)
        def b(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}

    with pytest.raises(RegistrationError, match="stream"):

        @app.command(
            "c",
            description="c",
            danger_level="safe",
            exit_codes=(),
            paginated=True,
            streaming=True,
        )
        def c(args: NoArgs, ctx: Ctx) -> Iterator[Item]:
            yield Item("x")

    @dataclass(frozen=True, slots=True)
    class Limited:
        limit: int = Flag(default=1, description="Taken by the framework")

    with pytest.raises(RegistrationError, match="limit"):

        @app.command("d", description="d", danger_level="safe", exit_codes=(), paginated=True)
        def d(args: Limited, ctx: Ctx) -> list[Item]:
            return []


def test_wrong_runtime_output_is_invalid_output() -> None:
    app = App("x", version="1.0.0")

    @app.command("a", description="a", danger_level="safe", exit_codes=(), paginated=True)
    def a(args: NoArgs, ctx: Ctx) -> list[Item]:
        return {"not": "a list"}  # type: ignore[return-value]

    code, env = call(app, ["a"])
    assert code == 1 and env["error"]["code"] == "INVALID_OUTPUT"


def test_f018_every_list_output_is_paginated_by_default() -> None:
    app = App("x", version="1.0.0")

    @app.command("ls", description="ls", danger_level="safe", exit_codes=())
    def ls(args: NoArgs, ctx: Ctx) -> list[Item]:
        return [Item(str(i)) for i in range(30)]

    @app.command("page", description="page", danger_level="safe", exit_codes=())
    def page(args: NoArgs, ctx: Ctx) -> Page[Item]:
        return Page(items=[Item("x")])

    @app.command("stream", description="s", danger_level="safe", exit_codes=(), streaming=True)
    def stream(args: NoArgs, ctx: Ctx) -> Iterator[list[Item]]:
        yield []

    assert app.commands[CommandPath("ls")].paginated
    assert app.commands[CommandPath("page")].paginated
    assert not app.commands[CommandPath("stream")].paginated
    code, env = call(app, ["ls"])
    assert code == 0 and len(env["data"]) == 20 and env["meta"]["pagination"]["has_more"]


def test_audit_advises_on_a_list_command_that_opts_out() -> None:
    app = App("x", version="1.0.0")

    @app.command("ls", description="ls", danger_level="safe", exit_codes=(), paginated=False)
    def ls(args: NoArgs, ctx: Ctx) -> list[Item]:
        return []

    code, env = call(app, ["ls"])
    assert code == 0 and "pagination" not in env["meta"]
    rule = next(r for r in audit(app, "x", limit=5).rules if r.id == "paginated-list")
    assert rule.severity == "advice" and rule.findings[0].command == "ls"
