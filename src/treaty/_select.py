"""What a caller keeps of ``data``: ``--fields`` (REQ-O-002) and the token budget flags
(REQ-O-049), applied after masking and trust tags and before the byte cap.

The budget is measured over ``data`` as compact JSON, the text the envelope carries.
``--token-offset`` and ``--token-limit`` move over whole items of the data array (or of
the largest array in object data): a window starts at the first item that ends after the
offset, and ``meta.next_token_offset`` is where the next one starts. A window whose first
item alone is over the limit has that item's fields cut, as the byte cap cuts them.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ._cap import Cut, shrink
from ._envelope import Envelope
from ._errors import ParseError, RegistrationError
from ._page import whole_number
from ._protect import TRUST_TAGS

FIELDS_FLAG = "fields"
FIELDS_KEY = "fields"
"""``--fields`` in a JSON payload: an exec line's ``_opts`` or an MCP call"""
STREAM_FLAG = "stream"
TOKEN_LIMIT_FLAG = "token-limit"
TOKEN_OFFSET_FLAG = "token-offset"
TOKEN_COUNT_FLAG = "token-count"
TOKENIZER_FLAG = "tokenizer"
STREAMING_NOT_SUPPORTED = "STREAMING_NOT_SUPPORTED"

APPROX = "approx"
TIKTOKEN = ("cl100k_base", "o200k_base")
"""Encodings the optional ``treaty[tiktoken]`` extra provides"""

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def parse_fields(raw: object) -> tuple[str, ...]:
    """``--fields id,name``, or a JSON list of names; an empty name is an error"""
    if isinstance(raw, list) and all(isinstance(n, str) for n in raw):
        names = [str(n).strip() for n in raw]
    elif isinstance(raw, str):
        names = [n.strip() for n in raw.split(",")]
    else:
        raise ParseError(
            "'fields' expects comma-separated names, such as id,name",
            context={"flag": FIELDS_FLAG},
        )
    if not names or not all(names):
        raise ParseError(
            "--fields takes comma-separated field names with none empty, such as id,name",
            context={"flag": FIELDS_FLAG, "value": raw if isinstance(raw, str) else names},
        )
    return tuple(dict.fromkeys(names))


def project(data: object, fields: tuple[str, ...]) -> object:
    """The named top-level keys of object ``data``, or of each object item of an array;
    unknown names are ignored and trust tags always kept"""
    keep = {*fields, *TRUST_TAGS}

    def one(item: object) -> object:
        if not isinstance(item, dict):
            return item
        return {k: v for k, v in item.items() if k in keep}

    if isinstance(data, list):
        return [one(i) for i in data]
    return one(data)


def approx(text: str) -> int:
    """The built-in tokenizer: UTF-8 bytes over four, rounded up (12-D4)"""
    return math.ceil(len(text.encode("utf-8")) / 4)


@dataclass(frozen=True, slots=True)
class Tokenizer:
    name: str
    count: Callable[[str], int]


def check_tokenizer(name: str, count: object) -> Tokenizer:
    """``app.tokenizer()``: a flag-safe name and a counting function"""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise RegistrationError(f"tokenizer name {name!r} is letters, digits, '.', '_', '-'")
    if not callable(count):
        raise RegistrationError(f"tokenizer {name}: count is a function of the text")
    return Tokenizer(name, count)


def tiktoken_tokenizer(name: str) -> Tokenizer:
    """``cl100k_base`` or ``o200k_base`` from tiktoken, which ``treaty[tiktoken]`` installs"""
    try:
        module = importlib.import_module("tiktoken")
    except ModuleNotFoundError:
        raise ParseError(
            f"--tokenizer {name} needs tiktoken, which is not installed",
            code="TOKENIZER_UNAVAILABLE",
            context={"flag": TOKENIZER_FLAG, "value": name},
            suggestion="install treaty[tiktoken], or pass --tokenizer approx",
        ) from None
    encoding = module.get_encoding(name)
    return Tokenizer(name, lambda text: len(encoding.encode(text, disallowed_special=())))


