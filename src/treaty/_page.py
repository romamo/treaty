"""Pagination for list commands (REQ-F-018, REQ-F-019, REQ-O-003).

A paginated handler returns the whole list, or a ``Page`` holding one batch of its
source and the source's own cursor after it. The framework slices what it gets to
``--limit`` and hands out its own cursor, which names the command, the handler's cursor,
and how many of the items returned there were already delivered. The same position
resumes after a page the byte cap cut short (REQ-F-052), so every cut can be followed.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ._errors import ParseError, SchemaError
from ._values import CommandPath, InvalidValue

LIMIT_FLAG = "limit"
CURSOR_FLAG = "cursor"
DEFAULT_LIMIT = 20
_VERSION = 1
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One batch from a paginated source: return it when the handler reads ``ctx.page``
    instead of loading the whole list

    ``next_cursor`` is the source's own position after the batch, handed back as
    ``ctx.page.cursor`` on the next call; ``None`` means the source is exhausted.
    ``total`` counts the whole collection when the source knows it.
    """

    items: Sequence[T]
    next_cursor: str | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.items, (str, bytes)) or not isinstance(self.items, Sequence):
            raise TypeError("Page.items must be a list or tuple")
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise TypeError("Page.next_cursor must be a string or None")
        if self.total is not None and (
            isinstance(self.total, bool) or not isinstance(self.total, int) or self.total < 0
        ):
            raise TypeError("Page.total must be a non-negative int or None")


@dataclass(frozen=True, slots=True)
class PageRequest:
    """``ctx.page``: return up to ``limit`` items (``None``: all) from ``cursor`` on
    (``None``: the start); returning more is allowed, the framework slices"""

    limit: int | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class Limit:
    """Most items one response carries; ``None`` is no limit"""

    count: int | None

    def __post_init__(self) -> None:
        if self.count is not None and self.count < 1:
            raise InvalidValue("a page limit is at least 1, or None for no limit")

    @classmethod
    def parse(cls, raw: object) -> Limit:
        """A flag or JSON value; ``0`` means no limit (REQ-F-019)"""
        context = {"flag": LIMIT_FLAG, "value": raw}
        if isinstance(raw, str) and raw.isascii() and raw.isdigit():
            value = int(raw)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            value = raw
        else:
            raise ParseError("'limit' expects a whole number of items", context=context)
        if value < 0:
            raise ParseError(
                "'limit' cannot be negative", context=context, suggestion="pass 0 for every item"
            )
        return cls(None) if value == 0 else cls(value)


@dataclass(frozen=True, slots=True)
class Position:
    """Where a page starts: the handler's cursor, and how many of the items the handler
    returns there were delivered already"""

    cursor: str | None = None
    skip: int = 0

    def encode(self, path: CommandPath) -> str:
        """The ``--cursor`` token: URL-safe, stateless, bound to one command"""
        body = {"v": _VERSION, "cmd": path.value, "c": self.cursor, "s": self.skip}
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: object, path: CommandPath) -> Position:
        """A token this command issued, or an ``INVALID_CURSOR`` argument error"""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise _invalid(token, "it is not a cursor token")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            body = json.loads(raw)
        except ValueError:  # binascii.Error, a JSON or UTF-8 decode error
            raise _invalid(token, "it does not decode") from None
        if (
            not isinstance(body, dict)
            or body.keys() != {"v", "cmd", "c", "s"}
            or body["v"] != _VERSION
            or not (body["c"] is None or isinstance(body["c"], str))
            or isinstance(body["s"], bool)
            or not isinstance(body["s"], int)
            or body["s"] < 0
        ):
            raise _invalid(token, "it was not issued by this tool version")
        if body["cmd"] != path.value:
            raise _invalid(token, f"it was issued by {body['cmd']!r}, not {path.value!r}")
        return cls(body["c"], body["s"])


def _invalid(token: object, why: str) -> ParseError:
    return ParseError(
        f"--cursor is invalid: {why}",
        context={"flag": CURSOR_FLAG},
        suggestion="rerun without --cursor to start from the first page",
        code="INVALID_CURSOR",
    )


@dataclass(frozen=True, slots=True)
class Pagination:
    """``meta.pagination`` (REQ-F-018); ``truncated`` and ``has_more`` agree, since a page
    is cut short exactly when more follow"""

    total: int | None
    returned: int
    next_cursor: str | None

    def to_json(self) -> dict[str, object]:
        more = self.next_cursor is not None
        return {
            "total": self.total,
            "returned": self.returned,
            "truncated": more,
            "has_more": more,
            "next_cursor": self.next_cursor,
        }


def request(position: Position, limit: Limit) -> PageRequest:
    """What the handler is asked for: the items already delivered at its cursor, plus a page"""
    return PageRequest(
        limit=None if limit.count is None else position.skip + limit.count,
        cursor=position.cursor,
    )


def take(
    result: object, position: Position, limit: Limit, path: CommandPath
) -> tuple[list[object], Pagination]:
    """The page to send and its metadata, from a handler's list or ``Page``"""
    if isinstance(result, Page):
        items, after, total = list(result.items), result.next_cursor, result.total
    elif isinstance(result, (list, tuple)):
        # A plain list is the whole collection
        items, after, total = list(result), None, len(result)
    else:
        raise SchemaError(f"{type(result).__name__}, not a list or a treaty.Page")
    end = None if limit.count is None else position.skip + limit.count
    window = items[position.skip : end]
    following: Position | None = None
    if end is not None and len(items) > end:
        following = Position(position.cursor, end)
    elif after is not None:
        following = Position(after)
    token = None if following is None else following.encode(path)
    return window, Pagination(total=total, returned=len(window), next_cursor=token)
