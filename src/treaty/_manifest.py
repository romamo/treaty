"""Manifest builder: the registry serialized as ``manifest-response.json``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from ._command import Command, DangerLevel
from ._exit import ExitCodeRegistry, FrameworkCode
from ._schema import schema_for
from ._values import CommandPath, Etag

SCHEMA_VERSION = "1.0"
_ALWAYS = (FrameworkCode.SUCCESS, FrameworkCode.GENERAL_ERROR, FrameworkCode.ARG_ERROR)


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
    flags: dict[str, object] = {f.flag: f.to_flag_entry() for f in command.fields}
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
    children = sorted(p.value for p in all_paths if p.is_direct_child_of(command.path))
    if children:
        out["subcommands"] = children
    if command.examples:
        out["examples"] = [e.to_json() for e in command.examples]
    if command.has_network_io:
        out["has_network_io"] = True
    return out


def command_schema(
    command: Command, exits: ExitCodeRegistry, all_paths: Mapping[CommandPath, Command]
) -> dict[str, object]:
    """``--schema`` output for one command (REQ-C-015, REQ-O-032)"""
    entry = command_entry(command, exits, all_paths)
    entry["parameters"] = entry["flags"]
    if command.supports_raw_payload:
        entry["raw_payload_schema"] = schema_for(command.args_type)
    return entry


def build_schema_manifest(
    commands: Mapping[CommandPath, Command], exits: ExitCodeRegistry, framework_version: str
) -> dict[str, object]:
    """``tool --schema``: the manifest with every entry in ``--schema`` form"""
    manifest = build_manifest(commands, exits, framework_version)
    manifest["commands"] = {
        path.value: command_schema(cmd, exits, commands)
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
        "exit_codes": shared,
        "commands": entries,
    }
