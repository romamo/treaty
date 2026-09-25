"""Argument parsing: global options, longest-prefix path routing, then per-command tokens.

Written in-house so that every failure becomes a ``ParseError`` with structured
context instead of a formatted message and a hard exit.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import MISSING, dataclass

from ._command import Command, DangerLevel
from ._errors import ParseError
from ._flags import FieldInfo
from ._timeout import Timeout
from ._types import Classified, FlagType
from ._values import CommandPath

TIMEOUT_FLAG = "timeout"
CONFIRM_FLAG = "confirm-destructive"
RAW_PAYLOAD_FLAG = "raw-payload"


@dataclass(frozen=True, slots=True)
class Invocation:
    """Parsed arguments plus framework-level flags for one command run"""

    args: object
    timeout: Timeout | None
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    format: str | None
    help: bool
    schema: bool
    max_output: str | None = None


def without_value(token: str) -> str:
    """``--name=value`` as ``--name``: an unrecognized token may carry a secret after ``=``"""
    return token.partition("=")[0] if token.startswith("--") else token


def split_globals(argv: list[str]) -> tuple[GlobalOptions, list[str]]:
    fmt: str | None = None
    max_output: str | None = None
    help_ = False
    schema = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        if tok in ("--help", "-h"):
            help_ = True
        elif tok == "--schema":
            schema = True
        elif tok == "--format":
            if i + 1 >= len(argv):
                raise ParseError("--format needs a value", context={"flag": "format"})
            fmt = argv[i + 1]
            i += 1
        elif tok.startswith("--format="):
            fmt = tok.partition("=")[2]
        elif tok == "--max-output":
            if i + 1 >= len(argv):
                raise ParseError("--max-output needs a value", context={"flag": "max-output"})
            max_output = argv[i + 1]
            i += 1
        elif tok.startswith("--max-output="):
            max_output = tok.partition("=")[2]
        else:
            rest.append(tok)
        i += 1
    return GlobalOptions(format=fmt, help=help_, schema=schema, max_output=max_output), rest


@dataclass(frozen=True, slots=True)
class Route:
    path: CommandPath | None
    prefix: tuple[str, ...]
    tokens: tuple[str, ...]


def resolve_path(argv: list[str], known: Collection[CommandPath]) -> Route:
    """Consume leading tokens while they extend a registered path or group prefix"""
    prefixes: set[tuple[str, ...]] = set()
    for p in known:
        for n in range(1, len(p.parts) + 1):
            prefixes.add(p.parts[:n])
    consumed: tuple[str, ...] = ()
    i = 0
    while i < len(argv) and not argv[i].startswith("-"):
        candidate = (*consumed, argv[i])
        if candidate not in prefixes:
            break
        consumed = candidate
        i += 1
    path = CommandPath(".".join(consumed)) if consumed else None
    if path is not None and path not in known:
        path = None
    return Route(path=path, prefix=consumed, tokens=tuple(argv[i:]))


def parse_command_args(command: Command, tokens: tuple[str, ...]) -> Invocation:
    values: dict[str, object] = {}
    arrays: dict[str, list[object]] = {}
    timeout: Timeout | None = None
    confirmed = False
    raw_payload: str | None = None
    positionals = [f for f in command.fields if f.positional]
    pos_index = 0
    i = 0
    only_positional = False

    def assign(field: FieldInfo, raw: str) -> None:
        if field.flag_type is FlagType.ARRAY:
            arrays.setdefault(field.name, []).append(field.parse(raw))
        elif field.name in values:
            raise ParseError(f"{field.flag!r} given more than once", context={"flag": field.flag})
        else:
            values[field.name] = field.parse(raw)

    while i < len(tokens):
        tok = tokens[i]
        if only_positional or not tok.startswith("-") or tok == "-":
            if pos_index >= len(positionals):
                raise ParseError(
                    f"unexpected argument {tok!r}",
                    context={"argument": tok, "command": command.path.value},
                )
            field = positionals[pos_index]
            assign(field, tok)
            if field.flag_type is not FlagType.ARRAY:
                pos_index += 1
            i += 1
            continue
        if tok == "--":
            only_positional = True
            i += 1
            continue
        negated = False
        found: FieldInfo | None
        if tok.startswith("--"):
            name, eq, inline = tok[2:].partition("=")
            has_eq = bool(eq)
            if name == TIMEOUT_FLAG and command.has_network_io:
                if has_eq:
                    raw = inline
                elif i + 1 < len(tokens):
                    raw = tokens[i + 1]
                    i += 1
                else:
                    raise ParseError(f"{tok!r} needs a value", context={"flag": TIMEOUT_FLAG})
                if timeout is not None:
                    raise ParseError(
                        "'timeout' given more than once", context={"flag": TIMEOUT_FLAG}
                    )
                timeout = Timeout.parse(raw)
                i += 1
                continue
            if name == RAW_PAYLOAD_FLAG and command.supports_raw_payload:
                if has_eq:
                    raw = inline
                elif i + 1 < len(tokens):
                    raw = tokens[i + 1]
                    i += 1
                else:
                    raise ParseError(f"{tok!r} needs a value", context={"flag": RAW_PAYLOAD_FLAG})
                if raw_payload is not None:
                    raise ParseError(
                        "'raw-payload' given more than once", context={"flag": RAW_PAYLOAD_FLAG}
                    )
                raw_payload = raw
                i += 1
                continue
            if name == CONFIRM_FLAG and command.danger_level is DangerLevel.DESTRUCTIVE:
                if has_eq:
                    raise ParseError(
                        f"'{CONFIRM_FLAG}' takes no value", context={"flag": CONFIRM_FLAG}
                    )
                confirmed = True
                i += 1
                continue
            found = command.field_by_flag(name)
            if found is None and name.startswith("no-"):
                found = command.field_by_flag(name[3:])
                negated = found is not None and found.flag_type is FlagType.BOOLEAN
                if not negated:
                    found = None
        else:
            name, has_eq, inline = tok[1:], False, ""
            found = command.field_by_short(name) if len(name) == 1 else None
        if found is None:
            raise ParseError(
                f"unknown flag {without_value(tok)!r}",
                context={
                    "flag": name,
                    "command": command.path.value,
                    "known": [f.flag for f in command.fields],
                },
            )
        if found.flag_type is FlagType.BOOLEAN:
            if has_eq:
                values[found.name] = found.parse(inline)
            elif found.name in values:
                raise ParseError(
                    f"{found.flag!r} given more than once", context={"flag": found.flag}
                )
            else:
                values[found.name] = not negated
            i += 1
            continue
        if has_eq:
            raw = inline
        elif i + 1 < len(tokens):
            raw = tokens[i + 1]
            i += 1
        else:
            raise ParseError(f"{tok!r} needs a value", context={"flag": found.flag})
        assign(found, raw)
        i += 1

    for name, items in arrays.items():
        values[name] = tuple(items)
    if raw_payload is not None:
        if values:
            raise ParseError(
                "Cannot combine --raw-payload with individual flags",
                context={"flag": RAW_PAYLOAD_FLAG, "also_given": sorted(values)},
            )
        mapping = _decode_raw_payload(raw_payload)
        built = build_from_mapping(command, mapping)
        return Invocation(args=built.args, timeout=timeout, confirmed=confirmed)
    return Invocation(args=_finish(command, values), timeout=timeout, confirmed=confirmed)


def _decode_raw_payload(raw: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(
            "--raw-payload is not valid JSON",
            context={"flag": RAW_PAYLOAD_FLAG, "cause": exc.msg, "position": exc.pos},
        ) from None
    if not isinstance(decoded, dict):
        raise ParseError("--raw-payload must be a JSON object", context={"flag": RAW_PAYLOAD_FLAG})
    return decoded


def known_flags(command: Command) -> list[str]:
    flags = [f.flag for f in command.fields]
    if command.supports_raw_payload:
        flags.append(RAW_PAYLOAD_FLAG)
    if command.has_network_io:
        flags.append(TIMEOUT_FLAG)
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        flags.append(CONFIRM_FLAG)
    return flags


def _finish(command: Command, values: dict[str, object]) -> object:
    missing = [f.flag for f in command.fields if f.required and f.name not in values]
    if missing:
        raise ParseError(
            f"missing required: {', '.join(missing)}",
            context={"missing": missing, "command": command.path.value},
        )
    for f in command.fields:
        if f.name not in values:
            values[f.name] = None if f.default is MISSING else f.default
    return command.args_type(**values)


def build_from_mapping(command: Command, mapping: Mapping[str, object]) -> Invocation:
    """Build an invocation from already-typed JSON values, as ``exec`` receives them"""
    values: dict[str, object] = {}
    timeout: Timeout | None = None
    confirmed = False
    for key, value in mapping.items():
        flag = key.replace("_", "-")
        if flag == TIMEOUT_FLAG and command.has_network_io:
            timeout = Timeout.parse(value)
            continue
        if flag == CONFIRM_FLAG and command.danger_level is DangerLevel.DESTRUCTIVE:
            if not isinstance(value, bool):
                raise ParseError(
                    f"{key!r} expects a boolean", context={"field": key, "value": value}
                )
            confirmed = value
            continue
        found = command.field_by_flag(flag)
        if found is None:
            raise ParseError(
                f"unknown field {key!r}",
                context={
                    "field": key,
                    "command": command.path.value,
                    "known": known_flags(command),
                },
            )
        if found.name in values:
            raise ParseError(f"{key!r} given more than once", context={"field": key})
        if value is None and found.classified.optional:
            values[found.name] = None
            continue
        values[found.name] = _check_json_value(found, value)
    return Invocation(args=_finish(command, values), timeout=timeout, confirmed=confirmed)


def _check_json_value(field: FieldInfo, value: object) -> object:
    try:
        return _check_field_value(field, value)
    except ParseError as exc:
        raise field.scrub(exc) from None


def _check_field_value(field: FieldInfo, value: object) -> object:
    ctx = {"field": field.flag, "value": value}
    if field.flag_type is FlagType.ARRAY:
        item = field.classified.item
        if not isinstance(value, list) or item is None:
            raise ParseError(f"{field.flag!r} expects an array", context=ctx)
        return tuple(_check_scalar(item, v, field.flag) for v in value)
    return _check_scalar(field.classified, value, field.flag)


def _check_scalar(target: Classified, value: object, flag: str) -> object:
    ctx = {"field": flag, "value": value}
    match target.flag_type:
        case FlagType.BOOLEAN:
            if isinstance(value, bool):
                return value
            raise ParseError(f"{flag!r} expects a boolean", context=ctx)
        case FlagType.INTEGER:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise ParseError(f"{flag!r} expects an integer", context=ctx)
        case FlagType.NUMBER:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            raise ParseError(f"{flag!r} expects a number", context=ctx)
        case FlagType.STRING:
            if isinstance(value, str):
                return value
            raise ParseError(f"{flag!r} expects a string", context=ctx)
        case FlagType.ENUM:
            if isinstance(value, str) and value in target.enum_values:
                cls = target.enum_cls
                return cls(value) if cls is not None else value
            raise ParseError(
                f"{flag!r} must be one of {', '.join(target.enum_values)}",
                context={**ctx, "allowed": list(target.enum_values)},
            )
        case FlagType.ARRAY:
            raise ParseError(f"{flag!r}: nested arrays are not supported", context=ctx)
