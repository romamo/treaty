"""The application: one flat registry, mode resolution, dispatch, and envelopes."""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import errno
import inspect
import io
import json
import math
import os
import re
import shlex
import sys
import threading
import time
import traceback
import types
import typing
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, NoReturn, TextIO, cast

from ._atomic import write_atomic
from ._auth import (
    OVER_PRIVILEGED,
    AuthFailure,
    AuthKind,
    Coverage,
    Credentials,
    Expired,
    expired,
    insufficient,
    names,
    not_logged_in,
    scope_set,
)
from ._cap import (
    DEFAULT_CAP,
    DEFAULT_STDIN_CAP,
    TRUNCATED_CODE,
    OutputCap,
    Rerun,
    StdinCap,
    cap_envelope,
)
from ._command import (
    DEFAULT_HEARTBEAT_MS,
    Cleanup,
    Command,
    DangerLevel,
    Example,
    Handler,
    OptionPlacement,
    Renderer,
    Shim,
    build_command,
)
from ._config import ConfigFile, ConfigScope, local_config, user_config
from ._context import Ctx, LogSink
from ._deprecation import Deprecated
from ._dispatch import DispatchRequest, parse_dispatch_line
from ._effect import affects_summary, effect_problem
from ._env import KNOWN, SESSION, STATE_DIR, app_var
from ._envelope import (
    ENVELOPE_SCHEMA_VERSION,
    Envelope,
    ErrorDetail,
    Meta,
    Redirect,
    RedirectReason,
    WarningDetail,
    clean,
    json_safe,
    write_envelope,
)
from ._errors import ArgsCrashed, CliExit, ParseError, RegistrationError, SchemaError
from ._exit import ExitCodeEntry, ExitCodeRegistry, FrameworkCode, RetryStrategy, SideEffects
from ._fix import fix_problem
from ._flags import REDACTED, Arg, Flag
from ._framework import framework_collisions
from ._help import global_rows, render_command, render_root
from ._idempotency import (
    KeyBusy,
    Record,
    RecordCorrupt,
    Slot,
    claim,
    fingerprint,
    session_key,
    state_dir,
)
from ._init import INIT_COMMAND, Init, Initialized, run_init
from ._init import required as init_required
from ._jobs import Job, JobStore, with_links
from ._locks import LockHeld, Locks
from ._manifest import (
    build_manifest,
    command_schema,
    global_flag_entries,
    implicit_exit_codes,
)
from ._meta import find_project_root, logical_cwd, read_trace_id, utc_timestamp
from ._mode import (
    Format,
    child_settings,
    color_allowed,
    is_headless,
    quiet_children,
    resolve_mode,
)
from ._out import NO_ORDER, OutSpec, arrange, sorted_indices
from ._page import (
    CURSOR_FLAG,
    DEFAULT_LIMIT,
    PageRequest,
    Position,
    invalid_cursor,
    request,
    take,
)
from ._parse import (
    Invocation,
    Route,
    build_from_mapping,
    format_hint,
    misplaced_flag_target,
    parse_command_args,
    resolve_path,
    split_globals,
    strict_argv,
    without_value,
)
from ._plain import render_event, render_plain
from ._prompt import InputRequired, NoPromptStdin, Prompter
from ._resources import Resolver
from ._retry import Retrier, RetriesExhausted, Retry
from ._rules import DefaultWhenAbsent, Excludes, RequiredWhen
from ._scalars import ScalarRegistry, ScalarSpec, default_serializer
from ._schema import to_jsonable
from ._settings import EMPTY as EMPTY_SETTINGS
from ._settings import ConfigOptions, Resolved, SettingsSpec
from ._settings import options as config_options
from ._settings import resolve as resolve_settings
from ._signals import Cancellation, Cancelled, CancelSignal, cancellation_handlers
from ._subprocess import BROWSER_OPEN, GRACE_SECONDS, Processes
from ._table import table
from ._timeout import Heartbeat, Pending, Timeout, TimeoutExpired, call_with_timeout
from ._values import (
    CommandPath,
    ExitCode,
    ExitCodeName,
    InvalidValue,
    SchemaVersion,
    Scope,
    ToolVersion,
)

EXEC_PATH = CommandPath("exec")
CHECK_PERMISSIONS_PATH = CommandPath("check-permissions")
MANIFEST_PATH = CommandPath("manifest")
VERSION_PATH = CommandPath("version")
# Built-ins that answer without the settings, so a bad config file never fails them
PURE_PATHS = frozenset({MANIFEST_PATH, VERSION_PATH})
DEFAULT_TIMEOUT = Timeout(60.0)


class _Inherit:
    """Sentinel: the command inherits the app default timeout"""


INHERIT = _Inherit()


class _Unset:
    """Sentinel: a required declaration was left out, reported as a ``RegistrationError``"""


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class NoArgs:
    """Arguments dataclass for commands that take nothing"""


@dataclass(frozen=True, slots=True)
class CheckPermissionsArgs:
    for_: str | None = Flag(
        default=None, description="Command to check, such as 'deploy rollback'; omit for all"
    )


@dataclass(frozen=True, slots=True)
class JobArgs:
    job_id: str = Arg(description="The job_id of the job descriptor")


@dataclass(frozen=True, slots=True)
class ExecArgs:
    ignore_errors: bool = Flag(default=False, description="Continue past a failed line")
    input_file: Path | None = Flag(
        default=None, description="Read the plan from this file instead of stdin; - is stdin"
    )
    dry_run: bool = Flag(
        default=False, description="Forward dry_run to every mutating or destructive line"
    )


class Group:
    """Prefix helper; registers into the parent app's flat registry"""

    def __init__(self, app: App, prefix: CommandPath) -> None:
        self._app = app
        self._prefix = prefix

    def group(self, name: str, *, description: str) -> Group:
        return self._app.group(f"{self._prefix}.{name}", description=description)

    def command(self, name: str, **meta: Any) -> Callable[[Handler], Handler]:
        return self._app.command(f"{self._prefix}.{name}", **meta)


