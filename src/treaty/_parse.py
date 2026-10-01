"""Argument parsing: global options, longest-prefix path routing, then per-command tokens.

Written in-house so that every failure becomes a ``ParseError`` with structured
context instead of a formatted message and a hard exit.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import MISSING, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from ._command import Command, OptionPlacement
from ._declare import shell_safe
from ._dispatch import invalid_json
from ._errors import ArgsCrashed, ParseError
from ._flags import FieldInfo, apply_scalar
from ._framework import (
    NO_INJECTION_FLAG,
    RAW_PAYLOAD_FLAG,
    RESERVED_GLOBAL,
    SCHEMA_VERSION_KEY,
    STABLE_OUTPUT_FLAG,
    STABLE_OUTPUT_KEY,
    UNIMPLEMENTED,
    UNMASK_FLAG,
    flag_named,
    framework_flags,
    reserved_flag,
    reserved_names,
    switch_value,
)
from ._idempotency import IdempotencyKey
from ._json5 import JsonFloat, Unreadable, loads_forgiving
from ._page import Limit, Position
from ._paths import check_path
from ._rules import check_rules
from ._scalars import DECIMAL
from ._secrets import (
    SecretRef,
    SecretSource,
    direct_secret_error,
    resolve_secret,
    split_source_flag,
)
from ._select import (
    FIELDS_FLAG,
    FIELDS_KEY,
    STREAM_FLAG,
    TOKEN_COUNT_FLAG,
    TOKEN_LIMIT_FLAG,
    TOKEN_OFFSET_FLAG,
    TOKENIZER_FLAG,
    parse_fields,
    token_number,
)
from ._steps import StepName
from ._timeout import Timeout
from ._types import Classified, FlagType
from ._values import CommandPath, InvalidValue, SchemaVersion
from ._verbosity import (
    DEBUG_FLAG,
    QUIET_FLAG,
    VERBOSE_FLAG,
    WARNINGS_AS_ERRORS_FLAG,
    short_verbosity,
)


@dataclass(frozen=True, slots=True)
class Invocation:
    """Parsed arguments plus framework-level flags for one command run"""

    args: object
    timeout: Timeout | None = None
    confirmed: bool = False
    preview: bool = False
    """A destructive run without ``--confirm-destructive``: phase 1 turned the dry-run
    switch on in ``args``, and the run ends ``CONFIRMATION_REQUIRED``"""
    idempotency_key: IdempotencyKey | None = None
    no_stream: bool = False
    """A streaming command asked for one buffered envelope instead of JSONL"""
    live: bool = False
    """A ``safe_default`` command asked to apply instead of its default dry run"""
    yes: bool = False
    """``--yes``: every ``ctx.confirm`` of an interactive command answers yes"""
    non_interactive: bool = False
    """``--non-interactive``: an interactive command never prompts, even on a terminal"""
    limit: Limit | None = None
    """``--limit`` of a list command; None takes the command's default"""
    cursor: Position | None = None
    """``--cursor`` of a list command, decoded; None is the first page"""
    heartbeat_ms: int | None = None
    """``--heartbeat-ms`` of a ``heartbeat=True`` command; None is the default, 0 is off"""
    input_file: Path | None = None
    """``--input-file`` of a ``stdin_input`` command; None or ``-`` reads stdin"""
    stdin_text: str | None = None
    """The payload of a ``stdin_input`` command, read before the handler runs"""
    output: Path | None = None
    """``--output`` of an ``output_file`` command: where the rendered ``data`` goes"""
    headless: bool = False
    """``--headless`` of a login command: never wait for a person at a browser"""
    token_env_var: str | None = None
    """``--token-env-var`` of a login command: the one variable to read the token from"""
    token: str | None = None
    """The pre-acquired token of a login command, read before the handler runs"""
    global_config: bool = False
    """``--global`` of a config-writing command: write the user file, not the project's"""
    retries: int | None = None
    """``--retries`` of a ``retry=`` command; None takes the declared budget"""
    retry_delay_ms: int | None = None
    """``--retry-delay`` of a ``retry=`` command, in milliseconds"""
    schema_version: SchemaVersion | None = None
    """The older output schema ``--schema-version`` pinned; None is the current"""
    stable_output: bool = False
    """``stable_output`` of an exec line or MCP call: the ``--stable-output`` global"""
    validate_only: bool = False
    """``--validate-only``: answer once phase 1 passes, without running (REQ-O-009)"""
    resume_from: StepName | None = None
    """``--resume-from`` of a ``resumable`` command: the step to start at (REQ-O-010)"""
    rollback_on_failure: bool = False
    """``--rollback-on-failure`` of a ``rollback=`` command (REQ-O-011)"""
    no_cache: bool = False
    """``--no-cache`` of a ``cache=`` command: ``ctx.cache`` reads and writes nothing"""
    cache_ttl: int | None = None
    """``--cache-ttl`` of a ``cache=`` command; None is the declared TTL (REQ-O-018)"""
    proxy: str | None = None
    """``--proxy`` of a network command: ``ctx.http``'s proxy, over the env (REQ-O-019)"""
    no_proxy: bool = False
    """``--no-proxy`` of a network command: ``ctx.http`` connects directly"""
    no_follow_symlinks: bool = False
    """``--no-follow-symlinks`` of a ``recursive_traversal`` command (REQ-O-040)"""
    max_depth: int | None = None
    """``--max-depth`` of a ``recursive_traversal`` command; None is the default, 50"""
    heartbeat_interval: float | None = None
    """``--heartbeat-interval`` of a ``heartbeat=True`` command: seconds between progress
    lines on stderr; None is off (REQ-O-012)"""
    fields: tuple[str, ...] | None = None
    """``fields`` of an exec line or MCP call: the ``--fields`` global (REQ-O-002)"""
    given: frozenset[str] = frozenset()
    """The fields the caller supplied, as opposed to defaulted"""

    def __post_init__(self) -> None:
        if self.proxy is not None and self.no_proxy:
            raise ParseError(
                "--proxy and --no-proxy contradict each other; pass one",
                context={"flag": "no-proxy", "also_given": ["proxy"]},
            )


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    format: str | None
    help: bool
    schema: bool
    max_output: str | None = None
    output_schema: bool = False
    schema_version: str | None = None
    stable_output: bool = False
    config: str | None = None
    """``--config PATH``: read only this config file, and write it (REQ-O-024)"""
    context: str | None = None
    no_config: bool = False
    show_config: bool = False
    instance_id: str | None = None
    unmask: bool = False
    """``--unmask``: raw high-entropy values (REQ-O-037)"""
    no_injection_protection: bool = False
    """``--no-injection-protection``: external content without trust tags (REQ-O-023)"""
    cwd: str | None = None
    """``--cwd PATH``: the directory relative paths resolve against (REQ-O-017)"""
    no_update_check: bool = False
    """``--no-update-check``: no update check this run (REQ-O-020)"""
    verbosity: frozenset[str] = frozenset()
    """Which of ``--quiet``, ``--verbose``, ``--debug`` were given (REQ-O-008)"""
    warnings_as_errors: bool = False
    """``--warnings-as-errors``: a warning fails an otherwise successful run (REQ-O-025)"""
    fields: tuple[str, ...] | None = None
    """``--fields``: the top-level keys of ``data`` to keep (REQ-O-002)"""
    stream: bool = False
    """``--stream``: JSONL events; a warning on a command that cannot stream (REQ-O-004)"""
    token_limit: int | None = None
    token_offset: int | None = None
    token_count: bool = False
    tokenizer: str | None = None
    """The token budget flags (REQ-O-049)"""


