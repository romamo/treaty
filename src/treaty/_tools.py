"""MCP tool construction as plain data: one tool per command, its ``inputSchema`` the
``--raw-payload`` schema and its ``outputSchema`` the response envelope. No ``mcp`` import
and no ``App`` import, so ``_builtins`` can compare an app against a saved tool list
(REQ-O-035) without an import cycle."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._command import Command, DangerLevel
from ._completion import COMPLETION_PATH
from ._framework import CONFIRM_FLAG, IDEMPOTENCY_FLAG
from ._manifest import EXEC_PATH, payload_schema
from ._mcp_shared import MCP_SERVE_PATH, NO_BINDINGS, Bindings
from ._refs import EMPTY_DEFS, defs_of, deref, merge_defs, ref_name, with_defs
from ._schema import JsonSchema
from ._values import CommandPath

if TYPE_CHECKING:
    from ._app import App

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
    idempotent: bool
    """A repeat converges: a safe command, or one declared ``idempotent=True``"""
    open_world: bool


def tool_name(path: CommandPath) -> str:
    """``deploy.rollback`` as ``deploy_rollback``; parts never contain ``_`` so this is injective"""
    return path.value.replace(".", "_")


def tool_description(command: Command, bound: Collection[str] = ()) -> str:
    """The command's description with what an MCP client should know; a rule naming a
    field ``McpServe(bind=)`` fixed is left out, since the call cannot pass that field,
    though the rule still applies to the bound value (#285)"""
    text = command.description
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        text += (
            f" Destructive: without {CONFIRM_KEY}=true the call is a dry run and returns "
            "CONFIRMATION_REQUIRED with a preview of what would change."
        )
    elif command.danger_level is DangerLevel.MUTATING and command.streaming:
        text += (
            " Mutating: each event in data carries its own effect and meta.effects counts "
            "them; a repeat runs again."
        )
    elif command.danger_level is DangerLevel.MUTATING:
        text += f" Mutating; pass {IDEMPOTENCY_KEY} to make retries safe."
    if command.streaming:
        text += " Streams on the CLI; here every event is returned in data."
    described = [r for r in command.requires if all(f.key not in bound for f in r.flags)]
    if described:
        # REQ-C-026: inputSchema has no top-level anyOf, which some MCP clients reject
        rules = "; ".join(r.describe() for r in described)
        text += f" Rules: {rules}."
    return text


# Treaty emits draft-07 (tuple ``items`` arrays among it); without this, MCP clients
# validate with the latest draft and reject those schemas
DRAFT_07 = "http://json-schema.org/draft-07/schema#"


def input_schema(command: Command, bound: Collection[str] = ()) -> JsonSchema:
    """The ``--raw-payload`` schema; MCP always buffers a stream, so no ``no_stream`` key.
    ``bound`` names the keys ``McpServe(bind=)`` fixed, which the schema leaves out (#285)"""
    schema = payload_schema(command, stream_key=False)
    if bound:
        properties = schema["properties"]
        assert isinstance(properties, dict)
        schema["properties"] = {k: v for k, v in properties.items() if k not in bound}
        required = [k for k in schema.get("required", ()) if k not in bound]
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
    return {"$schema": DRAFT_07, **schema}


def output_schema(command: Command) -> JsonSchema:
    """The response envelope with the command's output schema as ``data``

    ``data`` is checked against the command's schema only on a successful, whole
    response: a capped one keeps a prefix, and one ``fields`` projected (``meta.fields``)
    a subset, either of which may lack required fields; a failed one carries whatever
    ``Exit(data=...)`` or a preview put there.
    """
    shapes = [command.output_schema, *(c.output_schema for c in command.compat)]
    # A recursive type's $defs move to the envelope's root, where its $ref points
    shapes, defs = merge_defs(shapes)
    if command.streaming:
        shapes = [{"type": "array", "items": s} for s in shapes]
    # schema_version picks an older shape, which its shim's own schema describes
    data = shapes[0] if len(shapes) == 1 else {"anyOf": shapes}
    truncated = {
        "properties": {
            "meta": {
                "anyOf": [
                    {"properties": {"truncated": {"const": True}}, "required": ["truncated"]},
                    {"required": ["fields"]},
                ]
            }
        }
    }
    envelope: JsonSchema = {
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
    return with_defs(envelope, defs)


def tool_entries(
    app: App,
    served: frozenset[CommandPath] | None = None,
    bindings: Bindings = NO_BINDINGS,
) -> list[ToolEntry]:
    """Every command except ``exec``, the ``completion`` built-in, a script for a shell
    rather than a tool, the ``mcp serve`` built-in, the server itself (#239), passthrough
    commands, whose arguments and stdout belong to another tool that no input or output
    schema describes (#35), and a command registered ``mcp=False`` (#281), in path order.
    ``served``, what ``McpServe(commands=)`` selected, keeps only those paths; None keeps
    every one. A field ``bindings`` fixes leaves the tool's input schema, and a rule
    naming it its description (#285)"""
    entries: list[ToolEntry] = []
    for path, command in sorted(app.commands.items(), key=lambda kv: kv[0].value):
        if path == EXEC_PATH or (
            path in (COMPLETION_PATH, MCP_SERVE_PATH) and path in app.builtins
        ):
            continue
        if command.passthrough or not command.mcp:
            continue
        if served is not None and path not in served:
            continue
        bound = bindings.for_command(command)
        entries.append(
            ToolEntry(
                name=tool_name(path),
                path=path,
                description=tool_description(command, bound),
                input_schema=input_schema(command, bound),
                output_schema=output_schema(command),
                read_only=command.danger_level is DangerLevel.SAFE,
                destructive=command.danger_level is DangerLevel.DESTRUCTIVE,
                idempotent=command.danger_level is DangerLevel.SAFE or command.idempotent,
                open_world=command.has_network_io,
            )
        )
    return entries


def tool_list(
    app: App,
    *,
    extra: Sequence[ToolEntry] = (),
    served: frozenset[CommandPath] | None = None,
    bindings: Bindings = NO_BINDINGS,
) -> dict[str, object]:
    """``treaty-mcp module:app --list-tools``: the tools as MCP's ``tools/list`` names
    them, with the CLI version, to commit and compare with ``mcp-validate`` (REQ-O-035);
    ``extra`` are the tools ``mcp serve`` provides beside the commands (#240), and
    ``served`` the commands it selected (#281), and ``bindings`` the fields it fixed (#285)"""
    return {
        "cli_version": app.version,
        "tools": [
            {
                "name": e.name,
                "description": e.description,
                "inputSchema": e.input_schema,
                "outputSchema": e.output_schema,
                "annotations": {
                    "readOnlyHint": e.read_only,
                    "destructiveHint": e.destructive,
                    "idempotentHint": e.idempotent,
                    "openWorldHint": e.open_world,
                },
            }
            for e in (*tool_entries(app, served, bindings), *extra)
        ],
    }


def _type(schema: object, defs: Mapping[str, JsonSchema] = EMPTY_DEFS) -> str:
    if not isinstance(schema, dict):
        return "any"
    if ref_name(schema) in defs:
        schema = deref(schema, defs)
    kind = schema.get("type")
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    if isinstance(kind, str):
        return kind
    members = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(members, list):
        return "|".join(_type(m, defs) for m in members)
    return "enum" if "enum" in schema else "any"


def tool_fields(tool: Mapping[str, object]) -> dict[str, str]:
    """``input.<flag>`` and ``data.<field>`` of one listed tool, each with its JSON type"""
    fields: dict[str, str] = {}
    inputs = tool.get("inputSchema")
    properties = inputs.get("properties") if isinstance(inputs, dict) else None
    for name, schema in (properties or {}).items():
        fields[f"input.{name}"] = _type(schema)
    data: object = tool.get("outputSchema")
    defs = defs_of(data) if isinstance(data, dict) else EMPTY_DEFS
    for key in ("else", "then", "properties", "data", "anyOf"):
        data = data.get(key) if isinstance(data, dict) else None
    data = data[0] if isinstance(data, list) and data else None
    if isinstance(data, dict) and data.get("type") == "array":
        data = data.get("items")
    members = data.get("oneOf") if isinstance(data, dict) else None
    # An output union: the fields of each member, a name two share by its first
    for member in members if isinstance(members, list) else [data]:
        properties = member.get("properties") if isinstance(member, dict) else None
        for name, schema in (properties or {}).items():
            fields.setdefault(f"data.{name}", _type(schema, defs))
    return fields
