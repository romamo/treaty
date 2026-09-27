"""The flags treaty adds to a command, as one table.

Each row says which commands get the flag, how argv text and a JSON value become its
value, and how the manifest, the JSON payload schema, and ``--help`` describe it. The
parser, ``known_flags``, the collision check at registration, the manifest, and the help
all iterate ``FLAGS`` instead of spelling the flags out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._auth import HEADLESS_FLAG, TOKEN_ENV_FLAG, is_env_var_name
from ._command import (
    DEFAULT_HEARTBEAT_MS,
    HEARTBEAT_FLAG,
    INPUT_FILE_FLAG,
    OUTPUT_FLAG,
    Command,
    DangerLevel,
)
from ._config import GLOBAL_FLAG, ConfigScope
from ._errors import ParseError
from ._idempotency import IdempotencyKey
from ._page import CURSOR_FLAG, LIMIT_FLAG, Limit, Position, whole_number
from ._paths import check_path
from ._retry import RETRIES_FLAG, RETRY_DELAY_FLAG, parse_delay, parse_retries
from ._timeout import Timeout
from ._types import FlagType

SCHEMA_VERSION_KEY = "schema_version"
"""``--schema-version`` in a JSON payload; a global flag, so no ``FLAGS`` row"""
STABLE_OUTPUT_FLAG = "stable-output"
STABLE_OUTPUT_KEY = "stable_output"
"""``--stable-output`` in a JSON payload; a global flag, so no ``FLAGS`` row"""
TIMEOUT_FLAG = "timeout"
CONFIRM_FLAG = "confirm-destructive"
RAW_PAYLOAD_FLAG = "raw-payload"
IDEMPOTENCY_FLAG = "idempotency-key"
NO_STREAM_FLAG = "no-stream"
LIVE_FLAG = "live"
YES_FLAG = "yes"
NON_INTERACTIVE_FLAG = "non-interactive"

Parse = Callable[[object, Command], object]


@dataclass(frozen=True, slots=True)
class FrameworkFlag:
    name: str
    attr: str
    """The ``Invocation`` field it sets"""
    applies: Callable[[Command], bool]
    type: str
    description: str | Callable[[Command], str]
    parse: Parse | None = None
    """Argv text to the value; None is a switch, which takes no value"""
    from_json: Parse | None = None
    """A JSON value to the value; None when only argv takes the flag"""
    metavar: str = ""
    entry: Callable[[Command], dict[str, object]] = lambda c: {}
    """Manifest keys besides type, required, and description; a None value drops the key"""
    json_extra: dict[str, object] = field(default_factory=dict)
    argv_wins: bool = False
    """Argv overrides a ``--raw-payload`` value instead of conflicting with it"""

    @property
    def key(self) -> str:
        """The JSON spelling"""
        return self.name.replace("-", "_")

    @property
    def switch(self) -> bool:
        return self.parse is None

    def to_entry(self, command: Command) -> dict[str, object]:
        """The manifest ``FlagEntry``"""
        entry: dict[str, object] = {"type": self.type, "required": False}
        if self.switch:
            entry["default"] = False
        entry |= self.entry(command)
        entry["description"] = self.describe(command)
        return {k: v for k, v in entry.items() if v is not None}

    def to_property(self, command: Command) -> dict[str, object]:
        """The JSON Schema property of ``exec``, MCP, and ``--raw-payload`` payloads"""
        prop = {k: v for k, v in self.to_entry(command).items() if k != "required"}
        prop.pop("pattern_type", None)
        return prop | self.json_extra

    def describe(self, command: Command) -> str:
        text = self.description
        return text if isinstance(text, str) else text(command)

    def help_row(self, command: Command) -> tuple[str, str]:
        usage = f"--{self.name} {self.metavar}" if self.metavar else f"--{self.name}"
        return usage, self.describe(command)


def framework_flags(command: Command, *, json: bool = False) -> list[FrameworkFlag]:
    """The flags ``command`` gets, in help order; ``json=True`` leaves out argv-only ones"""
    return [f for f in FLAGS if f.applies(command) and (f.from_json is not None or not json)]


def flag_named(command: Command, name: str, *, json: bool = False) -> FrameworkFlag | None:
    return next((f for f in framework_flags(command, json=json) if f.name == name), None)


def _never(command: Command) -> bool:
    """For an opt-in keyword that does not exist yet: no command can declare it"""
    return False


# REQ-F-079: flag names treaty keeps for itself, so no app field takes one; reserving a
# name after 1.0 would break the apps that did. A name leaves UNIMPLEMENTED when its
# feature lands; until then, passing it exits 2 as reserved.
RESERVED_GLOBAL: frozenset[str] = frozenset(
    {
        "output-schema",
        "print-schema",
        "schema-version",
        "config",
        "context",
        "no-config",
        "show-config",
        "instance-id",
        "validate-only",
        "stable-output",
        "unmask",
        "no-injection-protection",
        "cwd",
        "no-update-check",
        "quiet",
        "verbose",
        "debug",
        "warnings-as-errors",
        "fields",
        "stream",
        "token-limit",
        "token-offset",
        "token-count",
        "tokenizer",
    }
)
"""Reserved on every command"""

RESERVED_OPT_IN: Mapping[str, Callable[[Command], bool]] = {
    "retries": lambda c: c.retry is not None,
    "retry-delay": lambda c: c.retry is not None,
    "resume-from": _never,  # resumable= (06)
    "rollback-on-failure": _never,  # rollback= (06)
    "proxy": lambda c: c.has_network_io,
    "no-proxy": lambda c: c.has_network_io,
    "no-follow-symlinks": _never,  # recursive_traversal= (10)
    "max-depth": _never,  # recursive_traversal= (10)
}
"""Reserved on the commands that opt in to the feature"""

IMPLEMENTED: frozenset[str] = frozenset(
    {
        "output-schema",
        "print-schema",
        "schema-version",
        "stable-output",
        "config",
        "context",
        "no-config",
        "show-config",
        "instance-id",
        "retries",
        "retry-delay",
    }
)
UNIMPLEMENTED: frozenset[str] = (RESERVED_GLOBAL | frozenset(RESERVED_OPT_IN)) - IMPLEMENTED
"""Reserved names whose feature has not landed yet"""


def reserved_names(command: Command) -> frozenset[str]:
    """Every name reserved on ``command``"""
    return RESERVED_GLOBAL | {n for n, applies in RESERVED_OPT_IN.items() if applies(command)}


def reserved_flag(name: str) -> ParseError:
    """A reserved name passed before its feature exists"""
    return ParseError(
        f"--{name} is reserved for a treaty feature this version does not have",
        code="RESERVED_FLAG",
        context={"flag": name},
        suggestion=f"drop --{name}; the manifest lists the flags this version accepts",
    )


def framework_collisions(command: Command) -> list[str]:
    """Fields whose flag a framework flag, or a name reserved for one, would shadow"""
    names = {f.name for f in framework_flags(command)} | reserved_names(command)
    return sorted(
        f.flag
        for f in command.fields
        if f.flag in names
        # --no-<name> negates a boolean, so a boolean 'stream' would lose --no-stream
        or (f.flag_type is FlagType.BOOLEAN and f"no-{f.flag}" in names)
    )


def env_var(raw: str) -> str:
    """``--token-env-var``: the name of a variable, never the token itself"""
    if not is_env_var_name(raw):
        raise ParseError(
            f"'{TOKEN_ENV_FLAG}' takes the name of an environment variable, such as MY_TOKEN",
            context={"flag": TOKEN_ENV_FLAG},
            suggestion="export the token in a variable and pass its name, not its value",
        )
    return raw


# REQ-O-001: names an agent may pass to --output meaning a representation, not a file
_FORMAT_NAMES = frozenset({"json", "jsonl", "tsv", "csv", "plain", "table", "id", "yaml"})


def output_path(raw: str) -> Path:
    """``--output``: a file path, never a format name such as ``json``"""
    if raw in _FORMAT_NAMES:
        raise ParseError(
            f"--output takes a file path, not the format {raw!r}",
            context={"flag": OUTPUT_FLAG, "value": raw},
            suggestion=f"use --format {raw} to choose the representation",
        )
    return check_path(raw, OUTPUT_FLAG)


def parse_heartbeat(raw: str) -> int:
    """``--heartbeat-ms``: whole milliseconds; 0 turns heartbeats off"""
    expects = "whole milliseconds, at most a day; 0 turns heartbeats off"
    value = whole_number(raw, HEARTBEAT_FLAG, expects)
    if value > 86_400_000:
        raise ParseError(f"'heartbeat-ms' expects {expects}", context={"flag": HEARTBEAT_FLAG})
    return value


def switch_value(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise ParseError(f"{key!r} expects a boolean", context={"field": key, "value": value})
    return value


def _text(parse: Callable[[str], object], key: str) -> Parse:
    """A string flag's JSON form: the same parse, once the value is known to be text"""

    def from_json(value: object, command: Command) -> object:
        if not isinstance(value, str):
            raise ParseError(f"{key!r} expects a string", context={"field": key})
        return parse(value)

    return from_json


