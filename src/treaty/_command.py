"""Command records built from a handler function and its metadata."""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._context import Ctx
from ._errors import RegistrationError
from ._flags import FieldInfo, inspect_fields
from ._schema import JsonSchema, is_payload_type, schema_for
from ._timeout import Timeout
from ._types import FlagType, is_dataclass_type
from ._values import CommandPath, ExitCodeName, Scope

Handler = Callable[[Any, Ctx], Any]
Cleanup = Callable[[], None]
HumanRenderer = Callable[[Any], str]


class DangerLevel(StrEnum):
    SAFE = "safe"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class Example:
    description: str
    command: str

    def to_json(self) -> dict[str, str]:
        return {"description": self.description, "command": self.command}


@dataclass(frozen=True, slots=True)
class Command:
    path: CommandPath
    handler: Handler
    args_type: type
    output_type: object
    output_schema: JsonSchema
    fields: tuple[FieldInfo, ...]
    description: str
    danger_level: DangerLevel
    required_scopes: tuple[Scope, ...]
    exit_codes: tuple[ExitCodeName, ...]
    examples: tuple[Example, ...]
    has_network_io: bool
    timeout: Timeout | None
    supports_raw_payload: bool
    cleanup: Cleanup | None
    human: HumanRenderer | None

    def field_by_flag(self, flag: str) -> FieldInfo | None:
        for f in self.fields:
            if f.flag == flag:
                return f
        return None

    def field_by_short(self, short: str) -> FieldInfo | None:
        for f in self.fields:
            if f.spec.short == short:
                return f
        return None


# Consumed by split_globals before any command sees its tokens
GLOBAL_FLAGS = frozenset({"format", "help", "max-output", "schema"})


def build_command(
    fn: Handler,
    *,
    path: CommandPath,
    description: str,
    danger_level: DangerLevel,
    required_scopes: Sequence[Scope],
    exit_codes: Sequence[ExitCodeName],
    examples: Sequence[Example],
    has_network_io: bool,
    timeout: Timeout | None,
    supports_raw_payload: bool,
    cleanup: Cleanup | None,
    human: HumanRenderer | None,
) -> Command:
    if not description:
        raise RegistrationError(f"{path}: description is required")
    args_type, output_type = _inspect_handler(fn, path)
    fields = inspect_fields(args_type)
    shadowed = sorted(f.flag for f in fields if not f.positional and f.flag in GLOBAL_FLAGS)
    if shadowed:
        raise RegistrationError(
            f"{path}: flags {shadowed} are global options and would never reach the handler"
        )
    if danger_level is DangerLevel.DESTRUCTIVE:
        dry_run = next((f for f in fields if f.name == "dry_run"), None)
        if dry_run is None or dry_run.flag_type is not FlagType.BOOLEAN:
            raise RegistrationError(
                f"{path}: destructive commands must declare a boolean 'dry_run' flag (REQ-C-004)"
            )
    if len(set(exit_codes)) != len(exit_codes):
        raise RegistrationError(f"{path}: duplicate exit code names")
    return Command(
        path=path,
        handler=fn,
        args_type=args_type,
        output_type=output_type,
        output_schema=schema_for(output_type),
        fields=fields,
        description=description,
        danger_level=danger_level,
        required_scopes=tuple(required_scopes),
        exit_codes=tuple(exit_codes),
        examples=tuple(examples),
        has_network_io=has_network_io,
        timeout=timeout,
        supports_raw_payload=supports_raw_payload,
        cleanup=cleanup,
        human=human,
    )


def _inspect_handler(fn: Handler, path: CommandPath) -> tuple[type, object]:
    params = list(inspect.signature(fn).parameters.values())
    if len(params) != 2 or any(
        p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params
    ):
        raise RegistrationError(f"{path}: handler must take exactly (args, ctx)")
    hints = typing.get_type_hints(fn)
    args_type = hints.get(params[0].name)
    if not is_dataclass_type(args_type):
        raise RegistrationError(f"{path}: first parameter must be annotated with an args dataclass")
    if hints.get(params[1].name) is not Ctx:
        raise RegistrationError(f"{path}: second parameter must be annotated with Ctx")
    if "return" not in hints:
        raise RegistrationError(f"{path}: handler needs a return annotation for output_schema")
    output_type = hints["return"]
    if not is_payload_type(output_type):
        raise RegistrationError(
            f"{path}: return type must serialize to a JSON object, array, or null"
        )
    assert isinstance(args_type, type)
    return args_type, output_type