def without_value(token: str) -> str:
    """``--name=value`` as ``--name``: an unrecognized token may carry a secret after ``=``"""
    return token.partition("=")[0] if token.startswith("--") else token


_NEGATIVE_NUMBER = re.compile(r"-([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?")


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


VALUED_GLOBALS = frozenset(
    {
        "format",
        "max-output",
        "schema-version",
        "config",
        "context",
        "instance-id",
        "cwd",
        FIELDS_FLAG,
        TOKEN_LIMIT_FLAG,
        TOKEN_OFFSET_FLAG,
        TOKENIZER_FLAG,
    }
)
SWITCH_GLOBALS = frozenset(
    {
        "output-schema",
        STABLE_OUTPUT_FLAG,
        "no-config",
        "show-config",
        UNMASK_FLAG,
        NO_INJECTION_FLAG,
        "no-update-check",
        QUIET_FLAG,
        VERBOSE_FLAG,
        DEBUG_FLAG,
        WARNINGS_AS_ERRORS_FLAG,
        STREAM_FLAG,
        TOKEN_COUNT_FLAG,
    }
)
FORMAT_GUESSES = frozenset({"--output", "--output-format", "--json"})


def format_hint(token: str) -> str | None:
    """Point agents that guess another output-representation flag at ``--format``"""
    if without_value(token) in FORMAT_GUESSES:
        return "use --format json (or --format plain) to choose the output representation"
    return None


