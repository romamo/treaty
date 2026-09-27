"""Per-invocation context handed to every handler."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from ._cap import MARKER, TRUNCATED_CODE
from ._config import ConfigFile
from ._errors import RegistrationError
from ._mode import Format
from ._page import PageRequest
from ._prompt import Prompter
from ._retry import Retrier
from ._subprocess import Argv, Completed, Processes
from ._timeout import Timeout

LogSink = Callable[[str, Mapping[str, object]], None]
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
    log_sink: LogSink = field(repr=False, compare=False)
    processes: Processes = field(repr=False, compare=False)
    prompter: Prompter = field(repr=False, compare=False)
    warn_sink: WarnSink = field(repr=False, compare=False)
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
    retrier: Retrier | None = field(default=None, repr=False, compare=False)

    def retry(self, fn: Callable[[], T]) -> T:
        """Call ``fn``, and again after ``--retry-delay`` while it raises one of the
        command's ``Retry.on`` exceptions, up to ``--retries`` times and never past the
        timeout; then the run exits with ``Retry.exhausted``. Needs ``retry=`` on the
        command; the retries made are ``meta.retries`` (REQ-F-078)."""
        if self.retrier is None:
            raise RegistrationError("ctx.retry needs retry=treaty.Retry(...) on the command")
        return self.retrier.call(fn)

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
        """Write one diagnostic line to stderr, never stdout (REQ-F-006)

        A JSON object in JSON mode, ``message key=value`` otherwise. Declared secrets and
        fields named like credentials (token, password, api_key, Authorization, ...) are
        written as ``[REDACTED]`` (REQ-F-051).
        """
        self.log_sink(message, fields)

    def warn(self, code: str, message: str, **context: object) -> None:
        """Add an entry to the response's ``warnings``; the run still succeeds"""
        self.warn_sink(code, message, context)

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
        return self.processes.run(argv, input=input, cwd=cwd, env=env, timeout=timeout, check=check)

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
        return self.processes.pipeline(
            stages, input=input, cwd=cwd, env=env, timeout=timeout, check=check
        )

    def open_url(self, url: str) -> bool:
        """Open ``url`` in a browser and return True; when headless, open nothing, return
        False, and the framework puts the URL in ``data.open_url`` (REQ-F-057)"""
        return self.processes.open_url(url)

    def prompt(self, text: str, *, flag: str) -> str:
        """Ask a person for ``text``; ``--<flag>`` is how an agent supplies the answer

        Only on a terminal (stdin and stdout) without ``--non-interactive``; otherwise the
        run ends with exit 4, ``INPUT_REQUIRED``, and a suggestion naming ``--<flag>``.
        Needs ``interactive=True`` on the command (REQ-F-009, REQ-C-005).
        """
        return self.prompter.prompt(text, flag=flag)

    def confirm(self, text: str) -> bool:
        """Ask a yes-or-no question; ``--yes`` answers yes without asking, and off a
        terminal without it the run ends with exit 4, ``INPUT_REQUIRED``"""
        return self.prompter.confirm(text)

    def edit(self, initial: str = "") -> str:
        """Let a person edit ``initial`` in ``$VISUAL`` or ``$EDITOR`` and return the text

        Off a terminal the run ends with exit 4, ``EDITOR_REQUIRED``, and ``alternatives``
        listing the command's ``editor_alternatives`` flags (REQ-F-055, REQ-C-023).
        """
        return self.prompter.edit(initial)