class App:
    def __init__(
        self,
        name: str,
        *,
        version: str,
        description: str = "",
        state: Mapping[str, object] | None = None,
        default_timeout: float | None = DEFAULT_TIMEOUT.seconds,
        max_output_bytes: int = DEFAULT_CAP.bytes,
        max_stdin_bytes: int = DEFAULT_STDIN_CAP.bytes,
        state_dir: str | Path | None = None,
        enable_exec: bool = True,
        credentials: Credentials | None = None,
        jobs: JobStore | None = None,
        settings: type | None = None,
        init: Init | None = None,
        companions: Sequence[str] = (),
    ) -> None:
        """``credentials`` tells treaty which scopes the active credential holds: it gates
        ``requires_auth=True`` commands and adds the ``check-permissions`` built-in.
        ``jobs`` looks up the jobs ``async_job=True`` commands start, for the ``job status``
        and ``job cancel`` built-ins. ``settings`` is a frozen dataclass read from the
        config files and ``<APP>_<FIELD>`` variables; a handler gets it by annotating a
        parameter with the class (REQ-F-028). ``init`` is the app's one-time setup: it
        adds the ``init`` built-in, and other commands exit 4 with ``INIT_REQUIRED``
        until it has run (REQ-F-076). ``companions`` names the other programs a
        ``fix_command`` may run, such as ``("mkdir",)`` (REQ-C-030)."""
        if not name or not version:
            raise RegistrationError("App needs a name and a version")
        try:
            ToolVersion(version)
        except InvalidValue as exc:
            # meta.tool_version is semver in every response (REQ-F-023)
            raise RegistrationError(f"App {name}: {exc}, such as 1.0.0") from None
        self.name = name
        self.version = version
        self.description = description
        self.default_timeout = Timeout(default_timeout)
        self.max_output = OutputCap(max_output_bytes)
        self.max_stdin = StdinCap(max_stdin_bytes)
        self.state_dir = None if state_dir is None else Path(state_dir)
        self.exits = ExitCodeRegistry()
        self.scalars = ScalarRegistry()
        self._state: Mapping[str, object] = dict(state or {})
        self._commands: dict[CommandPath, Command] = {}
        self._groups: dict[CommandPath, str] = {}
        self._renderers: dict[Format, Renderer] = {}
        self.credentials = credentials
        self.jobs = jobs
        self.settings = None if settings is None else SettingsSpec.inspect(settings, self.scalars)
        self.init = init
        if isinstance(companions, str) or not all(
            isinstance(c, str) and c and c == c.strip() and " " not in c for c in companions
        ):
            raise RegistrationError(
                f"App {name}: companions is a sequence of program names, such as ('mkdir',)"
            )
        self.companions = frozenset(companions)
        self._fixes_checked = False
        self._redirects: dict[CommandPath, Moved] = {}
        self._register_builtins(enable_exec)
        self._builtins = frozenset(self._commands)

    # Registration

    def exit_code(
        self,
        name: str,
        code: int,
        *,
        description: str,
        retryable: bool,
        side_effects: str,
        suggestion: str | None = None,
        retry_after_ms: int | None = None,
        retry_strategy: str | None = None,
    ) -> ExitCodeEntry:
        """Declare a command-specific exit code; ``suggestion`` is the next step an agent
        takes after it, and ``retry_after_ms`` and ``retry_strategy`` how a retryable one
        is retried, each used when the ``Exit`` raised gives none"""
        if retry_strategy is not None and retry_strategy not in RetryStrategy:
            strategies = ", ".join(RetryStrategy)
            raise RegistrationError(
                f"{name}: retry_strategy={retry_strategy!r} is not one of {strategies}"
            )
        return self.exits.register(
            ExitCodeEntry(
                name=ExitCodeName(name),
                code=ExitCode(code),
                description=description,
                retryable=retryable,
                side_effects=SideEffects(side_effects),
                suggestion=suggestion,
                retry_after_ms=retry_after_ms,
                retry_strategy=None if retry_strategy is None else RetryStrategy(retry_strategy),
            )
        )

    def scalar(
        self,
        cls: type,
        *,
        parse: Callable[[Any], object],
        base: type = str,
        pattern: str | None = None,
        pattern_type: str | None = None,
        minimum: int | float | None = None,
        maximum: int | float | None = None,
        serialize: Callable[[Any], object] | None = None,
    ) -> ScalarSpec:
        """Let a domain class annotate fields and outputs; declare it before the commands using it

        Values travel as ``base`` (``str``, ``int``, or ``float``), are checked against
        ``pattern`` or the bounds, then handed to ``parse``; a ``ValueError`` from it is one
        entry in ``error.errors``. ``serialize`` turns an instance back into the base value
        and defaults to its ``value`` field.
        """
        return self.scalars.register(
            ScalarSpec(
                cls=cls,
                parse=parse,
                serialize=serialize if serialize is not None else default_serializer(cls, base),
                base=base,
                pattern=pattern,
                pattern_type=pattern_type,
                minimum=minimum,
                maximum=maximum,
            )
        )

    def format(self, mode: Format, *, render: Renderer) -> None:
        """Offer ``--format <mode>``, written by ``render`` for every command without its own
        renderer for it; declare it before the commands overriding it

        ``plain`` and ``tsv`` are always offered, and registering one replaces its built-in
        renderer. ``json`` and ``jsonl`` are the response envelope agents read, so they take
        no renderer.
        """
        _check_renderer("app.format", mode, render)
        if mode in self._renderers:
            raise RegistrationError(f"--format {mode} already has a renderer")
        self._renderers[mode] = render

    @property
    def formats(self) -> tuple[Format, ...]:
        """The ``--format`` values this app offers, in ``Format`` order"""
        built_in = (Format.PLAIN, Format.JSON, Format.JSONL, Format.TSV)
        return tuple(m for m in Format if m in built_in or m in self._renderers)

    def renderer(self, command: Command, mode: Format) -> Renderer | None:
        """The command's renderer for a text mode, else the app's; None is plain's built-in"""
        if command.path == MANIFEST_PATH:
            # The manifest is for agents: every text mode keeps it JSON, indented for reading
            return _json_text
        return command.renderers.get(mode, self._renderers.get(mode, _BUILT_IN.get(mode)))

    def group(self, path: str, *, description: str) -> Group:
        prefix = CommandPath(path)
        if prefix in self._commands:
            raise RegistrationError(f"{prefix} is already a command")
        if any(r == prefix or r.is_ancestor_of(prefix) for r in self._redirects):
            raise RegistrationError(f"{prefix} overlaps a redirected path; it answers exit 13")
        self._check_nesting(prefix)
        if not description:
            raise RegistrationError(f"group {prefix} needs a description")
        self._groups[prefix] = description
        return Group(self, prefix)

    def command(
        self,
        path: str,
        *,
        description: str,
        danger_level: str | _Unset = UNSET,
        required_scopes: Sequence[str] = (),
        exit_codes: Sequence[str] | _Unset = UNSET,
        examples: Sequence[tuple[str, str]] = (),
        has_network_io: bool = False,
        timeout: float | None | _Inherit = INHERIT,
        supports_raw_payload: bool = False,
        cleanup: Cleanup | None = None,
        renderers: Mapping[Format, Renderer] | None = None,
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
        auth: str | None = None,
        token_env_vars: Sequence[str] = (),
        async_job: bool = False,
        config_write_scope: str | None = None,
        schema_version: str = "1.0",
        compat: Mapping[str, Shim] | None = None,
        project_root: Sequence[str] = (),
        retry: Retry | None = None,
        sort_key: str | None = None,
        ordered: bool = False,
        fix_commands: Mapping[str, str] | None = None,
        refreshes_auth: bool = False,
        requires: Sequence[RequiredWhen | Excludes | DefaultWhenAbsent] = (),
        option_placement: str = "any",
        introduced_in: str | None = None,
        deprecated: Deprecated | None = None,
    ) -> Callable[[Handler], Handler]:
        """Register a handler; ``danger_level`` and ``exit_codes`` are required, and
        ``exit_codes=()`` declares that the command raises only the implicit codes

        A command returning ``list[T]`` or ``Page[T]`` is a list command: it gets
        ``--limit`` (``default_limit`` items, 0 for all), ``--cursor``, and
        ``meta.pagination``; ``paginated=False`` opts a ``list[T]`` out.
        ``cursor_check`` validates the handler's own ``Page.next_cursor`` when it comes
        back, before the handler runs: a pure function raising ``ParseError`` to refuse it.
        ``heartbeat=True`` writes a heartbeat line to stdout every ``--heartbeat-ms``
        (10 s) while the handler runs, in JSON mode. ``stdin_input=True`` reads a payload
        into ``ctx.stdin_text`` before the handler runs: stdin up to the stdin cap, or any
        size from ``--input-file``. ``output_file=True`` adds ``--output PATH``, which
        writes ``data`` there in the ``--format`` representation and the envelope to stdout.
        ``requires_auth=True`` checks the app's ``credentials`` for ``required_scopes``
        before the handler runs. ``auth="browser"`` or ``"device"`` marks a login command:
        it gets ``--headless`` and ``--token-env-var``, and ``ctx.token`` from
        ``<APP>_TOKEN`` or the ``token_env_vars`` after it. ``async_job=True`` returns a
        ``treaty.Job`` polled with ``job status``. ``config_write_scope="local"`` or
        ``"global"`` lets ``ctx.write_config`` change the project or user config file.
        ``schema_version`` is the ``MAJOR.MINOR`` of the output contract, in every
        response's ``meta``: a breaking change bumps the major, an additive one the minor.
        ``compat={"1.4": to_v1}`` keeps an older major selectable with ``--schema-version
        1``; the shim takes the command's output and returns the old shape.
        ``project_root=(".git",)`` finds the nearest directory from the cwd up holding a
        marker, as ``ctx.project_root`` and ``meta.project_root``. ``retry=Retry(...)``
        enables ``ctx.retry`` with ``--retries`` and ``--retry-delay``.
        Arrays in ``data`` are sorted (REQ-F-020): ``sort_key="id"`` orders an output
        list of objects by that field; ``ordered=True`` keeps the handler's order, for a
        ranking. ``treaty.Out`` declares the same for a field of an output dataclass.
        ``fix_commands={"STORE_MISSING": "tool init"}`` gives ``error.fix_command`` for an
        error code when the raise gives none: one command of this app or a companion, run
        verbatim, never destructive (REQ-C-030). ``refreshes_auth=True`` marks the command
        that renews expired credentials, named in ``CREDENTIALS_EXPIRED`` (REQ-F-063).
        ``requires=[RequiredWhen("format", "csv", then=("separator",)), Excludes("output",
        prohibited=("stdout",))]`` declares cross-field rules, checked before the args
        ``__post_init__`` and listed in the manifest (REQ-C-026).
        ``option_placement="strict"`` is for a command that forwards the rest of argv to a
        child: options go before the first positional, and it and every token after it
        reach the positionals verbatim, the last a ``tuple[str, ...]`` (REQ-C-027).
        ``introduced_in="1.2.0"`` records the tool version that added the command, and
        ``deprecated=Deprecated("2.0.0", replacement="deploy.rollback", removed_in="3.0.0")``
        keeps a retiring command working with a warning on every run; both are in
        ``--schema`` (REQ-F-075). Once it is removed, ``redirect`` keeps its path answering.
        """
        cmd_path = CommandPath(path)
        missing = [
            fix
            for value, fix in ((exit_codes, "exit_codes=()"), (danger_level, 'danger_level="safe"'))
            if isinstance(value, _Unset)
        ]
        if missing:
            raise RegistrationError(
                f"{cmd_path}: every command declares its exit codes and danger level "
                f"(REQ-C-001, REQ-C-002); add {' and '.join(missing)}, or the values it has"
            )
        assert not isinstance(danger_level, _Unset) and not isinstance(exit_codes, _Unset)
        if exit_codes is None or isinstance(exit_codes, str):
            fix = f"exit_codes=({exit_codes!r},)" if exit_codes else "exit_codes=()"
            raise RegistrationError(
                f"{cmd_path}: exit_codes is a sequence of exit code names, not "
                f"{exit_codes!r}; write {fix}"
            )
        if danger_level not in DangerLevel:
            levels = ", ".join(d.value for d in DangerLevel)
            raise RegistrationError(
                f"{cmd_path}: danger_level={danger_level!r} is not one of {levels}"
            )
        if requires_auth and self.credentials is None:
            raise RegistrationError(
                f"{cmd_path}: requires_auth=True needs App(credentials=...), which tells "
                "treaty the scopes of the active credential"
            )
        if auth is not None and auth not in AuthKind:
            kinds = ", ".join(k.value for k in AuthKind)
            raise RegistrationError(f"{cmd_path}: auth={auth!r} is not one of {kinds}")
        if async_job and self.jobs is None:
            raise RegistrationError(
                f"{cmd_path}: async_job=True needs App(jobs=...), which answers job status and "
                "job cancel for the jobs it starts"
            )
        added: ToolVersion | None = None
        if introduced_in is not None:
            try:
                added = ToolVersion(introduced_in)
            except InvalidValue as exc:
                raise RegistrationError(f"{cmd_path}: introduced_in: {exc}") from None
        if option_placement not in OptionPlacement:
            placements = ", ".join(p.value for p in OptionPlacement)
            raise RegistrationError(
                f"{cmd_path}: option_placement={option_placement!r} is not one of {placements}"
            )
        if config_write_scope is not None and config_write_scope not in ConfigScope:
            scopes = ", ".join(c.value for c in ConfigScope)
            raise RegistrationError(
                f"{cmd_path}: config_write_scope={config_write_scope!r} is not one of {scopes}"
            )
        overrides = dict(renderers or {})
        for mode, render in overrides.items():
            _check_renderer(f"{cmd_path}: renderers", mode, render)
            if mode not in self.formats:
                raise RegistrationError(
                    f"{cmd_path}: --format {mode} is not offered; "
                    f"register it first with app.format(Format.{mode.name}, render=...)"
                )
        try:
            contract = SchemaVersion(schema_version)
        except InvalidValue as exc:
            raise RegistrationError(f"{cmd_path}: {exc}") from None
        # A stream's timeout is an idle limit: the wait for each event (REQ-F-011)
        command_timeout = None if isinstance(timeout, _Inherit) else Timeout(timeout)
        fixes = dict(fix_commands or {})
        for error_code, fix in fixes.items():
            if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
                raise RegistrationError(
                    f"{cmd_path}: fix_commands key {error_code!r} is not an UPPER_SNAKE error code"
                )
            # The target may register later: only the shape is checked now
            problem = fix_problem(
                fix, app_name=self.name, companions=self.companions, commands=None
            )
            if problem is not None:
                raise RegistrationError(f"{cmd_path}: fix_commands[{error_code!r}]: {problem}")

        def register(fn: Handler) -> Handler:
            self._register(
                build_command(
                    fn,
                    app_name=self.name,
                    path=cmd_path,
                    description=description,
                    danger_level=DangerLevel(danger_level),
                    required_scopes=[Scope(s) for s in required_scopes],
                    exit_codes=[ExitCodeName(n) for n in exit_codes],
                    examples=[Example(d, c) for d, c in examples],
                    has_network_io=has_network_io,
                    timeout=command_timeout,
                    supports_raw_payload=supports_raw_payload,
                    cleanup=cleanup,
                    renderers=overrides,
                    scalars=self.scalars,
                    streaming=streaming,
                    safe_default=safe_default,
                    gui_operations=gui_operations,
                    interactive=interactive,
                    editor_alternatives=editor_alternatives,
                    paginated=paginated,
                    default_limit=default_limit,
                    cursor_check=cursor_check,
                    heartbeat=heartbeat,
                    stdin_input=stdin_input,
                    output_file=output_file,
                    requires_auth=requires_auth,
                    auth=None if auth is None else AuthKind(auth),
                    token_env_vars=token_env_vars,
                    async_job=async_job,
                    config_write_scope=None
                    if config_write_scope is None
                    else ConfigScope(config_write_scope),
                    schema_version=contract,
                    compat=compat,
                    project_root=project_root,
                    retry=retry,
                    sort_key=sort_key,
                    ordered=ordered,
                    provided=() if self.settings is None else (self.settings.cls,),
                    fix_commands=fixes,
                    refreshes_auth=refreshes_auth,
                    requires=requires,
                    option_placement=OptionPlacement(option_placement),
                    introduced_in=added,
                    deprecated=deprecated,
                )
            )
            return fn

        return register

    def _register(self, command: Command) -> None:
        path = command.path
        taken = framework_collisions(command)
        if taken:
            raise RegistrationError(
                f"{path}: flags {taken} are supplied by the framework for this command, or "
                "reserved for it (REQ-F-079), and would never reach the handler; rename the fields"
            )
        if path in self._commands:
            raise RegistrationError(f"{path} is already registered")
        if path in self._groups:
            raise RegistrationError(f"{path} is already a group")
        if any(
            r == path or r.is_ancestor_of(path) or path.is_ancestor_of(r) for r in self._redirects
        ):
            raise RegistrationError(f"{path} overlaps a redirected path; it answers exit 13")
        self._check_nesting(path)
        for name in command.exit_codes:
            if name not in self.exits:
                raise RegistrationError(f"{path}: exit code {name} is not registered")
        if command.retry is not None:
            exhausted = ExitCodeName(command.retry.exhausted)
            implicit = {ExitCodeName(c.name) for c in implicit_exit_codes(command)}
            if exhausted not in command.exit_codes and exhausted not in implicit:
                raise RegistrationError(
                    f"{path}: Retry(exhausted={exhausted.value!r}) is not an exit code of the "
                    f"command; add {exhausted.value!r} to exit_codes"
                )
        if command.refreshes_auth:
            taken = [str(p) for p, c in self._commands.items() if c.refreshes_auth]
            if taken:
                raise RegistrationError(
                    f"{path}: refreshes_auth=True is already on {taken[0]}; one command renews "
                    "credentials"
                )
            if command.danger_level is DangerLevel.DESTRUCTIVE:
                raise RegistrationError(f"{path}: a refresh command cannot be destructive")
        self._commands[path] = command
        self._fixes_checked = False

    def redirect(
        self, old: str, *, to: str, reason: str = "renamed", permanent: bool = True
    ) -> None:
        """Answer the retired path ``old`` with exit 13 ``REDIRECTED`` and
        ``error.redirect.command``, the same invocation under ``to``, verbatim; ``to``
        lists ``old`` in its manifest ``aliases``. ``reason`` is ``renamed``,
        ``restructured``, ``deprecated``, or ``typo_corrected``; ``permanent=False``
        tells an agent not to remember the mapping."""
        source, target = CommandPath(old), CommandPath(to)
        if reason not in RedirectReason:
            reasons = ", ".join(RedirectReason)
            raise RegistrationError(f"redirect {source}: reason={reason!r} is not one of {reasons}")
        if not isinstance(permanent, bool):
            raise RegistrationError(f"redirect {source}: permanent is True or False")
        command = self._commands.get(target)
        if command is None:
            raise RegistrationError(
                f"redirect {source}: {target} is not a registered command; register it first"
            )
        # A group may hold the old path; a command above it would take it as arguments
        live = [
            str(p)
            for p in (*self._commands, *self._groups)
            if p == source
            or source.is_ancestor_of(p)
            or (p in self._commands and p.is_ancestor_of(source))
        ]
        if live or source in self._redirects:
            raise RegistrationError(
                f"redirect {source}: the path is still in use ({', '.join(live) or 'redirect'})"
            )
        self._redirects[source] = Moved(target, RedirectReason(reason), permanent)
        self._commands[target] = dataclasses.replace(command, aliases=(*command.aliases, source))

    def moved(self, words: Sequence[str]) -> tuple[CommandPath, Moved, tuple[str, ...]] | None:
        """The redirect whose old path starts ``words``, with the words after it"""
        for source, moved in self._redirects.items():
            n = len(source.parts)
            if tuple(words[:n]) == source.parts:
                return source, moved, tuple(words[n:])
        return None

    def check_fixes(self) -> None:
        """Every declared ``fix_commands`` value names a command that exists and is not
        destructive, and every ``Deprecated(replacement=)`` a command; run once the table
        is in use, since a target may register late"""
        if self._fixes_checked:
            return
        for path, command in self._commands.items():
            old = command.deprecated
            if old is not None and old.replacement is not None:
                if CommandPath(old.replacement) not in self._commands:
                    raise RegistrationError(
                        f"{path}: Deprecated(replacement={old.replacement!r}) is not a "
                        "registered command"
                    )
            for error_code, fix in command.fix_commands.items():
                problem = self.fix_problem(fix)
                if problem is not None:
                    raise RegistrationError(f"{path}: fix_commands[{error_code!r}]: {problem}")
        self._fixes_checked = True

    def fix_problem(self, fix: object) -> str | None:
        """Why ``fix`` cannot be an ``error.fix_command`` of this app, or None"""
        return fix_problem(
            fix, app_name=self.name, companions=self.companions, commands=self._commands
        )

    def _check_nesting(self, path: CommandPath) -> None:
        """A command is a leaf: nothing may sit under it, and it may not sit under another
        command, or `tool db migrate` would mean both 'db with argument migrate' and a group"""
        for command in self._commands:
            if command == path:
                continue
            if command.is_ancestor_of(path) or path.is_ancestor_of(command):
                raise RegistrationError(
                    f"{path} and {command} overlap: a command cannot also be a group"
                )

    def _register_builtins(self, enable_exec: bool) -> None:
        @self.command(
            MANIFEST_PATH.value,
            description="Print the command manifest for agents",
            danger_level="safe",
            exit_codes=(),
            ordered=True,  # positionals and enum values are in declaration order
        )
        def manifest(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            return self.manifest()

        @self.command(
            VERSION_PATH.value,
            description="Print the tool name and version",
            danger_level="safe",
            exit_codes=(),
        )
        def version(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {"name": self.name, "version": self.version}

        if self.init is not None:
            setup = self.init

            @self.command(
                INIT_COMMAND,
                description="Set up what the tool needs before first use; safe to repeat",
                danger_level="mutating",
                exit_codes=(),
                examples=[("Set up once", f"{self.name} init")],
            )
            def init(args: NoArgs, ctx: Ctx) -> Initialized:
                return run_init(setup, self.name, ctx)

        if self.credentials is not None:
            self._register_check_permissions(self.credentials)
        if self.jobs is not None:
            self._register_jobs(self.jobs)

        if enable_exec:

            @self.command(
                EXEC_PATH.value,
                description="Dispatch JSONL DispatchRequest lines from stdin in-process",
                danger_level="safe",
                exit_codes=(),
                examples=[("Run a plan", f"cat ops.jsonl | {self.name} exec --ignore-errors")],
            )
            def exec_(args: ExecArgs, ctx: Ctx) -> None:
                raise RegistrationError("exec is dispatched by the framework, not called directly")

    def _register_check_permissions(self, credentials: Credentials) -> None:
        @self.command(
            CHECK_PERMISSIONS_PATH.value,
            description="Compare the active credential's scopes with what commands require",
            danger_level="safe",
            exit_codes=("AUTH_REQUIRED", "NOT_FOUND"),
            examples=[("Check one command", f"{self.name} check-permissions --for manifest")],
        )
        def check_permissions(args: CheckPermissionsArgs, ctx: Ctx) -> dict[str, object]:
            # REQ-O-047: exit 0 unless no one is logged in or --for lacks a scope
            active = scope_set(credentials.active_scopes(ctx))
            if active is None:
                raise not_logged_in(CHECK_PERMISSIONS_PATH.value, self._logins())
            if isinstance(active, Expired):
                raise expired(CHECK_PERMISSIONS_PATH.value, active, self._refresh())
            if args.for_ is None:
                gated = sorted(
                    (
                        (p.value, Coverage(c.required_scopes, active))
                        for p, c in self._commands.items()
                        if c.requires_auth
                    ),
                    key=lambda item: item[0],
                )
                over = [path for path, cover in gated if cover.over_privileged]
                if over:
                    ctx.warn(
                        OVER_PRIVILEGED,
                        f"Credential is over-privileged for {len(over)} of the commands",
                        commands=over,
                    )
                return {
                    "active_scopes": names(active),
                    "commands": {
                        path: {
                            "required_scopes": [s.value for s in cover.required],
                            "covered": not cover.missing,
                            "over_privileged": cover.over_privileged,
                        }
                        for path, cover in gated
                    },
                }
            target = self._command_named(args.for_)
            coverage = Coverage(target.required_scopes if target.requires_auth else (), active)
            if coverage.missing:
                raise insufficient(
                    target.path.value, coverage, "AUTH_REQUIRED", "INSUFFICIENT_SCOPES"
                )
            flagged = target.requires_auth and coverage.over_privileged
            if flagged:
                self._warn_excess(ctx, target, coverage)
            report = coverage.report(target.path.value)
            report["over_privileged"] = flagged
            return report

    def _register_jobs(self, jobs: JobStore) -> None:
        """``job status`` and ``job cancel`` (REQ-C-022); the store is app code"""
        group = self.group("job", description="Check on or cancel async jobs")

        def found(job: Job | None, job_id: str) -> Job:
            if job is None:
                raise CliExit(
                    ExitCodeName("NOT_FOUND"),
                    f"no job {job_id!r}",
                    code="JOB_NOT_FOUND",
                    context={"job_id": job_id},
                )
            if not isinstance(job, Job):
                raise TypeError(f"the JobStore returned {type(job).__name__}, not treaty.Job")
            return job

        @group.command(
            "status",
            description="Show an async job: exit 0 complete, 3 running, 4 failed, 5 unknown",
            danger_level="safe",
            exit_codes=("PARTIAL_FAILURE", "PRECONDITION", "NOT_FOUND"),
        )
        def status(args: JobArgs, ctx: Ctx) -> Job:
            job = found(jobs.status(args.job_id, ctx), args.job_id)
            if job.status == "running":
                raise CliExit(
                    ExitCodeName("PARTIAL_FAILURE"),
                    f"job {job.job_id} is still running",
                    code="JOB_RUNNING",
                    context={"job_id": job.job_id, "poll_interval_ms": job.poll_interval_ms},
                    suggestion=f"poll again in {job.poll_interval_ms} ms",
                    data=job,
                )
            if job.status != "complete":
                raise CliExit(
                    ExitCodeName("PRECONDITION"),
                    f"job {job.job_id} is {job.status}",
                    code="JOB_FAILED" if job.status == "failed" else "JOB_CANCELLED",
                    context={"job_id": job.job_id, "status": job.status},
                    data=job,
                )
            return job

        @group.command(
            "cancel",
            description="Ask an async job to stop and show it",
            danger_level="mutating",
            exit_codes=("NOT_FOUND",),
        )
        def cancel(args: JobArgs, ctx: Ctx) -> Job:
            job = found(jobs.cancel(args.job_id, ctx), args.job_id)
            return job if job.effect is not None else dataclasses.replace(job, effect="updated")

    def _command_named(self, name: str) -> Command:
        """``deploy rollback`` as typed, or ``deploy.rollback`` as the manifest keys it"""
        try:
            path = CommandPath(".".join(name.split()))
        except InvalidValue:
            path = None
        command = None if path is None else self._commands.get(path)
        if command is None:
            raise CliExit(
                ExitCodeName("NOT_FOUND"),
                f"no command {name!r} to check",
                code="UNKNOWN_COMMAND",
                context={"for": name, "available": sorted(p.value for p in self._commands)},
                fix_required="pass --for one of the available commands",
            )
        return command

    def _logins(self) -> list[str]:
        """How to log in, for the fix of AUTH_REQUIRED"""
        return sorted(
            f"{self.name} {' '.join(p.parts)}" for p, c in self._commands.items() if c.auth
        )

    def _refresh(self) -> str | None:
        """The command that renews expired credentials, as an agent types it"""
        for path, command in self._commands.items():
            if command.refreshes_auth:
                return shlex.join([self.name, *path.parts])
        return None

    @staticmethod
    def _warn_excess(ctx: Ctx, command: Command, coverage: Coverage) -> None:
        ctx.warn(
            OVER_PRIVILEGED,
            f"Credential has scopes beyond what {command.path} requires",
            command=command.path.value,
            excess_scopes=coverage.excess,
            required_scopes=[s.value for s in coverage.required],
        )

    def _gate(self, command: Command, ctx: Ctx) -> None:
        """REQ-C-029: before a ``requires_auth`` handler, the credential holds its scopes"""
        assert self.credentials is not None  # checked at registration
        active = scope_set(self.credentials.active_scopes(ctx))
        if active is None:
            raise not_logged_in(command.path.value, self._logins())
        if isinstance(active, Expired):
            raise expired(command.path.value, active, self._refresh())
        coverage = Coverage(command.required_scopes, active)
        if coverage.missing:
            raise insufficient(
                command.path.value, coverage, "PERMISSION_DENIED", "PERMISSION_DENIED"
            )
        if coverage.over_privileged:
            self._warn_excess(ctx, command, coverage)  # REQ-O-047

    def _invocations(self, prefix: tuple[str, ...]) -> list[str]:
        """Commands as the agent must type them, scoped to the prefix it was already under

        Registry keys are dot paths (``deployments.list``); an agent that reads them in an
        error copies them literally, so errors show ``democli deployments list`` instead.
        """
        paths = [p for p in self._commands if p.parts[: len(prefix)] == prefix]
        if not paths:
            paths = list(self._commands)
        return sorted(f"{self.name} {' '.join(p.parts)}" for p in paths)

    def _misplaced_flag(self, route: Route) -> ParseError:
        """A flag given before the command path: command flags are parsed only after it"""
        flag = without_value(route.tokens[0])
        target = misplaced_flag_target(route, self._commands)
        command = (
            f"{self.name} {' '.join(target.parts)}"
            if target is not None
            else f"{self.name} {' '.join((*route.prefix, '<command>'))}"
        )
        context: dict[str, object] = {"flag": flag, "prefix": ".".join(route.prefix)}
        if target is not None:
            context["command"] = command
        else:
            context["available"] = self._invocations(route.prefix)
        return ParseError(
            f"flag {flag!r} must come after the command path",
            context=context,
            suggestion=f"flags go after the command: {command} [arguments] {flag}",
        )

    @property
    def builtins(self) -> frozenset[CommandPath]:
        """The commands treaty registered itself, such as ``manifest`` and ``init``"""
        return self._builtins

    @property
    def commands(self) -> Mapping[CommandPath, Command]:
        return self._commands

    def manifest(self) -> dict[str, object]:
        self.check_fixes()
        return build_manifest(self._commands, self.exits, self.version, self.formats, self.name)

    def environment(self) -> list[tuple[str, str]]:
        """Every variable the app reads, by its prefixed name, with what it sets
        (REQ-F-073): the framework's own, the settings, then each secret flag's default"""
        rows = [(app_var(self.name, v.key), v.description) for v in KNOWN]
        if self.settings is not None:
            rows += [
                (app_var(self.name, f.name), f"Setting {f.name}, over the config files")
                for f in self.settings.fields
            ]
        secrets = {
            var: f"Default of --{field.replace('_', '-')} of {path}"
            for path, c in sorted(self._commands.items(), key=lambda kv: kv[0].value)
            for field, var in c.secret_env_vars.items()
        }
        return rows + sorted(secrets.items())

    def effective_timeout(self, command: Command, override: Timeout | None) -> Timeout:
        if override is not None:
            return override
        if command.timeout is not None:
            return command.timeout
        return self.default_timeout

    # Execution

    def call(
        self,
        path: str,
        arguments: Mapping[str, object],
        *,
        env: Mapping[str, str] | None = None,
    ) -> Envelope:
        """Run one command in-process from JSON values, as an ``exec`` line would

        Field names use underscores; the framework keys ``confirm_destructive``,
        ``idempotency_key``, ``timeout``, and ``dry_run`` are accepted where the command
        declares them. A streaming command returns its buffered envelope, capped like
        stdout (REQ-F-052). Nothing is written to stdout: the caller owns the envelope;
        handler tracebacks go to stderr. Used by
        the MCP adapter.
        """
        environ = env if env is not None else os.environ
        self.check_fixes()
        # Tracebacks of crashed or late handlers go to the host process's stderr
        run = _Run(self, io.StringIO(), sys.stderr, environ)
        try:
            cap = OutputCap.resolve(None, environ, self.max_output, self.name)
        except ParseError as exc:
            return run.arg_error(exc, meta={"_cmd": path})
        try:
            # No argv here: <APP>_CONFIG, <APP>_CONTEXT, and <APP>_INSTANCE_ID stand in
            run.load_settings(config_options(self.name, environ))
        except ParseError as exc:
            if path not in {p.value for p in PURE_PATHS}:  # REQ-F-068
                return run.arg_error(exc, meta={"_cmd": path})
        envelope = self._call(run, path, arguments, environ)
        return cap_envelope(envelope, cap, Rerun(argv=None, page=run.page))

    def _call(
        self, run: _Run, path: str, arguments: Mapping[str, object], environ: Mapping[str, str]
    ) -> Envelope:
        meta: dict[str, object] = {"_cmd": path}
        if run.trace_error is not None:
            return run.arg_error(run.trace_error, meta=meta)
        try:
            command_path = CommandPath(path)
        except InvalidValue as exc:
            return run.arg_error(ParseError(str(exc), context={"_cmd": path}), meta=meta)
        command = self._commands.get(command_path)
        if command is None and (found := self.moved(command_path.parts)) is not None:
            source, moved, _ = found
            return run.redirected(source, moved, moved.to.value, meta=meta)
        if command is None or command_path == EXEC_PATH:
            available = sorted(p.value for p in self._commands if p != EXEC_PATH)
            return run.arg_error(
                ParseError(
                    f"unknown command {path}",
                    context={"_cmd": path, "available": available},
                ),
                code="UNKNOWN_COMMAND",
                meta=meta,
            )
        run.current = command
        try:
            invocation = build_from_mapping(command, arguments, environ)
        except ParseError as exc:
            return run.arg_error(exc, meta={**meta, **_mode_meta(command)})
        except ArgsCrashed as exc:
            return run.args_crashed(command, exc, meta=meta)
        if command.streaming:
            # Buffered in-process, a stream returns only when it ends, so it always gets a
            # deadline: the caller's, else the app default, even over a command's None
            if invocation.timeout is not None and invocation.timeout.seconds is None:
                return run.arg_error(
                    ParseError(
                        "a buffered stream needs a finite timeout; 0 would never return",
                        context={"field": "timeout", "_cmd": path},
                        suggestion="pass a timeout in seconds, or omit it for the app default",
                    ),
                    meta=meta,
                )
            if invocation.timeout is None and self.effective_timeout(command, None).seconds is None:
                invocation = dataclasses.replace(invocation, timeout=self.default_timeout)
            return buffer_stream(
                run.stream(command, invocation, Format.JSON, meta=meta, whole=True)
            )
        return run.execute(command, invocation, Format.JSON, meta=meta)

    def main(self) -> NoReturn:
        # A standard stream whose descriptor was closed at startup is None
        stdout, stdin = sys.stdout, sys.stdin
        # Only the console entry point changes os.environ, which every child inherits;
        # run() callers such as tests and embedders pass their own env
        quiet_children(
            os.environ,
            stdout_isatty=stdout is not None and stdout.isatty(),
            stdin_isatty=stdin is not None and stdin.isatty(),
        )
        # REQ-F-053: every line reaches a pipe reader as it is written, here and in children
        os.environ["PYTHONUNBUFFERED"] = "1"
        if stdout is None or sys.stderr is None:
            for stream in (stdout, sys.stderr):
                if isinstance(stream, io.TextIOWrapper):
                    stream.reconfigure(newline="\n")
            sys.exit(self.run(sys.argv[1:]))
        # REQ-F-006 below Python: for the run, descriptor 1 is stderr, so a child or C code
        # writing to it cannot corrupt the envelope, which goes to a copy of the original
        stdout.flush()
        saved = os.dup(1)
        os.dup2(sys.stderr.fileno(), 1)
        # REQ-F-072: LF on every platform; Windows text mode would write CRLF
        envelopes = open(  # noqa: SIM115 - closed below, after descriptor 1 is restored
            saved, "w", encoding=stdout.encoding, errors=stdout.errors, buffering=1, newline="\n"
        )
        if isinstance(sys.stderr, io.TextIOWrapper):
            sys.stderr.reconfigure(newline="\n")
        sys.stdout = envelopes
        try:
            code = self.run(sys.argv[1:])
        finally:
            sys.stdout = stdout
            envelopes.flush()
            os.dup2(saved, 1)
            envelopes.close()
        sys.exit(code)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
        env: Mapping[str, str] | None = None,
        isatty: bool | None = None,
    ) -> int:
        self.check_fixes()
        # A closed descriptor leaves its sys stream None: then only the exit code answers,
        # and stdin reads as empty
        out = stdout if stdout is not None else sys.stdout or io.StringIO()
        err = stderr if stderr is not None else sys.stderr or io.StringIO()
        inp = stdin if stdin is not None else sys.stdin
        environ = env if env is not None else os.environ
        tty = out.isatty() if isatty is None else isatty
        run = _Run(self, out, err, environ, tty=tty, stdin=inp)
        run.argv = (self.name, *argv)
        if inp is None:
            run.no_payload = "stdin is closed, so there is no payload to read"
        try:
            with run.guard_streams():
                return self._route(run, list(argv), environ)
        except OSError as exc:
            if not _closed_pipe(exc):
                raise
            # The reader of stdout went away, on any path: help, schema, errors, results
            return run.output_closed()

    def _route(
        self,
        run: _Run,
        argv: list[str],
        environ: Mapping[str, str],
    ) -> int:
        out = run.out
        try:
            globals_, rest = split_globals(strict_argv(argv, self._commands))
            mode = resolve_mode(globals_.format, environ, run.tty, self.formats, self.name)
            requested = mode
            if mode is Format.JSONL:
                mode = Format.JSON  # every JSON envelope is already one compact line
            run.cap = OutputCap.resolve(globals_.max_output, environ, self.max_output, self.name)
        except ParseError as exc:
            return run.emit(Format.JSON, run.arg_error(exc))
        run.stable = run.stable_all = globals_.stable_output
        if run.trace_error is not None:
            return run.emit(mode, run.arg_error(run.trace_error))
        # REQ-F-068: help, version, and the schema answer even when a config layer is
        # invalid, so a bad file is reported only by what reads the settings
        config_error: ParseError | None = None
        try:
            run.load_settings(
                config_options(
                    self.name,
                    environ,
                    config=globals_.config,
                    context=globals_.context,
                    no_config=globals_.no_config,
                    instance_id=globals_.instance_id,
                )
            )
        except ParseError as exc:
            config_error = exc
        if globals_.show_config:
            if config_error is not None:
                return run.emit(mode, run.arg_error(config_error))
            return run.show_config(mode)
        route = resolve_path(rest, self._commands)
        if route.path is None and not route.prefix and route.tokens == ("--version",):
            # Root-only alias so a command's own --version flag is never shadowed
            route = Route(path=VERSION_PATH, prefix=VERSION_PATH.parts, tokens=())
        if route.path is None and route.tokens and (found := self.moved(rest)) is not None:
            source, moved, remaining = found
            replacement = shlex.join([self.name, *moved.to.parts, *remaining])
            return run.emit(mode, run.redirected(source, moved, replacement))
        if route.path is None and route.tokens:
            # An unroutable path is an error even with --help or --schema, which would
            # otherwise answer with exit 0 about the enclosing group
            if route.tokens[0].startswith("-") and format_hint(route.tokens[0]) is None:
                return run.emit(mode, run.arg_error(self._misplaced_flag(route)))
            return run.emit(
                mode,
                run.arg_error(
                    ParseError(
                        f"unknown command {without_value(route.tokens[0])!r}",
                        context={
                            "argument": without_value(route.tokens[0]),
                            "prefix": ".".join(route.prefix),
                            "available": self._invocations(route.prefix),
                        },
                        suggestion=format_hint(route.tokens[0]),
                    )
                ),
            )
        command = None if route.path is None else self._commands[route.path]
        run.current = command
        pinned: SchemaVersion | None = None
        if command is not None and globals_.schema_version is not None:
            try:
                pinned = command.pin(globals_.schema_version)
            except ParseError as exc:
                return run.emit(mode, run.arg_error(exc, meta=_mode_meta(command)))
        if globals_.output_schema:
            return run.output_schema(mode, command, pinned)
        if globals_.schema:
            return run.schema(mode, route.path, route.prefix)
        if command is None:
            return run.help_root(mode, route.prefix)
        if globals_.help:
            return run.help_command(mode, command)
        if config_error is not None and command.path not in PURE_PATHS:
            return run.emit(mode, run.arg_error(config_error, meta=_mode_meta(command)))
        try:
            invocation = parse_command_args(
                command, route.tokens, environ, read_stdin=run.stdin_value
            )
        except ParseError as exc:
            return run.emit(mode, run.arg_error(exc, meta=_mode_meta(command)))
        except ArgsCrashed as exc:
            return run.emit(mode, run.args_crashed(command, exc))
        if pinned is not None:
            invocation = dataclasses.replace(invocation, schema_version=pinned)
        with cancellation_handlers(out) as cancellation:
            run.cancellation = cancellation
            if command.path == EXEC_PATH:
                assert isinstance(invocation.args, ExecArgs)
                run.argv = None  # a line's hint cannot rerun the whole plan
                return run.exec(invocation.args)
            render = self.renderer(command, mode)
            if command.streaming:
                if invocation.no_stream:
                    envelopes = run.stream(command, invocation, mode, whole=True)
                    return run.emit(mode, buffer_stream(envelopes), render=_each(render))
                envelopes = run.stream(command, invocation, mode)
                run.in_flight = command
                return run.emit_stream(mode, envelopes, render=render)
            envelope = run.execute(command, invocation, mode)
            if invocation.output is not None:
                # REQ-O-001: the file gets the representation; stdout gets the envelope
                written = run.to_file(invocation.output, requested, envelope, render)
                return run.emit(Format.JSON, written)
            return run.emit(mode, envelope, render=render)


# Runs that swapped sys.stdout and sys.stdin now, and the streams from before the first:
# with runs on several threads, or an abandoned handler's run, the last one out restores
_guard_lock = threading.Lock()
_guarded = 0
_unguarded: tuple[TextIO, TextIO] = (sys.stdout, sys.stdin)


@dataclass(frozen=True, slots=True)
class Moved:
    """Where ``App.redirect`` sends a retired path"""

    to: CommandPath
    reason: RedirectReason
    permanent: bool


def _invoke(
    app: App, command: Command, args: object, ctx: Ctx, provided: Mapping[type, object]
) -> object:
    """Check the credential, acquire the handler's resources, each once and in dependency
    order, then run it; ``active_scopes`` is app code, so it runs where the handler does.
    ``provided`` holds what the run already has, such as the settings."""
    if command.requires_auth:
        app._gate(command, ctx)
    if app.init is not None and command.path not in app.builtins and not app.init.initialized(ctx):
        raise init_required(app.name)
    resolver = Resolver(command.resource_graph, args, ctx, provided)
    result = command.handler(args, ctx, *resolver.all(command.resources))
    if inspect.isawaitable(result):
        # A decorated coroutine that registration could not see (REQ-F-049): its body
        # never ran, which a success envelope would hide
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError(
            f"handler {command.handler.__qualname__} returned an awaitable, which treaty never "
            "awaits; make it a plain def (REQ-F-049)"
        )
    return result


# A plan line that never became a command: a plan of only these exits 2
_UNREAD_LINE = frozenset({"DISPATCH_PARSE_ERROR", "INVALID_JSON"})

# ErrorDetail.code in response-envelope.json
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]+")

