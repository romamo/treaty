"""The application: one flat registry, mode resolution, dispatch, and envelopes."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, NoReturn

from ._cap import DEFAULT_CAP, DEFAULT_STDIN_CAP, OutputCap, StdinCap, cap_envelope
from ._command import (
    Cleanup,
    Command,
    DangerLevel,
    Example,
    Handler,
    HumanRenderer,
    build_command,
)
from ._context import Ctx
from ._dispatch import DispatchRequest, parse_dispatch_line
from ._effect import effect_problem
from ._envelope import Envelope, ErrorDetail, write_envelope
from ._errors import CliExit, ParseError, RegistrationError
from ._exit import ExitCodeEntry, ExitCodeRegistry, FrameworkCode, SideEffects
from ._flags import Flag
from ._help import render_command, render_root
from ._idempotency import Record, claim, fingerprint, state_dir
from ._manifest import build_manifest, build_schema_manifest, command_schema
from ._mode import OutputMode, resolve_mode
from ._parse import (
    Invocation,
    Route,
    build_from_mapping,
    format_hint,
    misplaced_flag_target,
    parse_command_args,
    resolve_path,
    split_globals,
    without_value,
)
from ._resources import Resolver
from ._scalars import ScalarRegistry, ScalarSpec, default_serializer
from ._schema import to_jsonable
from ._signals import Cancelled, CancelSignal, cancellation_handlers
from ._timeout import Timeout, TimeoutExpired, call_with_timeout
from ._values import CommandPath, ExitCode, ExitCodeName, InvalidValue, Scope

EXEC_PATH = CommandPath("exec")
VERSION_PATH = CommandPath("version")
DEFAULT_TIMEOUT = Timeout(60.0)


class _Inherit:
    """Sentinel: the command inherits the app default timeout"""


INHERIT = _Inherit()


@dataclass(frozen=True, slots=True)
class NoArgs:
    """Arguments dataclass for commands that take nothing"""


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
    ) -> None:
        if not name or not version:
            raise RegistrationError("App needs a name and a version")
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
        self._register_builtins(enable_exec)

    # Registration

    def exit_code(
        self,
        name: str,
        code: int,
        *,
        description: str,
        retryable: bool,
        side_effects: str,
    ) -> ExitCodeEntry:
        return self.exits.register(
            ExitCodeEntry(
                name=ExitCodeName(name),
                code=ExitCode(code),
                description=description,
                retryable=retryable,
                side_effects=SideEffects(side_effects),
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

    def group(self, path: str, *, description: str) -> Group:
        prefix = CommandPath(path)
        if prefix in self._commands:
            raise RegistrationError(f"{prefix} is already a command")
        if not description:
            raise RegistrationError(f"group {prefix} needs a description")
        self._groups[prefix] = description
        return Group(self, prefix)

    def command(
        self,
        path: str,
        *,
        description: str,
        danger_level: str = "safe",
        required_scopes: Sequence[str] = (),
        exit_codes: Sequence[str] = (),
        examples: Sequence[tuple[str, str]] = (),
        has_network_io: bool = False,
        timeout: float | None | _Inherit = INHERIT,
        supports_raw_payload: bool = False,
        cleanup: Cleanup | None = None,
        human: HumanRenderer | None = None,
        streaming: bool = False,
    ) -> Callable[[Handler], Handler]:
        cmd_path = CommandPath(path)
        if isinstance(timeout, _Inherit):
            # A stream serves until told to stop; the app default is for one-shot handlers
            command_timeout = Timeout(None) if streaming else None
        else:
            command_timeout = Timeout(timeout)

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
                    human=human,
                    scalars=self.scalars,
                    streaming=streaming,
                )
            )
            return fn

        return register

    def _register(self, command: Command) -> None:
        path = command.path
        if path in self._commands:
            raise RegistrationError(f"{path} is already registered")
        if path in self._groups:
            raise RegistrationError(f"{path} is already a group")
        for name in command.exit_codes:
            if name not in self.exits:
                raise RegistrationError(f"{path}: exit code {name} is not registered")
        self._commands[path] = command

    def _register_builtins(self, enable_exec: bool) -> None:
        @self.command("manifest", description="Print the command manifest for agents")
        def manifest(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            return build_manifest(self._commands, self.exits, self.version)

        @self.command(VERSION_PATH.value, description="Print the tool name and version")
        def version(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {"name": self.name, "version": self.version}

        if enable_exec:

            @self.command(
                EXEC_PATH.value,
                description="Dispatch JSONL DispatchRequest lines from stdin in-process",
                examples=[("Run a plan", f"cat ops.jsonl | {self.name} exec --ignore-errors")],
            )
            def exec_(args: ExecArgs, ctx: Ctx) -> None:
                raise RegistrationError("exec is dispatched by the framework, not called directly")

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
    def commands(self) -> Mapping[CommandPath, Command]:
        return self._commands

    def manifest(self) -> dict[str, object]:
        return build_manifest(self._commands, self.exits, self.version)

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
        declares them. A streaming command returns its buffered envelope. Nothing is
        written: the caller owns the envelope. Used by the MCP adapter.
        """
        environ = env if env is not None else os.environ
        run = _Run(self, io.StringIO(), io.StringIO(), environ)
        meta: dict[str, object] = {"_cmd": path}
        try:
            command_path = CommandPath(path)
        except InvalidValue as exc:
            return run.arg_error(ParseError(str(exc), context={"_cmd": path}), meta=meta)
        command = self._commands.get(command_path)
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
        try:
            invocation = build_from_mapping(command, arguments, environ)
        except ParseError as exc:
            return run.arg_error(exc, meta=meta)
        if command.streaming:
            return buffer_stream(run.stream(command, invocation, OutputMode.JSON, meta=meta))
        return run.execute(command, invocation, OutputMode.JSON, meta=meta)

    def main(self) -> NoReturn:
        sys.exit(self.run(sys.argv[1:]))

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
        out = stdout if stdout is not None else sys.stdout
        err = stderr if stderr is not None else sys.stderr
        inp = stdin if stdin is not None else sys.stdin
        environ = env if env is not None else os.environ
        run = _Run(self, out, err, environ)
        try:
            globals_, rest = split_globals(list(argv))
            mode = resolve_mode(
                globals_.format, environ, out.isatty() if isatty is None else isatty
            )
            run.cap = OutputCap.resolve(globals_.max_output, environ, self.max_output)
        except ParseError as exc:
            return run.emit(OutputMode.JSON, run.arg_error(exc))
        route = resolve_path(rest, self._commands)
        if route.path is None and not route.prefix and route.tokens == ("--version",):
            # Root-only alias so a command's own --version flag is never shadowed
            route = Route(path=VERSION_PATH, prefix=VERSION_PATH.parts, tokens=())
        if globals_.schema:
            return run.schema(mode, route.path, route.prefix)
        if route.path is None:
            if globals_.help or not route.tokens:
                return run.help_root(mode, route.prefix)
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
        command = self._commands[route.path]
        if globals_.help:
            return run.help_command(mode, command)
        try:
            invocation = parse_command_args(command, route.tokens, environ)
        except ParseError as exc:
            return run.emit(mode, run.arg_error(exc))
        with cancellation_handlers(out):
            if command.path == EXEC_PATH:
                assert isinstance(invocation.args, ExecArgs)
                return run.exec(invocation.args, inp)
            if command.streaming:
                envelopes = run.stream(command, invocation, mode)
                if invocation.no_stream:
                    return run.emit(mode, buffer_stream(envelopes), render=command.human)
                return run.emit_stream(mode, envelopes, render=command.human)
            return run.emit(mode, run.execute(command, invocation, mode), render=command.human)


