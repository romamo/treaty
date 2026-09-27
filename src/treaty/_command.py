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

from ._auth import AuthKind, check_declaration
from ._config import ConfigScope
from ._effect import can_carry, with_replay_effect
from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo, inspect_fields
from ._jobs import Job, descriptor_schema
from ._mode import Format
from ._out import NO_ORDER, OutSpec, check_order
from ._page import DEFAULT_LIMIT, Limit, Page
from ._resources import ResourceSpec, dependency_params, resource_graph
from ._retry import Retry
from ._scalars import ScalarRegistry
from ._scan import ctx_calls
from ._schema import JsonSchema, is_payload_type, schema_for
from ._secrets import default_env_var
from ._subprocess import BROWSER_OPEN
from ._timeout import Timeout
from ._types import FlagType, is_dataclass_type, resolve_alias
from ._values import CommandPath, ExitCodeName, InvalidValue, SchemaVersion, Scope

Handler = Callable[..., Any]
"""``(args, ctx, *resources)``: extra parameters are annotated with resource classes"""
Cleanup = Callable[[], None]
Renderer = Callable[[Any], str]
"""Text for one result, or one stream event, from its JSON-ready ``data``"""
Shim = Callable[[Any], Any]
"""The command's output in the shape of an older schema version (REQ-O-014)"""
DEFAULT_SCHEMA_VERSION = SchemaVersion("1.0")


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
class Compat:
    """One older major of a command's output that ``--schema-version`` can still select"""

    version: SchemaVersion
    shim: Shim
    output_type: object
    output_schema: JsonSchema


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
    renderers: Mapping[Format, Renderer]
    """Per-format overrides of the app's renderers"""
    secret_env_vars: Mapping[str, str]
    """Field name to the default ``<APP>_<FIELD>`` variable, for secret fields only"""
    streaming: bool
    """The handler is a generator; every yield is one envelope line (REQ-O-004)"""
    resources: tuple[type, ...]
    """Resource classes the handler takes after ``ctx``, in parameter order"""
    resource_graph: Mapping[type, ResourceSpec]
    """Every resource reachable from ``resources``, validated at registration"""
    safe_default: bool = False
    """A destructive command that runs as a dry run unless ``--live`` (REQ-O-048)"""
    gui_operations: tuple[str, ...] = ()
    """Display operations the handler may start; only ``browser_open`` (REQ-C-024)"""
    interactive: bool = False
    """The handler may ask through ``ctx.prompt`` and ``ctx.confirm`` (REQ-C-005)"""
    editor_alternatives: tuple[str, ...] = ()
    """Flags that replace ``ctx.edit``; non-empty means the command may open an editor"""
    paginated: bool = False
    """A list command: ``--limit``, ``--cursor``, and ``meta.pagination`` (REQ-F-018)"""
    default_limit: Limit = Limit(DEFAULT_LIMIT)
    """Items per page without ``--limit`` (REQ-F-019)"""
    cursor_check: Callable[[str], None] | None = None
    """Validates the handler's own cursor from a ``--cursor`` token; raises ``ParseError``"""
    heartbeat: bool = False
    """JSON runs write heartbeat lines to stdout while the handler runs (REQ-F-053)"""
    stdin_input: bool = False
    """The handler reads a payload, ``ctx.stdin_text``, from stdin or ``--input-file``"""
    output_file: bool = False
    """``--output PATH`` writes the rendered ``data`` to a file (REQ-O-001)"""
    requires_auth: bool = False
    """The app's credentials must hold ``required_scopes`` before the handler runs"""
    auth: AuthKind | None = None
    """A login command (REQ-C-021): ``--headless``, ``--token-env-var``, ``ctx.token``"""
    token_env_vars: tuple[str, ...] = ()
    """Where a login command looks for a pre-acquired token, in order; ``<APP>_TOKEN`` first"""
    async_job: bool = False
    """Returns a ``treaty.Job`` for work that goes on after the process exits (REQ-C-022)"""
    config_write_scope: ConfigScope | None = None
    """The config file ``ctx.write_config`` may change (REQ-C-025)"""
    schema_version: SchemaVersion = DEFAULT_SCHEMA_VERSION
    """``MAJOR.MINOR`` of the output contract, in ``meta.schema_version`` (REQ-F-022)"""
    compat: tuple[Compat, ...] = ()
    """Older majors still served, oldest first (REQ-O-014)"""
    project_root: tuple[str, ...] = ()
    """Marker files whose directory, found walking up from the cwd, is the project root"""
    retry: Retry | None = None
    """How ``ctx.retry`` retries (REQ-F-078); adds ``--retries`` and ``--retry-delay``"""
    order: OutSpec = NO_ORDER
    """``sort_key=`` and ``ordered=`` of a command whose output is an array (REQ-F-020)"""

    @property
    def min_schema_version(self) -> SchemaVersion:
        """The oldest version ``--schema-version`` can select"""
        return self.compat[0].version if self.compat else self.schema_version

    def compat_for(self, version: SchemaVersion) -> Compat:
        return next(c for c in self.compat if c.version == version)

    def pin(self, raw: object) -> SchemaVersion | None:
        """``--schema-version MAJOR``: the older version it selects, None for the current;
        a major the command does not serve is ``SCHEMA_VERSION_UNSUPPORTED`` (exit 2)"""
        text = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
        major_text = text.partition(".")[0] if isinstance(text, str) else ""
        context: dict[str, object] = {
            "flag": "schema-version",
            "schema_version": self.schema_version.value,
            "min_schema_version": self.min_schema_version.value,
        }
        if not (major_text.isascii() and major_text.isdigit() and len(major_text) <= 6):
            raise ParseError("--schema-version takes a major version, such as 1", context=context)
        major = int(major_text)
        if major == self.schema_version.major:
            return None
        served = next((c.version for c in self.compat if c.version.major == major), None)
        if served is None:
            majors = [str(c.version.major) for c in self.compat] + [str(self.schema_version.major)]
            raise ParseError(
                f"Command {self.path} does not serve schema version {major}",
                code="SCHEMA_VERSION_UNSUPPORTED",
                context={**context, "requested_version": text},
                suggestion=f"pass --schema-version {' or '.join(majors)}, or drop it for "
                f"the current {self.schema_version}",
            )
        return served

    @property
    def accepts_timeout(self) -> bool:
        """``--timeout``: network commands, and streams, which may never end on their own"""
        return self.has_network_io or self.streaming

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