# Shorter values would redact every digit or letter they share with a traceback
MIN_REDACTED = 4

# REQ-F-051: ctx.log field names whose values are credentials: API_KEY, *_TOKEN, DB_PASS,
# Authorization, Cookie, X-Api-Key, AUTH_URL, ...
_SECRET_KEY = re.compile(
    r"token|secret|password|key|credential|auth|cookie|(^|[_-])pass($|[_-])|^api([_-]|$)",
    re.IGNORECASE,
)


def _redacted(value: object, redact: Callable[[str], str]) -> object:
    """Every string of a JSON value with the run's secrets replaced"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, redact) for v in value]
    return value


def _scrub(key: str, value: object, redact: Callable[[str], str]) -> object:
    """A ``ctx.log`` field as JSON values, with credentials replaced at any depth"""
    if _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _scrub(k, v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub("", v, redact) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


class _StrayStdout(io.TextIOBase):
    """Stands in for ``sys.stdout`` during a run: what a handler or a library prints goes
    to stderr, and the next envelope reports how much (REQ-F-006)"""

    def __init__(self, err: _Stderr) -> None:
        super().__init__()
        self._err = err
        self._bytes = 0

    def writable(self) -> bool:
        return True

    @property
    def buffer(self) -> Any:
        """Bytes written here reach stderr too, uncounted"""
        return getattr(self._err.stream, "buffer")  # noqa: B009 - IO[str] does not declare it

    @property
    def encoding(self) -> Any:  # type: ignore[override]  # read-only, like a real stream's
        return getattr(self._err.stream, "encoding", None) or "utf-8"

    def write(self, text: str, /) -> int:
        self._err.write(text)
        self._bytes += len(text.encode("utf-8", "surrogatepass"))
        return len(text)

    def flush(self) -> None:
        self._err.flush()

    def take(self) -> int:
        """Bytes written since the last call"""
        written, self._bytes = self._bytes, 0
        return written


def _text(exc: BaseException) -> str:
    """``str(exc)``, even for an exception whose own ``__str__`` raises"""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - __str__ is user code
        return f"<{type(exc).__name__} whose str() failed>"


def _traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def _closed_pipe(exc: OSError) -> bool:
    """Whether a write failed because its reader went away; Windows reports that as EINVAL"""
    return isinstance(exc, BrokenPipeError) or (
        sys.platform == "win32" and exc.errno == errno.EINVAL
    )


class _Stderr:
    """Diagnostics with no reader left are dropped: a closed stderr must neither cost the
    stdout envelope nor pass for a closed stdout"""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    @property
    def stream(self) -> IO[str]:
        return self._stream

    def write(self, text: str) -> None:
        try:
            self._stream.write(text)
        except OSError as exc:
            if not _closed_pipe(exc):
                raise
            self._closed()

    def flush(self) -> None:
        try:
            self._stream.flush()
        except OSError as exc:
            if not _closed_pipe(exc):
                raise
            self._closed()

    def _closed(self) -> None:
        if self._stream is sys.stderr:
            # So the interpreter's flush at exit does not raise on it either
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stderr.fileno())
            os.close(devnull)


def _warned(envelope: Envelope, code: str, message: str, command: Command) -> Envelope:
    warning = WarningDetail(code, message, context={"command": command.path.value})
    return dataclasses.replace(envelope, warnings=(*envelope.warnings, warning))


_BUILT_IN: Mapping[Format, Renderer] = {Format.TSV: table("\t")}


def _json_text(data: Any) -> str:
    """Machine output in a text mode: the data alone, indented, without the envelope"""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _check_renderer(where: str, mode: object, render: object) -> None:
    if not isinstance(mode, Format):
        raise RegistrationError(f"{where}: {mode!r} is not a Format member")
    if mode in (Format.JSON, Format.JSONL):
        raise RegistrationError(f"{where}: {mode} is the response envelope and takes no renderer")
    if not callable(render):
        raise RegistrationError(f"{where}: the {mode} renderer is not callable")


def _each(render: Renderer | None) -> Renderer | None:
    """A renderer takes one event; --no-stream data is the list of them"""
    if render is None:
        return None
    return lambda events: "".join(render(e) for e in events)


def _still_running(running: Sequence[Pending]) -> Pending | None:
    """The handler's worker when an interrupted wait left it running"""
    return next((p for p in running if p.worker.is_alive()), None)


