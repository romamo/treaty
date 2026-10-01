"""Command records built from a handler function and its metadata."""

from __future__ import annotations

import collections.abc
import dataclasses
import inspect
import shlex
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._args_adapter import ArgsAdapters, ArgsModel
from ._auth import AuthKind, check_declaration
from ._batch import ITEM_KEYS, batch_item, batch_schema
from ._cache import CachePolicy
from ._config import ConfigScope
from ._declare import (
    Background,
    SideEffect,
    Subprocess,
    check_platform,
    check_side_effects,
    check_subprocess,
    derive_subprocess,
)
from ._deprecation import Deprecated
from ._deps import Version, check_required_tools
from ._effect import can_carry, lists_unrequired, with_replay_effect
from ._env import SESSION, app_var
from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo, dry_run_field, inspect_fields
from ._jobs import Job, descriptor_schema
from ._lines import INPUT_LINES_FLAG, StdinInput
from ._mode import Format
from ._out import NO_ORDER, Binary, OutSpec, check_order
from ._output_base import OutputBase, OutputRoot, output_root
from ._page import DEFAULT_LIMIT, Limit, Page
from ._protect import check_trust, declares_external, with_trust_tags
from ._resources import ResourceSpec, dependency_params, refuse_async, resource_graph
from ._retry import Retry
from ._rules import BoundRule, bind_rules
from ._scalars import ScalarRegistry
from ._scan import ctx_attribute, ctx_calls, shell_calls
from ._schema import JsonSchema, is_payload_type, schema_for
from ._secrets import default_env_var
from ._steps import STEP_KEYS, Rollback, StepName
from ._subprocess import BROWSER_OPEN, HeadlessBehavior
from ._timeout import Timeout
from ._types import (
    FlagType,
    is_dataclass_type,
    resolve_alias,
    signature,
    strip_optional,
    type_hints,
)
from ._values import CommandPath, ExitCodeName, InvalidValue, SchemaVersion, Scope, ToolVersion

if TYPE_CHECKING:
    from ._adapters import OutputAdapters
    from ._records import RecordSpec  # imports the parser, which imports this module

Handler = Callable[..., Any]
"""``(args, ctx, *resources)``: extra parameters are annotated with resource classes"""
Cleanup = Callable[[], None]
Renderer = Callable[[Any], str]
"""Text for one result, or one stream event, from its JSON-ready ``data``"""
Shim = Callable[[Any], Any]
"""The command's output in the shape of an older schema version (REQ-O-014)"""
DEFAULT_SCHEMA_VERSION = SchemaVersion("1.0")


