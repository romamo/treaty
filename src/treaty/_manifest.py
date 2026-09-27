"""Manifest builder: the registry serialized as ``manifest-response.json``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from ._auth import HEADLESS_FLAG, TOKEN_ENV_FLAG
from ._command import (
    DEFAULT_HEARTBEAT_MS,
    HEARTBEAT_FLAG,
    INPUT_FILE_FLAG,
    OUTPUT_FLAG,
    Command,
    DangerLevel,
)
from ._config import GLOBAL_FLAG, ConfigScope
from ._exit import ExitCodeRegistry, FrameworkCode
from ._mode import Format
from ._page import CURSOR_FLAG, LIMIT_FLAG
from ._parse import (
    CONFIRM_FLAG,
    IDEMPOTENCY_FLAG,
    LIVE_FLAG,
    NO_STREAM_FLAG,
    NON_INTERACTIVE_FLAG,
    TIMEOUT_FLAG,
    YES_FLAG,
)
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


def global_flag_entries(formats: Sequence[Format]) -> dict[str, object]:
    """REQ-F-079: split_globals accepts these anywhere on every command path"""
    return {
        "format": {
            "type": "enum",
            "required": False,
            "enum_values": [m.value for m in formats],
            "description": "Output representation; json when stdout is not a terminal, "
            "plain otherwise",
        },
        **_FIXED_GLOBAL_FLAGS,
    }


_FIXED_GLOBAL_FLAGS: dict[str, object] = {
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
    if shared is not None:
        exit_codes = {k: v for k, v in exit_codes.items() if shared.get(k) != v}
    flags: dict[str, object] = {}
    for f in command.fields:
        flags.update(f.to_flag_entries())
    if command.accepts_timeout:
        flags["timeout"] = {
            "type": "number",
            "required": False,
            "description": "Seconds to wait for each event before TIMEOUT; 0 disables the limit"
            if command.streaming
            else "Seconds before the framework aborts with TIMEOUT; 0 disables the limit",
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
    if command.interactive:
        flags[YES_FLAG] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Answer yes to every confirmation instead of asking",
        }
        flags[NON_INTERACTIVE_FLAG] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Never prompt, even on a terminal; a needed answer exits 4",
        }
    if command.interactive or command.editor_alternatives or command.auth is not None:
        # INPUT_REQUIRED or EDITOR_REQUIRED when no one can answer; TOKEN_REQUIRED
        entry = exits.framework(FrameworkCode.PRECONDITION)
        exit_codes.setdefault(str(entry.code.value), entry.to_json())
    if command.requires_auth:
        # Not logged in, or the credential lacks a required scope (REQ-C-029)
        for code in (FrameworkCode.PERMISSION_DENIED, FrameworkCode.AUTH_REQUIRED):
            entry = exits.framework(code)
            exit_codes.setdefault(str(entry.code.value), entry.to_json())
    if command.config_write_scope is not None:
        only_global = command.config_write_scope is ConfigScope.GLOBAL
        entry_global: dict[str, object] = {
            "type": "boolean",
            "required": only_global,
            "description": "Required: the command writes the user config file"
            if only_global
            else "Write the user config file instead of the project's",
        }
        if not only_global:
            entry_global["default"] = False
        flags[GLOBAL_FLAG] = entry_global
    if command.auth is not None:
        flags[HEADLESS_FLAG] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Never open a browser; read the token from --token-env-var or "
            "the default variables. Implied without a terminal",
        }
        flags[TOKEN_ENV_FLAG] = {
            "type": "string",
            "required": False,
            "description": "Name of the environment variable holding a pre-acquired token",
        }
    if command.paginated:
        flags[LIMIT_FLAG] = {
            "type": "integer",
            "required": False,
            "default": command.default_limit.count or 0,
            "description": "Most items to return; 0 returns every item",
        }
        flags[CURSOR_FLAG] = {
            "type": "string",
            "required": False,
            "description": "meta.pagination.next_cursor of the previous page, to get the next",
        }
    if command.stdin_input:
        flags[INPUT_FILE_FLAG] = {
            "type": "string",
            "required": False,
            "pattern_type": "filepath",
            "description": "Read the input from this file, of any size, instead of stdin; "
            "- is stdin, capped",
        }
    if command.output_file:
        flags[OUTPUT_FLAG] = {
            "type": "string",
            "required": False,
            "pattern_type": "filepath",
            "description": "Write the result to this file in the --format representation; "
            "stdout gets the envelope",
        }
    if command.heartbeat:
        flags[HEARTBEAT_FLAG] = {
            "type": "integer",
            "required": False,
            "default": DEFAULT_HEARTBEAT_MS,
            "description": "Milliseconds between heartbeat lines on stdout while the command "
            "runs; 0 turns them off",
        }
    if command.safe_default:
        flags[LIVE_FLAG] = {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Apply, with --confirm-destructive; without it the command runs "
            "as a dry run and exits 0",
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
    if command.safe_default:
        out["safe_default"] = True
    if command.interactive:
        out["interactive"] = True
    if command.editor_alternatives:
        out["requires_editor"] = True
        out["non_interactive_alternatives"] = list(command.editor_alternatives)
    if command.gui_operations:
        out["gui_operations"] = list(command.gui_operations)
        # The only behavior treaty has: the URL goes to data.open_url (REQ-C-024)
        out["headless_behavior"] = "emit_in_output"
    if command.auth is not None:
        out["headless_supported"] = command.auth.headless_supported  # REQ-C-021
        out["token_env_vars"] = list(command.token_env_vars)
    if command.async_job:
        out["async"] = True  # REQ-C-022
        out["job_descriptor_schema"] = command.output_schema
    if command.config_write_scope is not None:
        out["config_write_scope"] = command.config_write_scope.value  # REQ-C-025
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
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        entry["requires_confirmation"] = True  # REQ-O-021; not a ManifestResponse key
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
    if command.accepts_timeout:
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
    if command.safe_default:
        properties[LIVE_FLAG] = {
            "type": "boolean",
            "default": False,
            "description": "Apply, with confirm_destructive; without it the command runs "
            "as a dry run",
        }
    if command.interactive:
        properties[YES_FLAG] = {
            "type": "boolean",
            "default": False,
            "description": "Answer yes to every confirmation",
        }
        properties[NON_INTERACTIVE_FLAG.replace("-", "_")] = {
            "type": "boolean",
            "default": False,
            "description": "Never prompt; a needed answer exits 4",
        }
    if command.paginated:
        properties[LIMIT_FLAG] = {
            "type": "integer",
            "minimum": 0,
            "default": command.default_limit.count or 0,
            "description": "Most items to return; 0 returns every item",
        }
        properties[CURSOR_FLAG] = {
            "type": ["string", "null"],
            "description": "meta.pagination.next_cursor of the previous page, to get the next",
        }
    if command.stdin_input:
        properties[INPUT_FILE_FLAG.replace("-", "_")] = {
            "type": "string",
            "description": "Path of the file holding the input",
        }
    if command.config_write_scope is not None:
        properties[GLOBAL_FLAG] = {
            "type": "boolean",
            "default": False,
            "description": "Write the user config file instead of the project's",
        }
    if command.auth is not None:
        properties[HEADLESS_FLAG] = {
            "type": "boolean",
            "default": False,
            "description": "Never open a browser; read the token from a variable",
        }
        properties[TOKEN_ENV_FLAG.replace("-", "_")] = {
            "type": "string",
            "description": "Name of the environment variable holding a pre-acquired token",
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


def build_manifest(
    commands: Mapping[CommandPath, Command],
    exits: ExitCodeRegistry,
    framework_version: str,
    formats: Sequence[Format],
) -> dict[str, object]:
    """The manifest tree with the shared exit-code table hoisted to the root"""
    shared = shared_exit_codes(exits)
    flags = global_flag_entries(formats)
    entries = {
        path.value: command_entry(cmd, exits, commands, shared=shared)
        for path, cmd in sorted(commands.items(), key=lambda kv: kv[0].value)
    }
    # Everything an agent caches: a new global flag or shared code must change the etag
    shape = {
        "schema_version": SCHEMA_VERSION,
        "flags": flags,
        "exit_codes": shared,
        "commands": entries,
    }
    digest = hashlib.sha256(canonical_json(shape).encode()).hexdigest()
    etag = Etag(f"sha256:{digest[:32]}")
    return {
        "schema_version": SCHEMA_VERSION,
        "framework_version": framework_version,
        "etag": etag.value,
        "flags": flags,
        "exit_codes": shared,
        "commands": entries,
    }