def split_globals(
    argv: list[str], *, short_verbose: bool = True
) -> tuple[GlobalOptions, list[str]]:
    """Global options in any position; a valued one repeated with a different value is
    an error rather than last-wins (REQ-F-067, REQ-F-079). With ``short_verbose``, ``-v``
    is ``--verbose`` and ``-vv`` or ``-v -v`` is ``--debug``; without it, when the command
    declares its own ``-v``, those tokens stay for the command"""
    valued: dict[str, str] = {}
    switches: set[str] = set()
    vs = 0
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
        elif tok in ("--schema", "--print-schema"):
            schema = True  # REQ-O-013: --print-schema is an alias
        elif not eq and name in SWITCH_GLOBALS:
            switches.add(name)
        elif short_verbose and short_verbosity(tok):
            vs += short_verbosity(tok)
        elif name in RESERVED_GLOBAL and name in UNIMPLEMENTED:
            raise reserved_flag(name)
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
    if vs:
        switches.add(VERBOSE_FLAG if vs == 1 else DEBUG_FLAG)
    fields = valued.get(FIELDS_FLAG)
    limit, offset = valued.get(TOKEN_LIMIT_FLAG), valued.get(TOKEN_OFFSET_FLAG)
    return (
        GlobalOptions(
            format=valued.get("format"),
            help=help_,
            schema=schema,
            max_output=valued.get("max-output"),
            output_schema="output-schema" in switches,
            schema_version=valued.get("schema-version"),
            stable_output=STABLE_OUTPUT_FLAG in switches,
            config=valued.get("config"),
            context=valued.get("context"),
            no_config="no-config" in switches,
            show_config="show-config" in switches,
            instance_id=valued.get("instance-id"),
            unmask=UNMASK_FLAG in switches,
            no_injection_protection=NO_INJECTION_FLAG in switches,
            cwd=valued.get("cwd"),
            no_update_check="no-update-check" in switches,
            verbosity=frozenset(switches & {QUIET_FLAG, VERBOSE_FLAG, DEBUG_FLAG}),
            warnings_as_errors=WARNINGS_AS_ERRORS_FLAG in switches,
            fields=None if fields is None else parse_fields(fields),
            stream=STREAM_FLAG in switches,
            token_limit=None if limit is None else token_number(limit, TOKEN_LIMIT_FLAG),
            token_offset=None if offset is None else token_number(offset, TOKEN_OFFSET_FLAG),
            token_count=TOKEN_COUNT_FLAG in switches,
            tokenizer=valued.get(TOKENIZER_FLAG),
        ),
        rest,
    )


def _takes_value(command: Command, tok: str) -> bool:
    """Whether the option ``tok`` consumes the next token as its value"""
    if tok.startswith("--"):
        name, eq, _ = tok[2:].partition("=")
        if eq or name in SWITCH_GLOBALS:
            return False
        if name in VALUED_GLOBALS:
            return True
        spec = flag_named(command, name)
        if spec is not None:
            return not spec.switch
        found = command.field_by_flag(name)
        if found is None and split_source_flag(name) is not None:
            return True
        return found is not None and found.flag_type is not FlagType.BOOLEAN
    found = command.field_by_short(tok[1:]) if len(tok) == 2 else None
    return found is not None and found.flag_type is not FlagType.BOOLEAN


def _global(tok: str) -> bool:
    """Whether ``split_globals`` takes ``tok`` as a global option"""
    if tok in ("-h", "--help", "--schema", "--print-schema") or short_verbosity(tok):
        return True
    name = tok[2:].partition("=")[0] if tok.startswith("--") else ""
    return name in SWITCH_GLOBALS or name in VALUED_GLOBALS


