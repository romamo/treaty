"""Manifest builder: the registry serialized as ``manifest-response.json``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from ._command import DEFAULT_HEARTBEAT_MS, Command, DangerLevel
from ._env import CONFIG, CONTEXT, FORMAT, INSTANCE_ID, MAX_OUTPUT_BYTES, app_var
from ._exit import ExitCodeRegistry, FrameworkCode
from ._framework import (
    NO_INJECTION_FLAG,
    NO_STREAM_FLAG,
    STABLE_OUTPUT_KEY,
    UNMASK_FLAG,
    framework_flags,
)
from ._mode import Format
from ._schema import JsonSchema
from ._values import CommandPath, Etag

SCHEMA_VERSION = "3.0"
# TIMEOUT is shared: every handler runs under a deadline unless it is set to 0.
# PRECONDITION too: a stray input() no one can answer exits 4 on any command (REQ-F-047)
_ALWAYS = (
    FrameworkCode.SUCCESS,
    FrameworkCode.GENERAL_ERROR,
    FrameworkCode.ARG_ERROR,
    FrameworkCode.TIMEOUT,
    FrameworkCode.PRECONDITION,
)


def implicit_exit_codes(command: Command) -> tuple[FrameworkCode, ...]:
    """The codes a command may exit with undeclared: the shared ones, CONFLICT for a
    reused idempotency key on a non-safe command, and 7 and 8 behind the credential gate"""
    codes = list(_ALWAYS)
    if command.danger_level is not DangerLevel.SAFE:
        codes.append(FrameworkCode.CONFLICT)
    if command.requires_auth:
        # Not logged in, or the credential lacks a required scope (REQ-C-029)
        codes += (FrameworkCode.PERMISSION_DENIED, FrameworkCode.AUTH_REQUIRED)
    if command.steps or command.batch:
        # A step failed after one completed, or some items of a batch failed
        codes.append(FrameworkCode.PARTIAL_FAILURE)
    return tuple(codes)


def global_flag_entries(formats: Sequence[Format], app_name: str) -> dict[str, object]:
    """REQ-F-079: split_globals accepts these anywhere on every command path; a flag with
    an environment variable default names it (REQ-O-042)"""
    return {
        "format": {
            "type": "enum",
            "required": False,
            "enum_values": [m.value for m in formats],
            "description": f"Output representation; default ${app_var(app_name, FORMAT.key)}, "
            "else json when stdout is not a terminal, plain otherwise",
        },
        "max-output": {
            "type": "integer",
            "required": False,
            "description": "Largest stdout envelope in bytes (at least 4096) before truncation; "
            f"default ${app_var(app_name, MAX_OUTPUT_BYTES.key)}",
        },
        **_FIXED_GLOBAL_FLAGS,
        "config": {
            "type": "string",
            "required": False,
            "pattern_type": "filepath",
            "description": "Read settings only from this TOML (or .json) file, and write config "
            f"to it; default ${app_var(app_name, CONFIG.key)}",
        },
        "context": {
            "type": "string",
            "required": False,
            "description": "Apply the [contexts.<name>] table of the config files; default "
            f"${app_var(app_name, CONTEXT.key)}, else the files' current_context",
        },
        "no-config": {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Read no config file; environment variables still apply",
        },
        "show-config": {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Print the effective settings, the source of each, and the "
            "precedence order instead of running a command",
        },
        "instance-id": {
            "type": "string",
            "required": False,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
            "description": "Keep the user config file and state apart for this agent instance; "
            f"default ${app_var(app_name, INSTANCE_ID.key)}",
        },
    }


# REQ-O-037, REQ-O-023: argv only; no environment variable, config file, exec line, or
# MCP argument turns either on
SECURITY_FLAGS: dict[str, object] = {
    UNMASK_FLAG: {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Security: exposes sensitive values. Return tokens, keys, and base64 "
        "blobs in data raw instead of masked; pass it only when the next step needs one",
    },
    NO_INJECTION_FLAG: {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Security: returns external content without untrusted markers "
        "(_source, _trusted); only for sources the operator trusts. Each use is reported "
        "on stderr and in warnings",
    },
}

_FIXED_GLOBAL_FLAGS: dict[str, object] = {
    "schema": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Print the command's input and output schema instead of running it",
    },
    "print-schema": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Alias of --schema",
    },
    "output-schema": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Print the JSON Schema of the command's data instead of running it",
    },
    "schema-version": {
        "type": "integer",
        "required": False,
        "description": "Major version of the command's output schema to answer in; exit 2 "
        "with SCHEMA_VERSION_UNSUPPORTED when the command does not serve it",
    },
    "stable-output": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Byte-identical output for identical calls: meta leaves out "
        "request_id and timestamp, duration_ms is 0, and volatile data fields are dropped",
    },
    **SECURITY_FLAGS,
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
    for signal_code in (130, 141, 143):
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
    for code in implicit_exit_codes(command):
        entry = exits.framework(code)
        exit_codes.setdefault(str(entry.code.value), entry.to_json())
    timeout = exits.timeout(read_only=command.danger_level is DangerLevel.SAFE)
    exit_codes[str(timeout.code.value)] = timeout.to_json()
    if shared is not None:
        exit_codes = {k: v for k, v in exit_codes.items() if shared.get(k) != v}
    flags: dict[str, object] = {}
    for f in command.fields:
        flags.update(f.to_flag_entries())
    flags.update((f.name, f.to_entry(command)) for f in framework_flags(command))
    description = command.description
    if (old := command.deprecated) is not None:
        # CommandEntry has no deprecation keys (04-D2); a baseline audit reads this marker
        instead = "" if old.replacement is None else f"; use {old.replacement}"
        description = f"{description} (deprecated since {old.since}{instead})"
    out: dict[str, object] = {
        "description": description,
        "danger_level": command.danger_level.value,
        "required_scopes": [s.value for s in command.required_scopes],
        "option_placement": command.option_placement.value,  # REQ-C-027: on every entry
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
    if command.aliases:
        # Old paths that answer exit 13 with this command in error.redirect
        out["aliases"] = sorted(a.value for a in command.aliases)
    if command.examples:
        out["examples"] = [e.to_json() for e in command.examples]
    if command.has_network_io:
        out["has_network_io"] = True
    if command.streaming:
        out["streaming_default"] = True
    if command.safe_default:
        out["safe_default"] = True
    if command.interactive:
        out["interactive"] = True
    if command.editor_alternatives:
        out["requires_editor"] = True
        out["non_interactive_alternatives"] = list(command.editor_alternatives)
    if command.gui_operations:
        out["gui_operations"] = list(command.gui_operations)
        assert command.headless_behavior is not None  # checked at registration
        out["headless_behavior"] = command.headless_behavior.value  # REQ-C-024
    if command.auth is not None:
        out["headless_supported"] = command.auth.headless_supported  # REQ-C-021
        out["token_env_vars"] = list(command.token_env_vars)
    if command.async_job:
        out["async"] = True  # REQ-C-022
        out["job_descriptor_schema"] = command.output_schema
    if command.config_write_scope is not None:
        out["config_write_scope"] = command.config_write_scope.value  # REQ-C-025
    if command.requires:
        out["requires"] = [r.to_json() for r in command.requires]  # REQ-C-026
    if command.steps:
        out["steps"] = [s.value for s in command.steps]  # REQ-C-008
    if command.platform:
        out["platform"] = list(command.platform)  # REQ-C-018
    if command.required_tools:
        out["required_tools"] = {t: v.value for t, v in sorted(command.required_tools.items())}
    if command.subprocess is not None:
        out["subprocess"] = command.subprocess.to_json(command.fields)  # REQ-C-019
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
    # REQ-O-014; not ManifestResponse keys
    entry["schema_version"] = command.schema_version.value
    entry["min_schema_version"] = command.min_schema_version.value
    # REQ-F-075; not ManifestResponse keys (04-D2), so only here
    if command.introduced_in is not None:
        entry["introduced_in"] = command.introduced_in.value
    if command.deprecated is not None:
        entry.update(command.deprecated.to_json())
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        entry["requires_confirmation"] = True  # REQ-O-021; not a ManifestResponse key
    # REQ-O-037, REQ-O-023: the escapes from output protection; not a ManifestResponse key
    entry["security_flags"] = dict(SECURITY_FLAGS)
    # REQ-O-010, REQ-O-011; not ManifestResponse keys
    if command.resumable:
        entry["resumable"] = True
    if command.rollback is not None:
        entry["rollback_available"] = True
    if command.supports_raw_payload:
        entry["raw_payload_schema"] = payload_schema(command)
    if command.stdin_input:
        entry["stdin_input"] = True  # REQ-F-054; not a ManifestResponse key
    if command.heartbeat:
        # REQ-F-053: lines an agent skips before the envelope; not a ManifestResponse key
        entry["heartbeat_ms"] = DEFAULT_HEARTBEAT_MS
    if command.streaming:
        entry["timeout_kind"] = "idle"  # REQ-F-011: the limit restarts with every event
    if command.paginated:
        # REQ-F-019; not ManifestResponse keys, whose --limit flag shows the same default
        entry["paginated"] = True
        entry["default_limit"] = command.default_limit.count or 0
    return entry


def payload_schema(command: Command, *, stream_key: bool = True) -> JsonSchema:
    """Every key a JSON payload may carry: the args schema with secrets replaced by their
    sources, plus the framework keys the command declares"""
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []
    base = command.args_schema
    for f in command.fields:
        key = f.flag.replace("-", "_")  # the field name, less a keyword's trailing _
        if f.secret:
            what = f.spec.description
            properties[f"{key}_from_env"] = {
                "type": "string",
                "description": f"Name of the environment variable holding: {what}",
            }
            properties[f"{key}_from_file"] = {
                "type": "string",
                "description": f"Path of the file holding: {what}",
            }
            continue
        prop = dict(base["properties"][f.name])
        prop["description"] = f.spec.description
        properties[key] = prop
        if f.required:  # an X | None field without a default is optional, as the parser says
            required.append(key)
    properties.update(
        (f.key, f.to_property(command))
        for f in framework_flags(command, json=True)
        if stream_key or f.name != NO_STREAM_FLAG
    )
    properties[STABLE_OUTPUT_KEY] = {
        "type": "boolean",
        "default": False,
        "description": "Byte-identical output for identical calls (--stable-output)",
    }
    if command.compat:
        majors = [c.version.major for c in command.compat] + [command.schema_version.major]
        properties["schema_version"] = {
            "type": ["integer", "string"],
            "description": f"Major version of the output schema to answer in: {majors}",
        }
    schema: JsonSchema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def build_manifest(
    commands: Mapping[CommandPath, Command],
    exits: ExitCodeRegistry,
    framework_version: str,
    formats: Sequence[Format],
    app_name: str,
    *,
    dependencies: Sequence[Mapping[str, str]] = (),
) -> dict[str, object]:
    """The manifest tree with the shared exit-code table hoisted to the root; the app's
    declared ``dependencies`` too, when it has any (REQ-O-031)"""
    shared = shared_exit_codes(exits)
    flags = global_flag_entries(formats, app_name)
    entries = {
        path.value: command_entry(cmd, exits, commands, shared=shared)
        for path, cmd in sorted(commands.items(), key=lambda kv: kv[0].value)
    }
    # Everything an agent caches: a new global flag or shared code must change the etag
    shape: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "flags": flags,
        "exit_codes": shared,
        "commands": entries,
    }
    if dependencies:
        shape["dependencies"] = [dict(d) for d in sorted(dependencies, key=lambda d: d["name"])]
    digest = hashlib.sha256(canonical_json(shape).encode()).hexdigest()
    etag = Etag(f"sha256:{digest[:32]}")
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "framework_version": framework_version,
        "etag": etag.value,
        "flags": flags,
        "exit_codes": shared,
        "commands": entries,
    }
    if dependencies:
        manifest["dependencies"] = shape["dependencies"]
    return manifest