class OptionPlacement(StrEnum):
    """REQ-C-027: where options may go on a command's argv"""

    ANY = "any"
    """Anywhere after the path, among positionals too (REQ-F-067)"""
    STRICT = "strict"
    """Before the first positional; it and everything after it are taken verbatim"""


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
    session_env_var: str
    """``<APP>_SESSION``, whose value deduplicates repeated mutating calls"""
    streaming: bool
    """The handler is a generator; every yield is one envelope line (REQ-O-004)"""
    resources: tuple[type, ...]
    """Resource classes the handler takes after ``ctx``, in parameter order"""
    resource_graph: Mapping[type, ResourceSpec]
    """Every resource reachable from ``resources``, validated at registration"""
    outlasts_default: bool = False
    """Declares ``timeout=None``, or one longer than the app default: it gets ``--timeout``
    unless a field of its own is named ``timeout``"""
    safe_default: bool = False
    """A destructive command that runs as a dry run unless ``--live`` (REQ-O-048)"""
    gui_operations: tuple[str, ...] = ()
    """Display operations the handler may start; only ``browser_open`` (REQ-C-024)"""
    headless_behavior: HeadlessBehavior | None = None
    """What a headless run does instead; declared with every ``gui_operations``"""
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
    stdin_input: StdinInput | None = None
    """The handler reads stdin or ``--input-file``: the payload in ``ctx.stdin_text``, or
    its lines through ``ctx.stdin_lines``"""
    stdin_records: RecordSpec | None = None
    """``stdin_records=``: each input line a record of this type, ``ctx.stdin_records``"""
    output_file: bool = False
    """``--output PATH`` writes the rendered ``data`` to a file (REQ-O-001), or the raw
    bytes of a command that ``returns_binary``"""
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
    output_root: OutputRoot | None = None
    """Where a relative ``--output`` lands, when ``output_file`` is declared (#68)"""
    retry: Retry | None = None
    """How ``ctx.retry`` retries (REQ-F-078); adds ``--retries`` and ``--retry-delay``"""
    order: OutSpec = NO_ORDER
    """``sort_key=`` and ``ordered=`` of a command whose output is an array (REQ-F-020)"""
    fix_commands: Mapping[str, str] = field(default_factory=dict)
    """``error.code`` to the invocation that fixes it, when the raise gives none (REQ-C-030)"""
    refreshes_auth: bool = False
    """Renews expired credentials: ``error.refresh_command`` of ``CREDENTIALS_EXPIRED``"""
    aliases: tuple[CommandPath, ...] = ()
    """Old paths that redirect here with exit 13 (``App.redirect``); manifest ``aliases``"""
    requires: tuple[BoundRule, ...] = ()
    """Conditional argument rules, checked in phase 1 (REQ-C-026); manifest ``requires``"""
    option_placement: OptionPlacement = OptionPlacement.ANY
    """``strict``: options end at the first positional, for a command that forwards the
    rest of argv to a child verbatim (REQ-C-027)"""
    introduced_in: ToolVersion | None = None
    """The tool version that added the command, in ``--schema`` (REQ-F-075)"""
    deprecated: Deprecated | None = None
    """Retiring: runs warn and name ``replacement``, a command path (REQ-F-075)"""
    steps: tuple[StepName, ...] = ()
    """The ordered steps ``ctx.step`` walks through, in the manifest (REQ-C-008)"""
    resumable: bool = False
    """``--resume-from STEP`` starts at a step (REQ-O-010)"""
    rollback: Rollback | None = None
    """Undoes completed steps under ``--rollback-on-failure`` (REQ-O-011)"""
    external: bool | None = None
    """``data`` is content from outside the tool: trust-tagged (REQ-F-035); False says it
    is not, though the command calls out, and None leaves it undeclared"""
    subprocess: Subprocess | None = None
    """The child binary and its arguments: ``subprocess=``, else derived from the
    handler's ``ctx.run([...])`` calls (REQ-C-019)"""
    shell_checked: tuple[str, ...] = ()
    """Fields refused in phase 1 with a shell metacharacter: the declared
    ``user_controlled_args``, never derived ones (08-D1)"""
    platform: tuple[str, ...] = ()
    """``sys.platform`` values the command supports; empty is all (REQ-C-018)"""
    required_tools: Mapping[str, Version] = field(default_factory=dict)
    """Programs the command runs, to their minimum versions; checked by ``doctor``"""
    filesystem_side_effects: tuple[SideEffect, ...] = ()
    """Where the command writes on disk; ``cleanup`` removes the temp and cache ones"""
    background: Background | None = None
    """Starts a process that outlives the run with ``ctx.spawn`` (REQ-C-010)"""
    preserve_locale: bool = False
    """Children keep the user's locale instead of the C locale (REQ-F-066)"""
    child_log: bool = False
    """``ctx.run(stream="always")`` writes a child's lines to stderr in any format and
    verbosity but ``--quiet``; the manifest description says so (#173)"""
    cache: CachePolicy | None = None
    """``ctx.cache`` with ``--no-cache`` and ``--cache-ttl`` (REQ-O-018)"""
    recursive_traversal: bool = False
    """``ctx.walk`` with ``--no-follow-symlinks`` and ``--max-depth`` (REQ-O-040)"""
    batch: bool = False
    """Returns ``treaty.Batch[T]``, ``output_type`` being ``T``: ``data`` is ``summary``
    and ``results``, and a failed item exits 3 (REQ-C-009)"""
    id_field: str | None = None
    """The output's primary identifier, which ``--format id`` writes (REQ-O-005)"""
    is_async: bool = False
    """The handler is ``async def``: it runs on the run's event loop (REQ-F-049)"""
    args_model: ArgsModel | None = None
    """The handler's args model, taken through ``app.args_adapter``: ``args_type`` is
    then the dataclass phase 1 parses its arguments into"""
    passthrough: bool = False
    """Delegates its arguments to another tool's parser: ``ctx.argv_rest`` holds every
    token after the command path, verbatim, and the delegated tool owns stdout (#35)"""
    help_command: tuple[str, ...] | None = None
    """The ``ctx.argv_rest`` a passthrough command gets for a lone ``--help`` or ``-h``
    after its path; None hands those to the tool verbatim"""

    def handler_args(self, args: object) -> object:
        """What the handler, its resources, and its rollback receive for parsed ``args``"""
        return args if self.args_model is None else self.args_model.instance(args)

    @property
    def min_schema_version(self) -> SchemaVersion:
        """The oldest version ``--schema-version`` can select"""
        return self.compat[0].version if self.compat else self.schema_version

    def compat_for(self, version: SchemaVersion) -> Compat:
        return next(c for c in self.compat if c.version == version)

    def pin(self, raw: object) -> SchemaVersion | None:
        """``--schema-version MAJOR``, or the ``MAJOR.MINOR`` the command serves: the
        older version it selects, None for the current; a version the command does not
        serve is ``SCHEMA_VERSION_UNSUPPORTED`` (exit 2)"""
        text = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
        parts = text.split(".") if isinstance(text, str) else []
        context: dict[str, object] = {
            "flag": "schema-version",
            "schema_version": self.schema_version.value,
            "min_schema_version": self.min_schema_version.value,
        }
        if not (
            1 <= len(parts) <= 2 and all(p.isascii() and p.isdigit() and len(p) <= 6 for p in parts)
        ):
            raise ParseError(
                "--schema-version takes a major version, such as 1, or a MAJOR.MINOR one",
                context=context,
            )
        requested = ".".join(parts)
        major = int(parts[0])
        versions = [self.schema_version, *(c.version for c in self.compat)]
        served = next((v for v in versions if v.major == major), None)
        if served is not None and len(parts) == 2 and int(parts[1]) != served.minor:
            served = None  # the major is served, at another minor
        if served == self.schema_version:
            return None
        if served is None:
            majors = [str(c.version.major) for c in self.compat] + [str(self.schema_version.major)]
            raise ParseError(
                f"Command {self.path} does not serve schema version {requested}",
                code="SCHEMA_VERSION_UNSUPPORTED",
                context={**context, "requested_version": requested},
                suggestion=f"pass --schema-version {' or '.join(majors)}, or drop it for "
                f"the current {self.schema_version}",
            )
        return served

    @property
    def programs(self) -> frozenset[str]:
        """The programs the command runs: ``subprocess=`` and ``required_tools=``"""
        names = set(self.required_tools)
        if self.subprocess is not None:
            names.add(self.subprocess.binary)
        return frozenset(names)

    @property
    def accepts_timeout(self) -> bool:
        """``--timeout``: network commands, streams, which may never end on their own, and
        commands that may run unbounded or longer than the app default. On those last it
        yields to a field of the command's own named ``timeout``, as ``-v`` yields to a
        ``short="v"``, so an app that had one keeps it"""
        if self.has_network_io or self.streaming:
            return True
        return self.outlasts_default and self.field_by_flag("timeout") is None

    def field_by_flag(self, flag: str) -> FieldInfo | None:
        for f in self.fields:
            if f.flag == flag:
                return f
        return None

    @property
    def returns_binary(self) -> bool:
        """Returns ``bytes`` or ``treaty.Binary``, or None: ``--output`` writes the raw bytes"""
        if self.batch:
            return False
        base, _ = strip_optional(self.output_type)
        return base is bytes or base is Binary

    @property
    def dry_run_field(self) -> FieldInfo | None:
        """The dry-run switch: the ``Flag(dry_run=True)`` field, else a boolean ``dry_run``"""
        return dry_run_field(self.fields)

    def field_by_short(self, short: str) -> FieldInfo | None:
        for f in self.fields:
            if f.spec.short == short:
                return f
        return None


