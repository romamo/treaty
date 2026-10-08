"""Manifest builder: the registry serialized as ``manifest-response.json``."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType

from ._cap import DEFAULT_STDIN_CAP, StdinCap
from ._command import (
    ARGV_KEY,
    DEFAULT_HEARTBEAT_MS,
    OUTPUT_FLAG,
    Command,
    DangerLevel,
)
from ._completion import COMPLETION_PATH
from ._deps import ANY_VERSION
from ._env import (
    CONFIG,
    CONTEXT,
    FORMAT,
    INSTANCE_ID,
    KNOWN,
    MAX_OUTPUT_BYTES,
    NO_UPDATE,
    EnvVar,
    app_var,
)
from ._exit import ExitCodeRegistry, FrameworkCode
from ._framework import (
    IDEMPOTENCY_FLAG,
    NO_INJECTION_FLAG,
    NO_STREAM_FLAG,
    STABLE_OUTPUT_KEY,
    UNMASK_FLAG,
    framework_flags,
)
from ._lines import DEFAULT_LINE_CAP, INPUT_LINES_KEY, LineCap, StdinInput
from ._mcp_shared import MCP_SERVE_PATH
from ._mode import Format, FormatName, MediaType, media_type_map
from ._output_base import OutputBase
from ._scan import command_reach, exit_raises, source_tree, unnamed_exits
from ._schema import JsonSchema
from ._select import FIELDS_KEY
from ._types import FlagType
from ._values import CommandPath, Etag

SCHEMA_VERSION = "3.19"  # 3.1: CommandEntry.builtin (REQ-O-041)
# 3.2: ConditionalRule any_of and one_of (REQ-C-026)
# 3.3: CommandEntry.output_file (REQ-O-001)
# 3.4: FlagEntry.env_vars; 3.5: the root env_vars of variables that back no flag (REQ-F-073)
# 3.6: output_file handler and envelope; 3.7: object flags and output_file_base (REQ-O-001)
# 3.8: CommandEntry.stdin (REQ-F-054, REQ-O-004); 3.9: arguments and help_argv (REQ-C-031)
# 3.10: the output side-effect kind (REQ-C-011); 3.11: stderr child_log (REQ-F-038)
# 3.12: the format flag's media_types and output_media_types (REQ-O-001, REQ-O-049)
# 3.13: the root secret_env_vars (REQ-F-073); 3.14: confirm_flag (REQ-O-048)
# 3.15: CommandEntry.idempotent (REQ-C-002); 3.16: CommandEntry.stdout and protocol
# (REQ-C-032); 3.17: CommandEntry.mcp (REQ-C-032); 3.18: integer enum_values, which treaty
# does not emit (an integer Literal's values are in the description); 3.19:
# ExitCodeEntry.error_codes (#362)

EXEC_PATH = CommandPath("exec")
"""The ``exec`` built-in, which reads its plan from stdin as a buffered payload"""

PROTOCOL_STDOUT = "protocol"
"""CommandEntry.stdout of a command that serves a protocol over stdio (ManifestResponse 3.16)"""

NOT_AN_MCP_TOOL = "(not an MCP tool)"
"""Ends the manifest description of a command registered ``mcp=False`` (#281); since
ManifestResponse 3.17 the entry says ``mcp: false`` too"""


PERSON_RUNS = "(a person runs this at a terminal)"
"""Ends the manifest description of a command registered ``requires_person=True`` (#424):
CommandEntry has no key for it, so ``--schema`` says ``requires_person: true``"""


def never_a_tool(command: Command, *, builtin: bool) -> bool:
    """Whether no MCP server offers the command as a tool, whatever ``McpServe(commands=)``
    selects: ``exec``, the ``completion`` and ``mcp serve`` built-ins, a passthrough
    command, and one registered ``mcp=False`` (#281). Its manifest entry says ``mcp: false``
    (ManifestResponse 3.17), as absent means a server may offer it"""
    if command.path == EXEC_PATH:
        return True
    if builtin and command.path in (COMPLETION_PATH, MCP_SERVE_PATH):
        return True
    return command.passthrough or not command.mcp


# The --format values CommandEntry.output_formats leaves out (REQ-O-049): the spec's
# universal ones, and ndjson, which every treaty command takes
_DEFAULT_FORMATS = frozenset({Format.JSON, Format.JSONL, Format.TSV, Format.PLAIN, Format.NDJSON})

# The framework variables a root flag reads when it is not passed (REQ-O-042); the rest of
# KNOWN back no flag and are listed in the root env_vars
_FLAG_VARS: dict[str, EnvVar] = {
    "format": FORMAT,
    "max-output": MAX_OUTPUT_BYTES,
    "config": CONFIG,
    "context": CONTEXT,
    "instance-id": INSTANCE_ID,
    "no-update-check": NO_UPDATE,
}
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
    reused idempotency key on a non-safe command that does not stream, 7 and 8 behind the
    credential gate, and 12 when ``ctx.http`` can fail"""
    codes = list(_ALWAYS)
    if command.danger_level is not DangerLevel.SAFE and not command.streaming:
        codes.append(FrameworkCode.CONFLICT)
    if command.requires_auth:
        # Not logged in, or the credential lacks a required scope (REQ-C-029)
        codes += (FrameworkCode.PERMISSION_DENIED, FrameworkCode.AUTH_REQUIRED)
    if command.has_network_io:
        # ctx.http: a connection or TLS failure, or 502 to 504 upstream (REQ-F-037)
        codes.append(FrameworkCode.UNAVAILABLE)
    if command.steps or command.batch:
        # A step failed after one completed, or some items of a batch failed
        codes.append(FrameworkCode.PARTIAL_FAILURE)
    return tuple(codes)


_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]+")
"""ErrorDetail.code; a raise with any other code exits 1 with INVALID_EXIT instead"""

CONFLICT_CODE = FrameworkCode.CONFLICT
REUSED_KEY_CODE = "IDEMPOTENCY_KEY_REUSED"
"""The ``error.code`` of exit 6 when an ``--idempotency-key`` comes back with other arguments"""


def conflict_error_codes(command: Command) -> list[str] | None:
    """ExitCodeEntry 1.1 ``error_codes`` of the command's exit 6 (CONFLICT), sorted: the
    codes its handler, its resources' ``acquire``, and the first-party helpers they call
    raise under CONFLICT (``ALREADY_EXISTS`` for ``already_exists``, a literal ``code=``,
    else ``CONFLICT``), and ``IDEMPOTENCY_KEY_REUSED`` when it takes ``--idempotency-key``.
    A present list is read as complete, so it is None when the scan cannot see a code: a
    function without source, a raise whose ``code=`` is not a literal, an exit whose name is
    not a literal (``CliExit(name, ...)``, ``getattr(Exit, name)``), or a passthrough
    command, whose tool owns its exit codes; None too when there is nothing to list"""
    if command.passthrough:
        return None
    codes: set[str] = set()
    if any(f.name == IDEMPOTENCY_FLAG for f in framework_flags(command)):
        codes.add(REUSED_KEY_CODE)
    for unit in command_reach(command):
        if source_tree(unit.fn) is None or unnamed_exits(unit.fn):
            return None
        for raised in exit_raises(unit.fn):
            if raised.name.value != CONFLICT_CODE.name:
                continue
            if raised.error_code is None:
                return None
            if _ERROR_CODE.fullmatch(raised.error_code):
                codes.add(raised.error_code)
    return sorted(codes) or None


def global_flag_entries(
    formats: Sequence[FormatName], app_name: str, media_types: Mapping[FormatName, MediaType]
) -> dict[str, object]:
    """REQ-F-079: split_globals accepts these anywhere on every command path; a flag with
    an environment variable default names it, and lists it in ``env_vars`` (REQ-O-042).
    The ``--format`` values include the names an app registered, and ``media_types`` the
    media type of each value outside the spec's table (ManifestResponse 3.12, #179)"""
    output_format: dict[str, object] = {
        "type": "enum",
        "required": False,
        "enum_values": [m.value for m in formats],
        "description": f"Output representation; default ${app_var(app_name, FORMAT.key)}, "
        "else json when stdout is not a terminal, plain otherwise",
    }
    if written := media_type_map(formats, media_types):
        output_format["media_types"] = written
    entries: dict[str, object] = {
        "format": output_format,
        "max-output": {
            "type": "integer",
            "required": False,
            "description": "Largest stdout envelope, buffered ndjson answer, or stream line "
            "in bytes (at least 4096) before truncation; "
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
            "description": "Keep the user config file, state, and temp files apart for this "
            f"agent instance; default ${app_var(app_name, INSTANCE_ID.key)}",
        },
        "cwd": {
            "type": "string",
            "required": False,
            "pattern_type": "filepath",
            "description": "Resolve relative paths, find config, and run children in this "
            "directory instead of the working directory, which never changes; exit 2 when it "
            "is not a directory",
        },
        "no-update-check": {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Check for no newer release this run; default "
            f"${app_var(app_name, NO_UPDATE.key)}. Off a terminal or under CI no check runs",
        },
    }
    for flag, var in _FLAG_VARS.items():
        entry = entries[flag]
        assert isinstance(entry, dict), flag
        entry["env_vars"] = [{"name": app_var(app_name, var.key)}]
    return entries


def framework_env_vars(app_name: str) -> list[dict[str, object]]:
    """Root ``env_vars`` entries of the framework variables that back no flag, such as
    ``<APP>_AUDIT_LOG`` and ``<APP>_SESSION`` (ManifestResponse 3.5, REQ-O-030)"""
    backing = set(_FLAG_VARS.values())
    return [
        {"name": app_var(app_name, v.key), "description": v.description}
        for v in KNOWN
        if v not in backing
    ]


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
    "quiet": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Write nothing on stderr, not even errors; the envelope carries them",
    },
    "verbose": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Write info and progress lines on stderr even off a terminal or under "
        "CI, where only errors and warnings are written; -v for short, on a command without "
        "a -v of its own",
    },
    "debug": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Write the framework's trace on stderr too: config resolution, HTTP "
        "requests, child processes, locks, and the audit log, secrets redacted; -vv or -v -v "
        "for short, on a command without a -v of its own",
    },
    "warnings-as-errors": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Exit 1 with WARNINGS_AS_ERRORS when the command succeeds with any "
        "warning; the warnings and data stay in the response",
    },
    "fields": {
        "type": "string",
        "required": False,
        "description": "Comma-separated top-level keys of data to keep, of the object or of "
        "each item of an array, such as id,name; unknown names are ignored, and ok, error, "
        "warnings, and meta are never filtered",
    },
    "stream": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "One JSON line per event as it is produced, ending with a summary "
        "line carrying pagination; commands with streaming_default already stream, and "
        "any other answers buffered with a STREAMING_NOT_SUPPORTED warning",
    },
    "token-limit": {
        "type": "integer",
        "required": False,
        "description": "Most tokens of data to return, cut on item and field boundaries; "
        "meta.truncated, meta.token_limit, and meta.next_token_offset say what was cut",
    },
    "token-offset": {
        "type": "integer",
        "required": False,
        "description": "Start data at the first item ending after this many tokens; with "
        "--token-limit, pass the previous meta.next_token_offset for the next window",
    },
    "token-count": {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Run the command, then return data null and meta.token_count, the "
        "tokens data would take; the response is JSON whatever --format says",
    },
    "tokenizer": {
        "type": "string",
        "required": False,
        "description": "How the token flags count: approx (UTF-8 bytes over 4, the default "
        "unless the app sets one), cl100k_base or o200k_base with treaty[tiktoken], or a "
        "tokenizer the app registers",
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


def flag_entries(command: Command) -> dict[str, object]:
    """Every flag the command takes, its own fields' and the framework's, keyed by name"""
    flags: dict[str, object] = {}
    properties = command.args_schema["properties"]
    for f in command.fields:
        flags.update(f.to_flag_entries(command.own_env_var(f.name), properties[f.name]))
    flags.update((f.name, f.to_entry(command)) for f in framework_flags(command))
    return flags


def _output_file(command: Command) -> str | None:
    """REQ-O-001 (ManifestResponse 3.6): what ``--output`` gets. A passthrough command's
    gets the final envelope, even with ``output_file=``, which then only sets its base; an
    app's own ``--output PATH`` field, what its handler writes"""
    if command.passthrough:
        return "envelope"
    if command.output_file:
        return "binary" if command.returns_binary else "formatted"
    own = command.field_by_flag(OUTPUT_FLAG)
    if (
        own is not None
        and not own.positional
        and not own.secret
        and (own.path or own.flag_type is FlagType.STRING)
    ):
        return "handler"
    return None


def _stdin(
    command: Command, *, builtin: bool, max_stdin: StdinCap, max_line: LineCap
) -> dict[str, object] | None:
    """``StdinDeclaration`` (ManifestResponse 3.8): how the command reads stdin and
    ``--input-file``; a cap equal to the spec's default is left out, as absent means it"""
    if command.stdin_records is not None:
        stdin: dict[str, object] = {"mode": "records"}
    elif command.stdin_input is StdinInput.LINES:
        stdin = {"mode": "lines"}
    elif command.stdin_input is StdinInput.TEXT or (builtin and command.path == EXEC_PATH):
        # exec reads its whole plan as a payload, under the same cap
        if max_stdin == DEFAULT_STDIN_CAP:
            return {"mode": "buffered"}
        return {"mode": "buffered", "max_bytes": max_stdin.bytes}
    else:
        return None
    if max_line != DEFAULT_LINE_CAP:
        stdin["max_line_bytes"] = max_line.bytes
    if command.stdin_records is not None:
        stdin["record_schema"] = command.stdin_records.schema
    return stdin


def command_entry(
    command: Command,
    exits: ExitCodeRegistry,
    all_paths: Mapping[CommandPath, Command],
    *,
    builtin: bool,
    offered: Sequence[FormatName],
    media_types: Mapping[FormatName, MediaType],
    max_stdin: StdinCap,
    max_line: LineCap,
    shared: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """One CommandEntry; ``builtin`` when treaty registered the command, not the app.
    ``offered`` is the app's ``--format`` values, which the root flag lists with
    ``media_types``, the app's; ``max_stdin`` and ``max_line`` are the app's stdin caps.
    With ``shared`` given, entries equal to the shared table are hoisted"""
    exit_codes: dict[str, object] = dict(shared_exit_codes(exits))
    for name in command.exit_codes:
        entry = exits.by_name(name)
        exit_codes[str(entry.code.value)] = entry.to_json()
    for code in implicit_exit_codes(command):
        entry = exits.framework(code)
        exit_codes.setdefault(str(entry.code.value), entry.to_json())
    timeout = exits.timeout(read_only=command.danger_level is DangerLevel.SAFE)
    exit_codes[str(timeout.code.value)] = timeout.to_json()
    conflict = exits.framework(CONFLICT_CODE)
    key = str(conflict.code.value)
    if key in exit_codes and (errors := conflict_error_codes(command)) is not None:
        # ExitCodeEntry 1.1 (ManifestResponse 3.19, #362): the error.code values of exit 6
        exit_codes[key] = {**conflict.to_json(), "error_codes": errors}
    if shared is not None:
        exit_codes = {k: v for k, v in exit_codes.items() if shared.get(k) != v}
    # REQ-C-031: a passthrough command's own flags go before its path, as global options
    # do, so its entry lists none; --schema lists them
    flags = {} if command.passthrough else flag_entries(command)
    description = command.description
    if (old := command.deprecated) is not None:
        # CommandEntry has no deprecation keys (04-D2); a baseline audit reads this marker
        instead = "" if old.replacement is None else f"; use {old.replacement}"
        description = f"{description} (deprecated since {old.since}{instead})"
    if command.requires_person:
        # CommandEntry has no key for it, and an agent reads the description (#424)
        description = f"{description} {PERSON_RUNS}"
    if not command.mcp:
        # The marker predates CommandEntry.mcp (#281); the entry says mcp: false below too
        description = f"{description} {NOT_AN_MCP_TOOL}"
    out: dict[str, object] = {
        "description": description,
        "danger_level": command.danger_level.value,
        "required_scopes": [s.value for s in command.required_scopes],
        "option_placement": command.option_placement.value,  # REQ-C-027: on every entry
        "flags": flags,
        "exit_codes": exit_codes,
    }
    serves = command.protocol
    if serves is None:
        out["output_schema"] = command.output_schema
    else:
        # REQ-C-032 (ManifestResponse 3.16): stdout is the protocol's from the first byte,
        # so no output_schema, output_formats, or output_media_types describe it
        out["stdout"] = PROTOCOL_STDOUT
        out["protocol"] = serves.value
    if never_a_tool(command, builtin=builtin):
        out["mcp"] = False  # REQ-C-032 (ManifestResponse 3.17): absent means it may be served
    if builtin:
        # REQ-O-041: set by who registered it; an app command is left unmarked, read as false
        out["builtin"] = True
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
    if (output_file := _output_file(command)) is not None:
        out["output_file"] = output_file
        base = command.output_root
        if base is not None and not base.is_cwd:
            # ManifestResponse 3.7: a resource class or a function names the directory
            project = base.label == OutputBase.PROJECT_ROOT.value
            out["output_file_base"] = OutputBase.PROJECT_ROOT.value if project else "resource"
    stdin = _stdin(command, builtin=builtin, max_stdin=max_stdin, max_line=max_line)
    if stdin is not None:
        out["stdin"] = stdin
    if command.passthrough:
        out["arguments"] = "passthrough"  # REQ-C-031 (ManifestResponse 3.9)
        if command.help_command is not None:
            out["help_argv"] = list(command.help_command)
    if command.child_log:
        out["stderr"] = "child_log"  # REQ-F-038 (ManifestResponse 3.11)
    if command.idempotent:
        out["idempotent"] = True  # REQ-C-002 (ManifestResponse 3.15), on any danger level
    if (confirming := command.confirm_field) is not None:
        out["confirm_flag"] = confirming.flag  # REQ-O-048 (ManifestResponse 3.14)
    if command.streaming:
        out["streaming_default"] = True
    # REQ-O-049: every value the command takes beyond the defaults (#216): id (REQ-O-005),
    # the app's formats it inherits, then the formats only it offers (#209). Its override
    # of an app format is listed once, among the inherited
    beyond = [Format.ID.value] if command.id_field is not None else []
    beyond += (n.value for n in offered if n not in _DEFAULT_FORMATS and n.builtin is not Format.ID)
    beyond += (n.value for n in command.renderers if n not in _DEFAULT_FORMATS and n not in offered)
    if serves is not None:
        beyond = []  # a protocol's stdout has no --format representation
    if beyond:
        out["output_formats"] = beyond
    # ManifestResponse 3.12: what each of them writes, where the root map does not say it
    root = media_type_map(offered, media_types)
    written = media_type_map(
        [FormatName(n) for n in beyond], {**media_types, **command.media_types}
    )
    own = {name: kind for name, kind in written.items() if root.get(name) != kind}
    if own:
        out["output_media_types"] = own
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
        out["required_tools"] = {
            t: ANY_VERSION if v is None else v.value
            for t, v in sorted(command.required_tools.items())
        }
    if command.background is not None:
        out.update(command.background.to_json())  # REQ-C-010
    if command.filesystem_side_effects:
        out["filesystem_side_effects"] = [e.to_json() for e in command.filesystem_side_effects]
    if command.subprocess is not None:
        out["subprocess"] = command.subprocess.to_json(command.fields)  # REQ-C-019
    if command.secret_env_vars:
        # Each secret's <APP>_<NAME>, then the names its Flag(env=) declares, in order
        out["secret_env_vars"] = [
            var
            for f in command.fields
            if f.secret
            for var in (command.secret_env_vars[f.name], *(n.name for n in f.spec.env))
        ]
    return out


def command_schema(
    command: Command,
    exits: ExitCodeRegistry,
    all_paths: Mapping[CommandPath, Command],
    *,
    builtin: bool,
    offered: Sequence[FormatName],
    media_types: Mapping[FormatName, MediaType],
    max_stdin: StdinCap,
    max_line: LineCap,
) -> dict[str, object]:
    """``--schema`` output for one command (REQ-C-015, REQ-O-032); a passthrough
    command's lists the framework flags it takes before its path, too (REQ-C-031)"""
    entry = command_entry(
        command,
        exits,
        all_paths,
        builtin=builtin,
        offered=offered,
        media_types=media_types,
        max_stdin=max_stdin,
        max_line=max_line,
    )
    entry["flags"] = entry["parameters"] = flag_entries(command)
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
        raw = payload_schema(command)
        groups = [r for r in command.requires if r.group]
        if groups:
            constraints = [r.json_schema() for r in groups]
            raw.update(constraints[0] if len(constraints) == 1 else {"allOf": constraints})
        entry["raw_payload_schema"] = raw
    if command.stdin_input is not None:
        entry["stdin_input"] = True  # REQ-F-054; not a ManifestResponse key
    if command.stdin_input is StdinInput.LINES:
        entry["stdin_mode"] = StdinInput.LINES.value  # #33: read lazily, a cap per line
    if command.stdin_records is not None:
        # #32: what each input line holds, to match a producer's output_schema
        entry["stdin_records_schema"] = command.stdin_records.schema
    if command.heartbeat:
        # REQ-F-053: lines an agent skips before the envelope; not a ManifestResponse key
        entry["heartbeat_ms"] = DEFAULT_HEARTBEAT_MS
    if command.streaming:
        entry["timeout_kind"] = "idle"  # REQ-F-011: the limit restarts with every event
    if command.endless:
        # #389: the stream ends only when interrupted; not a ManifestResponse key
        entry["endless"] = True
    if command.requires_person:
        # #424: ctx.attest asks a person at a terminal; not a ManifestResponse key
        entry["requires_person"] = True
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
        key = f.key
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
        # An X | None field without a default is optional, as the parser says; a Flag(env=)
        # variable may supply one, as --x-from-env does a secret
        if f.required and not f.spec.env:
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
    if command.stdin_input is StdinInput.LINES:
        properties[INPUT_LINES_KEY] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "The input lines, one per item without its line break, in place "
            "of stdin, which an exec line or an MCP call does not have",
        }
    if command.passthrough:
        properties[ARGV_KEY] = {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "The delegated tool's arguments, verbatim, as argv has them after "
            "the command path",
        }
    properties[FIELDS_KEY] = {
        "type": "string",
        "description": "Comma-separated top-level keys of data to keep, such as id,name "
        "(--fields); the kept data may then lack keys output_schema requires",
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
    formats: Sequence[FormatName],
    app_name: str,
    *,
    builtins: frozenset[CommandPath],
    dependencies: Sequence[Mapping[str, str]] = (),
    audit_log_path: str | None = None,
    unlogged: Collection[CommandPath] = (),
    settings_env_vars: Sequence[Mapping[str, object]] = (),
    secret_env_vars: Sequence[str] = (),
    media_types: Mapping[FormatName, MediaType] = MappingProxyType({}),
    max_stdin: StdinCap = DEFAULT_STDIN_CAP,
    max_line: LineCap = DEFAULT_LINE_CAP,
) -> dict[str, object]:
    """The manifest tree with the shared exit-code table hoisted to the root, each of
    ``builtins`` marked ``builtin: true`` (REQ-O-041); the app's
    declared ``dependencies`` too, when it has any (REQ-O-031). ``framework_version`` is
    treaty's own version; the app's is ``meta.tool_version`` of the response. While the
    audit log is on, ``audit_log_path`` is a ``log`` side effect of every command it
    records, those not ``unlogged`` (REQ-O-030). The root ``env_vars`` lists the
    framework's variables that back no flag, then ``settings_env_vars``; the root
    ``secret_env_vars`` are the secrets any command may read (REQ-F-073)"""
    shared = shared_exit_codes(exits)
    root_env = [*framework_env_vars(app_name), *(dict(e) for e in settings_env_vars)]
    flags = global_flag_entries(formats, app_name, media_types)
    entries = {
        path.value: command_entry(
            cmd,
            exits,
            commands,
            builtin=path in builtins,
            offered=formats,
            media_types=media_types,
            max_stdin=max_stdin,
            max_line=max_line,
            shared=shared,
        )
        for path, cmd in sorted(commands.items(), key=lambda kv: kv[0].value)
    }
    if audit_log_path is not None:
        for path in commands:
            if path not in unlogged:
                entry = entries[path.value]
                declared = entry.get("filesystem_side_effects")
                effects = declared if isinstance(declared, list) else []
                entry["filesystem_side_effects"] = [
                    *effects,
                    {"path": audit_log_path, "type": "log"},
                ]
    # ManifestResponse 3.13: a command's secrets are the root ones plus its own, so a name
    # the root lists, such as a secret setting's that a secret field shares, is not repeated
    for entry in entries.values():
        own = entry.get("secret_env_vars")
        if isinstance(own, list) and secret_env_vars:
            kept = [name for name in own if name not in secret_env_vars]
            if kept:
                entry["secret_env_vars"] = kept
            else:
                del entry["secret_env_vars"]
    # Everything an agent caches: a new global flag or shared code must change the etag
    shape: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "flags": flags,
        "exit_codes": shared,
        "env_vars": root_env,
        "commands": entries,
    }
    if dependencies:
        shape["dependencies"] = [dict(d) for d in sorted(dependencies, key=lambda d: d["name"])]
    if secret_env_vars:
        shape["secret_env_vars"] = list(secret_env_vars)
    digest = hashlib.sha256(canonical_json(shape).encode()).hexdigest()
    etag = Etag(f"sha256:{digest[:32]}")
    from importlib.metadata import version  # only a manifest reads it (#360)

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "framework_version": version("treaty"),
        "etag": etag.value,
        "flags": flags,
        "exit_codes": shared,
        "env_vars": root_env,
        "commands": entries,
    }
    if dependencies:
        manifest["dependencies"] = shape["dependencies"]
    if secret_env_vars:
        manifest["secret_env_vars"] = shape["secret_env_vars"]
    return manifest