def _command_at(
    argv: list[str], known: Mapping[CommandPath, Command]
) -> tuple[Command | None, int]:
    """The command argv names, globals skipped, and the index after its path"""
    prefixes = {p.parts[:n] for p in known for n in range(1, len(p.parts) + 1)}
    consumed: tuple[str, ...] = ()
    i = 0
    while i < len(argv) and argv[i] != "--":
        tok = argv[i]
        if tok.startswith("-") and tok != "-":
            name, eq, _ = tok[2:].partition("=")
            if (
                tok in ("-h", "--help", "--schema", "--print-schema")
                or name in SWITCH_GLOBALS
                or short_verbosity(tok)
            ):
                i += 1
            elif tok.startswith("--") and name in VALUED_GLOBALS:
                i += 1 if eq else 2
            else:
                break  # a command's own option: the path ended before it
            continue
        if (*consumed, tok) not in prefixes:
            break
        consumed = (*consumed, tok)
        i += 1
    return (known.get(CommandPath(".".join(consumed))) if consumed else None), i


def bind_values(argv: list[str], known: Mapping[CommandPath, Command]) -> list[str]:
    """``--flag -h`` as ``--flag=-h`` when the command's own ``--flag`` takes a value, so
    ``split_globals`` reads the value as the flag's, not as ``--help`` (REQ-F-079); a short
    ``-s -v`` as ``--say=-v``, since a short flag takes no ``=``"""
    command, i = _command_at(argv, known)
    if command is None:
        return argv
    bound = list(argv)
    while i < len(bound) and bound[i] != "--":
        tok = bound[i]
        if not tok.startswith("-") or _global(tok) or not _takes_value(command, tok):
            i += 1
            continue
        if i + 1 < len(bound) and _global(bound[i + 1]):
            short = None if tok.startswith("--") else command.field_by_short(tok[1:])
            long = tok if short is None else f"--{short.flag}"
            bound[i : i + 2] = [f"{long}={bound[i + 1]}"]
            i += 1
        else:
            i += 2
    return bound


def strict_argv(argv: list[str], known: Mapping[CommandPath, Command]) -> list[str]:
    """REQ-C-027: for a ``strict`` command, a ``--`` before its first positional, so
    every later token reaches its positionals verbatim, a global's name included; any
    other argv comes back unchanged"""
    command, i = _command_at(argv, known)
    if command is None or command.option_placement is not OptionPlacement.STRICT:
        return argv
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv
        if not tok.startswith("-") or tok == "-" or _is_negative(tok, command):
            return [*argv[:i], "--", *argv[i:]]
        i += 2 if _takes_value(command, tok) else 1
    return argv


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
        """One error, or each of several an object's checks collected"""
        self.errors.extend(exc.errors or (exc,))

    def fail(self) -> NoReturn:
        raise ParseError.combine(self.errors)

    def finish(self) -> None:
        if self.errors:
            self.fail()


def stdin_values(field: FieldInfo, text: str) -> list[str]:
    """What ``-`` stands for (REQ-O-006): one value less its trailing newline, or one
    array item per non-empty line"""
    if not text.strip():
        raise ParseError(
            f"expected {field.flag} from stdin but stdin was empty",
            code="EMPTY_STDIN",
            context={"flag": field.flag},
            suggestion=f"pipe the value into stdin, or pass --{field.flag} <value>",
        )
    if field.flag_type is FlagType.ARRAY:
        return [line.removesuffix("\r") for line in text.split("\n") if line.strip()]
    value = text.removesuffix("\n").removesuffix("\r")
    lines = value.count("\n") + 1
    if lines > 1 and not field.spec.multiline:
        raise ParseError(
            f"stdin holds {lines} lines, but {field.flag!r} takes one value",
            context={"flag": field.flag, "lines": lines},
            suggestion="pipe a single line, such as with --format id | head -n 1",
        )
    return [value]


StdinReader = Callable[[str], str]
"""Reads the whole of stdin for the flag named; raises ``ParseError`` when it cannot"""