def _mode_meta(command: Command) -> dict[str, object]:
    """``meta.dry_run`` on a safe_default command's argument error: nothing was applied"""
    return {"dry_run": True} if command.safe_default else {}


def _previewing(command: Command, invocation: Invocation) -> bool:
    """A destructive command run without --confirm-destructive or --dry-run"""
    return command.danger_level is DangerLevel.DESTRUCTIVE and not (
        invocation.confirmed or _dry_run_requested(invocation.args)
    )


def _dry_run_requested(args: object) -> bool:
    """True when a destructive command's args carry dry_run=True (field guaranteed by REQ-C-004)"""
    value = getattr(args, "dry_run", False)
    return isinstance(value, bool) and value


_END = object()


def drain(envelopes: Generator[Envelope]) -> Iterator[Envelope]:
    """Iterate a stream; a signal that lands between events is thrown back into it

    The stream generator turns the signal into its CANCELLED envelope, so the caller
    sees the same terminal line whether the signal arrived inside the handler or not.
    """
    try:
        yield from envelopes
    except (Cancelled, KeyboardInterrupt) as exc:
        yield envelopes.throw(exc)


def buffer_stream(envelopes: Generator[Envelope]) -> Envelope:
    """``--no-stream``: every event in ``data`` under one envelope; a failure keeps its events"""
    events: list[object] = []
    last: Envelope | None = None
    for last in drain(envelopes):
        if last.ok and not last.extra_meta.get("end"):
            events.append(last.data)
    assert last is not None, "a stream always ends with a terminal envelope"
    meta = {k: v for k, v in last.extra_meta.items() if k not in ("seq", "end")}
    return dataclasses.replace(last, data=events, extra_meta={**meta, "total": len(events)})