def resolve_tokenizer(name: str, registered: Mapping[str, Tokenizer]) -> Tokenizer:
    if name in registered:
        return registered[name]
    if name in TIKTOKEN:
        return tiktoken_tokenizer(name)
    available = sorted({*registered, *TIKTOKEN})
    raise ParseError(
        f"unknown --tokenizer {name!r}",
        context={"flag": TOKENIZER_FLAG, "value": name[:64], "available": available},
        suggestion=f"pass one of: {', '.join(available)}",
    )


def token_number(raw: str, flag: str) -> int:
    return whole_number(raw, flag, "a whole number of tokens")


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """``--token-limit``, ``--token-offset``, and ``--token-count`` of one run"""

    tokenizer: Tokenizer
    limit: int | None = None
    offset: int | None = None
    count_only: bool = False

    def __post_init__(self) -> None:
        if self.count_only and (self.limit is not None or self.offset is not None):
            raise ParseError(
                "--token-count reports the size of the whole output; drop --token-limit "
                "and --token-offset",
                context={"flag": TOKEN_COUNT_FLAG},
            )
        if self.limit == 0:
            raise ParseError(
                "--token-limit must be at least 1", context={"flag": TOKEN_LIMIT_FLAG, "value": 0}
            )

    def measure(self, data: object) -> int:
        return 0 if data is None else self.tokenizer.count(_text(data))

    def apply(self, envelope: Envelope) -> Envelope:
        """``envelope`` with its ``data`` windowed and cut to the budget"""
        meta: dict[str, object] = {"tokenizer": self.tokenizer.name}
        if self.count_only:
            meta["token_count"] = self.measure(envelope.data)
            extra = {**envelope.extra_meta, **meta}
            return dataclasses.replace(envelope, data=None, extra_meta=extra)
        data, warnings = envelope.data, list(envelope.warnings)
        path, items = _window(data)
        starts = _starts(items, self.measure)
        first = 0
        if self.offset is not None:
            first = next((i for i in range(len(items)) if starts[i + 1] > self.offset), len(items))
            data = _with(data, path, items[first:])
            meta["token_offset"] = starts[first]
        if self.limit is not None:
            limit = self.limit
            meta["token_limit"] = limit
            if self.measure(data) > limit:
                meta["truncated"] = True
                shrunk = shrink(data, lambda candidate, cuts: self.measure(candidate) <= limit)
                cuts = [Cut((), self.measure(data), 0)] if shrunk is None else shrunk[1]
                data = None if shrunk is None else shrunk[0]
                warnings += (c.warning() for c in cuts)
                kept = next((c.kept for c in cuts if c.path == path), None)
                if kept is not None and first + kept < len(items):
                    meta["next_token_offset"] = starts[first + kept]
        return dataclasses.replace(
            envelope,
            data=data,
            warnings=tuple(warnings),
            extra_meta={**envelope.extra_meta, **meta},
        )


def _text(data: object) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _window(data: object) -> tuple[tuple[str, ...] | None, list[object]]:
    """The array a window moves over: ``data`` itself, or the largest array in object data"""
    if isinstance(data, list):
        return (), data
    if isinstance(data, dict):
        arrays = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if arrays:
            key, items = max(arrays, key=lambda kv: len(_text(kv[1])))
            return (key,), items
    return None, []


def _starts(items: list[object], measure: Callable[[object], int]) -> list[int]:
    """Where each item starts, in tokens, and where the last ends"""
    starts = [0]
    for item in items:
        starts.append(starts[-1] + measure(item))
    return starts


def _with(data: object, path: tuple[str, ...] | None, items: list[object]) -> object:
    if path is None:
        return data
    if not path:
        return items
    assert isinstance(data, dict)
    return {**data, path[0]: items}


def id_problem(data: object, field: str) -> str | None:
    """Why ``data`` cannot be written as bare ids, one per line; None when it can"""
    items = data if isinstance(data, list) else [data]
    for item in items:
        value = item.get(field) if isinstance(item, dict) else None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return f"{field} is {type(value).__name__}, not a string or integer id"
        text = str(value)
        if not text or any(c.isspace() or not c.isprintable() for c in text):
            return f"{field} {text[:64]!r} is empty or holds whitespace, so it cannot be piped"
    return None


def id_lines(data: object, *, field: str) -> str:
    """``--format id``: each id alone on a line; ``id_problem`` passed first"""
    items = data if isinstance(data, list) else [data]
    return "".join(f"{item[field]}\n" for item in items if isinstance(item, dict))
