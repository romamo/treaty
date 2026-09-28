"""Per-invocation context handed to every handler."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from ._cache import Cache
from ._cap import MARKER, TRUNCATED_CODE
from ._config import ConfigFile
from ._errors import RegistrationError
from ._http import Http
from ._lifecycle import Teardown
from ._locks import Locks
from ._mode import Format
from ._page import PageRequest
from ._prompt import Prompter
from ._retry import Retrier
from ._session import DEFAULT_KEEP_SECONDS, Session
from ._steps import StepTracker
from ._subprocess import GUI_SKIPPED, Argv, Completed, HeadlessBehavior, Processes, Spawned
from ._timeout import Timeout
from ._verbosity import Level
from ._walk import Traversal, Walk

LogSink = Callable[[Level, str, Mapping[str, object]], None]
WarnSink = Callable[[str, str, Mapping[str, object]], None]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Ctx:
    app_name: str
    version: str
    mode: Format
    request_id: str
    env: Mapping[str, str]
    state: Mapping[str, object]
    timeout: Timeout
    color: bool
    """Whether a renderer may color its text: never in JSON mode, under NO_COLOR, CI,
    or TERM=dumb, or when stdout is not a terminal (REQ-F-008)"""
    headless: bool
    """No person or display to open a window for (REQ-F-057); ``meta.headless`` says so"""
    cwd: Path
    """The directory relative paths resolve against: ``--cwd``, else the working
    directory, as ``meta.cwd`` reports it. Build paths from it; never ``os.chdir``, which
    the run undoes with a ``CWD_CHANGED`` warning (REQ-O-017, REQ-F-041)"""
    _log_sink: LogSink = field(repr=False, compare=False)
    _processes: Processes = field(repr=False, compare=False)
    _prompter: Prompter = field(repr=False, compare=False)
    _warn_sink: WarnSink = field(repr=False, compare=False)
    idempotency_key: str | None = None
    stdin_text: str | None = None
    """The payload of a ``stdin_input=True`` command: stdin, capped, or ``--input-file``"""
    page: PageRequest | None = None
    """The page a list command is asked for (``paginated=True``), else None; a handler
    that loads its whole list can ignore it and return the list"""
    token: str | None = field(default=None, repr=False)
    """A login command's pre-acquired token (``auth=``): from ``--token-env-var`` or the
    first set variable of ``token_env_vars``; redacted from logs and tracebacks"""
    _config_file: ConfigFile | None = field(default=None, repr=False, compare=False)
    """What ``write_config`` writes; read the app's settings through ``App(settings=)``"""
    trace_id: str | None = None
    """``TOOL_TRACE_ID`` of the run, when set; children inherit it (REQ-F-025)"""
    project_root: Path | None = None
    """The nearest directory, from the cwd up, holding a ``project_root=`` marker"""
    _retrier: Retrier | None = field(default=None, repr=False, compare=False)
    _locks: Locks | None = field(default=None, repr=False, compare=False)
    _teardown: Teardown | None = field(default=None, repr=False, compare=False)
    """What the run releases when it ends: resources' ``release``, then ``cleanup=``"""
    _steps: StepTracker | None = field(default=None, repr=False, compare=False)
    _session: Session | None = field(default=None, repr=False, compare=False)
    """The run's private temp directory and output files (REQ-F-032, REQ-F-043)"""
    _cache: Cache | None = field(default=None, repr=False, compare=False)
    _http: Http | None = field(default=None, repr=False, compare=False)
    _traversal: Traversal | None = field(default=None, repr=False, compare=False)
    _deadline: float | None = field(default=None, repr=False, compare=False)
    """``time.monotonic()`` when the command times out, else None"""

    @property
    def remaining(self) -> float | None:
        """Seconds left before the command times out, never below 0; None without a limit.
        The deadline ``ctx.run``, ``ctx.lock``, and ``ctx.http`` already clamp to: pass it
        as another client's ``timeout=``, or check it to stop a long loop with the work
        done so far instead of being abandoned at the limit (REQ-C-012)"""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        """The command's time is up: a handler still running is past its ``TIMEOUT``"""
        return self.remaining == 0.0

    @property
    def http(self) -> Http:
        """An HTTP client that honors ``HTTPS_PROXY``, ``HTTP_PROXY``, ``NO_PROXY``,
        ``REQUESTS_CA_BUNDLE``, ``SSL_CERT_FILE``, ``--proxy``, ``--no-proxy``, and the
        timeout: ``get(url)``, ``post(url, json=...)``, or ``request(method, url, ...)``
        return a ``treaty.HttpResponse``. A failure ends the run with exit 12 or 10 and
        ``error.network_context``. Needs ``has_network_io=True`` (REQ-F-036, REQ-F-037)."""
        if self._http is None:
            raise RegistrationError("ctx.http needs has_network_io=True on the command")
        return self._http

    def walk(self, root: Path | str) -> Walk:
        """Every entry under ``root``, depth first in name order, as ``treaty.WalkEntry``;
        a circular symlink exits 4 ``SYMLINK_LOOP`` and a tree deeper than ``--max-depth``
        exits 4 ``DEPTH_EXCEEDED``. With ``--no-follow-symlinks`` no symlink is entered;
        ``count`` and ``symlinks_skipped`` tally the walk. A relative ``root`` is under
        ``cwd``. Needs ``recursive_traversal=True`` (REQ-F-061, REQ-O-040)."""
        if self._traversal is None:
            raise RegistrationError("ctx.walk needs recursive_traversal=True on the command")
        return self._traversal.walk(self.cwd / root)

    @property
    def cache(self) -> Cache:
        """The command's cache: ``get(key)`` returns the bytes ``put(key, data)`` stored,
        or None once they are older than the TTL; with ``--no-cache`` or
        ``--cache-ttl 0`` every ``get`` misses and ``put`` keeps nothing. Needs
        ``cache=treaty.CachePolicy(ttl_seconds=...)`` on the command (REQ-O-018)."""
        if self._cache is None:
            raise RegistrationError("ctx.cache needs cache=treaty.CachePolicy(...) on the command")
        return self._cache

    @property
    def tmp_dir(self) -> Path:
        """This run's own temp directory, ``0700``, made on first use and removed when the
        run ends, on any exit; children of ``ctx.run`` get it as ``TMPDIR``. Two runs,
        even in parallel, never share one (REQ-F-032)"""
        return self._run_session().directory()

    def temp_file(self, suffix: str = "") -> Path:
        """A new, empty ``0600`` file in ``tmp_dir``, removed with it"""
        return self._run_session().temp_file(suffix)

    def output_file(self, name: str, *, keep_seconds: int = DEFAULT_KEEP_SECONDS) -> Path:
        """A new, empty ``0600`` file named ``name`` for the caller to read after the run.
        It outlives the run: an object ``data`` gets ``cleanup``, with the shell
        ``command`` that deletes it and ``auto_cleanup_after_seconds``, after which a
        later run of the tool deletes it (REQ-F-043)"""
        return self._run_session().output_file(name, keep_seconds)

    def _run_session(self) -> Session:
        if self._session is None:
            raise RegistrationError("the temp directory exists only while a command runs")
        return self._session

    def step(self, name: str) -> bool:
        """Complete the step in progress and start ``name``, the next of the command's
        ``steps=`` to run; the last completes when the handler returns. Returns False for a
        step before ``--resume-from``, so ``if ctx.step("backup"):`` skips it. The response
        lists ``completed_steps``, ``failed_step``, and ``skipped_steps`` (REQ-C-008)."""
        if self._steps is None:
            raise RegistrationError("ctx.step needs steps=[...] on the command")
        return self._steps.step(name)

    def retry(self, fn: Callable[[], T]) -> T:
        """Call ``fn``, and again after ``--retry-delay`` (growing by ``Retry.backoff``)
        while it raises one of the command's ``Retry.on`` exceptions or ``Retry.retry_if``
        holds for its result, up to ``--retries`` times and never past the timeout; then
        the run exits with ``Retry.exhausted``. Needs ``retry=`` on the command; the
        retries made are ``meta.retries`` (REQ-F-078)."""
        if self._retrier is None:
            raise RegistrationError("ctx.retry needs retry=treaty.Retry(...) on the command")
        return self._retrier.call(fn)

    def lock(
        self, name: str, *, wait: float | None = None, retry_after_ms: int = 1000
    ) -> AbstractContextManager[None]:
        """Hold the named lock, shared by every run of the app, for a ``with`` block. A run
        that cannot take it within ``wait`` seconds (default: what is left of the
        timeout) exits 4 with ``LOCK_HELD``, the holder's pid and age, and
        ``retry_after_ms`` (REQ-F-033). A holder that exits or is killed releases it."""
        locks = self._locks if self._locks is not None else Locks(None, None)
        return locks.hold(name, wait=wait, retry_after_ms=retry_after_ms)

    @property
    def config_path(self) -> Path | None:
        """The file ``write_config`` writes (``config_write_scope=``), else None: the
        project's ``./.<app>.toml``, the user's with ``--global``, or ``--config PATH``"""
        return None if self._config_file is None else self._config_file.path

    def write_config(self, text: str) -> Path:
        """Replace the config file with ``text`` through a temporary file and a rename, so
        an interrupted write leaves the old file; writes are locked, one at a time, and a
        global write adds a ``GLOBAL_CONFIG_MODIFIED`` warning (REQ-C-025, REQ-O-036)"""
        if self._config_file is None:
            raise RegistrationError("ctx.write_config needs config_write_scope= on the command")
        return self._config_file.write(text)

    def log(self, message: str, **fields: object) -> None:
        """Write one INFO line to stderr, never stdout (REQ-F-006)

        A JSON object in JSON mode, ``message key=value`` otherwise. Only a terminal, or
        ``--verbose``, shows it: off a terminal or under CI the run writes errors only
        (REQ-F-038). Declared secrets and fields named like credentials (token,
        password, api_key, Authorization, ...) are written as ``[REDACTED]`` (REQ-F-051).
        """
        self._log_sink(Level.INFO, message, fields)

    def log_error(self, message: str, **fields: object) -> None:
        """Like ``log``, at ERROR: written at every verbosity but ``--quiet``"""
        self._log_sink(Level.ERROR, message, fields)

    def debug(self, message: str, **fields: object) -> None:
        """Like ``log``, at DEBUG: written only under ``--debug`` (REQ-O-008)"""
        self._log_sink(Level.DEBUG, message, fields)

    def progress(self, message: str, *, done: int | None = None, total: int | None = None) -> None:
        """A progress line, such as ``done=3 total=10``: shown where ``log`` is, never off
        a terminal without ``--verbose`` (REQ-F-038)"""
        fields = {k: v for k, v in (("done", done), ("total", total)) if v is not None}
        self._log_sink(Level.PROGRESS, message, fields)

    def warn(self, code: str, message: str, **context: object) -> None:
        """Add an entry to the response's ``warnings``; the run still succeeds"""
        self._warn_sink(code, message, context)

    def truncated(self, value: str, *, field: str, original_length: int | None = None) -> str:
        """A value a backend already cut, such as a column limit: returned with the
        ``[truncated]`` marker, reported as a ``FIELD_TRUNCATED`` warning on
        ``data.<field>``, and ``meta.truncated`` is true (REQ-F-064)"""
        context: dict[str, object] = {"field": f"data.{field}", "truncated_length": len(value)}
        if original_length is not None:
            context["original_length"] = original_length
        self.warn(TRUNCATED_CODE, f"data.{field} was truncated by the backend", **context)
        return value + MARKER

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
        """Run one program from an argument list, never a shell, and capture its output

        The child reads ``input``, or ``/dev/null``, and never opens a pager, colors, or,
        off a terminal, an editor; ``env`` overrides single variables. ``timeout``
        defaults to what is left of the command's. A non-zero exit raises
        ``SUBPROCESS_FAILED`` (exit 1) unless ``check=False``; running out of time
        stops the child and raises ``TIMEOUT``.
        """
        return self._processes.run(
            argv, input=input, cwd=cwd, env=env, timeout=timeout, check=check
        )

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
        """``a | b | c`` with OS pipes and no shell; a failure in any stage fails the
        pipeline, the first failing stage named in ``context.stage`` (REQ-F-065)"""
        return self._processes.pipeline(
            stages, input=input, cwd=cwd, env=env, timeout=timeout, check=check
        )

    def spawn(
        self, argv: Argv, *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> Spawned:
        """Start a process that outlives the run, from an argument list, never a shell

        It gets its own session, reads ``/dev/null``, writes to ``log_path``, and is not
        stopped when the run ends; a later ``ctx.spawn`` of the command stops it once its
        ``max_lifetime_seconds`` are up. Needs ``background=treaty.Background(...)``,
        whose output carries ``background_pid`` and ``cleanup_command`` (REQ-C-010).
        """
        return self._processes.spawn(argv, cwd=cwd, env=env)

    def open_url(self, url: str) -> bool:
        """Open ``url`` in a browser and return True; when headless, open nothing and return
        False, and the command's ``headless_behavior`` says what happens instead: the URL
        in ``data.open_url``, a ``GUI_SKIPPED`` warning, or exit 4 (REQ-F-057, REQ-C-024)"""
        opened = self._processes.open_url(url)
        skipped = self._processes.headless_behavior is HeadlessBehavior.SKIP
        if self._processes.headless and skipped:
            self.warn(GUI_SKIPPED, "Headless: the browser was not opened", url=url)
        return opened

    def prompt(self, text: str, *, flag: str) -> str:
        """Ask a person for ``text``; ``--<flag>`` is how an agent supplies the answer

        Only on a terminal (stdin and stdout) without ``--non-interactive``; otherwise the
        run ends with exit 4, ``INPUT_REQUIRED``, and a suggestion naming ``--<flag>``.
        Needs ``interactive=True`` on the command (REQ-F-009, REQ-C-005).
        """
        return self._prompter.prompt(text, flag=flag)

    def confirm(self, text: str) -> bool:
        """Ask a yes-or-no question; ``--yes`` answers yes without asking, and off a
        terminal without it the run ends with exit 4, ``INPUT_REQUIRED``"""
        return self._prompter.confirm(text)

    def edit(self, initial: str = "") -> str:
        """Let a person edit ``initial`` in ``$VISUAL`` or ``$EDITOR`` and return the text

        Off a terminal the run ends with exit 4, ``EDITOR_REQUIRED``, and ``alternatives``
        listing the command's ``editor_alternatives`` flags (REQ-F-055, REQ-C-023).
        """
        return self._prompter.edit(initial, lambda: self.temp_file(".txt"))
