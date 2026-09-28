"""Prompts, confirmations, and editors that never wait for input an agent cannot give.

A command that asks a person declares ``interactive=True`` and asks through
``ctx.prompt`` and ``ctx.confirm``; one that opens an editor declares the flags that
replace it with ``editor_alternatives=`` and calls ``ctx.edit``. They ask only when stdin
and stdout are both terminals and ``--non-interactive`` is absent; otherwise the run
ends with exit 4 and an error naming the flag that answers instead (REQ-F-009,
REQ-C-005, REQ-F-055). A stray ``input()`` that no one can answer ends the same way
(REQ-F-047).
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from ._errors import CliExit, RegistrationError
from ._values import ExitCodeName


class InputRequired(BaseException):
    """The run needs an answer no one can give; becomes exit 4 with ``code``

    A ``BaseException``, like ``Cancelled``, so a handler's ``except Exception`` around
    an ``input()`` cannot turn it into a hang or a crash report.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        suggestion: str,
        context: Mapping[str, object],
        alternatives: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.context = dict(context)
        self.alternatives = tuple(alternatives)


def blocked_read(call: str) -> InputRequired:
    return InputRequired(
        "INTERACTIVE_BLOCKED",
        "Command requires interactive input, and no one can answer in this run",
        suggestion="no flag supplies this input; run it in a terminal, or ask the command "
        "author to declare interactive=True and read it with ctx.prompt(..., flag=...)",
        context={"call": call},
    )


def _editor_failed(editor: str, what: str) -> CliExit:
    return CliExit(
        ExitCodeName("GENERAL_ERROR"),
        f"The editor {editor!r} {what}; the edit was abandoned",
        code="EDITOR_FAILED",
        context={"editor": editor},
        suggestion="set VISUAL or EDITOR to an editor that exits 0 once the file is saved",
    )


class NoPromptStdin:
    """``sys.stdin`` while a run cannot prompt (REQ-F-047). A terminal is never read, since
    no one was asked; piped data reads as usual, except that a ``readline()`` finding stdin
    empty before any line raises ``INTERACTIVE_BLOCKED``, so ``input()`` on ``/dev/null``
    exits 4 while ``fileinput`` still ends a pipe normally. Everything else is the wrapped
    stream's."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lines = False
        """Whether ``readline`` returned a line: an empty one after it is the pipe's end"""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def isatty(self) -> bool:
        return self._stream.isatty()

    def read(self, size: int | None = -1, /) -> str:
        self._refuse_terminal("read")
        return self._stream.read(-1 if size is None else size)

    def readline(self, size: int | None = -1, /) -> str:
        self._refuse_terminal("readline")
        line = self._stream.readline(-1 if size is None else size)
        if not line and size != 0 and not self._lines:
            raise blocked_read("readline")
        self._lines = self._lines or bool(line)
        return line

    def readlines(self, hint: int = -1, /) -> list[str]:
        self._refuse_terminal("readlines")
        return self._stream.readlines(hint)

    def __iter__(self) -> Iterator[str]:
        self._refuse_terminal("iteration")
        return iter(self._stream)

    def _refuse_terminal(self, call: str) -> None:
        if self._stream.isatty():
            raise blocked_read(call)


@dataclass(frozen=True, slots=True)
class Prompter:
    """How one handler run may ask: ``interactive`` when stdin and stdout are terminals
    and ``--non-interactive`` is absent; ``assume_yes`` from ``--yes``"""

    command: str
    declared: bool
    """``interactive=True`` on the command"""
    editor_alternatives: tuple[str, ...]
    flags: frozenset[str]
    """The command's own flags, one of which ``prompt(flag=)`` must name"""
    interactive: bool
    assume_yes: bool
    stdin: IO[str] = field(repr=False)
    stderr: IO[str] = field(repr=False)
    env: Mapping[str, str] = field(repr=False)

    def prompt(self, text: str, *, flag: str) -> str:
        self._check_declared("prompt")
        if flag not in self.flags:
            # The suggestion off a terminal names it: a flag the command lacks is a dead end
            raise RegistrationError(
                f"{self.command}: ctx.prompt(flag={flag!r}) names no flag of the command; "
                "flag= is the one that supplies the answer instead"
            )
        if not self.interactive:
            raise InputRequired(
                "INPUT_REQUIRED",
                f"Command {self.command} asks {text!r}, and no one can answer off a terminal",
                suggestion=f"pass --{flag} with the answer",
                context={"prompt": text, "flag": flag},
            )
        return self._ask(f"{text}: ", flag=flag)

    def confirm(self, text: str) -> bool:
        self._check_declared("confirm")
        if self.assume_yes:
            return True
        if not self.interactive:
            raise InputRequired(
                "INPUT_REQUIRED",
                f"Command {self.command} asks {text!r}, and no one can answer off a terminal",
                suggestion="pass --yes to confirm",
                context={"prompt": text, "flag": "yes"},
            )
        return self._ask(f"{text} [y/N] ", flag="yes").strip().lower() in ("y", "yes")

    def edit(self, initial: str, where: Callable[[], Path]) -> str:
        """The text after a person edited ``initial`` in ``$VISUAL`` or ``$EDITOR``, in a
        file from ``where``, the run's own temp directory"""
        if not self.editor_alternatives:
            raise RegistrationError(
                f"{self.command}: ctx.edit needs editor_alternatives=[...] naming the flags "
                "that replace the editor (REQ-C-023)"
            )
        if not self.interactive:
            flags = [f"--{name}" for name in self.editor_alternatives]
            raise InputRequired(
                "EDITOR_REQUIRED",
                f"Command {self.command} opens an editor, and stdin is not a TTY",
                suggestion=f"use {' or '.join(flags)} instead of the editor",
                context={"alternatives": flags},
                alternatives=[
                    {"flag": flag, "description": "Supplies the text instead of the editor"}
                    for flag in flags
                ],
            )
        editor = self.env.get("VISUAL") or self.env.get("EDITOR") or "vi"
        path = where()
        try:
            path.write_text(initial, encoding="utf-8")
            # The person's terminal, not captured: this child is the interaction
            try:
                done = subprocess.run([*shlex.split(editor), str(path)], env=dict(self.env))
            except FileNotFoundError:
                raise _editor_failed(editor, "was not found") from None
            if done.returncode != 0:
                # Quitting with an error is how a person abandons the edit, as with git
                raise _editor_failed(editor, f"exited with {done.returncode}")
            return path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

    def _check_declared(self, method: str) -> None:
        if not self.declared:
            raise RegistrationError(
                f"{self.command}: ctx.{method} needs interactive=True on the command, which "
                "adds --yes and --non-interactive (REQ-C-005)"
            )

    def _ask(self, question: str, *, flag: str) -> str:
        self.stderr.write(question)
        self.stderr.flush()
        answer = self.stdin.readline()
        if not answer:
            raise InputRequired(
                "INPUT_REQUIRED",
                f"stdin closed before an answer to {question.strip()!r}",
                suggestion=f"pass --{flag}",
                context={"prompt": question.strip(), "flag": flag},
            )
        return answer.removesuffix("\n").removesuffix("\r")  # a Windows terminal sends \r\n
