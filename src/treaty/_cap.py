"""Response byte cap (REQ-F-052) with per-field truncation warnings (REQ-F-064).

When a JSON envelope serializes past the cap, the framework walks down from
``data`` into whichever child holds more than half of its parent's bytes, and
cuts where no single child dominates: a list keeps a prefix of its items, an
object a prefix of its keys, a string a prefix of its characters plus a marker.
The cut is the longest prefix that still fits. Lists and objects keep at least
one entry, so a single huge entry has its own fields cut instead of vanishing.
Each cut is reported as a ``FIELD_TRUNCATED`` warning, and ``meta`` says how to
get the full response. The cap governs ``data`` only: when error or meta alone
exceed it, ``data`` still gets the cap as its own budget, so the envelope is at most
that oversized base plus the cap.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ._envelope import Envelope, WarningDetail, serialize
from ._errors import ParseError
from ._values import InvalidValue

ENV_VAR = "TREATY_MAX_OUTPUT_BYTES"
MARKER = "[truncated]"
MIN_BYTES = 4096
_MIN_STRING = 64  # shorter strings save less than the marker costs

Key = str | int
FieldPath = tuple[Key, ...]
Node = list[object] | dict[str, object] | str


@dataclass(frozen=True, slots=True)
class OutputCap:
    """Most bytes a serialized JSON envelope may take"""

    bytes: int

    def __post_init__(self) -> None:
        if self.bytes < MIN_BYTES:
            raise InvalidValue(f"output cap must be at least {MIN_BYTES} bytes")

    @classmethod
    def resolve(cls, explicit: str | None, env: Mapping[str, str], default: OutputCap) -> OutputCap:
        """``--max-output``, then ``TREATY_MAX_OUTPUT_BYTES``, then the App default"""
        if explicit is not None:
            source, raw = "--max-output", explicit
        elif ENV_VAR in env:
            source, raw = ENV_VAR, env[ENV_VAR]
        else:
            return default
        try:
            return cls(int(raw))
        except ValueError:
            raise ParseError(
                f"{source} must be a whole number of bytes, at least {MIN_BYTES}",
                context={"source": source, "value": raw, "minimum": MIN_BYTES},
            ) from None


DEFAULT_CAP = OutputCap(1_048_576)

STDIN_ENV_VAR = "TREATY_MAX_STDIN_BYTES"


@dataclass(frozen=True, slots=True)
class StdinCap:
    """Most bytes ``exec`` reads from a pipe (REQ-F-054); ``--input-file`` has no cap"""

    bytes: int

    def __post_init__(self) -> None:
        if self.bytes < 1:
            raise InvalidValue("stdin cap must be at least 1 byte")

    @classmethod
    def resolve(cls, env: Mapping[str, str], default: StdinCap) -> StdinCap:
        raw = env.get(STDIN_ENV_VAR)
        if raw is None:
            return default
        try:
            return cls(int(raw))
        except ValueError:
            raise ParseError(
                f"{STDIN_ENV_VAR} must be a whole number of bytes, at least 1",
                context={"source": STDIN_ENV_VAR, "value": raw},
            ) from None


DEFAULT_STDIN_CAP = StdinCap(65_536)


@dataclass(frozen=True, slots=True)
class _Cut:
    path: FieldPath
    original: int
    kept: int

    def warning(self) -> WarningDetail:
        return WarningDetail(
            code="FIELD_TRUNCATED",
            message=f"{_render(self.path)} cut from {self.original} to {self.kept}",
            context={
                "field": _render(self.path),
                "original_length": self.original,
                "truncated_length": self.kept,
            },
        )


def cap_envelope(envelope: Envelope, cap: OutputCap, *, argv: bool = True) -> Envelope:
    """``argv=False`` for in-process calls, whose hint can only name the variable"""
    total = len(serialize(envelope).encode())
    if total <= cap.bytes or envelope.data is None:
        return envelope
    base = len(serialize(dataclasses.replace(envelope, data=None)).encode())
    if base > cap.bytes:
        # Error or meta alone exceed the cap, which cutting data cannot fix; data still
        # gets the cap as its own budget, so the envelope stays bounded
        return cap_envelope(envelope, OutputCap(base + cap.bytes), argv=argv)
    data: object = copy.deepcopy(envelope.data)
    cuts: list[_Cut] = []
    visited: set[FieldPath] = set()

    def fits(candidate: object, pending: list[_Cut]) -> bool:
        trial = _truncated(envelope, candidate, pending, total, argv)
        return len(serialize(trial).encode()) <= cap.bytes

    while (target := _target(data, visited)) is not None:
        path, node = target
        visited.add(path)
        floor = 0 if isinstance(node, str) else 1
        best: int | None = None
        lo, hi = floor, len(node) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            data = _replace(data, path, _prefix(node, mid))
            if fits(data, [*cuts, _Cut(path, len(node), mid)]):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        kept = floor if best is None else best
        data = _replace(data, path, _prefix(node, kept))
        cuts.append(_Cut(path, len(node), kept))
        if best is not None:
            return _truncated(envelope, data, cuts, total, argv)
    return _truncated(envelope, None, [_Cut((), total, 0)], total, argv)


def _truncated(
    envelope: Envelope, data: object, cuts: list[_Cut], total: int, argv: bool
) -> Envelope:
    hint = f"rerun with --max-output {total} or {ENV_VAR}={total}"
    meta: dict[str, object] = {
        **envelope.extra_meta,
        "truncated": True,
        "total_bytes": total,
        # An in-process caller (MCP) cannot pass --max-output, and the server's
        # environment is fixed at launch: it can only ask for less
        "truncation_hint": hint
        if argv
        else f"ask for less data (a filter or a smaller page); the full response is {total} "
        f"bytes, and the server's {ENV_VAR} sets the cap",
    }
    root = next((c for c in cuts if c.path == ()), None)
    if root is not None and data is not None and isinstance(envelope.data, list):
        meta["total_count"], meta["returned_count"] = root.original, root.kept
    return dataclasses.replace(
        envelope,
        data=data,
        warnings=[*envelope.warnings, *(c.warning() for c in cuts)],
        extra_meta=meta,
    )


def _target(data: object, visited: set[FieldPath]) -> tuple[FieldPath, Node] | None:
    """Descend into the child holding most of the bytes; cut where none dominates"""
    path: FieldPath = ()
    node = data
    while True:
        children = _children(node)
        heaviest = max(children, key=lambda kv: _size(kv[1]), default=None)
        dominant = heaviest is not None and 2 * _size(heaviest[1]) > _size(node)
        if _cuttable(node) and path not in visited and not dominant:
            assert isinstance(node, (list, dict, str))
            return path, node
        if heaviest is None:
            return None
        path, node = (*path, heaviest[0]), heaviest[1]


def _children(node: object) -> list[tuple[Key, object]]:
    if isinstance(node, dict):
        return list(node.items())
    if isinstance(node, list):
        return list(enumerate(node))
    return []


def _cuttable(node: object) -> bool:
    if isinstance(node, (list, dict)):
        return len(node) >= 2
    return isinstance(node, str) and len(node) >= _MIN_STRING


def _size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def _prefix(node: Node, n: int) -> Node:
    if isinstance(node, dict):
        return dict(itertools.islice(node.items(), n))
    return node[:n] if isinstance(node, list) else node[:n] + MARKER


def _replace(data: object, path: FieldPath, value: object) -> object:
    """Set the value at path in place (data is our private copy); the root is returned"""
    if not path:
        return value
    parent = data
    for key in path[:-1]:
        parent = parent[key]  # type: ignore[index]
    parent[path[-1]] = value  # type: ignore[index]
    return data


def _render(path: FieldPath) -> str:
    out = "$"
    for key in path:
        out += f"[{key}]" if isinstance(key, int) else f".{key}"
    return out
