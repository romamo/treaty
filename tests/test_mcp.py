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
    app = App("regctl", version="2", description="Registry control")

    @app.command("push", description="Push an image", danger_level="mutating", has_network_io=True)
    def push(args: PushArgs, ctx: Ctx) -> Pushed:
        return Pushed("created", args.image, len(args.token))

    @app.command("log.tail", description="Tail the log", streaming=True)
    def tail(args: TailArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        for n in range(args.count):
            yield {"n": n}

    return app


# Tool construction


def test_one_tool_per_command_except_exec_with_dots_as_underscores() -> None:
    names = [e.name for e in tool_entries(adapter_app())]
    assert names == ["log_tail", "manifest", "push", "version"]
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
    envelope = adapter_app().call("push", {"image": "app:1", "token": "abcd"})
    assert envelope.exit_code == 2
    assert envelope.error is not None
    assert "abcd" not in json.dumps(envelope.to_json())


def test_call_reports_validation_errors_and_unknown_commands() -> None:
    app = adapter_app()
    bad = app.call("push", {"image": "app:1", "retries": "many"}, env={"REGCTL_TOKEN": "x"})
    assert bad.exit_code == 2 and bad.error is not None
    assert bad.error.errors is not None and bad.error.errors[0]["field"] == "retries"
    unknown = app.call("nope", {})
    assert unknown.error is not None and unknown.error.code == "UNKNOWN_COMMAND"
    assert unknown.error.context["available"] == ["log.tail", "manifest", "push", "version"]
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
    assert unknown.error.context["available"] == ["log_tail", "manifest", "push", "version"]


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
    assert got["tools"] == ["deploy_rollback", "manifest", "version"]
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

    app = App("wide", version="1", max_output_bytes=4096)

    @app.command("wide", description="Big output")
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

    app = App("mk", version="1", state_dir=None)

    @app.command("mk", description="Make", danger_level="mutating")
    def mk(args: NoArgs, ctx: Ctx) -> Made:
        return Made("created", "x")

    entry = next(e for e in tool_entries(app) if e.name == "mk")
    validator = validator_for(entry.output_schema)(entry.output_schema)
    replay = {"ok": True, "data": {"effect": "noop", "name": "x"}, "error": None}
    validator.validate({**replay, "warnings": [], "meta": {}})
