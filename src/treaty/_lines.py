"""Line-mode stdin: ``stdin_input="lines"`` hands the handler its input one line at a time.

A payload command (``stdin_input=True``) reads all of stdin before the handler runs, up to
the stdin cap (REQ-F-054). A line-mode command reads nothing up front: ``ctx.stdin_lines``
reads the next line when the handler asks for it, so a producer piped into it runs at the
same time, and the input has no total size. Each line has a cap of its own instead, so a
runaway producer still cannot exhaust memory. The handler has already started when a line
fails, so a bad line exits 1, not 2 (REQ-F-002), with its 1-based number in the context.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import IO

from ._errors import CliExit, ParseError
from ._values import ExitCodeName, InvalidValue

LINE_TOO_LARGE = "LINE_TOO_LARGE"
LINE_NOT_UTF8 = "LINE_NOT_UTF8"
INPUT_LINES_KEY = "input_lines"
"""The lines of a line-mode command in a JSON payload, where stdin is not its own"""
INPUT_LINES_FLAG = "input-lines"


class StdinInput(StrEnum):
    """How a command reads stdin"""

    TEXT = "text"
    """``stdin_input=True``: the whole payload in ``ctx.stdin_text``, capped"""
    LINES = "lines"
    """``stdin_input="lines"``: ``ctx.stdin_lines``, read lazily, each line capped"""


def stdin_input_of(value: object) -> StdinInput | None:
    """``App.command(stdin_input=)``: False, True, or ``"lines"``"""
    if value is False:
        return None
    if value is True:
        return StdinInput.TEXT
    if value == StdinInput.LINES.value and isinstance(value, str):
        return StdinInput.LINES
    raise InvalidValue(f"stdin_input={value!r} is not one of False, True, or 'lines'")


@dataclass(frozen=True, slots=True)
class LineCap:
    """Most bytes one line of a line-mode command's input may take, its terminator aside"""

    bytes: int

    def __post_init__(self) -> None:
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 1:
            raise InvalidValue("line cap must be a whole number of bytes, at least 1")


DEFAULT_LINE_CAP = LineCap(1_048_576)


def input_lines(value: object) -> tuple[str, ...]:
    """The ``input_lines`` of a JSON payload: an array of strings, none holding a line break"""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ParseError(
            f"{INPUT_LINES_KEY!r} expects an array of strings, one per line",
            context={"field": INPUT_LINES_KEY},
        )
    for number, line in enumerate(value, 1):
        if "\n" in line or "\r" in line:
            raise ParseError(
                f"{INPUT_LINES_KEY!r} item {number} holds a line break; give each line "
                "as its own item",
                context={"field": INPUT_LINES_KEY, "line": number},
            )
    return tuple(value)


ReadLine = Callable[[int], bytes] | Callable[[int], str]


class Lines(Iterator[str]):
    """The lines of one input, read as the handler iterates: each without its ``\\n`` or
    ``\\r\\n``, the last one too when no newline ends it, a leading BOM dropped

    A line over ``cap`` ends the run with ``LINE_TOO_LARGE``, and one that is not UTF-8
    with ``LINE_NOT_UTF8``, both exit 1 with the line's number. ``last_read`` is the
    ``time.monotonic()`` of the latest line, which restarts a stream's idle timeout.
    """

    def __init__(
        self,
        readline: ReadLine,
        cap: LineCap,
        *,
        source: str,
        close: Callable[[], None] | None = None,
    ) -> None:
        self._readline = readline
        self._cap = cap
        self._source = source
        self._close = close
        self._failed: CliExit | None = None
        self._done = False
        self.number = 0
        """Lines read so far"""
        self.last_read: float | None = None

    @classmethod
    def of(cls, stream: IO[str], cap: LineCap, *, source: str) -> Lines:
        """Lines of a text stream; its bytes, when it has them, so a byte that is not UTF-8
        is reported on its own line, not on the line its decoder was reading ahead of"""
        buffer = getattr(stream, "buffer", None)
        if buffer is not None and hasattr(buffer, "readline"):
            return cls(buffer.readline, cap, source=source)
        return cls(stream.readline, cap, source=source)

    @classmethod
    def given(cls, lines: Sequence[str], cap: LineCap) -> Lines:
        """The ``input_lines`` of a JSON payload, held to the same cap as stdin's"""
        items = iter(lines)

        def readline(limit: int) -> str:
            line = next(items, None)
            return "" if line is None else f"{line}\n"

        return cls(readline, cap, source=INPUT_LINES_KEY)

    def __iter__(self) -> Lines:
        return self

    def __next__(self) -> str:
        if self._failed is not None:
            raise self._failed
        if self._done:
            raise StopIteration
        number = self.number + 1
        limit = self._cap.bytes + 2  # room for \r\n: a longer read is a longer line
        try:
            raw = self._readline(limit)
        except UnicodeDecodeError as exc:
            raise self._fail(self._not_utf8(number, exc.reason)) from None
        if not raw:
            self.close()
            raise StopIteration
        self.number = number
        self.last_read = time.monotonic()
        if isinstance(raw, bytes):
            content = raw.removesuffix(b"\n").removesuffix(b"\r")
            if len(content) > self._cap.bytes:
                raise self._fail(self._too_large(number))
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise self._fail(self._not_utf8(number, exc.reason)) from None
        else:
            text = raw.removesuffix("\n").removesuffix("\r")
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError as exc:  # a surrogateescape stdin's undecodable byte
                raise self._fail(self._not_utf8(number, exc.reason)) from None
            if size > self._cap.bytes:
                raise self._fail(self._too_large(number))
        return text.removeprefix("﻿") if number == 1 else text

    def close(self) -> None:
        """Close what this input opened, an ``--input-file``; stdin is the run's own"""
        self._done = True
        if self._close is not None:
            close, self._close = self._close, None
            close()

    def _fail(self, exc: CliExit) -> CliExit:
        self._failed = exc
        self.close()
        return exc

    def _too_large(self, number: int) -> CliExit:
        return CliExit(
            ExitCodeName("GENERAL_ERROR"),
            f"line {number} of {self._source} exceeds the {self._cap.bytes}-byte line limit",
            code=LINE_TOO_LARGE,
            context={"line": number, "limit_bytes": self._cap.bytes, "source": self._source},
            suggestion="split the record over several lines, or raise "
            "App(max_line_bytes=...) if such lines are expected",
        )

    def _not_utf8(self, number: int, reason: str) -> CliExit:
        return CliExit(
            ExitCodeName("GENERAL_ERROR"),
            f"line {number} of {self._source} is not valid UTF-8: {reason}",
            code=LINE_NOT_UTF8,
            context={"line": number, "source": self._source},
            suggestion="pipe UTF-8 text, one record per line",
        )