def parse_command_args(
    command: Command,
    tokens: tuple[str, ...],
    env: Mapping[str, str],
    *,
    read_stdin: StdinReader | None = None,
) -> Invocation:
    values: dict[str, object] = {}
    secrets: dict[str, SecretRef] = {}
    arrays: dict[str, list[object]] = {}
    framework: dict[str, Any] = {}
    """``Invocation`` fields set by framework flags, plus ``raw_payload``"""
    positionals = [f for f in command.fields if f.positional]
    counts: dict[str, int] = {}
    """How many values each field was given, so an object's errors name its index"""
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
        if raw == "-" and field.spec.from_stdin:
            try:
                if read_stdin is None:
                    raise ParseError(
                        f"'-' for {field.flag!r} reads stdin, which carries something else here",
                        context={"flag": field.flag},
                    )
                for value in stdin_values(field, read_stdin(field.flag)):
                    store(field, parse_token(field, value))
            except ParseError as exc:
                errors.add(exc)
            return
        try:
            parsed = parse_token(field, raw)
        except ParseError as exc:
            errors.add(exc)
            return
        store(field, (not parsed) if negated else parsed)

    def parse_token(field: FieldInfo, raw: str) -> object:
        """One argv or stdin value: an object field's is a JSON object"""
        index = counts.get(field.name, 0)
        counts[field.name] = index + 1
        target = field.object_type
        if target is None:
            return field.parse(raw)
        where = f"{field.flag}[{index}]" if field.flag_type is FlagType.ARRAY else field.flag
        return check_object(target, _decode_object(raw, field.flag, where), where)

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
                spec = flag_named(command, name)
                if spec is not None:
                    if spec.parse is None:
                        if has_eq:
                            raise ParseError(f"'{name}' takes no value", context={"flag": name})
                        value: object = True
                    else:
                        value = spec.parse(value_after(tok, name, has_eq, inline), command)
                    if framework.setdefault(spec.attr, value) != value:
                        raise _repeated(name)
                    i += 1
                    continue
                if name in UNIMPLEMENTED and name in reserved_names(command):
                    errors.add(reserved_flag(name))
                    if not has_eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                        i += 1  # its value, rather than misread it as a positional
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
    raw_payload = framework.pop("raw_payload", None)
    if raw_payload is not None:
        if values or secrets:
            raise ParseError(
                "Cannot combine --raw-payload with individual flags",
                context={"flag": RAW_PAYLOAD_FLAG, "also_given": sorted(values | secrets)},
            )
        errors.finish()
        mapping = _decode_raw_payload(raw_payload)
        built = build_from_mapping(command, mapping, env)
        # Framework keys may come from argv or the payload; both only if they agree, except
        # --limit and --cursor, which win so a truncation hint appended to argv runs
        in_payload = {k.replace("_", "-") for k in mapping}
        for spec in framework_flags(command):
            given = framework.get(spec.attr)
            if spec.name in in_payload and not spec.argv_wins and given is not None:
                if getattr(built, spec.attr) != given:
                    raise _repeated(spec.name)
        return replace(built, **framework)
    _apply_secrets(command, values, secrets, env, errors)
    given = frozenset(values)
    return Invocation(args=_finish(command, values, errors), given=given, **framework)


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
    """Strict JSON, or the JSON5 forms agents write (REQ-F-059)"""
    try:
        decoded = loads_forgiving(raw)
    except Unreadable as exc:
        raise invalid_json("--raw-payload", exc, {"flag": RAW_PAYLOAD_FLAG}) from None
    except ValueError as exc:
        raise ParseError(
            "--raw-payload is not valid JSON", context={"flag": RAW_PAYLOAD_FLAG, "cause": str(exc)}
        ) from None
    if not isinstance(decoded, dict):
        raise ParseError("--raw-payload must be a JSON object", context={"flag": RAW_PAYLOAD_FLAG})
    return decoded


def _decode_object(raw: str, flag: str, where: str) -> object:
    """An object field's argv value: strict JSON, or the JSON5 forms agents write; one
    holds no secret, since registration refuses one in an object"""
    what = f"--{flag}" if where == flag else f"--{flag} ({where})"
    try:
        return loads_forgiving(raw)
    except Unreadable as exc:
        raise invalid_json(what, exc, {"flag": where}) from None
    except ValueError as exc:
        raise ParseError(
            f"{what} is not valid JSON", context={"flag": where, "cause": str(exc)}
        ) from None


def known_flags(command: Command, *, argv: bool = True) -> list[str]:
    """The flags a command accepts; ``argv=False`` leaves out those only argv takes, for
    the unknown-field error of ``exec``, MCP, and ``--raw-payload``"""
    flags = [name for f in command.fields for name in f.exposed_flags()]
    flags += [f.name for f in framework_flags(command, json=not argv)]
    return flags if argv else [*flags, STABLE_OUTPUT_FLAG, FIELDS_FLAG]