HEARTBEAT_FLAG = "heartbeat-ms"
INPUT_FILE_FLAG = "input-file"
OUTPUT_FLAG = "output"
DEFAULT_HEARTBEAT_MS = 10_000

PASSTHROUGH_NOTE = (
    "Arguments after the command path go to the delegated tool unparsed; the envelope is the "
    "last stderr line"
)
"""What a passthrough command's manifest description adds: the spec's CommandEntry has
no key that says so, and ``option_placement: strict`` alone does not (#35)"""
CHILD_LOG_NOTE = (
    "While it runs, the log of the program it runs streams to stderr as plain lines in any "
    "--format, silenced only by --quiet"
)
"""What a ``child_log=True`` command's manifest description adds: the spec's CommandEntry
has no key for a command whose stderr carries a child's log (#173)"""
ARGV_KEY = "argv"
"""A passthrough command's argv for its tool in an exec line or ``App.call``"""
HELP_TOKENS = ("--help", "-h")
"""What a passthrough command's ``help_command`` stands in for, alone after its path"""


@dataclass(frozen=True, slots=True)
class Delegated:
    """``data`` of a passthrough command: the exit code of the tool it delegated to,
    which is also the process exit code"""

    exit_code: int


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
    outlasts_default: bool = False,
    streaming: bool = False,
    safe_default: bool = False,
    gui_operations: Sequence[str] = (),
    headless_behavior: HeadlessBehavior | None = None,
    interactive: bool = False,
    editor_alternatives: Sequence[str] = (),
    paginated: bool | None = None,
    default_limit: int = DEFAULT_LIMIT,
    cursor_check: Callable[[str], None] | None = None,
    heartbeat: bool = False,
    stdin_input: StdinInput | None = None,
    stdin_records: RecordSpec | None = None,
    output_file: bool | OutputBase | type | Callable[..., Path] = False,
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
    provided: Sequence[type] = (),
    fix_commands: Mapping[str, str] | None = None,
    refreshes_auth: bool = False,
    requires: Sequence[object] = (),
    option_placement: OptionPlacement = OptionPlacement.ANY,
    introduced_in: ToolVersion | None = None,
    deprecated: Deprecated | None = None,
    steps: Sequence[str] = (),
    resumable: bool = False,
    rollback: Rollback | None = None,
    external: bool | None = None,
    subprocess: Subprocess | None = None,
    platform: Sequence[str] = (),
    required_tools: Mapping[str, str] | None = None,
    filesystem_side_effects: Sequence[SideEffect] = (),
    background: Background | None = None,
    preserve_locale: bool = False,
    child_log: bool = False,
    cache: CachePolicy | None = None,
    recursive_traversal: bool = False,
    id_field: str | None = None,
    args_adapters: ArgsAdapters | None = None,
    passthrough: bool = False,
    help_command: Sequence[str] | None = None,
) -> Command:
    if not description:
        raise RegistrationError(f"{path}: description is required")
    help_argv = _check_help_command(path, help_command, passthrough)
    if passthrough:
        _check_passthrough(
            path,
            danger_level,
            {
                "streaming=True": streaming,
                "paginated=True": bool(paginated),
                "cursor_check=": cursor_check is not None,
                "safe_default=True": safe_default,
                "interactive=True": interactive,
                "editor_alternatives=": bool(editor_alternatives),
                "gui_operations=": bool(gui_operations),
                "heartbeat=True": heartbeat,
                "stdin_input=": stdin_input is not None,
                "stdin_records=": stdin_records is not None,
                "supports_raw_payload=True": supports_raw_payload,
                "auth=": auth is not None,
                "async_job=True": async_job,
                "config_write_scope=": config_write_scope is not None,
                "compat=": bool(compat),
                "sort_key=": sort_key is not None,
                "ordered=True": ordered,
                "requires=": bool(requires),
                "steps=": bool(steps),
                "resumable=True": resumable,
                "rollback=": rollback is not None,
                "external=": external is not None,
                "subprocess=": subprocess is not None,
                "background=": background is not None,
                "cache=": cache is not None,
                "recursive_traversal=True": recursive_traversal,
                "id_field=": id_field is not None,
            },
        )
        # REQ-C-027: everything after the path reaches the tool verbatim
        option_placement = OptionPlacement.STRICT
    if deprecated is not None and not isinstance(deprecated, Deprecated):
        raise RegistrationError(f"{path}: deprecated takes treaty.Deprecated(since=...)")
    if deprecated is not None and deprecated.replacement is not None:
        try:
            CommandPath(deprecated.replacement)
        except InvalidValue as exc:
            raise RegistrationError(
                f"{path}: Deprecated(replacement=...) is a command path such as deploy.rollback: "
                f"{exc}"
            ) from None
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
    paginated_asked = bool(paginated)
    if passthrough:
        args_type, output_type, resources, paginated = _inspect_passthrough(fn, path)
    else:
        args_type, output_type, resources, paginated = _inspect_handler(
            fn, path, streaming, paginated, scalars, args_adapters
        )
    args_model: ArgsModel | None = None
    if not is_dataclass_type(args_type):
        adapter = None if args_adapters is None else args_adapters.for_class(args_type)
        assert adapter is not None, "_inspect_handler checked the args type"
        args_model = ArgsModel.build(args_type, adapter)
        args_type = args_model.fields_type
    item_type = batch_item(resolve_alias(output_type))
    batch = item_type is not None
    if item_type is not None:
        _check_batch(path, item_type, streaming, danger_level, compat)
        if steps or paginated_asked:
            raise RegistrationError(
                f"{path}: a treaty.Batch reports per-item results; drop steps= and paginated="
            )
        output_type = item_type
    refuse_async(cleanup, f"{path}: cleanup")
    refuse_async(cursor_check, f"{path}: cursor_check")
    if cursor_check is not None and not (paginated and callable(cursor_check)):
        raise RegistrationError(
            f"{path}: cursor_check is a function validating a list command's own cursor; "
            "pass one to a command returning list[T] or treaty.Page[T]"
        )
    if isinstance(default_limit, bool) or not isinstance(default_limit, int) or default_limit < 0:
        raise RegistrationError(f"{path}: default_limit is a whole number of items; 0 is all")
    _check_gui(path, output_type, gui_operations, headless_behavior, scalars.adapters)
    _check_background(path, output_type, background, streaming, scalars.adapters)
    if cache is not None and not isinstance(cache, CachePolicy):
        raise RegistrationError(f"{path}: cache takes treaty.CachePolicy(ttl_seconds=...)")
    if cache is not None:
        # REQ-C-011: where the cache is, for the manifest and the cleanup built-in
        cached = SideEffect(f"~/.cache/{app_name}/{path}/", "cache", ttl_seconds=cache.ttl_seconds)
        filesystem_side_effects = (
            *check_side_effects(str(path), filesystem_side_effects),
            cached,
        )
    step_names = _check_steps(path, steps, resumable, rollback, streaming, output_type)
    _check_ctx_calls(
        fn,
        path,
        gui_operations,
        interactive,
        editor_alternatives,
        config_write_scope,
        retry,
        step_names,
        background,
        has_network_io,
        recursive_traversal,
    )
    if isinstance(project_root, str) or not all(isinstance(m, str) and m for m in project_root):
        raise RegistrationError(
            f"{path}: project_root names marker files, such as project_root=('.git',)"
        )
    out_root = output_root(output_file, str(path), project_root)
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
    if stdin_input is not None and any(f.spec.from_stdin for f in fields):
        raise RegistrationError(
            f"{path}: stdin_input reads the command's input from stdin, so no field can also "
            "take from_stdin=True"
        )
    if stdin_input is StdinInput.LINES and any(f.flag == INPUT_LINES_FLAG for f in fields):
        raise RegistrationError(
            f'{path}: stdin_input="lines" takes {INPUT_LINES_FLAG.replace("-", "_")} in exec '
            "and MCP, so no field can have that name"
        )
    rules = bind_rules(requires, fields, f"{path}")
    declared_child = check_subprocess(str(path), subprocess, fields)
    child = declared_child or derive_subprocess(ctx_calls(fn), fields)
    if option_placement is OptionPlacement.STRICT and not passthrough:
        _check_strict(path, fields)
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
        # A passthrough command's data is treaty's own Delegated, whose replay says noop
        if not passthrough and not _carries(path, output_type, "effect", scalars.adapters):
            what = "each Batch item's value" if batch else "an object"
            raise RegistrationError(
                f"{path}: {danger_level.value} commands must return {what} with an "
                "'effect' field (REQ-C-003)"
            )
        if any(f.name == "idempotency_key" for f in fields):
            raise RegistrationError(
                f"{path}: --idempotency-key is supplied by the framework for "
                f"{danger_level.value} commands; read ctx.idempotency_key instead"
            )
    if danger_level is DangerLevel.DESTRUCTIVE:
        if dry_run_field(fields) is None:
            raise RegistrationError(
                f"{path}: destructive commands must declare a boolean 'dry_run' flag, or mark "
                "their own with Flag(dry_run=True) (REQ-C-004)"
            )
        if not _carries(path, output_type, "would_affect", scalars.adapters):
            raise RegistrationError(
                f"{path}: destructive commands must return an object with a 'would_affect' "
                "field for dry runs, such as would_affect: treaty.Affects | None = None "
                "(REQ-C-004)"
            )
    if len(set(exit_codes)) != len(exit_codes):
        raise RegistrationError(f"{path}: duplicate exit code names")
    output_schema = schema_for(output_type, scalars, output=True)
    id_field = _id_field(path, output_schema, id_field, batch)
    if sort_key is not None and ordered:
        raise RegistrationError(
            f"{path}: sort_key orders the output array, ordered=True keeps it; pick one"
        )
    order = OutSpec(sort_key=sort_key, ordered=ordered)
    check_order(output_type, str(path), order, adapters=scalars.adapters)
    if ordered:
        output_schema = {**output_schema, "x-ordered": True}
    if external is not None and not isinstance(external, bool):
        raise RegistrationError(f"{path}: external is True, False, or None (undeclared)")
    check_trust(output_type, str(path), external=bool(external), adapters=scalars.adapters)
    # A field's Out(external=True) tags data too, and the tags go on data as served, after
    # any batch or job wrapper: a schema without them fails a client that validates
    # structured content, as MCP clients do
    trust_tags = bool(external) or declares_external(output_type, scalars.adapters)
    shims = _compat(
        path, compat or {}, schema_version, output_type, scalars, trust_tags, danger_level
    )
    for shim in shims:
        # An older shape still answers the same contracts, or every pinned call fails
        # after the handler has run
        where = f"{path}: compat[{shim.version.value!r}]"
        if danger_level is not DangerLevel.SAFE and not _carries(
            where, shim.output_type, "effect", scalars.adapters
        ):
            raise RegistrationError(
                f"{where} returns no 'effect' field, which {danger_level.value} commands "
                "answer with (REQ-C-003)"
            )
        if step_names:
            _check_step_output(where, shim.output_type)
    if returns_job:
        output_schema = descriptor_schema(output_schema)
    if batch:
        output_schema = batch_schema(output_schema)
        if danger_level is not DangerLevel.SAFE:
            effect: JsonSchema = {"type": "string", "description": "What the batch did overall"}
            output_schema["properties"]["effect"] = effect
    if danger_level is not DangerLevel.SAFE:
        output_schema = with_replay_effect(output_schema)
    if step_names:
        output_schema = _with_step_fields(output_schema, step_names)
    if trust_tags:
        output_schema = with_trust_tags(output_schema)
    roots = tuple(dict.fromkeys((*resources, *(() if out_root is None else out_root.deps))))
    graph = resource_graph(
        roots,
        str(path),
        args_type if args_model is None else args_model.model,
        provided,
        fields=[f.name for f in fields],
    )
    is_async = inspect.iscoroutinefunction(fn)
    needs_loop = sorted(s.cls.__qualname__ for s in graph.values() if s.is_async)
    if needs_loop and not is_async:
        raise RegistrationError(
            f"{path}: {', '.join(needs_loop)} acquire or release on an event loop, which "
            "only an async def handler runs; make the handler async def (REQ-F-049)"
        )
    return Command(
        path=path,
        handler=fn,
        args_type=args_type,
        output_type=output_type,
        output_schema=output_schema,
        args_schema=_with_field_keys(schema_for(args_type, scalars), fields),
        fields=fields,
        description=description,
        danger_level=danger_level,
        required_scopes=tuple(required_scopes),
        exit_codes=tuple(exit_codes),
        examples=tuple(examples),
        has_network_io=has_network_io,
        timeout=timeout,
        outlasts_default=outlasts_default,
        supports_raw_payload=supports_raw_payload,
        cleanup=cleanup,
        renderers=dict(renderers),
        secret_env_vars={f.name: default_env_var(app_name, f.name) for f in fields if f.secret},
        session_env_var=app_var(app_name, SESSION.key),
        streaming=streaming,
        resources=resources,
        resource_graph=graph,
        safe_default=safe_default,
        gui_operations=tuple(gui_operations),
        headless_behavior=headless_behavior,
        interactive=interactive,
        editor_alternatives=tuple(editor_alternatives),
        paginated=paginated,
        default_limit=Limit(default_limit or None),
        cursor_check=cursor_check,
        heartbeat=heartbeat,
        stdin_input=stdin_input,
        stdin_records=stdin_records,
        output_file=out_root is not None,
        output_root=out_root,
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
        fix_commands=dict(fix_commands or {}),
        refreshes_auth=refreshes_auth,
        requires=rules,
        option_placement=option_placement,
        introduced_in=introduced_in,
        deprecated=deprecated,
        steps=step_names,
        resumable=resumable,
        rollback=rollback,
        external=external,
        subprocess=child,
        shell_checked=() if declared_child is None else declared_child.user_controlled_args,
        platform=check_platform(str(path), platform),
        required_tools=check_required_tools(str(path), required_tools or {}),
        filesystem_side_effects=check_side_effects(str(path), filesystem_side_effects),
        background=background,
        preserve_locale=preserve_locale,
        child_log=child_log,
        cache=cache,
        recursive_traversal=recursive_traversal,
        batch=batch,
        id_field=id_field,
        args_model=args_model,
        is_async=is_async,
        passthrough=passthrough,
        help_command=help_argv,
    )