HEARTBEAT_FLAG = "heartbeat-ms"
INPUT_FILE_FLAG = "input-file"
OUTPUT_FLAG = "output"
DEFAULT_HEARTBEAT_MS = 10_000

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
    renderers: Mapping[Format, Renderer],
    scalars: ScalarRegistry,
    streaming: bool = False,
    safe_default: bool = False,
    gui_operations: Sequence[str] = (),
    interactive: bool = False,
    editor_alternatives: Sequence[str] = (),
    paginated: bool | None = None,
    default_limit: int = DEFAULT_LIMIT,
    cursor_check: Callable[[str], None] | None = None,
    heartbeat: bool = False,
    stdin_input: bool = False,
    output_file: bool = False,
    requires_auth: bool = False,
    auth: AuthKind | None = None,
    token_env_vars: Sequence[str] = (),
    async_job: bool = False,
    config_write_scope: ConfigScope | None = None,
    schema_version: SchemaVersion = DEFAULT_SCHEMA_VERSION,
    compat: Mapping[str, Shim] | None = None,
    project_root: Sequence[str] = (),
    retry: Retry | None = None,
    sort_key: str | None = None,
    ordered: bool = False,
) -> Command:
    if not description:
        raise RegistrationError(f"{path}: description is required")
    check_declaration(
        str(path),
        requires_auth=requires_auth,
        required_scopes=required_scopes,
        auth=auth,
        token_env_vars=token_env_vars,
        streaming=streaming,
    )
    if paginated and streaming:
        raise RegistrationError(
            f"{path}: a stream has no pages; drop paginated=True or streaming=True"
        )
    if output_file and streaming:
        raise RegistrationError(
            f"{path}: a stream writes events as they come; drop output_file=True or streaming=True"
        )
    if heartbeat and streaming:
        raise RegistrationError(
            f"{path}: a stream's events show it is alive; drop heartbeat=True or streaming=True"
        )
    args_type, output_type, resources, paginated = _inspect_handler(fn, path, streaming, paginated)
    if cursor_check is not None and not (paginated and callable(cursor_check)):
        raise RegistrationError(
            f"{path}: cursor_check is a function validating a list command's own cursor; "
            "pass one to a command returning list[T] or treaty.Page[T]"
        )
    if isinstance(default_limit, bool) or not isinstance(default_limit, int) or default_limit < 0:
        raise RegistrationError(f"{path}: default_limit is a whole number of items; 0 is all")
    _check_gui(path, output_type, gui_operations)
    _check_ctx_calls(
        fn, path, gui_operations, interactive, editor_alternatives, config_write_scope, retry
    )
    if isinstance(project_root, str) or not all(isinstance(m, str) and m for m in project_root):
        raise RegistrationError(
            f"{path}: project_root names marker files, such as project_root=('.git',)"
        )
    if retry is not None and not isinstance(retry, Retry):
        raise RegistrationError(f"{path}: retry takes treaty.Retry(...), not {retry!r}")
    returns_job = isinstance(output_type, type) and issubclass(output_type, Job)
    if async_job and not returns_job:
        raise RegistrationError(
            f"{path}: an async_job command returns treaty.Job, or a dataclass extending it, "
            "as its job descriptor (REQ-C-022)"
        )
    if config_write_scope is not None and danger_level is DangerLevel.SAFE:
        raise RegistrationError(
            f'{path}: a command that writes config is mutating; set danger_level="mutating"'
        )
    fields = inspect_fields(args_type, scalars)
    flags = {f.flag for f in fields}
    unknown = [name for name in editor_alternatives if name not in flags]
    if unknown:
        raise RegistrationError(
            f"{path}: editor_alternatives {unknown} are not flags of the command; name the "
            "flags that supply the text instead of the editor (REQ-C-023)"
        )
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
    if safe_default and danger_level is not DangerLevel.DESTRUCTIVE:
        raise RegistrationError(
            f"{path}: safe_default=True is for destructive commands, whose dry run it makes "
            "the default (REQ-O-048)"
        )
    for f in fields:
        f.to_flag_entries()  # a default the manifest cannot list fails now, not on --help
    if danger_level is not DangerLevel.SAFE:
        if not can_carry(output_type, "effect"):
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
        if not can_carry(output_type, "would_affect"):
            raise RegistrationError(
                f"{path}: destructive commands must return an object with a 'would_affect' "
                "field for dry runs, such as would_affect: treaty.Affects | None = None "
                "(REQ-C-004)"
            )
    if len(set(exit_codes)) != len(exit_codes):
        raise RegistrationError(f"{path}: duplicate exit code names")
    output_schema = schema_for(output_type, scalars, output=True)
    if sort_key is not None and ordered:
        raise RegistrationError(
            f"{path}: sort_key orders the output array, ordered=True keeps it; pick one"
        )
    order = OutSpec(sort_key=sort_key, ordered=ordered)
    check_order(output_type, str(path), order)
    if ordered:
        output_schema = {**output_schema, "x-ordered": True}
    shims = _compat(path, compat or {}, schema_version, output_type, scalars)
    if returns_job:
        output_schema = descriptor_schema(output_schema)
    if danger_level is not DangerLevel.SAFE:
        output_schema = with_replay_effect(output_schema)
    return Command(
        path=path,
        handler=fn,
        args_type=args_type,
        output_type=output_type,
        output_schema=output_schema,
        args_schema=_with_max_bytes(schema_for(args_type, scalars), fields),
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
        renderers=dict(renderers),
        secret_env_vars={f.name: default_env_var(app_name, f.name) for f in fields if f.secret},
        streaming=streaming,
        resources=resources,
        resource_graph=resource_graph(resources, str(path), args_type),
        safe_default=safe_default,
        gui_operations=tuple(gui_operations),
        interactive=interactive,
        editor_alternatives=tuple(editor_alternatives),
        paginated=paginated,
        default_limit=Limit(default_limit or None),
        cursor_check=cursor_check,
        heartbeat=heartbeat,
        stdin_input=stdin_input,
        output_file=output_file,
        requires_auth=requires_auth,
        auth=auth,
        token_env_vars=tuple(
            dict.fromkeys((default_env_var(app_name, "token"), *token_env_vars)) if auth else ()
        ),
        async_job=async_job,
        config_write_scope=config_write_scope,
        schema_version=schema_version,
        compat=shims,
        project_root=tuple(project_root),
        retry=retry,
        order=order,
    )