def _finish(command: Command, values: dict[str, object], errors: _Collector) -> object:
    """Report missing fields alongside everything collected, then build the dataclass

    The args ``__post_init__`` is the cross-field check of phase 1 (REQ-F-015): it runs
    whenever every field has a value, so its ``ParseError`` (or several, through
    ``ParseError.combine``) joins the errors of an unknown flag in the same run.
    """
    for f in command.fields:
        if f.name in command.shell_checked and f.name in values:
            refused = shell_safe(f, values[f.name])  # REQ-C-019, REQ-F-044
            if refused is not None:
                errors.add(refused)
    objects = {f.flag for f in command.fields if f.object_type is not None}
    failed = {_field_of(e.field, objects) for e in errors.errors}
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
    # REQ-C-026: on what the caller supplied, before defaults fill the rest
    broken = check_rules(command.requires, values, {f for f in failed if f is not None})
    for exc in broken:
        errors.add(exc)
    named = {n for f in command.fields for n in (f.name, f.flag, *f.exposed_flags())}
    if missing or broken or not failed.isdisjoint(named):
        errors.fail()  # a field without its value would give __post_init__ a false default
    for f in command.fields:
        if f.name not in values:
            values[f.name] = None if f.default is MISSING else f.default
    try:
        args = built_args(command, lambda: command.args_type(**values), values)
    except ParseError as exc:
        errors.errors.extend(exc.errors or (exc,))
        errors.fail()
    errors.finish()
    return args


def _field_of(location: str | None, objects: Collection[str]) -> str | None:
    """The flag an error's location is in: ``postings`` for ``postings[1].number``"""
    if location is None:
        return None
    head = re.split(r"[.\[]", location, maxsplit=1)[0]
    return head if head in objects else location


def built_args(
    command: Command, build: Callable[[], object], values: Mapping[str, object]
) -> object:
    """The args ``build`` returns, running their ``__post_init__``: a ``ParseError`` or
    ``InvalidValue`` it raises is phase 1, exit 2; anything else is ``ArgsCrashed``, a
    bug in user code (exit 1). Parsing and every later rebuild of the args, such as a
    forced dry run's, go through here (#161); ``values`` are the field values, for the
    crash report's redaction."""
    try:
        return build()
    except ParseError:
        raise
    except InvalidValue as exc:  # a value object built in __post_init__ refused its input
        raise ParseError(str(exc), context={"command": command.path.value}) from None
    except Exception as exc:  # noqa: BLE001 - __post_init__ is user code
        raise ArgsCrashed(exc, values) from exc


def build_from_mapping(
    command: Command, mapping: Mapping[str, object], env: Mapping[str, str]
) -> Invocation:
    """Build an invocation from already-typed JSON values, as ``exec`` receives them"""
    values: dict[str, object] = {}
    secrets: dict[str, SecretRef] = {}
    framework: dict[str, Any] = {}
    errors = _Collector()
    for key, value in mapping.items():
        try:
            if key == SCHEMA_VERSION_KEY:
                # The global --schema-version, which argv takes anywhere (REQ-O-014)
                framework["schema_version"] = command.pin(value)
                continue
            if key == STABLE_OUTPUT_KEY:
                framework["stable_output"] = switch_value(value, key)
                continue
            if key == FIELDS_KEY:
                framework["fields"] = parse_fields(value)
                continue
            flag = key.replace("_", "-")
            spec = flag_named(command, flag, json=True)
            if spec is not None and spec.from_json is not None:
                framework[spec.attr] = spec.from_json(value, command)
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
                        "known": known_flags(command, argv=False),
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
    given = frozenset(values)
    return Invocation(args=_finish(command, values, errors), given=given, **framework)


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
        if item.flag_type is FlagType.OBJECT:
            return _all_items(item, value, field.flag)
        return tuple(_check_patterned(field, item, v) for v in value)
    if field.flag_type is FlagType.OBJECT:
        return check_object(field.classified, value, field.flag)
    return _check_patterned(field, field.classified, value)


def _all_items(item: Classified, values: list[object], where: str) -> tuple[object, ...]:
    """Every object of an array, each item's errors collected under its index"""
    out: list[object] = []
    errors: list[ParseError] = []
    for index, value in enumerate(values):
        try:
            out.append(check_object(item, value, f"{where}[{index}]"))
        except ParseError as exc:
            errors.extend(exc.errors or (exc,))
    if errors:
        raise ParseError.combine(errors)
    return tuple(out)