def _check_help_command(
    path: CommandPath, help_command: Sequence[str] | None, passthrough: bool
) -> tuple[str, ...] | None:
    """``help_command=`` as the argv tuple it stands for; only a passthrough command,
    whose ``--help`` would otherwise reach the tool verbatim, takes one"""
    if help_command is None:
        return None
    if not passthrough:
        raise RegistrationError(
            f"{path}: help_command= is the argv a passthrough command hands its tool for "
            "--help; add passthrough=True, or drop it"
        )
    if isinstance(help_command, str) or not all(
        isinstance(token, str) and token for token in help_command
    ):
        raise RegistrationError(
            f"{path}: help_command= is a sequence of non-empty argv tokens, such as "
            f"help_command=('help',), not {help_command!r}"
        )
    if not help_command:
        raise RegistrationError(
            f"{path}: help_command=() would hand the tool no arguments for --help; pass the "
            "tokens it takes, or drop help_command="
        )
    return tuple(help_command)


def _check_passthrough(
    path: CommandPath, danger_level: DangerLevel, options: Mapping[str, bool]
) -> None:
    """A passthrough command's arguments, output, and stdout belong to the tool it
    delegates to, so treaty refuses what would need to parse, shape, or preview them"""
    taken = [name for name, given in options.items() if given]
    if taken:
        raise RegistrationError(
            f"{path}: passthrough=True hands argv and stdout to the delegated tool, which "
            f"{', '.join(taken)} would need treaty to parse or shape; drop "
            f"{'it' if len(taken) == 1 else 'them'}"
        )
    if danger_level is DangerLevel.DESTRUCTIVE:
        raise RegistrationError(
            f"{path}: a passthrough command cannot be destructive: treaty cannot preview "
            "what the delegated tool would do, which --confirm-destructive needs (REQ-C-004); "
            'declare danger_level="mutating"'
        )