def _with_max_bytes(schema: JsonSchema, fields: Sequence[FieldInfo]) -> JsonSchema:
    """``Flag(max_bytes=)`` as ``x-max-bytes`` on the property, or on its items"""
    properties = dict(schema["properties"])
    for f in fields:
        if f.spec.max_bytes is None:
            continue
        prop = dict(properties[f.name])
        if f.flag_type is FlagType.ARRAY:
            prop["items"] = {**prop["items"], "x-max-bytes": f.spec.max_bytes}
        else:
            prop["x-max-bytes"] = f.spec.max_bytes
        properties[f.name] = prop
    return {**schema, "properties": properties}


def _compat(
    path: CommandPath,
    compat: Mapping[str, Shim],
    current: SchemaVersion,
    output_type: object,
    scalars: ScalarRegistry,
) -> tuple[Compat, ...]:
    """``compat={"1.4": to_v1}``: each shim takes the command's output and returns the
    older shape, whose schema ``--output-schema`` shows when that major is pinned"""
    out: list[Compat] = []
    for key, shim in compat.items():
        try:
            version = SchemaVersion(key)
        except InvalidValue as exc:
            raise RegistrationError(f"{path}: compat key: {exc}") from None
        if version.major >= current.major:
            raise RegistrationError(
                f"{path}: compat key {key} is not an older major than schema_version="
                f"{current}; a shim serves a major the command has moved past"
            )
        if any(c.version.major == version.major for c in out):
            raise RegistrationError(f"{path}: compat names major {version.major} twice")
        params = list(inspect.signature(shim).parameters) if callable(shim) else []
        hints = typing.get_type_hints(shim) if params else {}
        if len(params) != 1 or hints.get(params[0]) != output_type:
            raise RegistrationError(
                f"{path}: compat[{key!r}] is a function of one parameter annotated "
                f"{output_type!r}, the command's output"
            )
        returned = hints.get("return")
        if returned is None or not is_payload_type(returned):
            raise RegistrationError(
                f"{path}: compat[{key!r}] needs a return annotation that serializes to a JSON "
                "object, array, or null, for its output schema"
            )
        check_order(returned, f"{path}: compat[{key!r}]")
        out.append(Compat(version, shim, returned, schema_for(returned, scalars, output=True)))
    return tuple(sorted(out, key=lambda c: c.version.key))