def check_object(target: Classified, value: object, where: str) -> object:
    """A JSON object as the frozen dataclass of ``target``, checked field by field as
    argument values are: an unknown key, a missing one, and every field's own error are
    collected, each at its location, such as ``postings[1].number``"""
    if not isinstance(value, dict):
        raise ParseError(
            f"{where!r} expects a JSON object",
            context={"field": where, "type": _json_type(value)},
        )
    members = {m.name: m for m in target.members}
    kwargs: dict[str, object] = {}
    errors: list[ParseError] = []
    for key, given in value.items():
        member = members.get(key)
        at = f"{where}.{key}"
        if member is None:
            errors.append(
                ParseError(
                    f"unknown field {at!r}",
                    context={"field": at, "known": list(members)},
                )
            )
            continue
        try:
            kwargs[key] = _check_member(replace(member, flag=at), given)
        except ParseError as exc:
            errors.extend(exc.errors or (exc,))
    for member in target.members:
        if member.name in value:
            continue
        if member.required:
            at = f"{where}.{member.name}"
            errors.append(ParseError(f"missing required: {at}", context={"field": at}))
        elif member.default is not MISSING:
            kwargs[member.name] = member.default  # else its default_factory fills it
    if errors:
        raise ParseError.combine(errors)
    try:
        return target.object_cls(**kwargs)
    except ParseError as exc:
        raise ParseError.combine([_located(e, where) for e in exc.errors or (exc,)]) from None
    except InvalidValue as exc:  # a value object built in __post_init__ refused its input
        raise ParseError(str(exc), context={"field": where}) from None
    except Exception as exc:  # noqa: BLE001 - __post_init__ is user code
        raise ArgsCrashed(exc, kwargs) from exc


def _located(exc: ParseError, where: str) -> ParseError:
    """An object's own ``__post_init__`` error, at the object unless it names a field"""
    if exc.field is None:
        exc.context["field"] = where
    elif exc.field != where and not exc.field.startswith((f"{where}.", f"{where}[")):
        exc.context["field"] = f"{where}.{exc.field}"
    return exc


def _json_type(value: object) -> str:
    """What a JSON value is, for an error that does not echo it"""
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case _:
            return type(value).__name__


def _check_member(member: FieldInfo, value: object) -> object:
    """One field of an object, ``member.flag`` its location"""
    if value is None and member.classified.optional:
        return None
    if member.flag_type is FlagType.ARRAY and isinstance(value, list):
        item = member.classified.item
        assert item is not None, "array fields always carry an item type"
        if item.flag_type is FlagType.OBJECT:
            return _all_items(item, value, member.flag)
        return tuple(
            _check_patterned(replace(member, flag=f"{member.flag}[{i}]"), item, v)
            for i, v in enumerate(value)
        )
    return _check_field_value(member, value)


def _check_patterned(field: FieldInfo, target: Classified, value: object) -> object:
    if isinstance(value, str):
        if target.flag_type is FlagType.STRING:
            field.check_text(value)
        field.check_pattern(value)
    base = check_json_base(target, value, field.flag)
    if isinstance(base, str) and not isinstance(value, str):
        # A Decimal sent as a number is checked as the text it becomes, as a string is
        field.check_text(base)
    if isinstance(base, (int, float)) and not isinstance(base, bool):
        try:
            token = str(base)  # the number's own text, as argv checks it: 2.0 is 2 on both
        except ValueError:
            raise ParseError(
                f"{field.flag!r} is too large",
                context={"field": field.flag, "value": describe_number(base)},
            ) from None
        field.check_pattern(token)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, field.flag, secret=field.secret)


def check_json_base(target: Classified, value: object, flag: str) -> object:
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
            if target.scalar is DECIMAL:
                return _decimal_text(value, flag)
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
        case FlagType.OBJECT:
            return check_object(target, value, flag)


def _decimal_text(value: object, flag: str) -> str:
    """A ``Decimal`` field's value other than a string, as the text the built-in parser
    checks: an integer is exact, a number read from JSON keeps its source text, and a
    Python caller's ``Decimal`` is its own. A float from elsewhere, such as an MCP client's
    or a TOML file's, may already have lost digits, so it is refused"""
    if isinstance(value, JsonFloat):
        return value.text
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    raise ParseError(
        f"{flag!r} expects a decimal as a string",
        context={"field": flag, "value": value},
        suggestion=f'pass it quoted, such as "12.30": a {type(value).__name__} may have '
        "lost digits",
    )