_EXIT_CODE_TYPES: tuple[object, ...] = (int, None, types.NoneType, int | None)


def _inspect_passthrough(
    fn: Handler, path: CommandPath
) -> tuple[type, object, tuple[type, ...], bool]:
    """A passthrough handler returns the delegated tool's exit code, or None for 0"""
    resources = dependency_params(fn, f"{path}: handler", allow_async=True)
    params = list(signature(fn).parameters.values())
    hints = type_hints(fn)
    args_type = hints.get(params[0].name)
    if not is_dataclass_type(args_type):
        raise RegistrationError(f"{path}: first parameter must be annotated with treaty.NoArgs")
    if "return" not in hints or hints["return"] not in _EXIT_CODE_TYPES:
        raise RegistrationError(
            f"{path}: a passthrough handler returns the delegated tool's exit code; annotate "
            "it -> int, or -> None for a tool that signals failure by raising SystemExit"
        )
    assert isinstance(args_type, type)
    return args_type, Delegated, resources, False


_ID_TYPES = ("string", "integer")


def _id_field(
    path: CommandPath, schema: JsonSchema, declared: str | None, batch: bool
) -> str | None:
    """The field ``--format id`` writes: ``id_field=``, else ``id`` when the output (or
    each item of it) has one; a string, integer, UUID, or string scalar (REQ-O-005)"""
    if declared is not None and (not isinstance(declared, str) or not declared):
        raise RegistrationError(f'{path}: id_field names a field of the output, such as "id"')
    if batch:
        if declared is not None:
            raise RegistrationError(f"{path}: a treaty.Batch has no one id; drop id_field=")
        return None
    item = schema.get("items", {}) if schema.get("type") == "array" else schema
    if not isinstance(item, dict):
        # A fixed-length tuple lists one schema per position: no one item to take an id from
        return declared
    properties = item.get("properties")
    if properties is None:
        if declared is not None and item.get("type") != "object":
            raise RegistrationError(
                f"{path}: id_field={declared!r} needs an object output, or a list of objects"
            )
        return declared  # an open object, such as dict[str, object]: checked as it answers
    name = "id" if declared is None else declared
    kind = properties.get(name, {}).get("type")
    if kind in _ID_TYPES:
        return name
    if declared is None:
        return None
    if name not in properties:
        raise RegistrationError(
            f"{path}: id_field={declared!r} is not a field of the output; it has "
            f"{', '.join(sorted(properties))}"
        )
    raise RegistrationError(
        f"{path}: id_field={declared!r} must be a str, int, UUID, or string scalar, and "
        "never None, so each id is one pipeable word"
    )