def _page_output(
    output_type: object, path: CommandPath, paginated: bool | None
) -> tuple[object, bool]:
    """A list command's output is ``list[T]`` or ``Page[T]``, served as ``list[T]``; with
    ``paginated=None`` every such output is a list command (REQ-F-018)"""
    annotation = resolve_alias(output_type)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if annotation is Page or origin is Page:
        if paginated is False:
            raise RegistrationError(
                f"{path}: returns treaty.Page but paginated=False; drop paginated=False"
            )
        return list[args[0] if args else object], True  # type: ignore[misc]
    item: object = None
    if origin in (list, collections.abc.Sequence) and len(args) == 1:
        item = args[0]
    elif origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        item = args[0]
    if paginated is False or (paginated is None and item is None):
        return output_type, False
    if item is None:
        raise RegistrationError(
            f"{path}: a paginated command returns list[T] or treaty.Page[T], not {output_type!r}"
        )
    return list[item], True  # type: ignore[valid-type]


def _check_gui(path: CommandPath, output_type: object, gui_operations: Sequence[str]) -> None:
    unknown = sorted(set(gui_operations) - {BROWSER_OPEN})
    if unknown:
        raise RegistrationError(
            f"{path}: gui_operations {unknown} are not supported; {BROWSER_OPEN!r} is"
        )
    if gui_operations and not can_carry(output_type, "open_url"):
        raise RegistrationError(
            f"{path}: a command that opens a browser must return an object with an "
            "'open_url' field, where a headless run puts the URL (REQ-F-057), such as "
            "open_url: str | None = None"
        )


