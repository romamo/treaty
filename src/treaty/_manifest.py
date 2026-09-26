"""Manifest builder: the registry serialized as ``manifest-response.json``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from ._command import Command, DangerLevel
from ._exit import ExitCodeRegistry, FrameworkCode
from ._mode import OutputMode
from ._parse import CONFIRM_FLAG, IDEMPOTENCY_FLAG, NO_STREAM_FLAG, TIMEOUT_FLAG
from ._schema import JsonSchema
from ._values import CommandPath, Etag

SCHEMA_VERSION = "3.0"
CONFIRM_KEY = CONFIRM_FLAG.replace("-", "_")
IDEMPOTENCY_KEY = IDEMPOTENCY_FLAG.replace("-", "_")
NO_STREAM_KEY = NO_STREAM_FLAG.replace("-", "_")
TIMEOUT_KEY = TIMEOUT_FLAG
# TIMEOUT is shared: every handler runs under a deadline unless it is set to 0
_ALWAYS = (
    FrameworkCode.SUCCESS,
    FrameworkCode.GENERAL_ERROR,
    FrameworkCode.ARG_ERROR,
    FrameworkCode.TIMEOUT,
)


# REQ-F-079: split_globals accepts these anywhere on every command path
GLOBAL_FLAG_ENTRIES: dict[str, object] = {
    "format": {
        "type": "enum",
        "required": False,
        "enum_values": [m.value for m in OutputMode],
        "description": "Output representation; defaults to json when stdout is not a terminal",
    },
    "max-output": {
        "type": "integer",
        "required": False,
        "description": "Largest stdout envelope in bytes (at least 4096) before truncation",
    },
    "schema": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Print the command's input and output schema instead of running it",
    },
    "help": {
        "type": "boolean",
        "required": False,
        "default": False,
        "short": "h",
        "description": "Print the command's help instead of running it",
    },
}


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def shared_exit_codes(exits: ExitCodeRegistry) -> dict[str, object]:
    """The table every command inherits: success, the two framework failures, and signals"""
    table: dict[str, object] = {}
    for code in _ALWAYS:
        entry = exits.framework(code)
        table[str(entry.code.value)] = entry.to_json()
    for signal_code in (130, 143):
        entry = exits.by_code(signal_code)
        table[str(signal_code)] = entry.to_json()
    return table


def command_entry(
    command: Command,
    exits: ExitCodeRegistry,
    all_paths: Mapping[CommandPath, Command],
    *,
    shared: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """One CommandEntry; with ``shared`` given, entries equal to the shared table are hoisted"""
    exit_codes: dict[str, object] = dict(shared_exit_codes(exits))
    for name in command.exit_codes:
        entry = exits.by_name(name)
        exit_codes[str(entry.code.value)] = entry.to_json()
    if shared is not None:
        exit_codes = {k: v for k, v in exit_codes.items() if shared.get(k) != v}
    flags: dict[str, object] = {}
    for f in command.fields:
        flags.update(f.to_flag_entries())
    if command.has_network_io:
        flags["timeout"] = {
            "type": "number",
            "required": False,
            "description": "Seconds before the framework aborts with TIMEOUT; 0 disables the limit",
        }
    if command.supports_raw_payload:
        flags["raw-payload"] = {
            "type": "string",
            "required": False,
            "description": "JSON object of field values; cannot be combined with individual flags",
        }
    if command.danger_level is not DangerLevel.SAFE:
        flags["idempotency-key"] = {
            "type": "string",
            "required": False,
            "description": "Repeat calls with the same key return the original result "
            "with effect noop instead of running again",
        }
        # A reused key is CONFLICT; an unusable state directory or record is PRECONDITION
        for code in (FrameworkCode.CONFLICT, FrameworkCode.PRECONDITION):
            entry = exits.framework(code)
            exit_codes.setdefault(str(entry.code.value), entry.to_json())
    if command.streaming:
        flags["no-stream"] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Return one envelope with every event in data instead of "
            "one envelope line per event",
        }
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        flags["confirm-destructive"] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Required to apply; without it the command previews and exits 2",
        }
    out: dict[str, object] = {
        "description": command.description,
        "danger_level": command.danger_level.value,
        "required_scopes": [s.value for s in command.required_scopes],
        "flags": flags,
        "exit_codes": exit_codes,
        "output_schema": command.output_schema,
    }
    positionals = [f.to_positional_entry() for f in command.fields if f.positional]
    if positionals:
        out["positionals"] = positionals
    children = sorted(p.value for p in all_paths if p.is_direct_child_of(command.path))
    if children:
        out["subcommands"] = children
    if command.examples:
        out["examples"] = [e.to_json() for e in command.examples]
    if command.has_network_io:
        out["has_network_io"] = True
    if command.streaming:
        out["streaming_default"] = True
    if command.secret_env_vars:
        out["secret_env_vars"] = [
            command.secret_env_vars[f.name] for f in command.fields if f.secret
        ]
    return out


def command_schema(
    command: Command, exits: ExitCodeRegistry, all_paths: Mapping[CommandPath, Command]
) -> dict[str, object]:
    """``--schema`` output for one command (REQ-C-015, REQ-O-032)"""
    entry = command_entry(command, exits, all_paths)
    entry["parameters"] = entry["flags"]
    if command.supports_raw_payload:
        entry["raw_payload_schema"] = payload_schema(command)
    return entry


def payload_schema(command: Command, *, stream_key: bool = True) -> JsonSchema:
    """Every key a JSON payload may carry: the args schema with secrets replaced by their
    sources, plus the framework keys the command declares"""
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []
    base = command.args_schema
    for f in command.fields:
        if f.secret:
            what = f.spec.description
            properties[f"{f.name}_from_env"] = {
                "type": "string",
                "description": f"Name of the environment variable holding: {what}",
            }
            properties[f"{f.name}_from_file"] = {
                "type": "string",
                "description": f"Path of the file holding: {what}",
            }
            continue
        prop = dict(base["properties"][f.name])
        prop["description"] = f.spec.description
        properties[f.name] = prop
        if f.required:  # an X | None field without a default is optional, as the parser says
            required.append(f.name)
    if command.has_network_io:
        properties[TIMEOUT_KEY] = {
            "type": "number",
            "description": "Seconds before the framework aborts with TIMEOUT; 0 disables it",
        }
    if command.danger_level is not DangerLevel.SAFE:
        properties[IDEMPOTENCY_KEY] = {
            "type": "string",
            "description": "Repeat calls with the same key return the original result "
            "with effect noop instead of running again",
        }
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        properties[CONFIRM_KEY] = {
            "type": "boolean",
            "default": False,
            "description": "Required to apply; without it the command previews and fails "
            "with CONFIRMATION_REQUIRED",
        }
    if command.streaming and stream_key:
        properties[NO_STREAM_KEY] = {
            "type": "boolean",
            "default": False,
            "description": "Return one envelope with every event in data",
        }
    schema: JsonSchema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def build_schema_manifest(
    commands: Mapping[CommandPath, Command], exits: ExitCodeRegistry, framework_version: str
) -> dict[str, object]:
    """``tool --schema``: the manifest with every entry in ``--schema`` form"""
    manifest = build_manifest(commands, exits, framework_version)
    # Full exit tables per entry, but still a valid CommandEntry: parameters and
    # raw_payload_schema belong to a single command's --schema only
    manifest["commands"] = {
        path.value: command_entry(cmd, exits, commands)
        for path, cmd in sorted(commands.items(), key=lambda kv: kv[0].value)
    }
    return manifest


def build_manifest(
    commands: Mapping[CommandPath, Command], exits: ExitCodeRegistry, framework_version: str
) -> dict[str, object]:
    """The manifest tree with the shared exit-code table hoisted to the root"""
    shared = shared_exit_codes(exits)
    entries = {
        path.value: command_entry(cmd, exits, commands, shared=shared)
        for path, cmd in sorted(commands.items(), key=lambda kv: kv[0].value)
    }
    digest = hashlib.sha256(canonical_json(entries).encode()).hexdigest()
    etag = Etag(f"sha256:{digest[:32]}")
    return {
        "schema_version": SCHEMA_VERSION,
        "framework_version": framework_version,
        "etag": etag.value,
        "flags": GLOBAL_FLAG_ENTRIES,
        "exit_codes": shared,
        "commands": entries,
    }