def _cursor(value: object, command: Command) -> Position | None:
    # null is the first page, as a JSON caller spells an absent cursor
    return None if value is None else Position.decode(value, command.path)


def _switch(
    name: str,
    attr: str,
    applies: Callable[[Command], bool],
    description: str | Callable[[Command], str],
    **extra: Any,
) -> FrameworkFlag:
    return FrameworkFlag(
        name,
        attr,
        applies,
        "boolean",
        description,
        from_json=lambda v, c: switch_value(v, name.replace("-", "_")),
        **extra,
    )


def _only_global(c: Command) -> bool:
    return c.config_write_scope is ConfigScope.GLOBAL


FLAGS: tuple[FrameworkFlag, ...] = (
    _switch(
        LIVE_FLAG,
        "live",
        lambda c: c.safe_default,
        "Apply; this is the confirmation. Without it the command runs as a dry run and exits 0",
    ),
    _switch(
        CONFIRM_FLAG,
        "confirmed",
        lambda c: c.danger_level is DangerLevel.DESTRUCTIVE,
        lambda c: (
            "Not needed: --live applies and is the confirmation"
            if c.safe_default
            else "Required to apply; without it the command previews and exits 2"
        ),
    ),
    FrameworkFlag(
        IDEMPOTENCY_FLAG,
        "idempotency_key",
        lambda c: c.danger_level is not DangerLevel.SAFE,
        "string",
        (
            "Repeat calls with the same key return the original result with effect noop "
            "instead of running again; without a key, repeats are deduplicated only within "
            "an agent session ($<APP>_SESSION)"
        ),
        parse=lambda v, c: IdempotencyKey(str(v)),
        from_json=_text(IdempotencyKey, "idempotency_key"),
        metavar="KEY",
    ),
    FrameworkFlag(
        TIMEOUT_FLAG,
        "timeout",
        lambda c: c.accepts_timeout,
        "number",
        lambda c: (
            "Seconds to wait for each event before TIMEOUT; 0 disables the limit"
            if c.streaming
            else "Seconds before the framework aborts with TIMEOUT; 0 disables the limit"
        ),
        parse=lambda v, c: Timeout.parse(v),
        from_json=lambda v, c: Timeout.parse(v),
        metavar="SECONDS",
    ),
    FrameworkFlag(
        RAW_PAYLOAD_FLAG,
        "raw_payload",
        lambda c: c.supports_raw_payload,
        "string",
        "JSON object of field values; cannot be combined with individual flags",
        parse=lambda v, c: v,
        metavar="JSON",
    ),
    _switch(
        NO_STREAM_FLAG,
        "no_stream",
        lambda c: c.streaming,
        "Return one envelope with every event in data instead of one envelope line per event",
    ),
    FrameworkFlag(
        LIMIT_FLAG,
        "limit",
        lambda c: c.paginated,
        "integer",
        "Most items to return; 0 returns every item",
        parse=lambda v, c: Limit.parse(str(v)),
        from_json=lambda v, c: Limit.from_json(v),
        metavar="N",
        entry=lambda c: {"default": c.default_limit.count or 0},
        json_extra={"minimum": 0},
        argv_wins=True,
    ),
    FrameworkFlag(
        CURSOR_FLAG,
        "cursor",
        lambda c: c.paginated,
        "string",
        "meta.pagination.next_cursor of the previous page, to get the next",
        parse=_cursor,
        from_json=_cursor,
        metavar="TOKEN",
        json_extra={"type": ["string", "null"]},
        argv_wins=True,
    ),
    FrameworkFlag(
        OUTPUT_FLAG,
        "output",
        lambda c: c.output_file,
        "string",
        "Write the result to this file in the --format representation; stdout gets the envelope",
        parse=lambda v, c: output_path(str(v)),
        metavar="PATH",
        entry=lambda c: {"pattern_type": "filepath"},
    ),
    FrameworkFlag(
        INPUT_FILE_FLAG,
        "input_file",
        lambda c: c.stdin_input,
        "string",
        "Read the input from this file, of any size, instead of stdin; - is stdin, capped",
        parse=lambda v, c: check_path(str(v), INPUT_FILE_FLAG),
        from_json=_text(lambda v: check_path(v, INPUT_FILE_FLAG), "input_file"),
        metavar="PATH",
        entry=lambda c: {"pattern_type": "filepath"},
    ),
    FrameworkFlag(
        HEARTBEAT_FLAG,
        "heartbeat_ms",
        lambda c: c.heartbeat,
        "integer",
        "Milliseconds between heartbeat lines on stdout while the command runs; 0 turns them off",
        parse=lambda v, c: parse_heartbeat(str(v)),
        metavar="MS",
        entry=lambda c: {"default": DEFAULT_HEARTBEAT_MS},
    ),
    _switch(
        YES_FLAG,
        "yes",
        lambda c: c.interactive,
        "Answer yes to every confirmation instead of asking",
    ),
    _switch(
        NON_INTERACTIVE_FLAG,
        "non_interactive",
        lambda c: c.interactive,
        "Never prompt, even on a terminal; a needed answer exits 4",
    ),
    _switch(
        GLOBAL_FLAG,
        "global_config",
        lambda c: c.config_write_scope is not None,
        lambda c: (
            "Required: the command writes the user config file"
            if _only_global(c)
            else "Write the user config file instead of the project's"
        ),
        entry=lambda c: {"required": True, "default": None} if _only_global(c) else {},
    ),
    _switch(
        HEADLESS_FLAG,
        "headless",
        lambda c: c.auth is not None,
        "Never open a browser; read the token from --token-env-var or the default "
        "variables. Implied without a terminal",
    ),
    FrameworkFlag(
        TOKEN_ENV_FLAG,
        "token_env_var",
        lambda c: c.auth is not None,
        "string",
        "Name of the environment variable holding a pre-acquired token",
        parse=lambda v, c: env_var(str(v)),
        from_json=_text(env_var, "token_env_var"),
        metavar="NAME",
    ),
    FrameworkFlag(
        RETRIES_FLAG,
        "retries",
        lambda c: c.retry is not None,
        "integer",
        "Most times to retry a transient failure inside the command; 0 fails on the first",
        parse=lambda v, c: parse_retries(v),
        from_json=lambda v, c: parse_retries(v),
        metavar="N",
        entry=lambda c: {"default": c.retry.retries if c.retry else None},
        json_extra={"minimum": 0},
    ),
    FrameworkFlag(
        RETRY_DELAY_FLAG,
        "retry_delay_ms",
        lambda c: c.retry is not None,
        "string",
        "Wait between retries, such as 500ms or 2s; the timeout bounds every attempt",
        parse=lambda v, c: parse_delay(v),
        from_json=lambda v, c: parse_delay(v),
        metavar="DURATION",
        entry=lambda c: {"default": f"{c.retry.delay_ms}ms" if c.retry else None},
        json_extra={"type": ["string", "integer"]},
    ),
)
