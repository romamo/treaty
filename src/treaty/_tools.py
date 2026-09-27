"""MCP tool construction as plain data: one tool per command, its ``inputSchema`` the
``--raw-payload`` schema and its ``outputSchema`` the response envelope. No ``mcp`` import
and no ``App`` import, so ``_builtins`` can compare an app against a saved tool list
(REQ-O-035) without an import cycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._command import Command, DangerLevel
from ._framework import CONFIRM_FLAG, IDEMPOTENCY_FLAG
from ._manifest import payload_schema
from ._schema import JsonSchema
from ._values import CommandPath

if TYPE_CHECKING:
    from ._app import App

CONFIRM_KEY = CONFIRM_FLAG.replace("-", "_")
IDEMPOTENCY_KEY = IDEMPOTENCY_FLAG.replace("-", "_")
EXEC_PATH = CommandPath("exec")


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
