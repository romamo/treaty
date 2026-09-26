"""Argument parsing: global options, longest-prefix path routing, then per-command tokens.

Written in-house so that every failure becomes a ``ParseError`` with structured
context instead of a formatted message and a hard exit.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import MISSING, dataclass

from ._command import Command, DangerLevel
from ._dispatch import loads_strict
from ._errors import ParseError
from ._flags import FieldInfo, apply_scalar
from ._idempotency import IdempotencyKey
from ._paths import check_path
from ._secrets import (
    SecretRef,
    SecretSource,
    direct_secret_error,
    resolve_secret,
    split_source_flag,
)
from ._timeout import Timeout
from ._types import Classified, FlagType
from ._values import CommandPath

TIMEOUT_FLAG = "timeout"
CONFIRM_FLAG = "confirm-destructive"
RAW_PAYLOAD_FLAG = "raw-payload"
IDEMPOTENCY_FLAG = "idempotency-key"
NO_STREAM_FLAG = "no-stream"


@dataclass(frozen=True, slots=True)
class Invocation:
    """Parsed arguments plus framework-level flags for one command run"""

    args: object
    timeout: Timeout | None
    confirmed: bool = False
    idempotency_key: IdempotencyKey | None = None
    no_stream: bool = False
    """A streaming command asked for one buffered envelope instead of JSONL"""


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    format: str | None
    help: bool
    schema: bool
    max_output: str | None = None


def without_value(token: str) -> str:
    """``--name=value`` as ``--name``: an unrecognized token may carry a secret after ``=``"""
    return token.partition("=")[0] if token.startswith("--") else token


_NEGATIVE_NUMBER = re.compile(r"-(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _is_negative(tok: str, command: Command) -> bool:
    """``-5`` is a value, not a flag, unless the command has a digit short flag"""
    return bool(_NEGATIVE_NUMBER.fullmatch(tok)) and command.field_by_short(tok[1]) is None


def describe_number(value: int | float) -> str:
    """A non-finite float or an out-of-range int for an error, never as a JSON number
    (NaN is invalid JSON, and str() refuses an int past the digit limit)"""
    if isinstance(value, float):
        return str(value)
    return f"an integer of {value.bit_length()} bits"


def _repeated(flag: str) -> ParseError:
    return ParseError(
        f"{flag!r} given more than once with different values", context={"flag": flag}
    )


VALUED_GLOBALS = frozenset({"format", "max-output"})
FORMAT_GUESSES = frozenset({"--output", "--output-format", "--json"})


def format_hint(token: str) -> str | None:
    """Point agents that guess another output-representation flag at ``--format``"""
    if without_value(token) in FORMAT_GUESSES:
        return "use --format json (or --format human) to choose the output representation"
    return None


def split_globals(argv: list[str]) -> tuple[GlobalOptions, list[str]]:
    """Global options in any position; a valued one repeated with a different value is
    an error rather than last-wins (REQ-F-067, REQ-F-079)"""
    valued: dict[str, str] = {}
    help_ = False
    schema = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        name, eq, inline = tok[2:].partition("=") if tok.startswith("--") else ("", "", "")
        if tok in ("--help", "-h"):
            help_ = True
        elif tok == "--schema":
            schema = True
        elif name in VALUED_GLOBALS:
            if eq:
                value = inline
            elif i + 1 < len(argv):
                i += 1
                value = argv[i]
            else:
                raise ParseError(f"--{name} needs a value", context={"flag": name})
            if valued.setdefault(name, value) != value:
                raise _repeated(name)
        else:
            rest.append(tok)
        i += 1
    return (
        GlobalOptions(
            format=valued.get("format"),
            help=help_,
            schema=schema,
            max_output=valued.get("max-output"),
        ),
        rest,
    )


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


def misplaced_flag_target(route: Route, known: Collection[CommandPath]) -> CommandPath | None:
    """The command a flag placed before the path was meant for, if the words after it name one

    Words are tried from each starting point so a flag value (``--to 1.3.9 deploy rollback``)
    does not hide the path that follows it.
    """
    words = [t for t in route.tokens if not t.startswith("-")]
    for start in range(len(words)):
        path = resolve_path([*route.prefix, *words[start:]], known).path
        if path is not None:
            return path
    return None


class _Collector:
    """Phase 1 keeps going past a field error so one run reports them all (REQ-F-015)

    Every argv token error is collected, a trailing flag with no value included; only
    invalid ``--raw-payload`` JSON, which makes the payload unreadable, is raised at once.
    """

    def __init__(self) -> None:
        self.errors: list[ParseError] = []

    def add(self, exc: ParseError) -> None:
        self.errors.append(exc)

    def finish(self) -> None:
        if self.errors:
            raise ParseError.combine(self.errors)


def parse_command_args(
    command: Command, tokens: tuple[str, ...], env: Mapping[str, str]
) -> Invocation:
    values: dict[str, object] = {}
    secrets: dict[str, SecretRef] = {}
    arrays: dict[str, list[object]] = {}
    timeout: Timeout | None = None
    confirmed = False
    raw_payload: str | None = None
    key: IdempotencyKey | None = None
    no_stream = False
    positionals = [f for f in command.fields if f.positional]
    pos_index = 0
    i = 0
    only_positional = False
    errors = _Collector()

    def store(field: FieldInfo, parsed: object) -> None:
        """A scalar repeated with the same value is accepted; a different value is not"""
        if field.flag_type is FlagType.ARRAY:
            arrays.setdefault(field.name, []).append(parsed)
        elif field.name in values and values[field.name] != parsed:
            errors.add(_repeated(field.flag))
        else:
            values[field.name] = parsed

    def assign(field: FieldInfo, raw: str, *, negated: bool = False) -> None:
        try:
            parsed = field.parse(raw)
        except ParseError as exc:
            errors.add(exc)
            return
        store(field, (not parsed) if negated else parsed)

    def value_after(tok: str, flag: str, has_eq: bool, inline: str) -> str:
        """The token's value, consuming the next token when it is not inline"""
        nonlocal i
        if has_eq:
            return inline
        if i + 1 < len(tokens):
            i += 1
            return tokens[i]
        raise ParseError(f"{tok!r} needs a value", context={"flag": flag})

    while i < len(tokens):
        # Every token error is collected (REQ-F-015); only the loop's own state moves on
        try:
            tok = tokens[i]
            if (
                only_positional
                or not tok.startswith("-")
                or tok == "-"
                or _is_negative(tok, command)
            ):
                # Values fill positionals in argv order; a slot a flag already set is skipped
                while (
                    pos_index < len(positionals)
                    and positionals[pos_index].flag_type is not FlagType.ARRAY
                    and positionals[pos_index].name in values
                ):
                    pos_index += 1
                if pos_index >= len(positionals):
                    errors.add(
                        ParseError(
                            f"unexpected argument {tok!r}",
                            context={"argument": tok, "command": command.path.value},
                        )
                    )
                else:
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
                if name == TIMEOUT_FLAG and command.accepts_timeout:
                    parsed_timeout = Timeout.parse(value_after(tok, TIMEOUT_FLAG, has_eq, inline))
                    if timeout is not None and timeout != parsed_timeout:
                        raise _repeated(TIMEOUT_FLAG)
                    timeout = parsed_timeout
                    i += 1
                    continue
                if name == RAW_PAYLOAD_FLAG and command.supports_raw_payload:
                    raw = value_after(tok, RAW_PAYLOAD_FLAG, has_eq, inline)
                    if raw_payload is not None and raw_payload != raw:
                        raise _repeated(RAW_PAYLOAD_FLAG)
                    raw_payload = raw
                    i += 1
                    continue
                if name == IDEMPOTENCY_FLAG and command.danger_level is not DangerLevel.SAFE:
                    parsed_key = IdempotencyKey(value_after(tok, IDEMPOTENCY_FLAG, has_eq, inline))
                    if key is not None and key != parsed_key:
                        raise _repeated(IDEMPOTENCY_FLAG)
                    key = parsed_key
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
                if name == NO_STREAM_FLAG and command.streaming:
                    if has_eq:
                        raise ParseError(
                            f"'{NO_STREAM_FLAG}' takes no value", context={"flag": NO_STREAM_FLAG}
                        )
                    no_stream = True
                    i += 1
                    continue
                found = command.field_by_flag(name)
                if found is not None and found.secret:
                    errors.add(direct_secret_error(found.flag))
                    if not has_eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                        i += 1  # the value that was meant for it; never echoed
                    i += 1
                    continue
                if found is None and (split := split_source_flag(name)) is not None:
                    base, source = split
                    owner = command.field_by_flag(base)
                    if owner is not None and owner.secret:
                        ref = value_after(tok, name, has_eq, inline)
                        try:
                            _take_secret(secrets, owner, SecretRef(source, ref))
                        except ParseError as exc:
                            errors.add(exc)
                        i += 1
                        continue
                if found is None and name.startswith("no-"):
                    found = command.field_by_flag(name[3:])
                    negated = found is not None and found.flag_type is FlagType.BOOLEAN
                    if not negated:
                        found = None
            else:
                name, has_eq, inline = tok[1:], False, ""
                found = command.field_by_short(name) if len(name) == 1 else None
            if found is None:
                errors.add(
                    ParseError(
                        f"unknown flag {without_value(tok)!r}",
                        context={
                            "flag": name,
                            "command": command.path.value,
                            "known": known_flags(command),
                        },
                        suggestion=format_hint(tok),
                    )
                )
                if not has_eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    i += 1  # skip what looks like its value rather than misread it as positional
                i += 1
                continue
            if found.flag_type is FlagType.BOOLEAN:
                if has_eq:
                    assign(found, inline, negated=negated)
                else:
                    store(found, not negated)
                i += 1
                continue
            raw = value_after(tok, found.flag, has_eq, inline)
            assign(found, raw)
            i += 1
        except ParseError as exc:
            errors.add(exc)
            i += 1

    for name, items in arrays.items():
        values[name] = tuple(items)
    if raw_payload is not None:
        if values or secrets:
            raise ParseError(
                "Cannot combine --raw-payload with individual flags",
                context={"flag": RAW_PAYLOAD_FLAG, "also_given": sorted(values | secrets)},
            )
        errors.finish()
        mapping = _decode_raw_payload(raw_payload)
        built = build_from_mapping(command, mapping, env)
        # Framework keys may come from argv or the payload; both only if they agree
        if timeout is not None and built.timeout is not None and timeout != built.timeout:
            raise _repeated(TIMEOUT_FLAG)
        if key is not None and built.idempotency_key not in (None, key):
            raise _repeated(IDEMPOTENCY_FLAG)
        for flag, given in ((CONFIRM_FLAG, confirmed), (NO_STREAM_FLAG, no_stream)):
            spellings = (flag, flag.replace("-", "_"))
            if given and any(mapping.get(k) is False for k in spellings):
                raise _repeated(flag)
        return Invocation(
            args=built.args,
            timeout=timeout or built.timeout,
            confirmed=confirmed or built.confirmed,
            idempotency_key=key or built.idempotency_key,
            no_stream=no_stream or built.no_stream,
        )
    _apply_secrets(command, values, secrets, env, errors)
    return Invocation(
        args=_finish(command, values, errors),
        timeout=timeout,
        confirmed=confirmed,
        idempotency_key=key,
        no_stream=no_stream,
    )


