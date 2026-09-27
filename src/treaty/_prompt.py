"""Prompts, confirmations, and editors that never wait for input an agent cannot give.

A command that asks a person declares ``interactive=True`` and asks through
``ctx.prompt`` and ``ctx.confirm``; one that opens an editor declares the flags that
replace it with ``editor_alternatives=`` and calls ``ctx.edit``. They ask only when stdin
and stdout are both terminals and ``--non-interactive`` is absent; otherwise the run
ends with exit 4 and an error naming the flag that answers instead (REQ-F-009,
REQ-C-005, REQ-F-055). A stray ``input()`` off a terminal ends the same way (REQ-F-047).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ._errors import RegistrationError


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


def blocked_read() -> InputRequired:
    return InputRequired(
        "INTERACTIVE_BLOCKED",
        "Command requires interactive input but stdin is not a TTY",
        suggestion="no flag supplies this input; run it in a terminal, or ask the command "
        "author to declare interactive=True and read it with ctx.prompt(..., flag=...)",
        context={"call": "readline"},
    )


class NoPromptStdin:
    """``sys.stdin`` while a handler runs off a terminal: ``input()`` and ``readline()``
    raise ``INTERACTIVE_BLOCKED``; ``read()``, line iteration, and ``buffer`` still read
    piped data"""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def buffer(self) -> object:
        return getattr(self._stream, "buffer")  # noqa: B009 - IO[str] does not declare it

    @property
    def encoding(self) -> str:
        return str(getattr(self._stream, "encoding", "utf-8"))

    def read(self, size: int | None = -1, /) -> str:
        return self._stream.read(-1 if size is None else size)

    def readline(self, size: int | None = -1, /) -> str:
        raise blocked_read()

    def __iter__(self) -> Iterator[str]:
        return iter(self._stream)


@dataclass(frozen=True, slots=True)
class Prompter:
    """How one handler run may ask: ``interactive`` when stdin and stdout are terminals
    and ``--non-interactive`` is absent; ``assume_yes`` from ``--yes``"""

    command: str
    declared: bool
    """``interactive=True`` on the command"""
    editor_alternatives: tuple[str, ...]
    interactive: bool
    assume_yes: bool
    stdin: IO[str] = field(repr=False)
    stderr: IO[str] = field(repr=False)
    env: Mapping[str, str] = field(repr=False)

    def prompt(self, text: str, *, flag: str) -> str:
        self._check_declared("prompt")
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

    def edit(self, initial: str = "") -> str:
        """The text after a person edited ``initial`` in ``$VISUAL`` or ``$EDITOR``"""
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
        fd, name = tempfile.mkstemp(suffix=".txt", text=True)
        path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(initial)
            # The person's terminal, not captured: this child is the interaction
            subprocess.run([*shlex.split(editor), str(path)], check=True, env=dict(self.env))
            return path.read_text(encoding="utf-8")
        finally:
            path.unlink()

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
        return answer.rstrip("\n")
