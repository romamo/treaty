"""Command records built from a handler function and its metadata."""

from __future__ import annotations

import collections.abc
import inspect
import shlex
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._effect import can_carry_effect, with_replay_effect
from ._errors import RegistrationError
from ._flags import FieldInfo, inspect_fields
from ._resources import ResourceSpec, dependency_params, resource_graph
from ._scalars import ScalarRegistry
from ._schema import JsonSchema, is_payload_type, schema_for
from ._secrets import default_env_var
from ._timeout import Timeout
from ._types import FlagType, is_dataclass_type
from ._values import CommandPath, ExitCodeName, Scope

Handler = Callable[..., Any]
"""``(args, ctx, *resources)``: extra parameters are annotated with resource classes"""
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

    def __post_init__(self) -> None:
        try:
            shlex.split(self.command)
        except ValueError as exc:
            raise RegistrationError(
                f"example {self.command!r} is not a valid shell command: {exc}"
            ) from None

    def to_json(self) -> dict[str, str]:
        return {"description": self.description, "command": self.command}


@dataclass(frozen=True, slots=True)
class Command:
    path: CommandPath
    handler: Handler
    args_type: type
    output_type: object
    output_schema: JsonSchema
    args_schema: JsonSchema
    """JSON Schema of the args dataclass; served as ``raw_payload_schema``"""
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
    secret_env_vars: Mapping[str, str]
    """Field name to the default ``<APP>_<FIELD>`` variable, for secret fields only"""
    streaming: bool
    """The handler is a generator; every yield is one envelope line (REQ-O-004)"""
    resources: tuple[type, ...]
    """Resource classes the handler takes after ``ctx``, in parameter order"""
    resource_graph: Mapping[type, ResourceSpec]
    """Every resource reachable from ``resources``, validated at registration"""

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
GLOBAL_SHORTS = {"h": "help"}


def build_command(
    fn: Handler,
    *,
    app_name: str,
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
    scalars: ScalarRegistry,
    streaming: bool = False,
) -> Command:
    if not description:
        raise RegistrationError(f"{path}: description is required")
    args_type, output_type, resources = _inspect_handler(fn, path, streaming)
    fields = inspect_fields(args_type, scalars)
    if streaming and danger_level is not DangerLevel.SAFE:
        raise RegistrationError(
            f"{path}: streaming commands must be safe; the effect and idempotency contracts "
            "describe one response, not a stream"
        )
    shadowed: list[str] = []
    for f in fields:
        # A positional is also accepted as --<name>, so it may not take a global's name
        if f.flag in GLOBAL_FLAGS:
            shadowed.append(f"--{f.flag} collides with --{f.flag}")
        elif f.spec.short is not None and f.spec.short in GLOBAL_SHORTS:
            owner = GLOBAL_SHORTS[f.spec.short]
            shadowed.append(
                f"-{f.spec.short} of --{f.flag} collides with -{f.spec.short} of --{owner}"
            )
    if shadowed:
        raise RegistrationError(
            f"{path}: {'; '.join(shadowed)}: global options, which would never reach "
            "the handler (REQ-F-079)"
        )
    for f in fields:
        f.to_flag_entries()  # a default the manifest cannot list fails now, not on --help
    framework_flags = {
        "timeout": has_network_io,
        "raw-payload": supports_raw_payload,
        "confirm-destructive": danger_level is DangerLevel.DESTRUCTIVE,
        "no-stream": streaming,
    }
    taken = sorted(
        f.flag
        for f in fields
        if framework_flags.get(f.flag, False)
        # --no-<name> negates a boolean, so a boolean 'stream' would lose --no-stream
        or (f.flag_type is FlagType.BOOLEAN and framework_flags.get(f"no-{f.flag}", False))
    )
    if taken:
        raise RegistrationError(
            f"{path}: flags {taken} are supplied by the framework for this command and "
            "would never reach the handler; rename the fields"
        )
    if danger_level is not DangerLevel.SAFE:
        if not can_carry_effect(output_type):
            raise RegistrationError(
                f"{path}: {danger_level.value} commands must return an object with an "
                "'effect' field (REQ-C-003)"
            )
        if any(f.name == "idempotency_key" for f in fields):
            raise RegistrationError(
                f"{path}: --idempotency-key is supplied by the framework for "
                f"{danger_level.value} commands; read ctx.idempotency_key instead"
            )
    if danger_level is DangerLevel.DESTRUCTIVE:
        dry_run = next((f for f in fields if f.name == "dry_run"), None)
        if dry_run is None or dry_run.flag_type is not FlagType.BOOLEAN:
            raise RegistrationError(
                f"{path}: destructive commands must declare a boolean 'dry_run' flag (REQ-C-004)"
            )
    if len(set(exit_codes)) != len(exit_codes):
        raise RegistrationError(f"{path}: duplicate exit code names")
    output_schema = schema_for(output_type, scalars)
    if danger_level is not DangerLevel.SAFE:
        output_schema = with_replay_effect(output_schema)
    return Command(
        path=path,
        handler=fn,
        args_type=args_type,
        output_type=output_type,
        output_schema=output_schema,
        args_schema=schema_for(args_type, scalars),
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
        secret_env_vars={f.name: default_env_var(app_name, f.name) for f in fields if f.secret},
        streaming=streaming,
        resources=resources,
        resource_graph=resource_graph(resources, str(path)),
    )


def _inspect_handler(
    fn: Handler, path: CommandPath, streaming: bool
) -> tuple[type, object, tuple[type, ...]]:
    resources = dependency_params(fn, f"{path}: handler")
    params = list(inspect.signature(fn).parameters.values())
    hints = typing.get_type_hints(fn)
    args_type = hints.get(params[0].name)
    if not is_dataclass_type(args_type):
        raise RegistrationError(f"{path}: first parameter must be annotated with an args dataclass")
    if "return" not in hints:
        raise RegistrationError(f"{path}: handler needs a return annotation for output_schema")
    output_type = hints["return"]
    if streaming:
        output_type = _event_type(output_type, path)
    if not is_payload_type(output_type):
        what = "each yielded event" if streaming else "return type"
        raise RegistrationError(f"{path}: {what} must serialize to a JSON object, array, or null")
    assert isinstance(args_type, type)
    return args_type, output_type, resources


_STREAM_ORIGINS = (
    collections.abc.Iterator,
    collections.abc.Iterable,
    collections.abc.Generator,
)


def _event_type(annotation: object, path: CommandPath) -> object:
    """The ``T`` of a streaming handler's ``Iterator[T]`` (or ``Iterable`` / ``Generator``)"""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin not in _STREAM_ORIGINS or not args:
        raise RegistrationError(
            f"{path}: a streaming handler must be annotated Iterator[T] for its event type"
        )
    return args[0]
