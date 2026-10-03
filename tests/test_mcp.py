"""MCP adapter: one tool per command over App.call, served in-process (treaty[mcp])."""

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs
from treaty._mcp import call_tool, input_schema, output_schema, tool_entries, tool_name

mcp = pytest.importorskip("mcp")


@dataclass(frozen=True, slots=True)
class PushArgs:
    image: str = Arg(description="Image to push")
    token: str = Flag(description="Registry token")
    retries: int = Flag(default=1, description="Attempts")


@dataclass(frozen=True, slots=True)
class Pushed:
    effect: str
    image: str
    token_len: int


@dataclass(frozen=True, slots=True)
class TailArgs:
    count: int = Arg(description="Events")


def adapter_app() -> App:
    app = App("regctl", version="2.0.0", description="Registry control")

    @app.command(
        "push",
        description="Push an image",
        danger_level="mutating",
        has_network_io=True,
        exit_codes=(),
    )
    def push(args: PushArgs, ctx: Ctx) -> Pushed:
        return Pushed("created", args.image, len(args.token))

    @app.command(
        "log.tail", description="Tail the log", streaming=True, danger_level="safe", exit_codes=()
    )
    def tail(args: TailArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        for n in range(args.count):
            yield {"n": n}

    return app


# Tool construction


def test_one_tool_per_command_except_exec_with_dots_as_underscores() -> None:
    """cleanup, generate-skills, and audit-log are mcp=False built-ins (#281)"""
    names = [e.name for e in tool_entries(adapter_app())]
    assert names == [
        "doctor",
        "log_tail",
        "manifest",
        "mcp-validate",
        "push",
        "status",
        "version",
    ]
    assert tool_name(next(e.path for e in tool_entries(adapter_app()) if e.name == "log_tail")) == (
        "log_tail"
    )


def test_input_schema_replaces_secrets_and_adds_framework_keys() -> None:
    app = adapter_app()
    command = next(c for p, c in app.commands.items() if p.value == "push")
    schema = input_schema(command)
    assert set(schema["properties"]) == {
        "image",
        "token_from_env",
        "token_from_file",
        "retries",
        "timeout",
        "idempotency_key",
        "proxy",
        "no_proxy",
        "validate_only",
        "stable_output",
        "fields",
    }
    assert "token" not in schema["properties"]
    assert schema["required"] == ["image"]
    assert schema["properties"]["image"] == {"type": "string", "description": "Image to push"}
    assert schema["additionalProperties"] is False


def test_destructive_tools_get_confirm_destructive_and_hints(app: App) -> None:
    entry = next(e for e in tool_entries(app) if e.name == "deploy_rollback")
    assert entry.destructive and not entry.read_only and entry.open_world
    assert entry.input_schema["properties"]["confirm_destructive"]["default"] is False
    assert "CONFIRMATION_REQUIRED" in entry.description


def test_output_schema_wraps_the_envelope_and_streams_become_arrays() -> None:
    app = adapter_app()
    push = next(c for p, c in app.commands.items() if p.value == "push")
    tail = next(c for p, c in app.commands.items() if p.value == "log.tail")
    assert (
        output_schema(push)["else"]["then"]["properties"]["data"]["anyOf"][0]["title"] == "Pushed"
    )
    assert output_schema(tail)["else"]["then"]["properties"]["data"]["anyOf"][0] == {
        "type": "array",
        "items": {"type": "object", "additionalProperties": {"type": "integer"}},
    }
    assert "in data" in next(e for e in tool_entries(app) if e.name == "log_tail").description


# App.call and call_tool


def test_call_runs_a_command_from_json_values_with_secret_from_env(tmp_path: Path) -> None:
    app = adapter_app()
    envelope = app.call(
        "push", {"image": "app:1", "token_from_env": "REG_TOKEN"}, env={"REG_TOKEN": "abcd"}
    )
    assert envelope.ok
    assert envelope.data == {"effect": "created", "image": "app:1", "token_len": 4}
    assert envelope.extra_meta["_cmd"] == "push"
    spec_validator("response-envelope").validate(envelope.to_json())


def test_call_refuses_direct_secret_values() -> None:
    envelope = adapter_app().call("push", {"image": "app:1", "token": "s3cret-token-value"})
    assert envelope.exit_code == 2
    assert envelope.error is not None
    assert "s3cret-token-value" not in json.dumps(envelope.to_json())


def test_call_reports_validation_errors_and_unknown_commands() -> None:
    app = adapter_app()
    bad = app.call("push", {"image": "app:1", "retries": "many"}, env={"REGCTL_TOKEN": "x"})
    assert bad.exit_code == 2 and bad.error is not None
    assert bad.error.errors is not None and bad.error.errors[0]["field"] == "retries"
    unknown = app.call("nope", {})
    assert unknown.error is not None and unknown.error.code == "UNKNOWN_COMMAND"
    assert unknown.error.context["available"] == [
        "audit-log",
        "cleanup",
        "completion",
        "doctor",
        "generate-skills",
        "log.tail",
        "manifest",
        "mcp-validate",
        "push",
        "status",
        "version",
    ]
    assert app.call("exec", {}).error is not None
    assert app.call("Not A Path", {}).error is not None


def test_call_buffers_streaming_commands() -> None:
    envelope = adapter_app().call("log.tail", {"count": 2})
    assert envelope.ok
    assert envelope.data == [{"n": 0}, {"n": 1}]
    assert envelope.extra_meta["total"] == 2


def test_call_tool_maps_names_and_passes_unknown_through() -> None:
    app = adapter_app()
    entries = {e.name: e for e in tool_entries(app)}
    assert call_tool(app, entries, "log_tail", {"count": 1}).data == [{"n": 0}]
    unknown = call_tool(app, entries, "log.tail", {"count": 1})
    assert unknown.error is not None and unknown.error.code == "UNKNOWN_TOOL"
    assert unknown.error.context["available"] == [
        "doctor",
        "log_tail",
        "manifest",
        "mcp-validate",
        "push",
        "status",
        "version",
    ]


def test_call_writes_nothing_to_the_process_streams(capsys: pytest.CaptureFixture[str]) -> None:
    adapter_app().call("version", {})
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


# End to end over stdio


def test_stdio_server_lists_tools_and_dispatches_calls() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> dict[str, object]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "treaty._mcp", "examples.deployctl:app"],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            preview = await session.call_tool("deploy_rollback", {"service": "api", "to": "1.3.9"})
            applied = await session.call_tool(
                "deploy_rollback", {"service": "api", "to": "1.3.9", "confirm_destructive": True}
            )
            unknown = await session.call_tool("nope", {})
            return {
                "server": (init.server_info.name, init.server_info.version),
                "tools": sorted(t.name for t in tools.tools),
                "rollback": next(t for t in tools.tools if t.name == "deploy_rollback"),
                "preview": preview,
                "applied": applied,
                "unknown": unknown,
            }

    got = asyncio.run(scenario())
    assert got["server"] == ("deployctl", "1.4.0")
    assert got["tools"] == [
        "config_set",
        "deploy_rollback",
        "deploy_start",
        "doctor",
        "job_cancel",
        "job_status",
        "manifest",
        "mcp-validate",
        "status",
        "version",
    ]
    rollback = got["rollback"]
    assert rollback.annotations.destructive_hint is True  # type: ignore[attr-defined]
    assert rollback.input_schema["required"] == ["service"]  # type: ignore[attr-defined]
    preview = got["preview"]
    assert preview.is_error is True  # type: ignore[attr-defined]
    body = preview.structured_content  # type: ignore[attr-defined]
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert body["data"]["effect"] == "would_update"
    assert json.loads(preview.content[0].text) == body  # type: ignore[attr-defined]
    applied = got["applied"].structured_content  # type: ignore[attr-defined]
    assert applied["ok"] is True and applied["data"]["effect"] == "updated"
    unknown = got["unknown"].structured_content  # type: ignore[attr-defined]
    assert unknown["error"]["code"] == "UNKNOWN_TOOL"


