"""MCP adapter: one tool per command, served in-process over stdio (``treaty[mcp]``).

Tool schemas come from what ``--schema`` already emits: the args dataclass
schema as ``inputSchema`` plus the framework keys the command declares, and
the response envelope around ``output_schema`` as ``outputSchema``. Every
call goes through ``App.call``, the same path as an ``exec`` line, so
confirmation, idempotency, timeouts, effect validation, and output caps all
apply. An unconfirmed destructive call returns the ``CONFIRMATION_REQUIRED``
envelope with its dry-run preview, exactly as the CLI does.

Only ``serve`` and ``main`` import the ``mcp`` package; everything else is
plain data so it can be inspected and tested without the SDK.
"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._app import EXEC_PATH, App, _Run
from ._command import Command, DangerLevel
from ._envelope import Envelope, serialize
from ._errors import CliExit, ParseError
from ._manifest import payload_schema
from ._parse import CONFIRM_FLAG, IDEMPOTENCY_FLAG
from ._schema import JsonSchema
from ._values import CommandPath

CONFIRM_KEY = CONFIRM_FLAG.replace("-", "_")
IDEMPOTENCY_KEY = IDEMPOTENCY_FLAG.replace("-", "_")


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One MCP tool, as plain data"""

    name: str
    path: CommandPath
    description: str
    input_schema: JsonSchema
    output_schema: JsonSchema
    read_only: bool
    destructive: bool
    open_world: bool


def tool_name(path: CommandPath) -> str:
    """``deploy.rollback`` as ``deploy_rollback``; parts never contain ``_`` so this is injective"""
    return path.value.replace(".", "_")


def tool_description(command: Command) -> str:
    text = command.description
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        text += (
            f" Destructive: without {CONFIRM_KEY}=true the call is a dry run and returns "
            "CONFIRMATION_REQUIRED with a preview of what would change."
        )
    elif command.danger_level is DangerLevel.MUTATING:
        text += f" Mutating; pass {IDEMPOTENCY_KEY} to make retries safe."
    if command.streaming:
        text += " Streams on the CLI; here every event is returned in data."
    return text


# Treaty emits draft-07 (tuple ``items`` arrays among it); without this, MCP clients
# validate with the latest draft and reject those schemas
DRAFT_07 = "http://json-schema.org/draft-07/schema#"


def input_schema(command: Command) -> JsonSchema:
    """The ``--raw-payload`` schema; MCP always buffers a stream, so no ``no_stream`` key"""
    return {"$schema": DRAFT_07, **payload_schema(command, stream_key=False)}


def output_schema(command: Command) -> JsonSchema:
    """The response envelope with the command's output schema as ``data``

    ``data`` is checked against the command's schema only on a successful, uncapped
    response: a capped one keeps a prefix that may lack required fields, and a failed
    one carries whatever ``Exit(data=...)`` or a preview put there.
    """
    data = command.output_schema
    if command.streaming:
        data = {"type": "array", "items": data}
    truncated = {
        "properties": {
            "meta": {"properties": {"truncated": {"const": True}}, "required": ["truncated"]}
        }
    }
    return {
        "$schema": DRAFT_07,
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "data": {},
            "error": {"type": ["object", "null"]},
            "warnings": {"type": "array"},
            "meta": {"type": "object"},
        },
        "required": ["ok", "data", "error", "warnings", "meta"],
        "if": truncated,
        "else": {
            "if": {"properties": {"ok": {"const": True}}, "required": ["ok"]},
            "then": {"properties": {"data": {"anyOf": [data, {"type": "null"}]}}},
        },
    }


def tool_entries(app: App) -> list[ToolEntry]:
    """Every command except ``exec``, in path order"""
    entries: list[ToolEntry] = []
    for path, command in sorted(app.commands.items(), key=lambda kv: kv[0].value):
        if path == EXEC_PATH:
            continue
        entries.append(
            ToolEntry(
                name=tool_name(path),
                path=path,
                description=tool_description(command),
                input_schema=input_schema(command),
                output_schema=output_schema(command),
                read_only=command.danger_level is DangerLevel.SAFE,
                destructive=command.danger_level is DangerLevel.DESTRUCTIVE,
                open_world=command.has_network_io,
            )
        )
    return entries


def call_tool(
    app: App,
    entries: Mapping[str, ToolEntry],
    name: str,
    arguments: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> Envelope:
    """Dispatch one tool call; an unknown tool name is an ``UNKNOWN_TOOL`` envelope"""
    entry = entries.get(name)
    if entry is None:
        run = _Run(app, io.StringIO(), io.StringIO(), env if env is not None else {})
        return run.arg_error(
            ParseError(
                f"unknown tool {name!r}", context={"tool": name, "available": sorted(entries)}
            ),
            code="UNKNOWN_TOOL",
            meta={"_cmd": name},
        )
    return app.call(entry.path.value, arguments, env=env)


# The SDK-facing part


def build_server(app: App) -> Any:
    """A low-level ``mcp`` Server whose tools are the app's commands"""
    from mcp import types
    from mcp.server.lowlevel.server import Server

    entries = {e.name: e for e in tool_entries(app)}
    tools = [
        types.Tool(
            name=e.name,
            description=e.description,
            input_schema=e.input_schema,
            output_schema=e.output_schema,
            annotations=types.ToolAnnotations(
                read_only_hint=e.read_only,
                destructive_hint=e.destructive,
                idempotent_hint=e.read_only,
                open_world_hint=e.open_world,
            ),
        )
        for e in entries.values()
    ]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = params.arguments or {}
        envelope = await asyncio.to_thread(call_tool, app, entries, params.name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=serialize(envelope))],
            structured_content=envelope.to_json(),
            is_error=not envelope.ok,
        )

    return Server(
        app.name,
        version=app.version,
        instructions=(
            f"{app.description or app.name}. Every result is a CLI Agent Spec response "
            "envelope: read ok, then data, else error.code and error.fix_required. "
            "Field names use underscores. Call the manifest tool for the full contract."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(app: App) -> None:
    """Run the server over stdio until the client disconnects"""
    from mcp.server.stdio import stdio_server

    server = build_server(app)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    """``treaty-mcp module:app``: serve that app's commands as MCP tools over stdio"""
    from ._cli import load_app

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0].startswith("-"):
        sys.stderr.write("usage: treaty-mcp module:app\n")
        return 2
    try:
        app = load_app(args[0])
    except CliExit as exc:
        sys.stderr.write(f"treaty-mcp: {exc.code}: {exc.message}\n")
        return 2
    try:
        import mcp  # noqa: F401 - probe for the optional dependency
    except ModuleNotFoundError:
        sys.stderr.write("treaty-mcp: the mcp package is missing; install treaty[mcp]\n")
        return 2
    asyncio.run(serve(app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