class _Run:
    """One process invocation: builds envelopes, writes them, tracks timing"""

    def __init__(
        self,
        app: App,
        out: IO[str],
        err: IO[str],
        env: Mapping[str, str],
        *,
        tty: bool = False,
        stdin: IO[str] | None = None,
    ) -> None:
        self.app = app
        self.out = out
        self.err = _Stderr(err)
        self.env = env
        self.tty = tty
        """Whether stdout is a terminal"""
        self.stdin = stdin if stdin is not None else io.StringIO()
        """What ``ctx.prompt`` reads; ``App.call`` has none"""
        self.interactive = tty and stdin is not None and stdin.isatty()
        """Whether a person can answer: stdin and stdout are both terminals"""
        self.headless = is_headless(env, interactive=self.interactive, platform=sys.platform)
        self.processes: Processes | None = None
        """The children of the handler that runs now, stopped on a signal or timeout"""
        self.stray: _StrayStdout | None = None
        self.started = time.perf_counter()
        self.request_id = uuid.uuid4().hex[:12]
        self.cap = app.max_output
        self.cancellation = Cancellation()
        self.in_flight: Command | None = None
        """A streaming command whose events are still being written"""
        self.abandoned: Pending | None = None
        """Set by ``_execute`` when the handler outlived its timeout and still runs"""
        self.page: tuple[CommandPath, Position] | None = None
        """Where the list page ``_execute`` last answered started, to resume after a cut"""
        self.argv: tuple[str, ...] | None = None
        """The invocation as typed, app name first; None where it cannot be rerun (exec)"""
        self.delivered = False
        """Whether a complete envelope, event, or rendered result was written and flushed"""
        self.payload_stdin: IO[str] | None = stdin
        """Where a ``stdin_input`` command reads its payload; None in ``App.call``"""
        self.no_payload = "stdin carries the plan or the call here, not a payload"
        """Why ``payload_stdin`` is None"""
        self.warnings: list[WarningDetail] = []
        """``ctx.warn`` entries of the command that runs now, added to its envelopes"""
        self.token: str | None = None
        """A login command's token, redacted wherever a secret argument is"""
        self.config_file: ConfigFile | None = None
        """The config file the command that runs now may write"""
        self.settings: Resolved = EMPTY_SETTINGS
        """The run's settings and their sources, read once before routing (REQ-F-028)"""
        self.timestamp = utc_timestamp()
        self.cwd = logical_cwd(env)
        self.trace_id: str | None = None
        self.trace_error: ParseError | None = None
        """An unusable ``TOOL_TRACE_ID``, answered with exit 2 before anything runs"""
        try:
            self.trace_id = read_trace_id(env)
        except ParseError as exc:
            self.trace_error = exc
        self.current: Command | None = None
        """The command being answered, for ``meta.command`` and ``meta.schema_version``"""
        self.pinned: SchemaVersion | None = None
        """The older schema version ``--schema-version`` selected for the current command"""
        self.retrier: Retrier | None = None
        """``ctx.retry`` of the current command, whose count is ``meta.retries``"""
        self.stable_all = False
        """``--stable-output`` on argv: every envelope of the run is stable (REQ-O-007)"""
        self.stable = False
        """The current envelope leaves out what differs between identical calls"""
        self._roots: dict[tuple[str, ...], Path | None] = {}

    @contextlib.contextmanager
    def guard_streams(self) -> Iterator[None]:
        """Point ``sys.stdout`` at stderr for the run, and, off a terminal, ``sys.stdin`` at
        a reader that refuses ``input()`` (REQ-F-047). Process-wide, not a context-local
        redirect, because handlers run on worker threads; one run owns the process."""
        global _guarded, _unguarded
        self.stray = _StrayStdout(self.err)
        with _guard_lock:
            if not _guarded:
                _unguarded = (sys.stdout, sys.stdin)
            _guarded += 1
            saved = (sys.stdout, sys.stdin)
            ours = (
                cast(TextIO, self.stray),
                sys.stdin if self.interactive else cast(TextIO, NoPromptStdin(self.stdin)),
            )
            sys.stdout, sys.stdin = ours
        try:
            yield
        finally:
            with _guard_lock:
                _guarded -= 1
                if not _guarded:
                    sys.stdout, sys.stdin = _unguarded
                elif sys.stdout is ours[0]:
                    # A run nested in another's handler: give back what it found. A run
                    # that another swapped over leaves the streams to the last one out.
                    sys.stdout, sys.stdin = saved

    def _write(self, envelope: Envelope) -> None:
        """One JSON envelope on stdout, warning when text was printed there since the last"""
        written = 0 if self.stray is None else self.stray.take()
        if written:
            warning = WarningDetail(
                "THIRD_PARTY_STDOUT",
                "Third-party code wrote to stdout; the text went to stderr",
                context={"bytes": written},
            )
            envelope = dataclasses.replace(envelope, warnings=(*envelope.warnings, warning))
        write_envelope(cap_envelope(envelope, self.cap, Rerun(self.argv, self.page)), self.out)
        self.delivered = True

    def _ctx(
        self,
        command: Command,
        args: object,
        mode: Format,
        timeout: Timeout,
        idempotency_key: str | None = None,
        *,
        invocation: Invocation,
        page: PageRequest | None = None,
    ) -> Ctx:
        deadline = None if timeout.seconds is None else time.monotonic() + timeout.seconds
        # Output is captured, so a child never colors; editors only for a person
        settings = child_settings(color=False, interactive=self.interactive)
        # REQ-O-033: --headless opens no browser even where one could be shown
        headless = self.headless or invocation.headless
        # The run's env holds TOOL_TRACE_ID, so every child inherits it (REQ-F-025)
        self.processes = Processes(
            {**self.env, **settings},
            deadline=deadline,
            headless=headless,
            browser_open=BROWSER_OPEN in command.gui_operations,
        )
        policy = command.retry
        self.retrier = (
            None
            if policy is None
            else Retrier(
                policy,
                retries=policy.retries if invocation.retries is None else invocation.retries,
                delay_ms=policy.delay_ms
                if invocation.retry_delay_ms is None
                else invocation.retry_delay_ms,
                deadline=deadline,
            )
        )
        return Ctx(
            app_name=self.app.name,
            version=self.app.version,
            mode=mode,
            request_id=self.request_id,
            env=self.env,
            state=self.app._state,
            timeout=timeout,
            color=mode is not Format.JSON and color_allowed(self.env, self.tty),
            headless=headless,
            log_sink=self._log_sink(command, args, mode),
            processes=self.processes,
            prompter=Prompter(
                command=command.path.value,
                declared=command.interactive,
                editor_alternatives=command.editor_alternatives,
                interactive=self.interactive and not invocation.non_interactive,
                assume_yes=invocation.yes,
                stdin=self.stdin,
                stderr=self.err.stream,
                env=self.env,
            ),
            warn_sink=self._warn,
            idempotency_key=idempotency_key,
            stdin_text=invocation.stdin_text,
            page=page,
            token=invocation.token,
            _config_file=self.config_file if command.config_write_scope is not None else None,
            trace_id=self.trace_id,
            project_root=self.project_root(command),
            retrier=self.retrier,
            locks=Locks(self.locks_dir(), deadline),
        )

    def locks_dir(self) -> Path | None:
        """``locks/`` of the state directory, for ``ctx.lock`` (REQ-F-033)"""
        base = state_dir(
            self.app.name, self.app.state_dir, self.env, self.settings.options.instance_id
        )
        return None if base is None else base / "locks"

    def load_settings(self, options: ConfigOptions) -> None:
        """Read the settings layers once for the run; ``CONFIG_INVALID`` stops it"""
        app = self.app
        self.settings = resolve_settings(
            app.settings, app.name, options, self.env, self.cwd, app.scalars
        )

    def provided(self) -> dict[type, object]:
        """What a handler may ask for by type without a resource: the settings"""
        spec = self.app.settings
        return {} if spec is None else {spec.cls: self.settings.value}

    def project_root(self, command: Command) -> Path | None:
        """The directory holding one of the command's ``project_root`` markers, found once
        per run walking up from ``meta.cwd`` (REQ-F-027)"""
        markers = command.project_root
        if not markers:
            return None
        if markers not in self._roots:
            self._roots[markers] = find_project_root(self.cwd, markers)
        return self._roots[markers]

    def _trace_suffix(self) -> str:
        """`` trace=<id>`` for a framework line on stderr, when a trace is set"""
        return "" if self.trace_id is None else f" trace={self.trace_id}"

    def _warn(self, code: str, message: str, context: Mapping[str, object]) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError(f"warning code {code!r} is not UPPER_SNAKE_CASE")
        safe = json_safe(dict(context))
        assert isinstance(safe, dict)
        self.warnings.append(WarningDetail(code, message, context=safe))

    def _log_sink(self, command: Command, args: object, mode: Format) -> LogSink:
        """``ctx.log``: one line on stderr, secrets redacted, escapes stripped unless the
        run may color (REQ-F-006, REQ-F-051)"""
        keep_escapes = mode is not Format.JSON and color_allowed(self.env, self.tty)

        def write(message: str, fields: Mapping[str, object]) -> None:
            # Built per call, inside the handler: a secret scalar's serialize= is user code
            redact = self._redactor(command, args)
            safe = {k: _scrub(k, json_safe(v), redact) for k, v in fields.items()}
            if mode is Format.JSON:
                record = {"level": "info", "message": redact(message), "fields": safe}
                if self.trace_id is not None:
                    record["trace_id"] = self.trace_id
                line = json.dumps(clean(record), separators=(",", ":"), sort_keys=True)
            else:
                pairs = (
                    f"{k}={v if isinstance(v, str) else json.dumps(v)}" for k, v in safe.items()
                )
                line = " ".join((redact(message), *pairs)) + self._trace_suffix()
                if not keep_escapes:
                    line = str(clean(line))
            self.err.write(line + "\n")
            self.err.flush()

        return write

    # Envelope construction

    def _envelope(
        self,
        code: int,
        *,
        data: object = None,
        error: ErrorDetail | None = None,
        started: float | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        origin = self.started if started is None else started
        # REQ-F-057: every response says when no window could have been opened
        extra: dict[str, object] = {"headless": True} if self.headless else {}
        if any(w.code == TRUNCATED_CODE for w in self.warnings):
            extra["truncated"] = True  # ctx.truncated reported a backend's cut (REQ-F-064)
        extra |= self.settings.meta()
        command = self.current
        if command is None:
            name, version, root = self.app.name, ENVELOPE_SCHEMA_VERSION, None
        else:
            name = command.path.value
            version = (self.pinned or command.schema_version).value
            root = self.project_root(command)
        return Envelope(
            exit_code=code,
            data=data,
            error=error,
            meta=Meta(
                # REQ-O-007: what differs between identical calls is left out, and the
                # required duration_ms is 0
                duration_ms=0 if self.stable else int((time.perf_counter() - origin) * 1000),
                request_id=None if self.stable else self.request_id,
                command=name,
                timestamp=None if self.stable else self.timestamp,
                schema_version=version,
                tool_version=self.app.version,
                cwd=str(self.cwd),
                trace_id=self.trace_id,
                project_root=None if root is None else str(root),
                retries=0 if self.retrier is None or self.stable else self.retrier.count,
            ),
            warnings=tuple(self.warnings),
            extra_meta={**extra, **(meta or {})},
        )

    def arg_error(self, exc: ParseError, *, code: str | None = None, **kw: Any) -> Envelope:
        entry = self.app.exits.framework(FrameworkCode.ARG_ERROR)
        corrected = exc.context.get("corrected_input")
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=code or exc.code or "ARG_ERROR",
                message=exc.message,
                retryable=False,
                context=exc.context,
                suggestion=exc.suggestion,
                phase="validation",
                fix_required="correct the arguments and reissue",
                errors=exc.items(),
                corrected_input=corrected if isinstance(corrected, str) else None,
            ),
            **kw,
        )

    def redirected(
        self,
        source: CommandPath,
        moved: Moved,
        replacement: str,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        """Exit 13: ``source`` is retired; ``replacement`` is what to run instead"""
        entry = self.app.exits.framework(FrameworkCode.REDIRECTED)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code="REDIRECTED",
                message=f"{source} is now {moved.to}",
                retryable=False,
                context={"from": source.value, "to": moved.to.value},
                suggestion=f"run {replacement} instead",
                phase="validation",
                redirect=Redirect(replacement, moved.permanent, moved.reason),
            ),
            meta=meta,
        )

    def args_crashed(
        self,
        command: Command,
        exc: ArgsCrashed,
        *,
        started: float | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        """A bug in the args ``__post_init__``: reported like a handler crash"""
        values = types.SimpleNamespace(**exc.values)  # what the redactor reads secrets from
        return self._crashed(command, values, exc.cause, started or self.started, meta or {})

    def after_start(self, exc: ParseError, **kw: Any) -> Envelope:
        """A ``ParseError`` from a handler or a resource's ``acquire``: user code already
        ran, so exit 2 would promise an agent a side-effect-free failure it cannot have
        (REQ-F-002). Phase 1 checks belong in the args ``__post_init__``."""
        entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code="VALIDATION_AFTER_START",
                message=exc.message,
                retryable=False,
                context=exc.context,
                suggestion=exc.suggestion,
                phase="execution",
                fix_required="correct the arguments; the command author should move this "
                "check into the args dataclass's __post_init__ so it runs before any side effect",
            ),
            **kw,
        )

    def execute(
        self,
        command: Command,
        invocation: Invocation,
        mode: Format,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        """Run one handler, replaying the stored result when its idempotency key was seen"""
        self._pin(command, invocation)
        if command.paginated:
            position = self._position(command, invocation, meta)
            if isinstance(position, Envelope):
                return position
            invocation = dataclasses.replace(invocation, cursor=position)
        if invocation.validate_only:
            return self.validated(meta)
        if command.auth is not None:
            token = self._login_token(command, invocation, meta)
            if isinstance(token, Envelope):
                return token
            self.token = token
            invocation = dataclasses.replace(invocation, token=token)
        if command.config_write_scope is not None:
            config = self._config_file(command, invocation, meta)
            if isinstance(config, Envelope):
                return config
            self.config_file = config
        if command.stdin_input:
            try:
                payload = self._read_input(invocation.input_file, meta=meta)
            except Cancelled as exc:
                return self._cancelled(
                    command, exc.signal, self.started, meta or {}, handler_started=False
                )
            if isinstance(payload, Envelope):
                return payload
            invocation = dataclasses.replace(invocation, stdin_text=payload)
        if not command.safe_default:
            return self._keyed(command, invocation, mode, meta=meta)
        # REQ-O-048: a dry run unless --live; --dry-run still wins, as a preview is safe
        dry_run = not invocation.live or _dry_run_requested(invocation.args)
        args = invocation.args
        assert dataclasses.is_dataclass(args) and not isinstance(args, type)
        # --live is the explicit confirmation; --confirm-destructive is not also needed
        invocation = dataclasses.replace(
            invocation,
            args=dataclasses.replace(args, dry_run=True) if dry_run else args,
            confirmed=not dry_run,
        )
        envelope = self._keyed(command, invocation, mode, meta=meta)
        extra: dict[str, object] = {"dry_run": dry_run}
        if not dry_run:
            extra["confirmed"] = True
        return dataclasses.replace(envelope, extra_meta={**envelope.extra_meta, **extra})

    def validated(self, meta: Mapping[str, object] | None) -> Envelope:
        """``--validate-only`` (REQ-O-009): phase 1 passed, so the command would run; the
        credential gate, the idempotency store, and the handler never do"""
        return self._envelope(0, meta={**(meta or {}), "validation_only": True})

    def _deprecations(self, command: Command, invocation: Invocation) -> None:
        """REQ-F-075: a deprecated command, or a deprecated flag the caller passed, still
        runs; stderr gets one structured line and ``warnings`` an entry, each naming the
        replacement"""
        found: list[tuple[str, str, Deprecated, str | None]] = []
        if (old := command.deprecated) is not None:
            instead = old.replacement and shlex.join(
                [self.app.name, *CommandPath(old.replacement).parts]
            )
            what = shlex.join([self.app.name, *command.path.parts])
            found.append(("DEPRECATED", what, old, instead))
        for f in command.fields:
            if (old := f.spec.deprecated) is not None and f.name in invocation.given:
                instead = old.replacement and f"--{old.replacement}"
                found.append(("DEPRECATED_FLAG", f"--{f.flag}", old, instead))
        for code, what, old, instead in found:
            message = f"{what} is deprecated since {old.since}"
            if instead:
                message += f"; use {instead} instead"
            context: dict[str, object] = {"since": old.since}
            if instead:
                context["replacement"] = instead
            if old.removed_in is not None:
                context["removed_in"] = old.removed_in
            self._warn(code, message, context)
            line: dict[str, object] = {"level": "warn", "code": code, "message": message}
            line |= {k: v for k, v in context.items() if k != "since"}
            self.err.write(json.dumps(line, separators=(",", ":")) + "\n")
            self.err.flush()

    def _pin(self, command: Command, invocation: Invocation) -> None:
        """Answer in the schema version ``--schema-version`` selected; an older one is
        deprecated, which a warning says (REQ-O-014). ``stable_output`` of an exec line
        or MCP call applies to that call only."""
        self.stable = self.stable_all or invocation.stable_output
        self._deprecations(command, invocation)
        self.pinned = invocation.schema_version
        if self.pinned is not None:
            self._warn(
                "SCHEMA_DEPRECATED",
                f"Schema version {self.pinned} is deprecated; current is {command.schema_version}",
                {
                    "current_version": command.schema_version.value,
                    "requested_version": self.pinned.value,
                },
            )

    def _shimmed(self, command: Command, result: object) -> object:
        """The handler's result in the pinned schema's shape"""
        if self.pinned is None:
            return result
        return command.compat_for(self.pinned).shim(result)

    def _position(
        self, command: Command, invocation: Invocation, meta: Mapping[str, object] | None
    ) -> Position | Envelope:
        """Where the page starts, bound to the command's other arguments (REQ-O-003): a
        cursor from another listing, or one the command's ``cursor_check`` refuses, is
        ``INVALID_CURSOR`` before the handler runs"""
        args = invocation.args
        # Secrets stay out: the digest is written to stdout inside every cursor
        listed = {f.name: getattr(args, f.name) for f in command.fields if not f.secret}
        try:
            digest = fingerprint(command.path, listed, self.app.scalars)[:16]
        except SchemaError as exc:
            message = f"Command {command.path} has arguments a cursor cannot bind to: {exc}"
            return self._broken(command, "INVALID_ARGS", message, self.started, meta or {})
        except Exception as exc:  # noqa: BLE001 - a scalar's serialize= is user code
            return self._crashed(command, args, exc, self.started, meta or {})
        given = invocation.cursor
        if given is None:
            return Position(args=digest)
        try:
            if given.args != digest:
                raise invalid_cursor("it was issued for different arguments")
            if given.cursor is not None and command.cursor_check is not None:
                command.cursor_check(given.cursor)
        except ParseError as exc:
            exc.context.setdefault("flag", CURSOR_FLAG)
            return self.arg_error(exc, code="INVALID_CURSOR", meta=meta)
        except Exception as exc:  # noqa: BLE001 - cursor_check= is user code
            return self._crashed(command, args, exc, self.started, meta or {})
        return given

    def _config_file(
        self, command: Command, invocation: Invocation, meta: Mapping[str, object] | None
    ) -> ConfigFile | Envelope:
        """The file ``ctx.write_config`` writes; a global-only command needs ``--global``"""
        if command.config_write_scope is ConfigScope.GLOBAL and not invocation.global_config:
            return self.arg_error(
                ParseError(
                    f"Command {command.path} writes the global config and needs --global",
                    context={"flag": "global", "command": command.path.value},
                    suggestion="rerun with --global to confirm the user-wide change",
                ),
                meta=meta,
            )
        chosen = self.settings.options
        if chosen.config is not None:
            # REQ-O-024: --config is the one file this run reads and writes
            return ConfigFile(self.cwd / chosen.config, False, self._warn)
        if not invocation.global_config:
            return ConfigFile(local_config(self.app.name, self.cwd), False, self._warn)
        path = user_config(self.app.name, self.env, chosen.instance_id)
        if path is None:
            return self._state_error(
                "CONFIG_DIR_UNKNOWN",
                "no directory for the global config",
                "set XDG_CONFIG_HOME or HOME",
                time.perf_counter(),
                meta or {},
            )
        return ConfigFile(path, True, self._warn)

    def _login_token(
        self, command: Command, invocation: Invocation, meta: Mapping[str, object] | None
    ) -> str | None | Envelope:
        """A login command's token: ``--token-env-var``, else the first set variable of
        ``token_env_vars``. A browser login no person can finish, or a named variable
        that is empty, exits 4 listing where the token goes (REQ-C-021, REQ-O-033)."""
        named = invocation.token_env_var
        candidates = (named,) if named is not None else command.token_env_vars
        token = next((self.env[v] for v in candidates if self.env.get(v)), None)
        headless = self.headless or invocation.headless
        if token is not None or (
            named is None and not (headless and command.auth is AuthKind.BROWSER)
        ):
            return token
        first = candidates[0]
        entry = self.app.exits.framework(FrameworkCode.PRECONDITION)
        why = (
            f"{named} is not set"
            if named is not None
            else f"Command {command.path} logs in through a browser, and this run is headless"
        )
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code="TOKEN_REQUIRED",
                message=f"{why}; set {first} to a token to log in without one",
                retryable=False,
                context={"command": command.path.value, "token_env_vars": list(candidates)},
                fix_required=f"export {first}=<token>, or pass --token-env-var NAME naming "
                "a variable that holds it",
                phase="validation",
                auth_methods=[
                    {"type": "env_var", "name": v, "hint": f"Set {v} to your API token"}
                    for v in candidates
                ],
            ),
            meta=meta,
        )

    def _keyed(
        self,
        command: Command,
        invocation: Invocation,
        mode: Format,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        key = invocation.idempotency_key
        # REQ-C-007: within an agent session, a repeat without a key is still deduplicated
        session = (
            self.env.get(app_var(self.app.name, SESSION.key))
            if key is None and command.danger_level is not DangerLevel.SAFE
            else None
        )
        if key is None and not session:
            return self._execute(command, invocation, mode, meta=meta)
        if _previewing(command, invocation) or _dry_run_requested(invocation.args):
            return self._execute(command, invocation, mode, meta=meta)
        started = time.perf_counter()
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        directory = state_dir(
            self.app.name, self.app.state_dir, self.env, self.settings.options.instance_id
        )
        if directory is None:
            entry = self.app.exits.framework(FrameworkCode.PRECONDITION)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="STATE_DIR_UNKNOWN",
                    message="no directory to keep idempotency records in",
                    retryable=False,
                    phase="validation",
                    fix_required=f"set {self.state_var}, XDG_STATE_HOME, or HOME",
                ),
                started=started,
                meta=full_meta,
            )
        try:
            call = fingerprint(command.path, invocation.args, self.app.scalars)
        except SchemaError as exc:
            message = f"Command {command.path} has arguments the key cannot fingerprint: {exc}"
            return self._broken(command, "INVALID_ARGS", message, started, full_meta)
        except Exception as exc:  # noqa: BLE001 - a scalar's serialize= is user code
            return self._crashed(command, invocation.args, exc, started, full_meta)
        if key is None:
            assert session
            key = session_key(session, call)
            invocation = dataclasses.replace(invocation, idempotency_key=key)
            full_meta["idempotency_key"] = key.value
            meta = {**(meta or {}), "idempotency_key": key.value}
        with contextlib.ExitStack() as held:
            # Only acquiring the key is guarded here: the call itself reports its own errors
            try:
                # The wait for an earlier call's lock is bounded by this call's timeout
                slot = held.enter_context(
                    claim(
                        directory,
                        key,
                        wait_seconds=timeout.seconds,
                        waiting=self.cancellation.armed,
                    )
                )
            except KeyBusy as exc:
                entry = self.app.exits.framework(FrameworkCode.TIMEOUT)
                return self._envelope(
                    entry.code.value,
                    error=ErrorDetail(
                        code="IDEMPOTENCY_KEY_BUSY",
                        message=f"an earlier call with this idempotency key still runs after "
                        f"{exc.seconds:g}s",
                        # Nothing ran, so retrying with the same key is safe
                        retryable=True,
                        retry_after_ms=1000,
                        retry_strategy=RetryStrategy.IMMEDIATE,
                        context={
                            "command": command.path.value,
                            "timeout_ms": timeout.milliseconds,
                        },
                        phase="execution",
                        suggestion="retry with the same key once the earlier call has finished",
                    ),
                    started=started,
                    meta=full_meta,
                )
            except Cancelled as exc:
                return self._cancelled(
                    command, exc.signal, started, full_meta, handler_started=False
                )
            except RecordCorrupt as exc:
                return self._state_error(
                    "IDEMPOTENCY_RECORD_CORRUPT",
                    str(exc),
                    f"delete {exc.path}; its call must then be checked by hand",
                    started,
                    full_meta,
                )
            except OSError as exc:
                return self._state_error(
                    "STATE_DIR_UNWRITABLE",
                    f"cannot use the idempotency state directory {directory}: {exc.strerror}",
                    f"make it writable, or point {self.state_var} at a writable directory",
                    started,
                    full_meta,
                )
            return self._claimed(
                command,
                invocation,
                mode,
                slot,
                call,
                meta=meta,
                started=started,
                full_meta=full_meta,
            )

    @property
    def state_var(self) -> str:
        return app_var(self.app.name, STATE_DIR.key)

    def _state_error(
        self, code: str, message: str, fix: str, started: float, meta: Mapping[str, object]
    ) -> Envelope:
        entry = self.app.exits.framework(FrameworkCode.PRECONDITION)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=code, message=message, retryable=False, phase="validation", fix_required=fix
            ),
            started=started,
            meta=meta,
        )

    def _claimed(
        self,
        command: Command,
        invocation: Invocation,
        mode: Format,
        slot: Slot,
        call: str,
        *,
        meta: Mapping[str, object] | None,
        started: float,
        full_meta: Mapping[str, object],
    ) -> Envelope:
        """The key is ours: answer from its record, or run the call and record it"""
        if slot.record is not None and slot.record.fingerprint != call:
            entry = self.app.exits.framework(FrameworkCode.CONFLICT)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="this idempotency key was already used with different arguments",
                    retryable=False,
                    context={"command": slot.record.command},
                    phase="validation",
                    fix_required="use a new --idempotency-key, or repeat the original call",
                ),
                started=started,
                meta=full_meta,
            )
        if slot.record is not None:
            data = slot.record.data
            assert isinstance(data, dict)

            def replay() -> Envelope:
                return self._envelope(
                    0,
                    data={**data, "effect": "noop"},
                    started=started,
                    meta={**full_meta, "idempotency_hit": True},
                )

            if not command.requires_auth:
                return replay()
            # A stored result is still the command's output: the credential must cover it
            return self._execute(command, invocation, mode, meta=meta, replay=replay)
        envelope = self._execute(command, invocation, mode, meta=meta)
        pending = self.abandoned
        if pending is not None:
            # A retry must wait for the abandoned handler, not run beside it
            slot.hand_off(lambda held: self._record_late(command, invocation, pending, held, call))
            return envelope
        if envelope.exit_code != 0:
            return envelope
        try:
            slot.save(Record(call, command.path.value, envelope.data, time.time()))
        except OSError as exc:
            # The call succeeded; its envelope must not be lost to the record
            return _warned(
                envelope,
                "IDEMPOTENCY_NOT_RECORDED",
                f"the result was not recorded ({exc.strerror}); a retry with this key "
                "runs the call again",
                command,
            )
        try:
            slot.prune(time.time())
        except OSError as exc:
            return _warned(
                envelope,
                "IDEMPOTENCY_PRUNE_FAILED",
                f"the result was recorded, but expired records could not be removed "
                f"({exc.strerror})",
                command,
            )
        return envelope

    def _execute(
        self,
        command: Command,
        invocation: Invocation,
        mode: Format,
        *,
        meta: Mapping[str, object] | None = None,
        replay: Callable[[], Envelope] | None = None,
    ) -> Envelope:
        """Run one handler under its timeout and turn the outcome into an envelope;
        with ``replay``, run only the credential gate there and answer with ``replay()``"""
        self.abandoned = None
        self.page = None
        started = time.perf_counter()
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        key = invocation.idempotency_key
        position = invocation.cursor if invocation.cursor is not None else Position()
        limit = invocation.limit if invocation.limit is not None else command.default_limit
        ctx = self._ctx(
            command,
            invocation.args,
            mode,
            timeout,
            None if key is None else key.value,
            invocation=invocation,
            page=request(position, limit) if command.paginated else None,
        )
        args = invocation.args
        preview_only = _previewing(command, invocation)
        if preview_only:
            assert dataclasses.is_dataclass(args) and not isinstance(args, type)
            args = dataclasses.replace(args, dry_run=True)
        running: list[Pending] = []
        try:
            self.cancellation.check()
            result = call_with_timeout(
                (lambda: self.app._gate(command, ctx))
                if replay is not None
                else (lambda: _invoke(self.app, command, args, ctx, self.provided())),
                timeout,
                running.append,
                self.cancellation.armed,
                heartbeat=self._heartbeat(command, invocation, mode, started),
            )
        except CliExit as exc:
            return self._exit_envelope(command, args, exc, started, full_meta)
        except ParseError as exc:
            return self.after_start(exc, started=started, meta=full_meta)
        except TimeoutExpired as exc:
            self.abandoned = exc.pending
            self._stop_children()
            entry = self.app.exits.timeout(read_only=command.danger_level is DangerLevel.SAFE)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="TIMEOUT",
                    message=f"Command {command.path} exceeded its {timeout.seconds}s timeout",
                    retryable=entry.retryable,
                    retry_strategy=entry.retry_strategy,
                    context={"timeout_ms": timeout.milliseconds, "command": command.path.value},
                    phase="execution",
                ),
                started=started,
                meta=full_meta,
            )
        except Cancelled as exc:
            self.abandoned = _still_running(running)
            # A held signal raised before fn() or before the worker started: nothing ran
            ran = not exc.held or bool(running)
            return self._cancelled(
                command, exc.signal, started, full_meta, handler_started=ran, running=running
            )
        except KeyboardInterrupt:
            self.abandoned = _still_running(running)
            sig = CancelSignal("SIGINT", 130)
            return self._cancelled(command, sig, started, full_meta, running=running)
        except InputRequired as exc:
            return self._input_required(exc, started, full_meta)
        except GeneratorExit:
            raise
        except BaseException as exc:  # noqa: BLE001 - the handler boundary; see _crashed
            # asyncio.CancelledError, SystemExit, trio.Cancelled: user code, not a signal
            return self._crashed(command, args, exc, started, full_meta)
        if replay is not None:
            return replay()
        page_meta: dict[str, object] = {}
        try:
            if command.paginated:
                if isinstance(result, (list, tuple)):
                    result = self._sorted_list(command, result)
                result, pagination = take(result, position, limit, command.path)
                page_meta["pagination"] = pagination.to_json()
                self.page = (command.path, position)
            data = self._payload(self._shimmed(command, result), *self._output(command))
        except SchemaError as exc:
            return self._broken(
                command,
                "INVALID_OUTPUT",
                f"Command {command.path} returned {exc}",
                started,
                full_meta,
            )
        except Exception as exc:  # noqa: BLE001 - a scalar's serialize= is handler code
            return self._crashed(command, args, exc, started, full_meta)
        data = self._with_open_url(data)
        if command.danger_level is not DangerLevel.SAFE:
            problem = effect_problem(
                data,
                preview=_dry_run_requested(args),
                destructive=command.danger_level is DangerLevel.DESTRUCTIVE,
            )
            if problem is not None:
                entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
                return self._envelope(
                    entry.code.value,
                    error=ErrorDetail(
                        code="INVALID_EFFECT",
                        message=f"Command {command.path} broke the effect contract: {problem}",
                        retryable=False,
                        context={"command": command.path.value},
                        phase="execution",
                    ),
                    started=started,
                    meta=full_meta,
                )
        if preview_only:
            entry = self.app.exits.framework(FrameworkCode.ARG_ERROR)
            return self._envelope(
                entry.code.value,
                data=data,
                error=ErrorDetail(
                    code="CONFIRMATION_REQUIRED",
                    message=f"Command {command.path} is destructive and was not applied; "
                    f"it would: {affects_summary(data)}",
                    retryable=False,
                    context={"command": command.path.value, "flag": "confirm-destructive"},
                    phase="validation",
                    fix_required="rerun with --confirm-destructive to apply "
                    "(confirm_destructive: true in exec, MCP, or --raw-payload)",
                ),
                started=started,
                meta=full_meta,
            )
        return self._envelope(0, data=data, started=started, meta={**full_meta, **page_meta})

    def stream(
        self,
        command: Command,
        invocation: Invocation,
        mode: Format,
        *,
        meta: Mapping[str, object] | None = None,
        whole: bool = False,
    ) -> Generator[Envelope]:
        """Run a generator handler: one envelope per event, then a terminal one (REQ-O-004)

        The timeout limits the wait for each event, so a stream runs as long as it keeps
        producing (REQ-F-011); ``whole`` makes it a deadline for the whole stream, for a
        caller that sees nothing until the stream ends. A failure after some events keeps
        their count in ``meta.seq`` and marks the response ``partial``.
        """
        self._pin(command, invocation)
        if invocation.validate_only:
            yield self.validated(meta)
            return
        started = time.perf_counter()
        waiting_since = started
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        ctx = self._ctx(command, invocation.args, mode, timeout, invocation=invocation)
        args = invocation.args
        seq = 0
        events: Iterator[object] | None = None

        def remaining() -> Timeout:
            if timeout.seconds is None:
                return timeout
            left = timeout.seconds - (time.perf_counter() - (started if whole else waiting_since))
            if left <= 0:
                raise TimeoutExpired(timeout)
            return Timeout(left)

        def partial() -> dict[str, object]:
            """Partial means some events were delivered before the failure"""
            return {**full_meta, "seq": seq, "partial": seq > 0}

        running: list[Pending] = []
        # One context for every next(): what the generator sets survives between events
        stream_context = contextvars.copy_context()

        def latest(pending: Pending) -> None:
            running[:] = [pending]  # only the current worker matters; a stream may be endless

        try:
            self.cancellation.check()
            produced = call_with_timeout(
                lambda: _invoke(self.app, command, args, ctx, self.provided()),
                remaining(),
                running.append,
                self.cancellation.armed,
                stream_context,
            )
            if isinstance(produced, Iterable) and not isinstance(produced, (str, bytes, Mapping)):
                # Registration accepts Iterable[T]: a returned list streams its items
                produced = iter(produced)
            if not isinstance(produced, Iterator):
                raise TypeError(
                    f"{command.path} is streaming but returned {type(produced).__name__}, "
                    "not a generator"
                )
            events = produced
            while True:
                self.cancellation.check()
                waiting_since = time.perf_counter()
                event = call_with_timeout(
                    lambda: next(produced, _END),
                    remaining(),
                    latest,
                    self.cancellation.armed,
                    stream_context,
                )
                if event is _END:
                    self.in_flight = None  # the handler finished; nothing is left to clean up
                    break
                seq += 1
                data = self._payload(self._shimmed(command, event), *self._output(command))
                yield self._envelope(0, data=data, started=started, meta={**full_meta, "seq": seq})
        except CliExit as exc:
            yield self._exit_envelope(command, args, exc, started, partial())
            return
        except ParseError as exc:
            yield self.after_start(exc, started=started, meta=partial())
            return
        except TimeoutExpired:
            self._stop_children()
            entry = self.app.exits.timeout(read_only=command.danger_level is DangerLevel.SAFE)
            what = "timeout" if whole else "timeout waiting for its next event"
            yield self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="TIMEOUT",
                    message=f"Command {command.path} exceeded its {timeout.seconds}s {what}",
                    retryable=entry.retryable,
                    retry_strategy=entry.retry_strategy,
                    context={"timeout_ms": timeout.milliseconds, "command": command.path.value},
                    phase="execution",
                ),
                started=started,
                meta=partial(),
            )
            return
        except Cancelled as exc:
            ran = events is not None or not exc.held or bool(running)
            meta_now = {**full_meta, "seq": seq}
            yield self._cancelled(
                command, exc.signal, started, meta_now, handler_started=ran, running=running
            )
            return
        except KeyboardInterrupt:
            sig = CancelSignal("SIGINT", 130)
            meta_now = {**full_meta, "seq": seq}
            yield self._cancelled(command, sig, started, meta_now, running=running)
            return
        except InputRequired as exc:
            yield self._input_required(exc, started, partial())
            return
        except SchemaError as exc:
            message = f"Command {command.path} yielded {exc}"
            yield self._broken(command, "INVALID_OUTPUT", message, started, partial())
            return
        except GeneratorExit:
            raise  # the consumer closed the stream; nothing more may be yielded
        except BaseException as exc:  # noqa: BLE001 - the handler boundary; see _crashed
            yield self._crashed(command, args, exc, started, partial())
            return
        finally:
            # Run the handler's finally blocks now, unless a timed-out worker still holds it
            held = any(p.worker.is_alive() for p in running)
            if events is not None and not held and isinstance(events, Generator):
                try:
                    events.close()
                except Exception as exc:  # noqa: BLE001 - the handler's finally is user code
                    # The terminal envelope is already decided; the failure goes to stderr
                    self.err.write(self._redactor(command, args)(_traceback(exc)))
        yield self._envelope(
            0, started=started, meta={**full_meta, "seq": seq, "end": True, "total": seq}
        )

    def _heartbeat(
        self, command: Command, invocation: Invocation, mode: Format, started: float
    ) -> Heartbeat | None:
        """Heartbeat lines for a JSON run from argv (REQ-F-053); an exec plan and an MCP
        call have one reader for many results, and plain output is for a person"""
        ms = invocation.heartbeat_ms
        if ms is None:
            ms = DEFAULT_HEARTBEAT_MS
        if not command.heartbeat or not ms or mode is not Format.JSON or self.argv is None:
            return None
        if self.stable:
            return None  # how many lines come depends on timing (REQ-O-007)
        beating = [True]

        def tick() -> None:
            if not beating[0]:
                return
            elapsed = int((time.perf_counter() - started) * 1000)
            line = {"status": "running", "heartbeat": True, "elapsed_ms": elapsed}
            try:
                self.out.write(json.dumps(line, separators=(",", ":")) + "\n")
                self.out.flush()
            except OSError as exc:
                if not _closed_pipe(exc):
                    raise
                beating[0] = False  # the envelope write reports the closed pipe

        return Heartbeat(ms / 1000, tick)

    def _input_required(
        self, exc: InputRequired, started: float, meta: Mapping[str, object]
    ) -> Envelope:
        """Exit 4: the run needs an answer only a person at a terminal could give
        (REQ-F-009, REQ-F-047, REQ-F-055); the suggestion names the flag that gives it"""
        entry = self.app.exits.framework(FrameworkCode.PRECONDITION)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                retryable=False,
                context=exc.context,
                suggestion=exc.suggestion,
                fix_required=exc.suggestion,
                phase="execution",
                alternatives=exc.alternatives or None,
            ),
            started=started,
            meta=meta,
        )

    def _stop_children(self, cancelled: CancelSignal | None = None) -> None:
        """Terminate what the interrupted or abandoned handler still runs (REQ-F-030);
        ``cancelled`` is the signal, None for a timeout"""
        if self.processes is not None:
            self.processes.terminate(cancelled)

    def _with_open_url(self, data: object) -> object:
        """A headless ``ctx.open_url`` leaves its URL in ``data.open_url`` (REQ-F-057)"""
        url = None if self.processes is None else self.processes.suppressed_url
        if url is None or not isinstance(data, dict) or data.get("open_url") is not None:
            return data
        return {**data, "open_url": url}

    def _cancelled(
        self,
        command: Command,
        sig: CancelSignal,
        started: float,
        meta: Mapping[str, object],
        *,
        handler_started: bool = True,
        running: Sequence[Pending] = (),
    ) -> Envelope:
        """Run the cleanup hook, then build the CANCELLED envelope (REQ-F-013, REQ-F-069)

        Before the handler started there is nothing to clean up and nothing partial.
        ``running`` holds the handler's worker, which gets the children's grace to finish
        before the cleanup hook runs beside it.
        """
        context: dict[str, object] = {"signal": sig.name, "command": command.path.value}
        # Children first, so the envelope is written after they were signaled (REQ-F-031)
        self._stop_children(sig)
        for pending in running:
            # Waiting on a child or its next event, not stuck: a stream's generator is
            # handed back so it can be closed, and cleanup never races the handler
            pending.worker.join(GRACE_SECONDS)
        self.in_flight = None  # cleaned up here; a closed stdout must not clean up again
        if handler_started and command.cleanup is not None:
            try:
                command.cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup= is user code
                self.err.write("".join(traceback.format_exception(exc)))
                context["cleanup_failed"] = type(exc).__qualname__
        entry = self.app.exits.by_code(sig.exit_code)
        return self._envelope(
            sig.exit_code,
            error=ErrorDetail(
                code="CANCELLED",
                message=f"Command {command.path} was cancelled by {sig.name}",
                retryable=entry.retryable,
                context=context,
                phase="execution",
            ),
            started=started,
            meta={**meta, "partial": True} if handler_started else dict(meta),
        )

    def _exit_envelope(
        self,
        command: Command,
        args: object,
        exc: CliExit,
        started: float,
        meta: Mapping[str, object],
    ) -> Envelope:
        # A failed child's argv and stderr, or ctx.token, may carry a secret: stdout never
        # gets one, as the traceback on stderr does not
        redact = self._redactor(command, args)
        # Checked below: a handler may pass anything as the message
        message = redact(exc.message) if isinstance(exc.message, str) else exc.message
        if exc.name.value == FrameworkCode.ARG_ERROR.name:
            # Exit 2 promises nothing ran; from a handler, something did (REQ-F-002)
            rejected = ParseError(
                message,
                context=cast(dict[str, object], _redacted(json_safe(exc.context), redact)),
                suggestion=exc.suggestion,
            )
            return self.after_start(rejected, started=started, meta=meta)
        # The codes the manifest lists for this command without a declaration; any other
        # framework code (NOT_FOUND, RATE_LIMITED, ...) must be declared like a custom one
        allowed = {ExitCodeName(c.name) for c in implicit_exit_codes(command)}
        if exc.name not in command.exit_codes and exc.name not in allowed:
            entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="UNDECLARED_EXIT_CODE",
                    message=f"Command {command.path} raised {exc.name}, which it does not declare",
                    retryable=False,
                    context={
                        "declared": [n.value for n in command.exit_codes],
                        "raised": exc.name.value,
                        "original_message": message,
                    },
                    phase="execution",
                ),
                started=started,
                meta=meta,
            )
        entry = self.app.exits.by_name(exc.name)
        if entry.code.value == 0:
            message = (
                f"Command {command.path} raised {exc.name}; return the result instead of raising"
            )
            return self._broken(command, "INVALID_EXIT", message, started, meta)
        if not _ERROR_CODE.fullmatch(exc.code) or not isinstance(exc.message, str):
            message = (
                f"Command {command.path} raised {exc.name} with code {exc.code!r}; error codes are "
                "UPPER_SNAKE_CASE and messages are text"
            )
            return self._broken(command, "INVALID_EXIT", message, started, meta)
        retry_after = exc.retry_after_ms
        if retry_after is not None:
            if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
                message = (
                    f"Command {command.path} raised {exc.name} with a non-numeric retry_after_ms"
                )
                return self._broken(command, "INVALID_EXIT", message, started, meta)
            # A reset time already past (negative) means retry now; fractions round up
            retry_after = max(0, math.ceil(retry_after))
        else:
            retry_after = entry.retry_after_ms
        # ctx.retry's own RATE_LIMITED has no Retry-After to pass on (REQ-F-078)
        if entry.name.value == FrameworkCode.RATE_LIMITED.name and not isinstance(
            exc, RetriesExhausted
        ):
            if retry_after is None:
                # REQ-C-014: an agent told only "rate limited" retries at once, into the limit
                message = (
                    f"Command {command.path} raised RATE_LIMITED without retry_after_ms; pass "
                    "retry_after_ms=<the upstream Retry-After in ms>"
                )
                return self._broken(command, "INVALID_EXIT", message, started, meta)
            retry_after = max(1, retry_after)  # the limit resets now: wait the least there is
        strategy = entry.retry_strategy
        if exc.retry_strategy is not None:
            try:
                strategy = RetryStrategy(exc.retry_strategy)
            except ValueError:
                message = (
                    f"Command {command.path} raised {exc.name} with retry_strategy "
                    f"{exc.retry_strategy!r}; use one of {', '.join(RetryStrategy)}"
                )
                return self._broken(command, "INVALID_EXIT", message, started, meta)
        fix = exc.fix_command if exc.fix_command is not None else command.fix_commands.get(exc.code)
        if exc.fix_command is not None:
            problem = self.app.fix_problem(exc.fix_command)
            if problem is not None:
                message = f"Command {command.path} raised {exc.name} with fix_command {problem}"
                return self._broken(command, "INVALID_EXIT", message, started, meta)
        if exc.conflict_id is not None and not isinstance(exc.conflict_id, str):
            message = (
                f"Command {command.path} raised {exc.name} with a conflict_id that is not text"
            )
            return self._broken(command, "INVALID_EXIT", message, started, meta)
        try:
            data = self._payload(exc.data)
            context = _redacted(to_jsonable(exc.context, self.app.scalars, base=self.cwd), redact)
        except SchemaError as err:
            message = f"Command {command.path} raised {exc.name} with {err}"
            return self._broken(command, "INVALID_EXIT", message, started, meta)
        except Exception as err:  # noqa: BLE001 - a scalar's serialize= is handler code
            return self._crashed(command, args, err, started, meta)
        assert isinstance(context, dict)
        # REQ-F-078: after the tool's own retries, an agent retrying on top would double them
        retried = exc.retried if isinstance(exc, RetriesExhausted) else None
        # 03-D1: PRECONDITION is not retryable, but nothing ran behind a held lock
        retrying = (entry.retryable or isinstance(exc, LockHeld)) and not retried
        auth = exc if isinstance(exc, AuthFailure) else None  # REQ-F-063: the gate's fields
        return self._envelope(
            entry.code.value,
            data=data,
            error=ErrorDetail(
                code=exc.code,
                message=message,
                retryable=retrying,
                retries_exhausted=retried or None,
                detail=exc.detail,
                context=context,
                suggestion=exc.suggestion if exc.suggestion is not None else entry.suggestion,
                fix_command=fix,
                fix_required=exc.fix_required,
                hint=None if auth is None else auth.hint,
                refresh_command=None if auth is None else auth.refresh_command,
                expires_at=None if auth is None else auth.expires_at,
                required_permission=None if auth is None else auth.required_permission,
                retry_after_ms=retry_after if retrying else None,
                retry_strategy=strategy if retrying else None,
                conflict_id=exc.conflict_id,
                phase="execution",
            ),
            started=started,
            meta=meta,
        )

    def _record_late(
        self, command: Command, invocation: Invocation, pending: Pending, slot: Slot, call: str
    ) -> None:
        """Record an abandoned handler's result once it finishes, so a retry replays it
        instead of applying the mutation again. TIMEOUT or CANCELLED was already the
        response, so anything that keeps the result from being recorded goes to stderr."""
        outcome = pending.wait()
        redact = self._redactor(command, invocation.args)
        where = f"{command.path} finished after its response was written"
        if outcome.exc is not None:
            self.err.write(f"{where}, but failed:\n")
            self.err.write(redact("".join(traceback.format_exception(outcome.exc))))
            return
        try:
            data = self._payload(outcome.result, *self._output(command))
        except SchemaError as exc:
            self.err.write(f"{where}; its result was not recorded: {redact(str(exc))}\n")
            return
        except Exception as exc:  # noqa: BLE001 - a scalar's serialize= is user code
            self.err.write(f"{where}; serializing its result failed:\n")
            self.err.write(redact(_traceback(exc)))
            return
        problem = effect_problem(data, preview=False)
        if problem is not None:
            self.err.write(f"{where}; its result was not recorded: {problem}\n")
            return
        try:
            slot.save(Record(call, command.path.value, data, time.time()))
        except OSError as exc:
            self.err.write(f"{where}; recording its result failed: {exc.strerror}\n")
            return
        try:
            slot.prune(time.time())
        except OSError as exc:
            self.err.write(f"{where}; recorded, but pruning expired records failed: {exc}\n")

    def _redactor(self, command: Command, args: object) -> Callable[[str], str]:
        """Replace every spelling of the run's secret values: the value, its serialized
        form for a registered scalar, and the escaped form ``repr`` puts in messages"""
        spellings: set[str] = set()
        if self.token is not None and len(self.token) >= MIN_REDACTED:
            spellings.update({self.token, repr(self.token)[1:-1]})
        for f in command.fields:
            value = getattr(args, f.name, None) if f.secret else None
            # A default is in the source anyway; redacting it (max_tokens=1) garbles text
            if value is None or value == f.default:
                continue
            forms: list[object] = [value, str(value), repr(value)]
            if not isinstance(value, (str, int, float)):
                forms.append(to_jsonable(value, self.app.scalars, base=self.cwd))
            for form in forms:
                if isinstance(form, str) and len(form) >= MIN_REDACTED:
                    spellings.update({form, repr(form)[1:-1]})
        ordered = sorted(spellings, key=len, reverse=True)

        def redact(text: str) -> str:
            for spelling in ordered:
                text = text.replace(spelling, REDACTED)
            return text

        return redact

    def _output(self, command: Command) -> tuple[object, OutSpec]:
        """The type the handler's result has in the answered schema, and its order"""
        if self.pinned is None:
            return command.output_type, command.order
        return command.compat_for(self.pinned).output_type, NO_ORDER

    def _sorted_list(self, command: Command, result: Sequence[object]) -> list[object]:
        """A list command's whole list in output order, so every page is a slice of one
        order (REQ-F-020)"""
        items = list(result)
        if command.order.ordered:
            return items
        item_type = (typing.get_args(command.output_type) or (object,))[0]
        jsonable = [
            arrange(to_jsonable(i, self.app.scalars, base=self.cwd), item_type) for i in items
        ]
        return [items[i] for i in sorted_indices(jsonable, command.order.sort_key)]

    def _payload(self, value: object, tp: object = object, order: OutSpec = NO_ORDER) -> object:
        """A result or exit ``data`` as envelope data: an object, an array, or null, with
        relative paths made absolute and arrays sorted as ``tp`` declares"""
        data = arrange(
            to_jsonable(value, self.app.scalars, base=self.cwd), tp, order, stable=self.stable
        )
        if isinstance(value, Job) and isinstance(data, dict):
            data = with_links(data, self.app.name)  # REQ-C-022
        if data is not None and not isinstance(data, (dict, list)):
            raise SchemaError(f"{type(value).__name__}, not an object, array, or null")
        return data

    def _broken(
        self, command: Command, code: str, message: str, started: float, meta: Mapping[str, object]
    ) -> Envelope:
        """GENERAL_ERROR for a handler that broke the framework contract"""
        entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=code,
                message=message,
                retryable=False,
                context={"command": command.path.value},
                phase="execution",
            ),
            started=started,
            meta=meta,
        )

    def _crashed(
        self,
        command: Command,
        args: object,
        exc: BaseException,
        started: float,
        meta: Mapping[str, object],
    ) -> Envelope:
        """A handler bug: the traceback goes to stderr and the envelope names the exception,
        so every exit still carries an envelope. Secret argument values are redacted.
        ``sys.exit()`` counts: a handler ends a run by returning or raising ``Exit``."""
        redact = self._redactor(command, args)
        if self.trace_id is not None:
            self.err.write(f"{self.app.name}: {command.path} crashed{self._trace_suffix()}\n")
        self.err.write(redact(_traceback(exc)))
        entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code="HANDLER_CRASHED",
                message=redact(f"Command {command.path} raised {type(exc).__name__}: {_text(exc)}"),
                retryable=False,
                context={"command": command.path.value, "exception": type(exc).__qualname__},
                phase="execution",
                fix_required="a bug in the command; stderr has the traceback",
            ),
            started=started,
            meta=meta,
        )

    # Output

    def emit(self, mode: Format, envelope: Envelope, *, render: Renderer | None = None) -> int:
        if mode is Format.JSON:
            self._write(envelope)
            return envelope.exit_code
        return self._emit_text(mode, envelope, render)

    def output_closed(self) -> int:
        """The reader went away: nothing more can be written, and nothing goes to stderr.
        After a complete envelope or event (``tool logs | head -1``) the reader got what it
        wanted, so exit 0 (REQ-F-014); before any, it got no answer, so exit 141, the SIGPIPE
        convention (``OUTPUT_CLOSED``). A stream cut off mid-way runs its cleanup hook like
        a cancellation; a finished handler has nothing left to clean up."""
        command, self.in_flight = self.in_flight, None
        if command is not None and command.cleanup is not None:
            try:
                command.cleanup()
            except Exception as exc:  # noqa: BLE001 - cleanup= is user code
                self.err.write(_traceback(exc))
        if self.out is sys.stdout:
            # The interpreter flushes stdout at exit; a dead pipe would raise there too
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        return 0 if self.delivered else 141

    def to_file(
        self, path: Path, mode: Format, envelope: Envelope, render: Renderer | None
    ) -> Envelope:
        """Write a successful result's ``data`` to ``path``; the envelope then describes
        the write. A failed run writes no file."""
        if not envelope.ok or envelope.data is None:
            return envelope
        data = envelope.data
        if mode is Format.JSONL:
            items = data if isinstance(data, list) else [data]
            text = "".join(
                json.dumps(i, separators=(",", ":"), sort_keys=True) + "\n" for i in items
            )
        elif mode is Format.JSON:
            text = _json_text(data)
        else:
            try:
                text = render(data) if render is not None else render_plain(data)
            except Exception as exc:  # noqa: BLE001 - a renderer is user code
                self.err.write(_traceback(exc))
                return self._file_error(
                    envelope, "RENDER_FAILED", f"the {mode} renderer failed", path
                )
        try:
            write_atomic(path, text, new_mode=0o644)  # REQ-F-070
        except OSError as exc:
            return self._file_error(
                envelope, "OUTPUT_UNWRITABLE", f"cannot write --output: {exc.strerror}", path
            )
        written = {"path": str(path), "bytes": len(text.encode("utf-8"))}
        return dataclasses.replace(envelope, data=written)

    def _file_error(self, envelope: Envelope, code: str, message: str, path: Path) -> Envelope:
        """The result stays in ``data``, so a run that could not write its file loses nothing"""
        entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
        return dataclasses.replace(
            envelope,
            exit_code=entry.code.value,
            error=ErrorDetail(
                code=code,
                message=message,
                retryable=False,
                context={"output": str(path)},
                phase="execution",
            ),
        )

    def emit_stream(
        self,
        mode: Format,
        envelopes: Generator[Envelope],
        *,
        render: Renderer | None,
    ) -> int:
        """Write each envelope as it arrives; the exit code is the terminal envelope's,
        or GENERAL_ERROR when the renderer failed on a successful stream"""
        # Closed on any exit, so a dead reader (BrokenPipeError) still runs the handler's
        # finally blocks instead of leaving them to garbage collection
        with contextlib.closing(envelopes):
            return self._write_stream(mode, envelopes, render)

    def _write_stream(
        self,
        mode: Format,
        envelopes: Generator[Envelope],
        render: Renderer | None,
    ) -> int:
        code = 0
        render_failed = False
        for envelope in drain(envelopes):
            if mode is Format.JSON:
                self._write(envelope)
                code = envelope.exit_code
                continue
            code = self._emit_text(mode, envelope, render, fallback=render_event)
            if code != envelope.exit_code:
                # One traceback is enough: later events use the plain fallback
                render_failed, render = True, None
        if render_failed and code == 0:
            return FrameworkCode.GENERAL_ERROR.value
        return code

    def _emit_text(
        self,
        mode: Format,
        envelope: Envelope,
        render: Renderer | None,
        *,
        fallback: Renderer = render_plain,
    ) -> int:
        """Data through the renderer on stdout, errors as prose on stderr"""
        code = envelope.exit_code
        if envelope.data is not None and render is not None:
            try:
                text = render(envelope.data)
            except Exception as exc:  # noqa: BLE001 - a renderer is user code
                self.err.write("".join(traceback.format_exception(exc)))
                self.err.write(f"{self.app.name}: HANDLER_CRASHED: the {mode} renderer failed\n")
                code = FrameworkCode.GENERAL_ERROR.value
            else:
                # Outside the renderer's try: a closed stdout is not a renderer bug
                self.out.write(text)
        elif envelope.data is not None:
            self.out.write(fallback(envelope.data))
        if envelope.error is not None:
            error = envelope.error
            self.err.write(
                f"{self.app.name}: {error.code}: {error.message}{self._trace_suffix()}\n"
            )
            errors = envelope.error.errors or ()
            if len(errors) > 1:
                for item in errors:
                    where = f"{item['field']}: " if "field" in item else ""
                    self.err.write(f"  - {where}{item['message']}\n")
            else:
                for key, value in envelope.error.context.items():
                    self.err.write(f"  {key}: {value}\n")
            if envelope.error.suggestion is not None:
                self.err.write(f"hint: {envelope.error.suggestion}\n")
        self.out.flush()
        self.delivered = True
        self.err.flush()
        return code

    def schema(self, mode: Format, path: CommandPath | None, prefix: tuple[str, ...]) -> int:
        """``--schema`` is machine output in every mode; the envelope carries it as data"""
        if path is not None:
            data = command_schema(self.app.commands[path], self.app.exits, self.app.commands)
        else:
            subtree = {
                p: c for p, c in self.app.commands.items() if p.parts[: len(prefix)] == prefix
            }
            data = build_manifest(
                subtree, self.app.exits, self.app.version, self.app.formats, self.app.name
            )
        return self.emit(mode, self._envelope(0, data=data), render=_json_text)

    def show_config(self, mode: Format) -> int:
        """``--show-config``: the effective settings, where each came from, and the layers
        in precedence order, as JSON in every mode (REQ-O-015)"""
        data = self.settings.show(self.app.settings)
        return self.emit(mode, self._envelope(0, data=data), render=_json_text)

    def output_schema(
        self, mode: Format, command: Command | None, pinned: SchemaVersion | None
    ) -> int:
        """``<cmd> --output-schema``: the JSON Schema of the command's ``data``, in the
        pinned schema version's shape when ``--schema-version`` selected an older one"""
        if command is None:
            return self.emit(
                mode,
                self.arg_error(
                    ParseError(
                        "--output-schema describes one command's data; name the command",
                        context={"flag": "output-schema"},
                        suggestion=f"{self.app.name} <command> --output-schema, or "
                        f"{self.app.name} --schema for every command",
                    )
                ),
            )
        self.pinned = pinned
        data = command.output_schema if pinned is None else command.compat_for(pinned).output_schema
        return self.emit(mode, self._envelope(0, data=dict(data)), render=_json_text)

    def help_root(self, mode: Format, prefix: tuple[str, ...]) -> int:
        text = render_root(
            self.app.name,
            self.app.description,
            self.app.commands,
            self.app._groups,
            self._global_rows(),
            self.app.environment(),
            prefix,
        )
        return self._help(mode, prefix, text)

    def help_command(self, mode: Format, command: Command) -> int:
        text = render_command(self.app.name, command, self._global_rows())
        return self._help(mode, command.path.parts, text)

    def _global_rows(self) -> list[tuple[str, str]]:
        return global_rows(global_flag_entries(self.app.formats, self.app.name))

    def _help(self, mode: Format, parts: tuple[str, ...], text: str) -> int:
        """Help text on stdout for a person; in JSON mode it goes to stderr and stdout gets
        only a pointer to ``--schema`` (REQ-F-048)"""
        if mode is not Format.JSON:
            self.out.write(text)
            return 0
        self.err.write(text)
        self.err.flush()
        schema_ref = " ".join((*parts, "--schema"))
        meta = {"help": True, "schema_ref": schema_ref}
        return self.emit(mode, self._envelope(0, meta=meta))

    # exec (REQ-O-050)

    def exec(self, args: ExecArgs) -> int:
        """Dispatch each plan line in-process; JSONL envelopes out; 0, 1, or 2"""
        plan_command = self.current
        text = self._read_plan(args)
        self.payload_stdin = None  # the plan is stdin; a line's payload needs input_file
        if isinstance(text, Envelope):
            return self.emit(Format.JSON, text)
        # Only \n ends a JSONL line: splitlines() would also break on U+2028, U+2029,
        # and U+0085, which JSON allows raw inside strings
        plan = text.removeprefix("\ufeff").split("\n")
        any_failed = False
        parsed_any = False
        lines_seen = 0
        last: Envelope | None = None
        # Closed on any exit, so a dead reader still closes the step in flight
        with contextlib.closing(self._exec_lines(args, plan)) as lines:
            for line_no, envelope in lines:
                lines_seen, last = line_no, envelope
                self._write(envelope)
                if envelope.error is None or envelope.error.code not in _UNREAD_LINE:
                    parsed_any = True
                if not envelope.ok:
                    any_failed = True
                    if not args.ignore_errors:
                        break
        # What follows answers the plan, not its last line
        self.current, self.pinned, self.retrier, self.warnings = plan_command, None, None, []
        self.stable = self.stable_all
        if (received := self.cancellation.received) is not None:
            # A signal ends the plan whatever --ignore-errors says (REQ-F-069). One held
            # between lines left no CANCELLED line, so the plan says where it stopped.
            if last is None or last.error is None or last.error.code != "CANCELLED":
                self._write(self._plan_cancelled(received, lines_seen))
            return received.exit_code
        if not lines_seen:
            return self.emit(
                Format.JSON,
                self._stream_error(
                    "EMPTY_STREAM", "no DispatchRequest lines in the plan", context={"lines": 0}
                ),
            )
        if not parsed_any:
            return FrameworkCode.ARG_ERROR.value
        return FrameworkCode.GENERAL_ERROR.value if any_failed else 0

    def _plan_cancelled(self, sig: CancelSignal, lines_run: int) -> Envelope:
        entry = self.app.exits.by_code(sig.exit_code)
        return self._envelope(
            sig.exit_code,
            error=ErrorDetail(
                code="CANCELLED",
                message=f"exec plan cancelled by {sig.name} after line {lines_run}",
                retryable=entry.retryable,
                context={"signal": sig.name, "lines_run": lines_run},
                phase="execution",
            ),
            meta={"partial": lines_run > 0},
        )

    def _read_plan(self, args: ExecArgs) -> str | Envelope:
        """The whole plan, read before dispatch so a write-then-read caller cannot deadlock"""
        try:
            return self._read_input(args.input_file)
        except Cancelled as exc:
            return self._plan_cancelled(exc.signal, 0)

    def _read_input(
        self, input_file: Path | None, *, meta: Mapping[str, object] | None = None
    ) -> str | Envelope:
        """A payload from ``--input-file``, of any size, or from stdin up to the stdin cap
        (REQ-F-054); ``Cancelled`` when a signal ends the wait for a slow writer"""
        if input_file is not None and input_file != Path("-"):
            try:
                return input_file.read_text(encoding="utf-8-sig")  # tolerate a BOM
            except (OSError, UnicodeDecodeError) as exc:
                return self._stream_error(
                    "INPUT_FILE_UNREADABLE",
                    f"cannot read --input-file: {exc}",
                    context={"input_file": str(input_file)},
                    fix_required="pass a readable UTF-8 file, or - to read stdin",
                    meta=meta,
                )
        stdin = self.payload_stdin
        if stdin is None:
            return self._stream_error(
                "STDIN_UNAVAILABLE",
                self.no_payload,
                context={},
                fix_required="pass input_file with the path of the payload",
                meta=meta,
            )
        if stdin.isatty():
            # Reading a terminal would block until the user types EOF
            return self._stream_error(
                "STDIN_IS_TTY",
                "the input is read from stdin, not a terminal",
                context={"lines": 0},
                fix_required="pipe the input into stdin, or pass --input-file",
                meta=meta,
            )
        try:
            cap = StdinCap.resolve(self.env, self.app.max_stdin, self.app.name)
        except ParseError as exc:
            return self.arg_error(exc, meta=meta)
        # One more character than the cap is always more bytes than the cap
        try:
            # A writer that keeps the pipe open must still be able to cancel the read
            with self.cancellation.armed():
                text = stdin.read(cap.bytes + 1)
            size = len(text.encode("utf-8"))
        except (UnicodeDecodeError, UnicodeEncodeError) as exc:
            # A strict stdin fails to decode; a surrogateescape one fails to re-encode
            return self._stream_error(
                "STDIN_NOT_UTF8",
                f"stdin is not valid UTF-8: {exc.reason}",
                context={"lines": 0},
                fix_required="pipe UTF-8 into stdin, or pass --input-file",
                meta=meta,
            )
        if size > cap.bytes:
            return self._stream_error(
                "STDIN_TOO_LARGE",
                f"stdin exceeds the {cap.bytes}-byte limit",
                context={"limit_bytes": cap.bytes},
                fix_required="write the input to a file and pass --input-file <path>",
                hint="--input-file <path> reads the input from a file, with no size limit",
                meta=meta,
            )
        return text

    def stdin_value(self, flag: str) -> str:
        """All of stdin for ``--flag -`` (REQ-O-006), capped like a payload; every failure
        is phase 1, exit 2"""
        stdin = self.payload_stdin
        if stdin is None:
            raise ParseError(
                f"'-' for {flag!r} reads stdin, which is closed",
                code="EMPTY_STDIN",
                context={"flag": flag},
            )
        if stdin.isatty():
            raise ParseError(
                f"'-' for {flag!r} reads stdin, not a terminal",
                code="STDIN_IS_TTY",
                context={"flag": flag},
                suggestion=f"pipe the value into stdin, or pass --{flag} <value>",
            )
        cap = StdinCap.resolve(self.env, self.app.max_stdin, self.app.name)
        try:
            text = stdin.read(cap.bytes + 1)
        except (UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise ParseError(
                f"stdin is not valid UTF-8: {exc.reason}",
                code="STDIN_NOT_UTF8",
                context={"flag": flag},
            ) from None
        if len(text.encode("utf-8")) > cap.bytes:
            raise ParseError(
                f"stdin exceeds the {cap.bytes}-byte limit",
                code="STDIN_TOO_LARGE",
                context={"flag": flag, "limit_bytes": cap.bytes},
            )
        return text

    def _stream_error(
        self,
        code: str,
        message: str,
        *,
        context: Mapping[str, object],
        fix_required: str = "pipe one DispatchRequest JSON object per line into exec, "
        "or pass --input-file",
        hint: str | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        entry = self.app.exits.framework(FrameworkCode.ARG_ERROR)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=code,
                message=message,
                retryable=False,
                context=context,
                phase="validation",
                fix_required=fix_required,
                hint=hint,
            ),
            meta=meta,
        )

    def _exec_lines(self, args: ExecArgs, plan: list[str]) -> Generator[tuple[int, Envelope]]:
        for line_no, raw in enumerate(plan, start=1):  # physical lines, as an editor counts
            if self.cancellation.received is not None:
                return  # a signal stops the plan before its next line
            line = raw.strip()
            if not line:
                continue
            # One command per line
            self.warnings, self.token, self.config_file = [], None, None
            self.current, self.pinned, self.retrier = None, None, None
            started = time.perf_counter()
            meta: dict[str, object] = {"_line": line_no}
            try:
                request = parse_dispatch_line(line, line_no)
            except ParseError as exc:
                yield (
                    line_no,
                    self.arg_error(
                        exc, code=exc.code or "DISPATCH_PARSE_ERROR", started=started, meta=meta
                    ),
                )
                continue
            meta["_cmd"] = request.path.value
            command = self.app.commands.get(request.path)
            if command is None and (found := self.app.moved(request.path.parts)) is not None:
                source, moved, _ = found
                # A plan line names the command by its path, so that is what it resends
                yield line_no, self.redirected(source, moved, moved.to.value, meta=meta)
                continue
            if command is None or request.path == EXEC_PATH:
                yield (
                    line_no,
                    self.arg_error(
                        ParseError(
                            f"line {line_no}: unknown command {request.path}",
                            context={"line": line_no, "_cmd": request.path.value},
                        ),
                        code="UNKNOWN_COMMAND",
                        started=started,
                        meta=meta,
                    ),
                )
                continue
            self.current = command
            try:
                invocation = self._exec_invocation(command, request, args.dry_run, line_no)
            except ParseError as exc:
                failed = {**meta, **_mode_meta(command)}
                yield line_no, self.arg_error(exc, started=started, meta=failed)
                continue
            except ArgsCrashed as exc:
                yield line_no, self.args_crashed(command, exc, started=started, meta=meta)
                continue
            if command.streaming:
                if invocation.no_stream:
                    envelopes = self.stream(command, invocation, Format.JSON, meta=meta, whole=True)
                    yield line_no, buffer_stream(envelopes)
                    continue
                envelopes = self.stream(command, invocation, Format.JSON, meta=meta)
                self.in_flight = command
                with contextlib.closing(envelopes):
                    for envelope in envelopes:
                        yield line_no, envelope
                self.in_flight = None
                continue
            yield line_no, self.execute(command, invocation, Format.JSON, meta=meta)

    def _exec_invocation(
        self, command: Command, request: DispatchRequest, dry_run: bool, line_no: int
    ) -> Invocation:
        mapping: dict[str, object] = {}
        for key, value in (*request.payload.items(), *request.opts.items()):
            # One field may be spelled dry_run in the payload and dry-run in _opts
            name = key.replace("-", "_")
            if name in mapping and mapping[name] != value:
                raise ParseError(
                    f"line {line_no}: {key!r} given twice with different values",
                    context={"line": line_no, "_cmd": command.path.value, "field": key},
                )
            mapping[name] = value
        if dry_run and command.danger_level is not DangerLevel.SAFE:
            if command.field_by_flag("dry-run") is None:
                raise ParseError(
                    f"line {line_no}: {command.path} is {command.danger_level.value} "
                    "but has no dry_run flag to honor --dry-run",
                    context={"line": line_no, "_cmd": command.path.value},
                )
            mapping["dry_run"] = True
        return build_from_mapping(command, mapping, self.env)