def _invoke(command: Command, args: object, ctx: Ctx) -> object:
    """Acquire the handler's resources, each once and in dependency order, then run it"""
    resolver = Resolver(command.resource_graph, args, ctx)
    return command.handler(args, ctx, *resolver.all(command.resources))


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

    def __init__(self, app: App, out: IO[str], err: IO[str], env: Mapping[str, str]) -> None:
        self.app = app
        self.out = out
        self.err = err
        self.env = env
        self.started = time.perf_counter()
        self.request_id = uuid.uuid4().hex[:12]
        self.cap = app.max_output

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
        return Envelope(
            exit_code=code,
            data=data,
            error=error,
            duration_ms=int((time.perf_counter() - origin) * 1000),
            request_id=self.request_id,
            extra_meta=dict(meta or {}),
        )

    def arg_error(self, exc: ParseError, *, code: str = "ARG_ERROR", **kw: Any) -> Envelope:
        entry = self.app.exits.framework(FrameworkCode.ARG_ERROR)
        return self._envelope(
            entry.code.value,
            error=ErrorDetail(
                code=code,
                message=exc.message,
                retryable=False,
                context=exc.context,
                suggestion=exc.suggestion,
                phase="validation",
                fix_required="correct the arguments and reissue",
                errors=exc.items(),
            ),
            **kw,
        )

    def execute(
        self,
        command: Command,
        invocation: Invocation,
        mode: OutputMode,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        """Run one handler, replaying the stored result when its idempotency key was seen"""
        key = invocation.idempotency_key
        if key is None or _previewing(command, invocation) or _dry_run_requested(invocation.args):
            return self._execute(command, invocation, mode, meta=meta)
        started = time.perf_counter()
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        directory = state_dir(self.app.name, self.app.state_dir, self.env)
        if directory is None:
            entry = self.app.exits.framework(FrameworkCode.PRECONDITION)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="STATE_DIR_UNKNOWN",
                    message="no directory to keep idempotency records in",
                    retryable=False,
                    phase="validation",
                    fix_required="set TREATY_STATE_DIR, XDG_STATE_HOME, or HOME",
                ),
                started=started,
                meta=full_meta,
            )
        call = fingerprint(command.path, invocation.args, self.app.scalars)
        with claim(directory, key) as slot:
            if slot.record is None:
                envelope = self._execute(command, invocation, mode, meta=meta)
                if envelope.exit_code == 0:
                    slot.save(Record(call, command.path.value, envelope.data, time.time()))
                return envelope
            if slot.record.fingerprint != call:
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
            assert isinstance(slot.record.data, dict)
            return self._envelope(
                0,
                data={**slot.record.data, "effect": "noop"},
                started=started,
                meta={**full_meta, "idempotency_hit": True},
            )

    def _execute(
        self,
        command: Command,
        invocation: Invocation,
        mode: OutputMode,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Envelope:
        """Run one handler under its timeout and turn the outcome into an envelope"""
        started = time.perf_counter()
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        ctx = Ctx(
            app_name=self.app.name,
            version=self.app.version,
            mode=mode,
            request_id=self.request_id,
            env=self.env,
            state=self.app._state,
            timeout=timeout,
            idempotency_key=None
            if invocation.idempotency_key is None
            else invocation.idempotency_key.value,
        )
        args = invocation.args
        preview_only = _previewing(command, invocation)
        if preview_only:
            assert dataclasses.is_dataclass(args) and not isinstance(args, type)
            args = dataclasses.replace(args, dry_run=True)
        try:
            result = call_with_timeout(lambda: _invoke(command, args, ctx), timeout)
        except CliExit as exc:
            return self._exit_envelope(command, exc, started, full_meta)
        except ParseError as exc:
            # A handler validating its own input before any side effect
            return self.arg_error(exc, started=started, meta=full_meta)
        except TimeoutExpired:
            entry = self.app.exits.framework(FrameworkCode.TIMEOUT)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="TIMEOUT",
                    message=f"{command.path} exceeded {timeout.seconds}s",
                    retryable=entry.retryable,
                    context={"timeout_ms": timeout.milliseconds, "command": command.path.value},
                    phase="execution",
                ),
                started=started,
                meta=full_meta,
            )
        except Cancelled as exc:
            return self._cancelled(command, exc.signal, started, full_meta)
        except KeyboardInterrupt:
            return self._cancelled(command, CancelSignal("SIGINT", 130), started, full_meta)
        data = to_jsonable(result, self.app.scalars)
        if command.danger_level is not DangerLevel.SAFE:
            problem = effect_problem(data, preview=_dry_run_requested(args))
            if problem is not None:
                entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
                return self._envelope(
                    entry.code.value,
                    error=ErrorDetail(
                        code="INVALID_EFFECT",
                        message=f"{command.path} broke the effect contract: {problem}",
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
                    message=f"{command.path} is destructive; nothing was applied",
                    retryable=False,
                    context={"command": command.path.value, "flag": "confirm-destructive"},
                    phase="validation",
                    fix_required="rerun with --confirm-destructive to apply",
                ),
                started=started,
                meta=full_meta,
            )
        return self._envelope(0, data=data, started=started, meta=full_meta)

    def stream(
        self,
        command: Command,
        invocation: Invocation,
        mode: OutputMode,
        *,
        meta: Mapping[str, object] | None = None,
    ) -> Generator[Envelope]:
        """Run a generator handler: one envelope per event, then a terminal one (REQ-O-004)

        A timeout is a deadline for the whole stream. A failure after some events keeps
        their count in ``meta.seq`` and marks the response ``partial``.
        """
        started = time.perf_counter()
        timeout = self.app.effective_timeout(command, invocation.timeout)
        full_meta: dict[str, object] = {"timeout_ms": timeout.milliseconds, **(meta or {})}
        ctx = Ctx(
            app_name=self.app.name,
            version=self.app.version,
            mode=mode,
            request_id=self.request_id,
            env=self.env,
            state=self.app._state,
            timeout=timeout,
        )
        args = invocation.args
        seq = 0
        events: Iterator[object] | None = None

        def remaining() -> Timeout:
            if timeout.seconds is None:
                return timeout
            left = timeout.seconds - (time.perf_counter() - started)
            if left <= 0:
                raise TimeoutExpired(timeout)
            return Timeout(left)

        def partial() -> dict[str, object]:
            return {**full_meta, "seq": seq, "partial": True}

        try:
            produced = call_with_timeout(lambda: _invoke(command, args, ctx), remaining())
            if not isinstance(produced, Iterator):
                raise TypeError(
                    f"{command.path} is streaming but returned {type(produced).__name__}, "
                    "not a generator"
                )
            events = produced
            while True:
                event = call_with_timeout(lambda: next(produced, _END), remaining())
                if event is _END:
                    break
                seq += 1
                data = to_jsonable(event, self.app.scalars)
                yield self._envelope(0, data=data, started=started, meta={**full_meta, "seq": seq})
        except CliExit as exc:
            yield self._exit_envelope(command, exc, started, partial())
            return
        except ParseError as exc:
            yield self.arg_error(exc, started=started, meta=partial())
            return
        except TimeoutExpired:
            entry = self.app.exits.framework(FrameworkCode.TIMEOUT)
            yield self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="TIMEOUT",
                    message=f"{command.path} exceeded {timeout.seconds}s",
                    retryable=entry.retryable,
                    context={"timeout_ms": timeout.milliseconds, "command": command.path.value},
                    phase="execution",
                ),
                started=started,
                meta=partial(),
            )
            return
        except Cancelled as exc:
            yield self._cancelled(command, exc.signal, started, {**full_meta, "seq": seq})
            return
        except KeyboardInterrupt:
            sig = CancelSignal("SIGINT", 130)
            yield self._cancelled(command, sig, started, {**full_meta, "seq": seq})
            return
        finally:
            # Run the handler's finally blocks now, unless a timed-out worker still holds it
            if events is not None and timeout.seconds is None and isinstance(events, Generator):
                events.close()
        yield self._envelope(
            0, started=started, meta={**full_meta, "seq": seq, "end": True, "total": seq}
        )

    def _cancelled(
        self, command: Command, sig: CancelSignal, started: float, meta: Mapping[str, object]
    ) -> Envelope:
        """Run the cleanup hook, then build the CANCELLED envelope (REQ-F-013, REQ-F-069)"""
        if command.cleanup is not None:
            command.cleanup()
        entry = self.app.exits.by_code(sig.exit_code)
        return self._envelope(
            sig.exit_code,
            error=ErrorDetail(
                code="CANCELLED",
                message=f"{command.path} cancelled by {sig.name}",
                retryable=entry.retryable,
                context={"signal": sig.name, "command": command.path.value},
                phase="execution",
            ),
            started=started,
            meta={**meta, "partial": True},
        )

    def _exit_envelope(
        self, command: Command, exc: CliExit, started: float, meta: Mapping[str, object]
    ) -> Envelope:
        framework_names = {ExitCodeName(c.name) for c in FrameworkCode}
        if exc.name not in command.exit_codes and exc.name not in framework_names:
            entry = self.app.exits.framework(FrameworkCode.GENERAL_ERROR)
            return self._envelope(
                entry.code.value,
                error=ErrorDetail(
                    code="UNDECLARED_EXIT_CODE",
                    message=f"{command.path} raised {exc.name}, which it does not declare",
                    retryable=False,
                    context={
                        "declared": [n.value for n in command.exit_codes],
                        "raised": exc.name.value,
                        "original_message": exc.message,
                    },
                    phase="execution",
                ),
                started=started,
                meta=meta,
            )
        entry = self.app.exits.by_name(exc.name)
        return self._envelope(
            entry.code.value,
            data=to_jsonable(exc.data, self.app.scalars),
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                retryable=entry.retryable,
                detail=exc.detail,
                context=exc.context,
                suggestion=exc.suggestion,
                fix_command=exc.fix_command,
                fix_required=exc.fix_required,
                retry_after_ms=exc.retry_after_ms if entry.retryable else None,
                phase="execution",
            ),
            started=started,
            meta=meta,
        )

    # Output

    def emit(
        self, mode: OutputMode, envelope: Envelope, *, render: HumanRenderer | None = None
    ) -> int:
        if mode is OutputMode.JSON:
            write_envelope(cap_envelope(envelope, self.cap), self.out)
            return envelope.exit_code
        return self._emit_human(envelope, render)

    def emit_stream(
        self,
        mode: OutputMode,
        envelopes: Generator[Envelope],
        *,
        render: HumanRenderer | None,
    ) -> int:
        """Write each envelope as it arrives; the exit code is the terminal envelope's"""
        code = 0
        for envelope in drain(envelopes):
            if mode is OutputMode.JSON:
                write_envelope(cap_envelope(envelope, self.cap), self.out)
                code = envelope.exit_code
            else:
                code = self._emit_human(envelope, render)
        return code

    def _emit_human(self, envelope: Envelope, render: HumanRenderer | None) -> int:
        if envelope.data is not None and render is not None:
            self.out.write(render(envelope.data))
        elif envelope.data is not None:
            self.out.write(json.dumps(envelope.data, indent=2, sort_keys=True) + "\n")
        if envelope.error is not None:
            self.err.write(f"{self.app.name}: {envelope.error.code}: {envelope.error.message}\n")
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
        self.err.flush()
        return envelope.exit_code

    def schema(self, mode: OutputMode, path: CommandPath | None, prefix: tuple[str, ...]) -> int:
        """``--schema`` is machine output in every mode; the envelope carries it as data"""
        if path is not None:
            data = command_schema(self.app.commands[path], self.app.exits, self.app.commands)
        else:
            subtree = {
                p: c for p, c in self.app.commands.items() if p.parts[: len(prefix)] == prefix
            }
            data = build_schema_manifest(subtree, self.app.exits, self.app.version)
        return self.emit(mode, self._envelope(0, data=data))

    def help_root(self, mode: OutputMode, prefix: tuple[str, ...]) -> int:
        if mode is OutputMode.JSON:
            if not prefix:
                return self.emit(mode, self._envelope(0, data=self.app.manifest()))
            # Group help is the group's subtree, not the whole tree (same scoping as --schema)
            subtree = {
                p: c for p, c in self.app.commands.items() if p.parts[: len(prefix)] == prefix
            }
            data = build_manifest(subtree, self.app.exits, self.app.version)
            return self.emit(mode, self._envelope(0, data=data))
        self.out.write(
            render_root(
                self.app.name, self.app.description, self.app.commands, self.app._groups, prefix
            )
        )
        return 0

    def help_command(self, mode: OutputMode, command: Command) -> int:
        if mode is OutputMode.JSON:
            commands = self.app.manifest()["commands"]
            assert isinstance(commands, dict)
            entry = {command.path.value: commands[command.path.value]}
            return self.emit(mode, self._envelope(0, data=entry))
        self.out.write(render_command(self.app.name, command))
        return 0

    # exec (REQ-O-050)

    def exec(self, args: ExecArgs, stdin: IO[str]) -> int:
        """Dispatch each plan line in-process; JSONL envelopes out; 0, 1, or 2"""
        text = self._read_plan(args, stdin)
        if isinstance(text, Envelope):
            return self.emit(OutputMode.JSON, text)
        plan = text.splitlines()
        any_failed = False
        parsed_any = False
        lines_seen = 0
        for line_no, envelope in self._exec_lines(args, plan):
            lines_seen = line_no
            write_envelope(cap_envelope(envelope, self.cap), self.out)
            if envelope.error is not None and envelope.error.code != "DISPATCH_PARSE_ERROR":
                parsed_any = True
            elif envelope.error is None:
                parsed_any = True
            if not envelope.ok:
                any_failed = True
                if not args.ignore_errors:
                    break
        if not lines_seen:
            return self.emit(
                OutputMode.JSON,
                self._stream_error(
                    "EMPTY_STREAM", "no DispatchRequest lines in the plan", context={"lines": 0}
                ),
            )
        if not parsed_any:
            return FrameworkCode.ARG_ERROR.value
        return FrameworkCode.GENERAL_ERROR.value if any_failed else 0

    def _read_plan(self, args: ExecArgs, stdin: IO[str]) -> str | Envelope:
        """The whole plan, read before dispatch so a write-then-read caller cannot deadlock"""
        if args.input_file is not None and args.input_file != Path("-"):
            try:
                return args.input_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return self._stream_error(
                    "INPUT_FILE_UNREADABLE",
                    f"cannot read --input-file: {exc}",
                    context={"input_file": str(args.input_file)},
                    fix_required="pass a readable UTF-8 file, or - to read stdin",
                )
        if stdin.isatty():
            # Reading a terminal would block until the user types EOF
            return self._stream_error(
                "STDIN_IS_TTY", "exec reads JSONL from stdin, not a terminal", context={"lines": 0}
            )
        try:
            cap = StdinCap.resolve(self.env, self.app.max_stdin)
        except ParseError as exc:
            return self.arg_error(exc)
        # One more character than the cap is always more bytes than the cap
        text = stdin.read(cap.bytes + 1)
        if len(text.encode()) > cap.bytes:
            return self._stream_error(
                "STDIN_TOO_LARGE",
                f"stdin plan exceeds the {cap.bytes}-byte limit",
                context={"limit_bytes": cap.bytes},
                fix_required="write the plan to a file and pass --input-file <path>",
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
            ),
        )

    def _exec_lines(self, args: ExecArgs, plan: list[str]) -> Iterator[tuple[int, Envelope]]:
        line_no = 0
        for raw in plan:
            line = raw.strip()
            if not line:
                continue
            line_no += 1
            started = time.perf_counter()
            meta: dict[str, object] = {"_line": line_no}
            try:
                request = parse_dispatch_line(line, line_no)
            except ParseError as exc:
                yield (
                    line_no,
                    self.arg_error(exc, code="DISPATCH_PARSE_ERROR", started=started, meta=meta),
                )
                continue
            meta["_cmd"] = request.path.value
            command = self.app.commands.get(request.path)
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
            try:
                invocation = self._exec_invocation(command, request, args.dry_run, line_no)
            except ParseError as exc:
                yield line_no, self.arg_error(exc, started=started, meta=meta)
                continue
            if command.streaming:
                envelopes = self.stream(command, invocation, OutputMode.JSON, meta=meta)
                if invocation.no_stream:
                    yield line_no, buffer_stream(envelopes)
                else:
                    for envelope in envelopes:
                        yield line_no, envelope
                continue
            yield line_no, self.execute(command, invocation, OutputMode.JSON, meta=meta)

    def _exec_invocation(
        self, command: Command, request: DispatchRequest, dry_run: bool, line_no: int
    ) -> Invocation:
        mapping: dict[str, object] = {**request.payload, **request.opts}
        if dry_run and command.danger_level is not DangerLevel.SAFE:
            if command.field_by_flag("dry-run") is None:
                raise ParseError(
                    f"line {line_no}: {command.path} is {command.danger_level.value} "
                    "but has no dry_run flag to honor --dry-run",
                    context={"line": line_no, "_cmd": command.path.value},
                )
            mapping["dry_run"] = True
        return build_from_mapping(command, mapping, self.env)
