"""Running other programs from a handler: argument lists only, never a shell.

``ctx.run`` and ``ctx.pipeline`` are the one way a handler starts a child. An argument
list goes to the program as is, so spaces, globs, and ``; rm -rf /`` arrive as literal
text (REQ-F-044, REQ-F-062). Every child reads ``/dev/null`` unless given ``input``, gets
an environment without pagers, color, or (off a terminal) editors (REQ-F-046,
REQ-F-055), and starts in its own session, so it cannot open the terminal and its whole
process group can be signaled. A non-zero exit in any stage raises ``SUBPROCESS_FAILED``
(REQ-F-065); a cancelled or timed-out run terminates every child it still tracks
(REQ-F-030, REQ-F-031).
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
from pathlib import Path
from typing import IO

from ._errors import CliExit, RegistrationError
from ._timeout import Timeout
from ._values import ExitCodeName

Argv = Sequence[str | os.PathLike[str]]
"""One program and its arguments: a list or tuple, never a single string"""

GRACE_SECONDS = 2.0
"""How long a child has between SIGTERM and SIGKILL"""
STDERR_TAIL = 4096
"""Characters of a failed child's stderr kept in the error context"""
BROWSER_OPEN = "browser_open"
"""The ``gui_operations`` entry that allows ``ctx.open_url``"""


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
    ) -> None:
        self.env = dict(env)
        self.deadline = deadline
        self.headless = headless
        self.browser_open = browser_open
        self.suppressed_url: str | None = None
        """The URL ``open_url`` did not open because the run is headless (REQ-F-057)"""
        self._live: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.Lock()

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
                self._stop(procs)
                assert seconds is not None
                raise CliExit(
                    ExitCodeName("TIMEOUT"),
                    f"{argvs[0][0]} ran past its {seconds:g}s timeout and was stopped",
                    context={"argv": list(argvs[0]), "timeout_ms": int(seconds * 1000)},
                ) from None
            except BaseException:
                # A signal (Cancelled), or a spawn that failed after earlier stages started
                self._stop(procs)
                raise
            finally:
                with self._lock:
                    self._live.difference_update(procs)
            stderrs = [_read(err) for err in errs]
        codes = [proc.returncode for proc in procs]
        stage = next((i for i, code in enumerate(codes) if code != 0), len(codes) - 1)
        done = Completed(
            argv=argvs[stage],
            returncode=codes[stage],
            stdout=out.decode("utf-8", "replace"),
            stderr="".join(stderrs),
            duration_ms=int((time.perf_counter() - started) * 1000),
            stage=stage,
        )
        if check and done.returncode != 0:
            raise CliExit(
                ExitCodeName("GENERAL_ERROR"),
                f"{done.argv[0]} exited with {done.returncode}",
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
        """Open ``url`` in a browser, or, headless, leave it for ``data.open_url``"""
        if not self.browser_open:
            raise RegistrationError(
                f"ctx.open_url needs gui_operations=[{BROWSER_OPEN!r}] on the command (REQ-C-024)"
            )
        if self.headless:
            self.suppressed_url = url
            return False
        return webbrowser.open(url)

    def terminate(self) -> None:
        """SIGTERM every tracked child's process group, SIGKILL what outlives the grace"""
        with self._lock:
            live = list(self._live)
        self._stop(live)

    def _seconds(self, timeout: Timeout | None, argv: tuple[str, ...]) -> float | None:
        """The child's time limit: the one given, else what is left of the command's"""
        if timeout is not None:
            return timeout.seconds
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CliExit(
                ExitCodeName("TIMEOUT"),
                f"No time is left on the command's deadline to run {argv[0]}",
                context={"argv": list(argv)},
            )
        return left

    def _spawn(
        self,
        argv: tuple[str, ...],
        index: int,
        stdin: IO[bytes] | int,
        stderr: IO[bytes],
        cwd: Path | None,
        env: Mapping[str, str] | None,
    ) -> subprocess.Popen[bytes]:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=stderr,
                cwd=cwd,
                env={**self.env, **(env or {})},
                start_new_session=True,
            )
        except OSError as exc:
            raise CliExit(
                ExitCodeName("GENERAL_ERROR"),
                f"Cannot start {argv[0]}: {exc.strerror}",
                code="SUBPROCESS_FAILED",
                context={"argv": list(argv), "stage": index, "cause": exc.strerror},
            ) from None
        with self._lock:
            self._live.add(proc)
        return proc

    @staticmethod
    def _stop(procs: Sequence[subprocess.Popen[bytes]]) -> None:
        running = [p for p in procs if p.poll() is None]
        for proc in running:
            _signal(proc, kill=False)
        end = time.monotonic() + GRACE_SECONDS
        for proc in running:
            try:
                proc.wait(timeout=max(end - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                _signal(proc, kill=True)
                proc.wait()


def _signal(proc: subprocess.Popen[bytes], *, kill: bool) -> None:
    """Signal the child's whole process group: grandchildren share it"""
    if sys.platform == "win32":
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


def _left(end: float | None, *, floor: float = 0.0) -> float | None:
    return None if end is None else max(end - time.monotonic(), floor)


def _read(file: IO[bytes]) -> str:
    file.seek(0)
    return file.read().decode("utf-8", "replace")