def _take_secret(secrets: dict[str, SecretRef], field: FieldInfo, ref: SecretRef) -> None:
    if field.name in secrets and secrets[field.name] != ref:
        raise ParseError(
            f"{field.flag!r} given more than once; use one of --{field.env_flag} "
            f"or --{field.file_flag}",
            context={"flag": field.flag + ref.source.suffix},
        )
    secrets[field.name] = ref


def _apply_secrets(
    command: Command,
    values: dict[str, object],
    secrets: dict[str, SecretRef],
    env: Mapping[str, str],
    errors: _Collector,
) -> None:
    """Resolve every secret field in phase 1: named source, else its default variable"""
    for f in command.fields:
        if not f.secret:
            continue
        ref = secrets.get(f.name)
        if ref is None:
            var = command.secret_env_vars[f.name]
            if env.get(var):
                ref = SecretRef(SecretSource.ENV, var)
            else:
                continue
        try:
            values[f.name] = f.parse(resolve_secret(f.flag, ref, env))
        except ParseError as exc:
            errors.add(exc)


def _decode_raw_payload(raw: str) -> Mapping[str, object]:
    try:
        decoded = loads_strict(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(
            "--raw-payload is not valid JSON",
            context={"flag": RAW_PAYLOAD_FLAG, "cause": exc.msg, "position": exc.pos},
        ) from None
    except ValueError as exc:
        raise ParseError(
            "--raw-payload is not valid JSON", context={"flag": RAW_PAYLOAD_FLAG, "cause": str(exc)}
        ) from None
    if not isinstance(decoded, dict):
        raise ParseError("--raw-payload must be a JSON object", context={"flag": RAW_PAYLOAD_FLAG})
    return decoded


def known_flags(command: Command) -> list[str]:
    flags = [name for f in command.fields for name in f.exposed_flags()]
    if command.supports_raw_payload:
        flags.append(RAW_PAYLOAD_FLAG)
    if command.accepts_timeout:
        flags.append(TIMEOUT_FLAG)
    if command.danger_level is DangerLevel.DESTRUCTIVE:
        flags.append(CONFIRM_FLAG)
    if command.danger_level is not DangerLevel.SAFE:
        flags.append(IDEMPOTENCY_FLAG)
    if command.streaming:
        flags.append(NO_STREAM_FLAG)
    return flags


def _finish(command: Command, values: dict[str, object], errors: _Collector) -> object:
    """Report missing fields alongside everything collected, then build the dataclass"""
    failed = {e.field for e in errors.errors}
    missing = [
        f.env_flag if f.secret else f.flag
        for f in command.fields
        # A secret's source errors name --x-from-env or --x-from-file, not --x
        if f.required and f.name not in values and failed.isdisjoint({f.flag, *f.exposed_flags()})
    ]
    if missing:
        errors.add(
            ParseError(
                f"missing required: {', '.join(missing)}",
                context={"missing": missing, "command": command.path.value},
            )
        )
    errors.finish()
    for f in command.fields:
        if f.name not in values:
            values[f.name] = None if f.default is MISSING else f.default
    return command.args_type(**values)


def build_from_mapping(
    command: Command, mapping: Mapping[str, object], env: Mapping[str, str]
) -> Invocation:
    """Build an invocation from already-typed JSON values, as ``exec`` receives them"""
    values: dict[str, object] = {}
    secrets: dict[str, SecretRef] = {}
    timeout: Timeout | None = None
    confirmed = False
    idempotency_key: IdempotencyKey | None = None
    no_stream = False
    errors = _Collector()
    for key, value in mapping.items():
        try:
            flag = key.replace("_", "-")
            if flag == TIMEOUT_FLAG and command.accepts_timeout:
                timeout = Timeout.parse(value)
                continue
            if flag == IDEMPOTENCY_FLAG and command.danger_level is not DangerLevel.SAFE:
                if not isinstance(value, str):
                    raise ParseError(f"{key!r} expects a string", context={"field": key})
                idempotency_key = IdempotencyKey(value)
                continue
            if flag == CONFIRM_FLAG and command.danger_level is DangerLevel.DESTRUCTIVE:
                if not isinstance(value, bool):
                    raise ParseError(
                        f"{key!r} expects a boolean", context={"field": key, "value": value}
                    )
                confirmed = value
                continue
            if flag == NO_STREAM_FLAG and command.streaming:
                if not isinstance(value, bool):
                    raise ParseError(
                        f"{key!r} expects a boolean", context={"field": key, "value": value}
                    )
                no_stream = value
                continue
            found = command.field_by_flag(flag)
            if found is not None and found.secret:
                raise direct_secret_error(found.flag)
            if found is None and (split := split_source_flag(flag)) is not None:
                base, source = split
                owner = command.field_by_flag(base)
                if owner is not None and owner.secret:
                    if not isinstance(value, str):
                        raise ParseError(f"{key!r} expects a string", context={"field": key})
                    _take_secret(secrets, owner, SecretRef(source, value))
                    continue
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
        except ParseError as exc:
            errors.add(exc)
    _apply_secrets(command, values, secrets, env, errors)
    return Invocation(
        args=_finish(command, values, errors),
        timeout=timeout,
        confirmed=confirmed,
        idempotency_key=idempotency_key,
        no_stream=no_stream,
    )


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
        return tuple(_check_patterned(field, item, v) for v in value)
    return _check_patterned(field, field.classified, value)


def _check_patterned(field: FieldInfo, target: Classified, value: object) -> object:
    if isinstance(value, str):
        field.check_pattern(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            token = str(value)  # the token argv would have carried
        except ValueError:
            raise ParseError(
                f"{field.flag!r} is too large",
                context={"field": field.flag, "value": describe_number(value)},
            ) from None
        field.check_pattern(token)
    base = _check_base(target, value, field.flag)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, field.flag, secret=field.secret)


def _check_base(target: Classified, value: object, flag: str) -> object:
    ctx = {"field": flag, "value": value}
    match target.flag_type:
        case FlagType.BOOLEAN:
            if isinstance(value, bool):
                return value
            raise ParseError(f"{flag!r} expects a boolean", context=ctx)
        case FlagType.INTEGER:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)  # JSON Schema counts 2.0 as an integer, and clients send it
            raise ParseError(f"{flag!r} expects an integer", context=ctx)
        case FlagType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParseError(f"{flag!r} expects a number", context=ctx)
            try:
                number = float(value)
            except OverflowError:
                number = math.inf
            if not math.isfinite(number):
                raise ParseError(
                    f"{flag!r} expects a finite number",
                    context={"field": flag, "value": describe_number(value)},
                )
            return number
        case FlagType.STRING:
            if isinstance(value, str):
                return check_path(value, flag) if target.path else value
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
