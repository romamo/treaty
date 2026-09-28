"""Running other programs from a handler: argument lists only, never a shell.

``ctx.run`` and ``ctx.pipeline`` are the one way a handler starts a child. An argument
list goes to the program as is, so spaces, globs, and ``; rm -rf /`` arrive as literal
text (REQ-F-044, REQ-F-062). Every child reads ``/dev/null`` unless given ``input``, gets
an environment without pagers, color, or (off a terminal) editors (REQ-F-046,
REQ-F-055), and starts in its own session, so it cannot open the terminal and its whole
process group can be signaled. A non-zero exit in any stage raises ``SUBPROCESS_FAILED``
(REQ-F-065); a cancelled or timed-out run terminates every child it still tracks, with
its background grandchildren, and starts no new one (REQ-F-030, REQ-F-031). On Windows
only the child itself is stopped: its grandchildren are not killed.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from ._atomic import write_atomic
from ._errors import CliExit, RegistrationError
from ._session import BACKGROUND_DIR, Session, SessionRoot, private_dir
from ._signals import Cancelled, CancelSignal
from ._timeout import Timeout
from ._values import ExitCodeName
from ._verbosity import trace

Argv = Sequence[str | os.PathLike[str]]
"""One program and its arguments: a list or tuple, never a single string"""

GRACE_SECONDS = 2.0
"""How long a child has between SIGTERM and SIGKILL"""
STDERR_TAIL = 4096
"""Characters of a failed child's stderr kept in the error context"""
BROWSER_OPEN = "browser_open"
"""The ``gui_operations`` entry that allows ``ctx.open_url``"""
GUI_SKIPPED = "GUI_SKIPPED"


class HeadlessBehavior(StrEnum):
    """What a headless ``ctx.open_url`` does instead of opening a window (REQ-C-024)"""

    EMIT_IN_OUTPUT = "emit_in_output"
    """Opens nothing; the URL goes to ``data.open_url``"""
    SKIP = "skip"
    """Opens nothing; a ``GUI_SKIPPED`` warning names the URL"""
    ERROR = "error"
    """The run exits 4, ``PRECONDITION``, with the URL in ``error.context``"""


@dataclass(frozen=True, slots=True)
class Completed:
    """A finished child, or pipeline; ``argv``, ``returncode``, and ``stage`` name the
    first stage that failed, else the last one"""

    argv: tuple[str, ...]
    returncode: int
    """Negative when a signal ended the child, as in ``subprocess``"""
    stdout: str
    """The last stage's output"""
    stderr: str
    """Every stage's stderr, in stage order"""
    duration_ms: int
    stage: int = 0


@dataclass(frozen=True, slots=True)
class Spawned:
    """A background process ``ctx.spawn`` started (REQ-C-010)"""

    pid: int
    log_path: Path
    """Its stdout and stderr"""
    pid_file: Path
    """Where treaty tracks it until its max lifetime is up"""


@dataclass(frozen=True, slots=True)
class BackgroundSlot:
    """Where one command's background processes are tracked, and for how long"""

    directory: Path
    command: str
    lifetime_seconds: int
    root: SessionRoot | None = None
    """The temp root ``directory`` is under when the app has no state directory"""

    @classmethod
    def in_temp(cls, root: SessionRoot, command: str, lifetime_seconds: int) -> BackgroundSlot:
        """Under the user's private temp root: a shared temp directory would let another
        user plant the pid file, and have this run signal the pids in it"""
        return cls(root.path / BACKGROUND_DIR, command, lifetime_seconds, root)

    @property
    def pid_file(self) -> Path:
        return self.directory / f"{self.command}.pids"

    def make(self) -> Path:
        """The directory; under a temp root, private, and refused when planted"""
        if self.root is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            return self.directory
        return private_dir(self.root.make() / BACKGROUND_DIR)


_DETACHED: list[subprocess.Popen[bytes]] = []
"""Background children, kept so the interpreter never reports them as leaked"""


def shell_string_prohibited(value: object) -> CliExit:
    return CliExit(
        ExitCodeName("GENERAL_ERROR"),
        "A program was given as one shell string; treaty never runs a shell",
        code="SHELL_STRING_PROHIBITED",
        context={"argv": str(value)[:200]},
        fix_required="pass an argument list such as ['git', 'log', '-1'] (REQ-F-062)",
    )