def _check_batch(
    path: CommandPath,
    item: object,
    streaming: bool,
    danger_level: DangerLevel,
    compat: Mapping[str, Shim] | None,
) -> None:
    """``Batch[T]``: ``T`` is an object whose fields sit beside ``id`` and ``ok``"""
    if streaming:
        raise RegistrationError(f"{path}: a stream yields events, not a treaty.Batch")
    if danger_level is DangerLevel.DESTRUCTIVE:
        raise RegistrationError(
            f"{path}: a destructive command previews one would_affect for its dry run; "
            "return one result, or make each item its own call, instead of treaty.Batch"
        )
    if compat:
        raise RegistrationError(f"{path}: compat= shims one result, not a treaty.Batch")
    base, optional = strip_optional(item)
    is_object = is_dataclass_type(base) or base is dict or typing.get_origin(base) is dict
    if optional or not is_object:
        raise RegistrationError(
            f"{path}: Batch[T] needs T to be a dataclass or dict, whose fields go beside "
            f"id and ok in each result; not {item!r}"
        )
    if is_dataclass_type(base):
        assert isinstance(base, type)
        taken = sorted(ITEM_KEYS & {f.name for f in dataclasses.fields(base)})
        if taken:
            raise RegistrationError(
                f"{path}: fields {taken} of {base.__qualname__} are keys every result "
                "already has; rename them, and pass the item's id as Item(id=...)"
            )


def _check_steps(
    path: CommandPath,
    steps: Sequence[str],
    resumable: bool,
    rollback: Rollback | None,
    streaming: bool,
    output_type: object,
) -> tuple[StepName, ...]:
    """``steps=`` names each step once, in order, and the output can carry the step fields"""
    if isinstance(steps, str):
        raise RegistrationError(f"{path}: steps is a list of step names, such as steps=[{steps!r}]")
    try:
        names = tuple(StepName(s) for s in steps)
    except InvalidValue as exc:
        raise RegistrationError(f"{path}: steps: {exc}") from None
    if len(set(names)) != len(names):
        raise RegistrationError(f"{path}: steps names a step twice")
    if not names:
        if resumable:
            raise RegistrationError(
                f"{path}: resumable=True resumes at one of the command's steps; declare "
                "steps=[...] (REQ-O-010)"
            )
        if rollback is not None:
            raise RegistrationError(
                f"{path}: rollback= undoes the command's completed steps; declare steps=[...] "
                "(REQ-O-011)"
            )
        return names
    if rollback is not None:
        refuse_async(rollback, f"{path}: rollback")
        if not callable(rollback):
            raise RegistrationError(f"{path}: rollback is a function (args, ctx, completed)")
    if streaming:
        raise RegistrationError(
            f"{path}: a stream's events show its progress; drop steps= or streaming=True"
        )
    _check_step_output(str(path), output_type)
    return names


