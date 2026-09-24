"""The application: one flat registry, mode resolution, dispatch, and envelopes."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, NoReturn

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
from ._envelope import Envelope, ErrorDetail, write_envelope
from ._errors import CliExit, ParseError, RegistrationError
from ._exit import ExitCodeEntry, ExitCodeRegistry, FrameworkCode, SideEffects
from ._flags import Flag
from ._help import render_command, render_root
from ._manifest import build_manifest, build_schema_manifest, command_schema
from ._mode import OutputMode, resolve_mode
from ._parse import Invocation, build_from_mapping, parse_command_args, resolve_path, split_globals
from ._schema import to_jsonable
from ._signals import Cancelled, CancelSignal, cancellation_handlers
from ._timeout import Timeout, TimeoutExpired, call_with_timeout
from ._values import CommandPath, ExitCode, ExitCodeName, Scope

EXEC_PATH = CommandPath("exec")
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
        enable_exec: bool = True,
    ) -> None:
        if not name or not version:
            raise RegistrationError("App needs a name and a version")
        self.name = name
        self.version = version
        self.description = description
        self.default_timeout = Timeout(default_timeout)
        self.exits = ExitCodeRegistry()
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
    ) -> Callable[[Handler], Handler]:
        cmd_path = CommandPath(path)
        command_timeout = None if isinstance(timeout, _Inherit) else Timeout(timeout)

        def register(fn: Handler) -> Handler:
            self._register(
                build_command(
                    fn,
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

        @self.command("version", description="Print the tool name and version")
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
        except ParseError as exc:
            return run.emit(OutputMode.JSON, run.arg_error(exc))
        route = resolve_path(rest, self._commands)
        if globals_.schema:
            return run.schema(mode, route.path, route.prefix)
        if route.path is None:
            if globals_.help or not route.tokens:
                return run.help_root(mode, route.prefix)
            return run.emit(
                mode,
                run.arg_error(
                    ParseError(
                        f"unknown command {route.tokens[0]!r}",
                        context={
                            "argument": route.tokens[0],
                            "prefix": ".".join(route.prefix),
                            "available": sorted(p.value for p in self._commands),
                        },
                    )
                ),
            )
        command = self._commands[route.path]
        if globals_.help:
            return run.help_command(mode, command)
        try:
            invocation = parse_command_args(command, route.tokens)
        except ParseError as exc:
            return run.emit(mode, run.arg_error(exc))
        with cancellation_handlers(out):
            if command.path == EXEC_PATH:
                assert isinstance(invocation.args, ExecArgs)
                return run.exec(invocation.args, inp)
            return run.emit(mode, run.execute(command, invocation, mode), render=command.human)


def _dry_run_requested(args: object) -> bool:
    """True when a destructive command's args carry dry_run=True (field guaranteed by REQ-C-004)"""
    value = getattr(args, "dry_run", False)
    return isinstance(value, bool) and value


class _Run:
    """One process invocation: builds envelopes, writes them, tracks timing"""

    def __init__(self, app: App, out: IO[str], err: IO[str], env: Mapping[str, str]) -> None:
        self.app = app
        self.out = out
        self.err = err
        self.env = env
        self.started = time.perf_counter()
        self.request_id = uuid.uuid4().hex[:12]

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
                phase="validation",
                fix_required="correct the arguments and reissue",
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
        )
        args = invocation.args
        preview_only = command.danger_level is DangerLevel.DESTRUCTIVE and not (
            invocation.confirmed or _dry_run_requested(args)
        )
        if preview_only:
            assert dataclasses.is_dataclass(args) and not isinstance(args, type)
            args = dataclasses.replace(args, dry_run=True)
        try:
            result = call_with_timeout(lambda: command.handler(args, ctx), timeout)
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
        if preview_only:
            entry = self.app.exits.framework(FrameworkCode.ARG_ERROR)
            return self._envelope(
                entry.code.value,
                data=to_jsonable(result),
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
        return self._envelope(0, data=to_jsonable(result), started=started, meta=full_meta)

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
            data=to_jsonable(exc.data),
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
            write_envelope(envelope, self.out)
            return envelope.exit_code
        if envelope.data is not None and render is not None:
            self.out.write(render(envelope.data))
        elif envelope.data is not None:
            self.out.write(json.dumps(envelope.data, indent=2, sort_keys=True) + "\n")
        if envelope.error is not None:
            self.err.write(f"{self.app.name}: {envelope.error.code}: {envelope.error.message}\n")
            for key, value in envelope.error.context.items():
                self.err.write(f"  {key}: {value}\n")
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
            return self.emit(mode, self._envelope(0, data=self.app.manifest()))
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
        """Dispatch each stdin line in-process; JSONL envelopes out; 0, 1, or 2"""
        any_failed = False
        parsed_any = False
        lines_seen = 0
        for line_no, envelope in self._exec_lines(args, stdin):
            lines_seen = line_no
            write_envelope(envelope, self.out)
            if envelope.error is not None and envelope.error.code != "DISPATCH_PARSE_ERROR":
                parsed_any = True
            elif envelope.error is None:
                parsed_any = True
            if not envelope.ok:
                any_failed = True
                if not args.ignore_errors:
                    break
        if lines_seen and not parsed_any:
            return FrameworkCode.ARG_ERROR.value
        return FrameworkCode.GENERAL_ERROR.value if any_failed else 0

    def _exec_lines(self, args: ExecArgs, stdin: IO[str]) -> Iterator[tuple[int, Envelope]]:
        line_no = 0
        for raw in stdin:
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
        return build_from_mapping(command, mapping)