def _check_ctx_calls(
    fn: Handler,
    path: CommandPath,
    gui_operations: Sequence[str],
    interactive: bool,
    editor_alternatives: Sequence[str],
    config_write_scope: ConfigScope | None,
    retry: Retry | None,
) -> None:
    """Refuse at registration what the handler's source shows would fail at run time"""
    for call in ctx_calls(fn):
        where = f"{path}: ctx.{call.method}() on line {call.line} of the handler"
        if call.shell:
            raise RegistrationError(
                f"{where} gets a shell string (SHELL_STRING_PROHIBITED); treaty never "
                "runs a shell, so pass an argument list such as ['git', 'log', '-1'] (REQ-F-062)"
            )
        if call.method == "open_url" and BROWSER_OPEN not in gui_operations:
            raise RegistrationError(
                f"{where} opens a browser; declare gui_operations=[{BROWSER_OPEN!r}] (REQ-C-024)"
            )
        if call.method in ("prompt", "confirm") and not interactive:
            raise RegistrationError(
                f"{where} asks a person; declare interactive=True, which adds --yes and "
                "--non-interactive (REQ-C-005)"
            )
        if call.method == "write_config" and config_write_scope is None:
            raise RegistrationError(
                f'{where} writes config; declare config_write_scope="local" (or "global") '
                "(REQ-C-025)"
            )
        if call.method == "retry" and retry is None:
            raise RegistrationError(
                f"{where} retries; declare retry=treaty.Retry(...), which adds --retries and "
                "--retry-delay (REQ-F-078)"
            )
        if call.method == "edit" and not editor_alternatives:
            raise RegistrationError(
                f"{where} opens an editor; declare editor_alternatives=[...] naming the flags "
                "that supply the text instead (REQ-C-023)"
            )


def _inspect_handler(
    fn: Handler, path: CommandPath, streaming: bool, paginated: bool | None
) -> tuple[type, object, tuple[type, ...], bool]:
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
        paginated = False  # a stream has no pages; its events may be lists
    output_type, paginated = _page_output(output_type, path, paginated)
    if not is_payload_type(output_type):
        what = "each yielded event" if streaming else "return type"
        raise RegistrationError(f"{path}: {what} must serialize to a JSON object, array, or null")
    assert isinstance(args_type, type)
    return args_type, output_type, resources, paginated


_STREAM_ORIGINS = (
    collections.abc.Iterator,
    collections.abc.Iterable,
    collections.abc.Generator,
)


def _event_type(annotation: object, path: CommandPath) -> object:
    """The ``T`` of a streaming handler's ``Iterator[T]`` (or ``Iterable`` / ``Generator``)"""
    annotation = resolve_alias(annotation)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin not in _STREAM_ORIGINS or not args:
        raise RegistrationError(
            f"{path}: a streaming handler must be annotated Iterator[T] for its event type"
        )
    return args[0]