def _check_step_output(where: str, output_type: object) -> None:
    """A steps= command's output, or an older shape of it, carries the step fields"""
    base, optional = strip_optional(output_type)
    is_object = is_dataclass_type(base) or base is dict or typing.get_origin(base) is dict
    if optional or not is_object:
        raise RegistrationError(
            f"{where}: a steps= command returns an object, a dataclass or dict, whose data "
            f"carries completed_steps, failed_step, and skipped_steps; not {output_type!r}"
        )
    if is_dataclass_type(base):
        assert isinstance(base, type)
        taken = sorted(STEP_KEYS & {f.name for f in dataclasses.fields(base)})
        if taken:
            raise RegistrationError(
                f"{where}: output fields {taken} are the step fields treaty adds to data; "
                "rename them"
            )


def _with_step_fields(schema: JsonSchema, steps: Sequence[StepName]) -> JsonSchema:
    """The step fields a successful run adds to ``data`` (REQ-C-008)"""
    names: JsonSchema = {"type": "array", "items": {"enum": [s.value for s in steps]}}
    properties = {
        **schema.get("properties", {}),
        "completed_steps": {**names, "description": "Steps that completed, in order"},
        "failed_step": {
            "type": ["string", "null"],
            "description": "The step that failed; null when the command completed",
        },
        "skipped_steps": {**names, "description": "Steps that did not run"},
    }
    return {**schema, "properties": properties}


def _check_strict(path: CommandPath, fields: Sequence[FieldInfo]) -> None:
    """Strict placement forwards the tail of argv, which needs a place to go"""
    positionals = [f for f in fields if f.positional]
    last = positionals[-1] if positionals else None
    item = None if last is None else last.classified.item
    if item is None or item.flag_type is not FlagType.STRING or item.path or item.scalar:
        raise RegistrationError(
            f'{path}: option_placement="strict" forwards everything from the first '
            "positional on, so the last positional must be a variadic tuple[str, ...], "
            'such as child_args: tuple[str, ...] = Arg(description="Passed to the child")'
        )


def _with_field_keys(schema: JsonSchema, fields: Sequence[FieldInfo]) -> JsonSchema:
    """``Flag(max_bytes=)`` as ``x-max-bytes`` on the property, or on its items, and
    ``audit=False`` as ``x-audited: false`` on the property"""
    properties = dict(schema["properties"])
    for f in fields:
        if f.spec.max_bytes is None and f.spec.audit:
            continue
        prop = dict(properties[f.name])
        if f.spec.max_bytes is not None and f.flag_type is FlagType.ARRAY:
            prop["items"] = {**prop["items"], "x-max-bytes": f.spec.max_bytes}
        elif f.spec.max_bytes is not None:
            prop["x-max-bytes"] = f.spec.max_bytes
        if not f.spec.audit:
            prop["x-audited"] = False
        properties[f.name] = prop
    return {**schema, "properties": properties}