def _argv(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise shell_string_prohibited(value)
    if not isinstance(value, (list, tuple)) or not value:
        raise TypeError(f"argv must be a non-empty list of str or Path, not {value!r}")
    parts: list[str] = []
    for part in value:
        if not isinstance(part, (str, os.PathLike)):
            raise TypeError(f"argv entries must be str or Path, not {type(part).__name__}")
        parts.append(os.fspath(part))
    return tuple(parts)


class Processes:
    """The children of one handler run: their environment, deadline, and tracking

    ``env`` is the hardened base environment; ``deadline`` is the command's, on the
    ``time.monotonic`` clock. ``terminate`` may run on another thread than the handler,
    which is how a signal or a timeout reaches a child the handler waits on.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        deadline: float | None,
        headless: bool,
        browser_open: bool,
        headless_behavior: HeadlessBehavior = HeadlessBehavior.EMIT_IN_OUTPUT,
        background: BackgroundSlot | None = None,
        cwd: Path | None = None,
        session: Session | None = None,
    ) -> None:
        self.env = dict(env)
        self.session = session
        """The run's temp directory, the children's ``TMPDIR`` and pid file (REQ-F-030)"""
        self.cwd = cwd
        """``--cwd``: where children start, and what a relative ``cwd=`` is under"""
        self.deadline = deadline
        self.headless = headless
        self.browser_open = browser_open
        self.headless_behavior = headless_behavior
        self.background = background
        self.suppressed_url: str | None = None
        """The URL ``open_url`` did not open because the run is headless (REQ-F-057)"""
        self._live: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()
        self._closed = False
        """Set by ``terminate``: the run's response is decided, so no child may start"""
        self._signal: CancelSignal | None = None

    def run(
        self,
        argv: Argv,
        *,
        input: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
        check: bool = True,
    ) -> Completed:
        if isinstance(argv, (str, bytes)):
            raise shell_string_prohibited(argv)
        return self.pipeline([argv], input=input, cwd=cwd, env=env, timeout=timeout, check=check)

    def pipeline(
        self,
        stages: Sequence[Argv],
        *,
        input: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: Timeout | None = None,
        check: bool = True,
    ) -> Completed:
        if isinstance(stages, (str, bytes)):
            raise shell_string_prohibited(stages)
        argvs = [_argv(stage) for stage in stages]
        if not argvs:
            raise TypeError("a pipeline needs at least one stage")
        seconds = self._seconds(timeout, argvs[0])
        pipeline = len(argvs) > 1
        end = None if seconds is None else time.monotonic() + seconds
        started = time.perf_counter()
        procs: list[subprocess.Popen[bytes]] = []
        with contextlib.ExitStack() as files:
            errs = [files.enter_context(tempfile.TemporaryFile()) for _ in argvs]
            source: IO[bytes] | int = subprocess.DEVNULL
            if input is not None:
                source = files.enter_context(tempfile.TemporaryFile())
                source.write(input.encode("utf-8"))
                source.seek(0)
            try:
                for index, argv in enumerate(argvs):
                    proc = self._spawn(argv, index, source, errs[index], cwd, env)
                    procs.append(proc)
                    if index > 0:
                        # Only the next stage reads it, so SIGPIPE reaches the writer
                        previous = procs[index - 1].stdout
                        assert previous is not None
                        previous.close()
                    assert proc.stdout is not None
                    source = proc.stdout
                out, _ = procs[-1].communicate(timeout=_left(end))
                for proc in procs[:-1]:
                    proc.wait(timeout=_left(end, floor=0.1))
            except subprocess.TimeoutExpired:
                # The first stage still running; else the last, whose output a background
                # grandchild holds open
                hung = next((i for i, p in enumerate(procs) if p.poll() is None), len(procs) - 1)
                self._stop(procs)
                assert seconds is not None
                raise CliExit(
                    ExitCodeName("TIMEOUT"),
                    f"{_name(argvs[hung], hung, pipeline)} ran past its {seconds:g}s timeout "
                    "and was stopped",
                    context={
                        "argv": list(argvs[hung]),
                        "stage": hung,
                        "timeout_ms": int(seconds * 1000),
                    },
                ) from None
            except BaseException:
                # A signal (Cancelled), or a spawn that failed after earlier stages started
                self._stop(procs)
                raise
            finally:
                for proc in procs:
                    if proc.stdout is not None:
                        proc.stdout.close()
                with self._lock:
                    self._live.difference_update(procs)
                self._track()
            stderrs = [_read(err) for err in errs]
        codes = [proc.returncode for proc in procs]
        stage = next((i for i in range(len(codes)) if _failed(codes, i)), len(codes) - 1)
        done = Completed(
            argv=argvs[stage],
            returncode=codes[stage],
            stdout=out.decode("utf-8", "replace"),
            stderr="".join(stderrs),
            duration_ms=int((time.perf_counter() - started) * 1000),
            stage=stage,
        )
        for argv, code in zip(argvs, codes, strict=True):
            trace("child exited", argv=list(argv), returncode=code, duration_ms=done.duration_ms)
        if check and done.returncode != 0:
            raise CliExit(
                ExitCodeName("GENERAL_ERROR"),
                f"{_name(done.argv, stage, pipeline)} exited with {done.returncode}",
                code="SUBPROCESS_FAILED",
                context={
                    "argv": list(done.argv),
                    "returncode": done.returncode,
                    "stage": stage,
                    "stderr": stderrs[stage][-STDERR_TAIL:],
                },
            )
        return done

    def open_url(self, url: str) -> bool:
        """Open ``url`` in a browser; headless, do what ``headless_behavior`` says"""
        if not self.browser_open:
            raise RegistrationError(
                f"ctx.open_url needs gui_operations=[{BROWSER_OPEN!r}] on the command (REQ-C-024)"
            )
        if self.headless:
            if self.headless_behavior is HeadlessBehavior.ERROR:
                raise CliExit(
                    ExitCodeName("PRECONDITION"),
                    "The command opens a browser, and no display or person is here to see it",
                    code="GUI_UNAVAILABLE",
                    context={"url": url, "gui_operation": BROWSER_OPEN},
                    fix_required="open the URL in error.context.url yourself, or run the "
                    "command where a display is available",
                )
            if self.headless_behavior is HeadlessBehavior.EMIT_IN_OUTPUT:
                self.suppressed_url = url
            return False
        return webbrowser.open(url)

    def spawn(
        self, argv: Argv, *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> Spawned:
        """Start a child that outlives the run: its own session, stdin from /dev/null,
        output to a log file, and no stop when the run ends; its pid and deadline go to
        the command's pid file, and expired entries there are stopped first"""
        if self.background is None:
            raise RegistrationError("ctx.spawn needs background=treaty.Background(...)")
        parts = _argv(argv)
        slot = self.background
        with self._lock:
            if self._closed:
                raise self._refusal(parts)
        slot.make()
        kept = reap(slot.pid_file)
        log_path = slot.directory / f"{slot.command}.{time.time_ns()}.log"
        detach: dict[str, Any]
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            detach = {"creationflags": flags}
        else:
            detach = {"start_new_session": True}
        with open(log_path, "wb") as log:
            try:
                proc = subprocess.Popen(
                    parts,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=self._where(cwd),
                    env={**self.env, **(env or {})},
                    **detach,
                )
            except OSError as exc:
                raise CliExit(
                    ExitCodeName("GENERAL_ERROR"),
                    f"Cannot start `{parts[0]}`: {exc.strerror}",
                    code="SUBPROCESS_FAILED",
                    context={"argv": list(parts), "cause": exc.strerror},
                ) from None
        _DETACHED.append(proc)
        deadline = time.time() + slot.lifetime_seconds
        entries = [*kept, f"{proc.pid} {deadline:.0f}"]
        write_atomic(slot.pid_file, "".join(f"{e}\n" for e in entries))
        return Spawned(proc.pid, log_path, slot.pid_file)

    def terminate(self, cancelled: CancelSignal | None = None) -> None:
        """SIGTERM every tracked child's process group, SIGKILL what outlives the grace;
        from now on no child starts. ``cancelled`` is the signal that ended the run, None
        for a timeout; an abandoned handler that tries another child gets the same."""
        with self._lock:
            self._closed = True
            self._signal = cancelled
            live = list(self._live)
        self._stop(live)

    @property
    def tracked(self) -> bool:
        """Children still run: ``meta.session_pid_file`` names their pid file (09-D2)"""
        with self._lock:
            return bool(self._live)

    def _track(self) -> None:
        if self.session is None:
            return
        with self._lock:
            pids = [p.pid for p in self._live]
        self.session.track(pids)

    def _temp_env(self) -> dict[str, str]:
        return {} if self.session is None else self.session.child_env()

    def _where(self, cwd: Path | None) -> Path | None:
        if self.cwd is None:
            return cwd
        return self.cwd if cwd is None else self.cwd / cwd

    def _seconds(self, timeout: Timeout | None, argv: tuple[str, ...]) -> float | None:
        """The child's time limit: the one given, capped by what is left of the command's"""
        given = None if timeout is None else timeout.seconds
        if self.deadline is None:
            return given
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CliExit(
                ExitCodeName("TIMEOUT"),
                f"No time is left on the command's deadline to run `{argv[0]}`",
                context={"argv": list(argv)},
            )
        return left if given is None else min(given, left)

    def _refusal(self, argv: tuple[str, ...]) -> BaseException:
        """Why a child cannot start after ``terminate``: the run's own ending"""
        if self._signal is not None:
            return Cancelled(self._signal)
        return CliExit(
            ExitCodeName("TIMEOUT"),
            f"`{argv[0]}` was not started: the command already timed out",
            context={"argv": list(argv)},
        )

    def _spawn(
        self,
        argv: tuple[str, ...],
        index: int,
        stdin: IO[bytes] | int,
        stderr: IO[bytes],
        cwd: Path | None,
        env: Mapping[str, str] | None,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._closed:
                raise self._refusal(argv)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=stderr,
                cwd=self._where(cwd),
                env={**self.env, **self._temp_env(), **(env or {})},
                start_new_session=True,
            )
        except OSError as exc:
            raise CliExit(
                ExitCodeName("GENERAL_ERROR"),
                f"Cannot start `{argv[0]}`: {exc.strerror}",
                code="SUBPROCESS_FAILED",
                context={"argv": list(argv), "stage": index, "cause": exc.strerror},
            ) from None
        with self._lock:
            closed = self._closed
            if not closed:
                self._live.add(proc)
        if not closed:
            self._track()
        if closed:
            # terminate() ran while Popen did: this child is not tracked, so stop it here
            _signal(proc, kill=True)
            proc.wait()
            assert proc.stdout is not None
            proc.stdout.close()
            raise self._refusal(argv)
        return proc

    @staticmethod
    def _stop(procs: Sequence[subprocess.Popen[bytes]]) -> None:
        """Every group, its leader exited or not: a background grandchild outlives it"""
        for proc in procs:
            proc.poll()  # reap an exited leader; macOS refuses to signal a zombie's group
            _signal(proc, kill=False)
        end = time.monotonic() + GRACE_SECONDS
        while any(_alive(p) for p in procs) and time.monotonic() < end:
            time.sleep(0.02)
        for proc in procs:
            if _alive(proc):
                _signal(proc, kill=True)
            proc.wait()


def reap(pid_file: Path) -> list[str]:
    """Stop every background process in ``pid_file`` past its deadline; the entries of
    those still running, which the caller writes back"""
    try:
        lines = pid_file.read_text().splitlines()
    except FileNotFoundError:
        return []
    kept: list[str] = []
    now = time.time()
    for line in lines:
        pid_text, _, deadline_text = line.partition(" ")
        if not (pid_text.isdigit() and deadline_text.isdigit()):
            continue  # not an entry treaty wrote
        pid = int(pid_text)
        if int(deadline_text) <= now:
            _stop_background(pid)
        elif _running(pid):
            kept.append(line)
    return kept


def _stop_background(pid: int) -> None:
    """SIGTERM the process group ``spawn`` started, if that pid still leads it"""
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        elif os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        pass  # gone already, or the pid now belongs to someone else


def _running(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # kept until its deadline; os.kill(pid, 0) would terminate it there
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(proc: subprocess.Popen[bytes], *, kill: bool) -> None:
    """Signal the child's whole process group: grandchildren share it. Windows has no
    groups here, so only the child is stopped there."""
    if sys.platform == "win32":
        if proc.poll() is not None:
            return
        if kill:
            proc.kill()
        else:
            proc.terminate()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        # Gone already; macOS answers EPERM for a group whose leader is a zombie
        pass


def _alive(proc: subprocess.Popen[bytes]) -> bool:
    """The child, or on POSIX anything left in its process group, still runs"""
    if proc.poll() is None:
        return True
    if sys.platform == "win32":
        return False
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


def _failed(codes: Sequence[int], index: int) -> bool:
    """A stage failed, unless SIGPIPE ended a writer whose reader stopped early and
    succeeded, as ``head`` does in ``yes | head -1``"""
    code = codes[index]
    if code == 0:
        return False
    if sys.platform != "win32" and code == -signal.SIGPIPE:
        return not any(c == 0 for c in codes[index + 1 :])
    return True


def _name(argv: tuple[str, ...], stage: int, pipeline: bool) -> str:
    """The program in backticks, so a message never capitalizes it, and its stage"""
    return f"`{argv[0]}` (stage {stage})" if pipeline else f"`{argv[0]}`"


def _left(end: float | None, *, floor: float = 0.0) -> float | None:
    return None if end is None else max(end - time.monotonic(), floor)


def _read(file: IO[bytes]) -> str:
    file.seek(0)
    return file.read().decode("utf-8", "replace")