def test_stdio_server_turns_a_stray_input_into_exit_4() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "treaty._mcp", "fixture_prompt_app:app"],
            cwd=str(Path(__file__).resolve().parent),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            asked = await session.call_tool("ask", {})
            after = await session.call_tool("version", {})
            return asked.structured_content, after.structured_content  # type: ignore[return-value]

    asked, after = asyncio.run(scenario())
    assert asked["error"]["code"] == "INTERACTIVE_BLOCKED"  # type: ignore[index]
    assert asked["meta"]["exit_code"] == 4  # type: ignore[index]
    assert after["ok"] is True


def test_stdio_server_redacts_a_handlers_print_on_stderr(tmp_path: Path) -> None:
    """The server points ``sys.stdout`` at stderr: what a handler prints or writes to
    stderr during its call lands there redacted, and the protocol on stdout still works
    (#141)"""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    token = "sk-live-abcdef123456"
    errlog = tmp_path / "stderr.txt"

    async def scenario() -> dict[str, object]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "treaty._mcp", "fixture_print_app:app"],
            cwd=str(Path(__file__).resolve().parent),
            env={"PRINTCTL_API_TOKEN": token, "PRINTCTL_AUDIT_LOG": "0"},
        )
        with errlog.open("w", encoding="utf-8") as err:
            async with (
                stdio_client(params, errlog=err) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                shown = await session.call_tool("show", {})
                return shown.structured_content  # type: ignore[return-value]

    shown = asyncio.run(scenario())
    assert shown["ok"] is True and shown["data"] == {"ok": True}
    written = errlog.read_text(encoding="utf-8")
    assert token not in written, written
    assert ["out [REDACTED]", "err [REDACTED]"] == [
        line for line in written.splitlines() if line.startswith(("out ", "err "))
    ], written


def test_unknown_field_lists_only_flags_a_mapping_accepts() -> None:
    app = App("files", version="1.0.0")

    @app.command(
        "dump",
        description="Dump",
        danger_level="safe",
        exit_codes=(),
        heartbeat=True,
        output_file=True,
        has_network_io=True,
    )
    def dump(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": 1}

    envelope = app.call("dump", {"nope": 1})
    assert envelope.error is not None
    known = envelope.error.context["known"]
    # Not heartbeat-ms or output, which only argv takes
    assert known == ["timeout", "proxy", "no-proxy", "validate-only", "stable-output", "fields"]


def test_console_script_usage_errors() -> None:
    from treaty._mcp import main

    assert main([]) == 2
    assert main(["--help"]) == 2
    assert main(["examples.nothing:app"]) == 2


def test_capped_and_tuple_outputs_validate_like_an_mcp_client() -> None:
    from dataclasses import dataclass

    from jsonschema.validators import validator_for

    @dataclass(frozen=True, slots=True)
    class Wide:
        pair: tuple[int, str]
        fields: dict[str, str]

    app = App("wide", version="1.0.0", max_output_bytes=4096)

    @app.command("wide", description="Big output", danger_level="safe", exit_codes=())
    def wide(args: NoArgs, ctx: Ctx) -> Wide:
        return Wide((1, "a"), {f"k{i}": "x" * 300 for i in range(20)})

    entry = next(e for e in tool_entries(app) if e.name == "wide")
    envelope = app.call("wide", {}, env={})
    assert envelope.extra_meta["truncated"] is True
    validator = validator_for(entry.output_schema)
    validator.check_schema(entry.output_schema)
    validator(entry.output_schema).validate(envelope.to_json())


def test_replayed_noop_matches_a_closed_effect_enum() -> None:
    from dataclasses import dataclass
    from typing import Literal

    from jsonschema.validators import validator_for

    @dataclass(frozen=True, slots=True)
    class Made:
        effect: Literal["created"]
        name: str

    app = App("mk", version="1.0.0", state_dir=None)

    @app.command("mk", description="Make", danger_level="mutating", exit_codes=())
    def mk(args: NoArgs, ctx: Ctx) -> Made:
        return Made("created", "x")

    entry = next(e for e in tool_entries(app) if e.name == "mk")
    validator = validator_for(entry.output_schema)(entry.output_schema)
    replay = {"ok": True, "data": {"effect": "noop", "name": "x"}, "error": None}
    validator.validate({**replay, "warnings": [], "meta": {}})


def test_an_old_tool_name_answers_redirected_as_on_the_command_line() -> None:
    """A client that learned the old name gets the new path, not UNKNOWN_TOOL"""
    app = App("x", version="1.0.0")

    @app.command("mark-read", description="Mark it read", danger_level="safe", exit_codes=())
    def mark_read(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"read": True}

    app.redirect("read", to="mark-read")
    entries = {e.name: e for e in tool_entries(app)}
    assert "read" not in entries
    envelope = call_tool(app, entries, "read", {})
    assert envelope.exit_code == 13 and envelope.error is not None
    assert envelope.error.code == "REDIRECTED"
    assert call_tool(app, entries, "nosuch", {}).error.code == "UNKNOWN_TOOL"  # type: ignore[union-attr]


def test_a_redirect_over_mcp_names_the_tool_to_call() -> None:
    app = App("x", version="1.0.0")
    labels = app.group("labels", description="Labels")

    @labels.command("list", description="List labels", danger_level="safe", exit_codes=())
    def list_labels(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    app.redirect("tags.list", to="labels.list")
    entries = {e.name: e for e in tool_entries(app)}
    error = call_tool(app, entries, "tags_list", {}).error
    assert error is not None and error.redirect is not None
    assert (
        error.redirect.command == "labels_list" and error.suggestion == "call labels_list instead"
    )
    assert error.context == {"from": "tags_list", "to": "labels_list"}


def test_a_redirect_message_over_mcp_names_tools() -> None:
    app = App("x", version="1.0.0")
    db = app.group("db", description="Database")

    @db.command("up", description="Migrate up", danger_level="safe", exit_codes=())
    def up(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    app.redirect("db.upgrade", to="db.up")
    entries = {e.name: e for e in tool_entries(app)}
    error = call_tool(app, entries, "db_upgrade", {}).error
    assert error is not None and error.message == "Tool db_upgrade is now db_up."
