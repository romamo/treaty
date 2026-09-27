"""Forgiving JSON input (REQ-F-059): what agents write that strict JSON refuses.

Strict JSON parses as before. Otherwise the text is read once more, accepting trailing
commas, ``//`` and ``/* */`` comments, single-quoted strings, and unquoted keys; a value
read that way is exactly the value of the equivalent strict JSON. Anything further, such
as a bare word as a value, a missing ``:`` or ``,``, or an unclosed bracket, is repaired
only to show the caller ``corrected_input``: the input is still refused.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_PUNCT = frozenset("{}[]:,")
_WORD_END = frozenset(" \t\r\n{}[]:,\"'")
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?")
_LITERALS = {"true": True, "false": False, "null": None}


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def loads_strict(text: str) -> object:
    """``json.loads`` without the NaN and Infinity extensions; also raises ``ValueError``
    for an integer longer than the interpreter's digit limit"""
    try:
        return json.loads(text, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


class Unreadable(ValueError):
    """The text is not JSON, even forgivingly; ``corrected`` is the repaired form, if any"""

    def __init__(
        self, message: str, corrected: str | None = None, position: int | None = None
    ) -> None:
        super().__init__(message)
        self.corrected = corrected
        self.position = position
        """Where strict JSON first failed"""


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    """``punct``, ``string``, or ``word``"""
    text: str
    value: object = None


def loads_forgiving(text: str) -> object:
    """The value of ``text`` as JSON or the JSON5 forms above; ``Unreadable`` otherwise.
    Strict JSON's own ``ValueError`` (a NaN, an oversized integer, deep nesting) stands."""
    try:
        return loads_strict(text)
    except json.JSONDecodeError as exc:
        strict_error, position = f"{exc.msg} at position {exc.pos}", exc.pos
    try:
        parser = _Parser(_tokens(text))
        value = parser.document()
    except RecursionError:
        raise Unreadable("JSON nested too deeply", position=position) from None
    except Unreadable as exc:
        raise Unreadable(f"{strict_error}; {exc}", position=position) from None
    if parser.repairs:
        corrected = json.dumps(value, ensure_ascii=False)
        raise Unreadable(f"{strict_error}; {parser.repairs[0]}", corrected, position)
    return value


def _tokens(text: str) -> list[_Token]:
    out: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise Unreadable("a /* comment is never closed")
            i = end + 2
        elif ch in _PUNCT:
            out.append(_Token("punct", ch))
            i += 1
        elif ch in "\"'":
            i = _string(text, i, out)
        else:
            start = i
            while i < n and text[i] not in _WORD_END:
                i += 1
            out.append(_Token("word", text[start:i]))
    return out


def _string(text: str, start: int, out: list[_Token]) -> int:
    """One quoted string from ``start``; a single-quoted one is decoded as JSON would
    decode the same content in double quotes. Returns the index after it."""
    quote = text[start]
    body: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            body.append(nxt if quote == "'" and nxt == "'" else ch + nxt)
            i += 2
            continue
        if ch == quote:
            literal = '"' + "".join(body) + '"'
            try:
                value = json.loads(literal)
            except json.JSONDecodeError as exc:
                raise Unreadable(f"string at position {start}: {exc.msg}") from None
            out.append(_Token("string", text[start : i + 1], value))
            return i + 1
        body.append('\\"' if ch == '"' else ch)
        i += 1
    raise Unreadable(f"string at position {start} is never closed")


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.repairs: list[str] = []
        """What had to be invented; any entry means the input is refused"""

    def peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def punct(self, char: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == "punct" and tok.text == char:
            self.pos += 1
            return True
        return False

    def document(self) -> object:
        if self.peek() is None:
            raise Unreadable("there is no value")
        value = self.value()
        extra = self.peek()
        if extra is not None:
            raise Unreadable(f"unexpected {extra.text!r} after the value")
        return value

    def value(self) -> object:
        tok = self.peek()
        if tok is None:
            raise Unreadable("the input ends where a value belongs")
        if self.punct("{"):
            return self.container("}")
        if self.punct("["):
            return self.container("]")
        if tok.kind == "punct":
            raise Unreadable(f"unexpected {tok.text!r} where a value belongs")
        self.pos += 1
        if tok.kind == "string":
            return tok.value
        if tok.text in _LITERALS:
            return _LITERALS[tok.text]
        if _NUMBER.fullmatch(tok.text):
            return loads_strict(tok.text)
        self.repairs.append(f"{tok.text!r} is not quoted")
        return tok.text

    def key(self) -> str:
        tok = self.peek()
        if tok is None or tok.kind == "punct":
            raise Unreadable("an object key is missing")
        self.pos += 1
        if tok.kind == "string":
            if not isinstance(tok.value, str):
                raise Unreadable("an object key is not a string")
            return tok.value
        if not _IDENTIFIER.fullmatch(tok.text):
            self.repairs.append(f"key {tok.text!r} is not quoted")
        return tok.text

    def container(self, close: str) -> object:
        obj = close == "}"
        items: list[object] = []
        members: dict[str, object] = {}
        while True:
            if self.punct(close):
                return members if obj else items
            if self.peek() is None:
                self.repairs.append(f"a {'{' if obj else '['} is never closed")
                return members if obj else items
            if obj:
                name = self.key()
                if not self.punct(":"):
                    self.repairs.append(f"':' is missing after key {name!r}")
                members[name] = self.value()
            else:
                items.append(self.value())
            if self.punct(","):
                continue  # a trailing comma is fine: the close is checked next
            tok = self.peek()
            if tok is not None and not (tok.kind == "punct" and tok.text == close):
                if tok.kind == "punct" and tok.text in "}]":
                    raise Unreadable(f"unexpected {tok.text!r}")
                self.repairs.append("',' is missing between values")