def _compat(
    path: CommandPath,
    compat: Mapping[str, Shim],
    current: SchemaVersion,
    output_type: object,
    scalars: ScalarRegistry,
    trust_tags: bool,
    danger_level: DangerLevel,
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
        params = list(signature(shim).parameters) if callable(shim) else []
        hints = type_hints(shim) if params else {}
        if len(params) != 1 or hints.get(params[0]) != output_type:
            raise RegistrationError(
                f"{path}: compat[{key!r}] is a function of one parameter annotated "
                f"{output_type!r}, the command's output"
            )
        returned = hints.get("return")
        if returned is None or not is_payload_type(returned, scalars):
            raise RegistrationError(
                f"{path}: compat[{key!r}] needs a return annotation that serializes to a JSON "
                "object, array, or null, for its output schema"
            )
        check_order(returned, f"{path}: compat[{key!r}]", adapters=scalars.adapters)
        schema = schema_for(returned, scalars, output=True)
        if danger_level is not DangerLevel.SAFE:
            # A replayed idempotency key answers noop in the older shape too
            schema = with_replay_effect(schema)
        if trust_tags or declares_external(returned, scalars.adapters):
            schema = with_trust_tags(schema)
        out.append(Compat(version, shim, returned, schema))
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


def _carries(where: object, output_type: object, name: str, adapters: OutputAdapters) -> bool:
    """``can_carry``, failing on an adapted class whose schema lists ``name`` without
    requiring it: its ``data`` may lack the key the contract reads"""
    if lists_unrequired(output_type, name, adapters):
        raise RegistrationError(
            f"{where}: the output adapter's schema lists {name!r} but does not require it; "
            "an x-volatile key is left out of data, so drop x-volatile from it"
        )
    return can_carry(output_type, name, adapters)


def _check_gui(
    path: CommandPath,
    output_type: object,
    gui_operations: Sequence[str],
    headless_behavior: HeadlessBehavior | None,
    adapters: OutputAdapters,
) -> None:
    unknown = sorted(set(gui_operations) - {BROWSER_OPEN})
    if unknown:
        raise RegistrationError(
            f"{path}: gui_operations {unknown} are not supported; {BROWSER_OPEN!r} is"
        )
    if gui_operations and headless_behavior is None:
        behaviors = ", ".join(repr(b.value) for b in HeadlessBehavior)
        raise RegistrationError(
            f"{path}: gui_operations needs headless_behavior= ({behaviors}), what a run "
            'without a display does instead (REQ-C-024); headless_behavior="emit_in_output" '
            "puts the URL in data.open_url"
        )
    if headless_behavior is not None and not gui_operations:
        raise RegistrationError(
            f"{path}: headless_behavior= describes gui_operations; declare "
            f"gui_operations=[{BROWSER_OPEN!r}] or drop it"
        )
    emits = headless_behavior is HeadlessBehavior.EMIT_IN_OUTPUT
    if emits and not _carries(path, output_type, "open_url", adapters):
        raise RegistrationError(
            f"{path}: a command that opens a browser must return an object with an "
            "'open_url' field, where a headless run puts the URL (REQ-F-057), such as "
            "open_url: str | None = None"
        )


def _check_background(
    path: CommandPath,
    output_type: object,
    background: Background | None,
    streaming: bool,
    adapters: OutputAdapters,
) -> None:
    if background is None:
        return
    if not isinstance(background, Background):
        raise RegistrationError(f"{path}: background takes treaty.Background(...)")
    if streaming:
        raise RegistrationError(f"{path}: a stream cannot start a background process")
    missing = [
        f
        for f in ("background_pid", "cleanup_command")
        if not _carries(path, output_type, f, adapters)
    ]
    if missing:
        raise RegistrationError(
            f"{path}: a command that starts a background process returns an object with "
            f"{' and '.join(repr(m) for m in missing)} fields, such as background_pid: int "
            "and cleanup_command: str (REQ-C-010)"
        )


def _check_ctx_calls(
    fn: Handler,
    path: CommandPath,
    gui_operations: Sequence[str],
    interactive: bool,
    editor_alternatives: Sequence[str],
    config_write_scope: ConfigScope | None,
    retry: Retry | None,
    steps: Sequence[StepName] = (),
    background: Background | None = None,
    has_network_io: bool = False,
    recursive_traversal: bool = False,
) -> None:
    """Refuse at registration what the handler's source shows would fail at run time"""
    line = ctx_attribute(fn, "http")
    if line is not None and not has_network_io:
        raise RegistrationError(
            f"{path}: ctx.http on line {line} of the handler goes out to the network; declare "
            "has_network_io=True, which adds --proxy, --no-proxy, and --timeout (REQ-F-036)"
        )
    for shell in shell_calls(fn):
        raise RegistrationError(
            f"{path}: {shell.name}() on line {shell.line} of the handler runs a shell, which "
            "splits words, expands globs, and executes metacharacters in arguments; use "
            "ctx.run(['program', 'arg', ...]), which never starts one (REQ-C-019)"
        )
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
        if call.method == "spawn" and background is None:
            raise RegistrationError(
                f"{where} starts a background process; declare background=treaty.Background("
                'cleanup_command="<tool> <stop command>", max_lifetime_seconds=3600) (REQ-C-010)'
            )
        if call.method == "retry" and retry is None:
            raise RegistrationError(
                f"{where} retries; declare retry=treaty.Retry(...), which adds --retries and "
                "--retry-delay (REQ-F-078)"
            )
        if call.method == "step" and not steps:
            raise RegistrationError(
                f'{where} starts a step; declare steps=["...", ...] naming them in order '
                "(REQ-C-008)"
            )
        if call.method == "step" and call.literal is not None:
            if call.literal not in {s.value for s in steps}:
                raise RegistrationError(
                    f"{where} starts {call.literal!r}, which is not one of steps="
                    f"{[s.value for s in steps]}"
                )
        if call.method == "walk" and not recursive_traversal:
            raise RegistrationError(
                f"{where} walks a directory tree; declare recursive_traversal=True, which adds "
                "--no-follow-symlinks and --max-depth (REQ-O-040)"
            )
        if call.method == "edit" and not editor_alternatives:
            raise RegistrationError(
                f"{where} opens an editor; declare editor_alternatives=[...] naming the flags "
                "that supply the text instead (REQ-C-023)"
            )


def _inspect_handler(
    fn: Handler,
    path: CommandPath,
    streaming: bool,
    paginated: bool | None,
    scalars: ScalarRegistry,
    args_adapters: ArgsAdapters | None = None,
) -> tuple[type, object, tuple[type, ...], bool]:
    resources = dependency_params(fn, f"{path}: handler", allow_async=True)
    if streaming and inspect.iscoroutinefunction(fn):
        raise RegistrationError(
            f"{path}: a streaming handler is a plain generator; an async def cannot yield "
            "events to treaty (REQ-F-049)"
        )
    params = list(signature(fn).parameters.values())
    hints = type_hints(fn)
    args_type = hints.get(params[0].name)
    adapted = args_adapters is not None and args_adapters.for_class(args_type) is not None
    if not is_dataclass_type(args_type) and not adapted:
        raise RegistrationError(
            f"{path}: first parameter must be annotated with an args dataclass, or a class "
            "app.args_adapter(...) covers"
        )
    if "return" not in hints:
        raise RegistrationError(f"{path}: handler needs a return annotation for output_schema")
    output_type = hints["return"]
    if streaming:
        output_type = _event_type(output_type, path)
        paginated = False  # a stream has no pages; its events may be lists
    if batch_item(resolve_alias(output_type)) is not None:
        assert isinstance(args_type, type)
        return args_type, output_type, resources, False  # checked by _check_batch
    output_type, paginated = _page_output(output_type, path, paginated)
    if not is_payload_type(output_type, scalars):
        what = "each yielded event" if streaming else "return type"
        raise RegistrationError(
            f"{path}: {what} must serialize to a JSON object, array, or null; a model class "
            "is written once its family is registered with app.output_adapter(...) before "
            "the commands returning it"
        )
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
