"""Response byte cap (REQ-F-052) with per-field truncation warnings (REQ-F-064).

When a JSON envelope serializes past the cap, the framework walks down from
``data`` into whichever child holds more than half of its parent's bytes, and
cuts where no single child dominates: a list keeps a prefix of its items, an
object a prefix of its keys, a string a prefix of its characters plus a marker.
The cut is the longest prefix that still fits. Lists and objects keep at least
one entry, so a single huge entry has its own fields cut instead of vanishing.
Each cut is reported as a ``FIELD_TRUNCATED`` warning, and ``meta.truncation_hint``
is the command that gets the rest: the next page of a list command, else the same
command with a larger ``--max-output``. The cap governs ``data`` only: when error or meta alone
exceed it, ``data`` still gets the cap as its own budget, so the envelope is at most
that oversized base plus the cap. When the cut's own report (the truncation meta and its
warning) leaves no room for any ``data``, ``data`` is dropped and the line passes the cap
by that report alone, with the hint in its shortest form. Sizes are of the line as
written, its newline included, so ``meta.total_bytes`` and the cap count the same bytes.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ._env import MAX_OUTPUT_BYTES, MAX_STDIN_BYTES, app_var
from ._envelope import Envelope, WarningDetail, written_size
from ._errors import ParseError
from ._out import data_path, is_binary
from ._page import CURSOR_FLAG, LIMIT_FLAG, Position
from ._values import CommandPath, InvalidValue

MAX_OUTPUT_FLAG = "max-output"
MARKER = "[truncated]"
TRUNCATED_CODE = "FIELD_TRUNCATED"
MIN_BYTES = 4096
SLACK = 1024
"""Bytes a ``--max-output`` hint adds to the full size, for what varies between runs"""
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
    def resolve(
        cls, explicit: str | None, env: Mapping[str, str], default: OutputCap, app_name: str
    ) -> OutputCap:
        """``--max-output``, then ``<APP>_MAX_OUTPUT_BYTES``, then the App default
        (REQ-F-052)"""
        var = env_var(app_name)
        if explicit is not None:
            source, raw = "--max-output", explicit
        elif var in env:
            source, raw = var, env[var]
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


def env_var(app_name: str) -> str:
    """The tool's own cap variable, such as ``DEPLOYCTL_MAX_OUTPUT_BYTES``"""
    return app_var(app_name, MAX_OUTPUT_BYTES.key)


@dataclass(frozen=True, slots=True)
class Rerun:
    """What the cap needs to say how to get what a cut left out"""

    argv: tuple[str, ...] | None
    """The invocation as typed, app name first; None where it cannot be rerun (exec, MCP)"""
    app_name: str
    """Names the ``<APP>_MAX_OUTPUT_BYTES`` variable a hint without argv points at"""
    page: tuple[CommandPath, Position] | None = None
    """Where a list page started, so a cut page gets a cursor to its first dropped item"""


def with_flags(argv: Sequence[str], flags: Mapping[str, str]) -> list[str]:
    """``argv`` with each of ``flags`` set to its value, replacing earlier spellings
    (``--name value`` or ``--name=value``), before any ``--``"""
    out: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest = list(argv[i:])
            break
        name, eq, _ = tok[2:].partition("=") if tok.startswith("--") else ("", "", "")
        if name in flags:
            i += 1 if eq else 2
            continue
        out.append(tok)
        i += 1
    for name, value in flags.items():
        out.extend((f"--{name}", value))
    return out + rest


@dataclass(frozen=True, slots=True)
class StdinCap:
    """Most bytes read from a piped stdin by ``exec`` and ``stdin_input`` commands
    (REQ-F-054); ``--input-file`` has no cap"""

    bytes: int

    def __post_init__(self) -> None:
        if self.bytes < 1:
            raise InvalidValue("stdin cap must be at least 1 byte")

    @classmethod
    def resolve(cls, env: Mapping[str, str], default: StdinCap, app_name: str) -> StdinCap:
        """``<APP>_MAX_STDIN_BYTES``, then the App default"""
        source = app_var(app_name, MAX_STDIN_BYTES.key)
        if source not in env:
            return default
        raw = env[source]
        try:
            return cls(int(raw))
        except ValueError:
            raise ParseError(
                f"{source} must be a whole number of bytes, at least 1",
                context={"source": source, "value": raw},
            ) from None


DEFAULT_STDIN_CAP = StdinCap(65_536)


@dataclass(frozen=True, slots=True)
class Cut:
    path: FieldPath
    original: int
    kept: int

    def warning(self) -> WarningDetail:
        return WarningDetail(
            code=TRUNCATED_CODE,
            message=f"{data_path(self.path)} cut from {self.original} to {self.kept}",
            context={
                "field": data_path(self.path),
                "original_length": self.original,
                "truncated_length": self.kept,
            },
        )


def cap_envelope(envelope: Envelope, cap: OutputCap, rerun: Rerun) -> Envelope:
    """``envelope`` within ``cap``, with the command that gets the rest in its ``meta``;
    ``data`` is cleaned of terminal escapes first, as every envelope written is"""
    return cut_envelope(envelope, cap, rerun)[0]


def cut_envelope(
    envelope: Envelope, cap: OutputCap, rerun: Rerun, *, total: int | None = None
) -> tuple[Envelope, int | None]:
    """``cap_envelope``, with the full response's size when it was cut, None when it fit;
    ``total`` is that size when ``envelope`` is not the whole response, as on a re-cut"""
    envelope = envelope.cleaned()
    size = written_size(envelope)
    if size <= cap.bytes or envelope.data is None:
        return envelope, None
    total = size if total is None else total
    base = written_size(dataclasses.replace(envelope, data=None))
    if base > cap.bytes:
        # Error or meta alone exceed the cap, which cutting data cannot fix; data still
        # gets the cap as its own budget, so the envelope stays bounded
        return cut_envelope(envelope, OutputCap(base + cap.bytes), rerun, total=total)

    def fits(candidate: object, pending: list[Cut]) -> bool:
        trial = _truncated(envelope, candidate, pending, total, rerun)
        # The envelope as written, newline included (#129)
        return written_size(trial) <= cap.bytes

    shrunk = shrink(envelope.data, fits)
    if shrunk is not None:
        return _truncated(envelope, *shrunk, total, rerun), total
    dropped = [Cut((), total, 0)]
    fallback = _truncated(envelope, None, dropped, total, rerun)
    if written_size(fallback) <= cap.bytes:
        return fallback, total
    # Even without data the report passes the cap (#134): it cannot be left out, so it
    # takes the fewest bytes the hint can say it in
    return _truncated(envelope, None, dropped, total, rerun, brief=True), total


def recap(
    full: Envelope,
    cut: Envelope,
    total: int | None,
    settled: Envelope,
    cap: OutputCap,
    rerun: Rerun,
) -> Envelope:
    """``settled`` within ``cap``: ``cut`` (``full`` as ``cut_envelope`` left it, ``total``
    its result) with what settling added, a warning or an error. A first cut is redone on
    ``full``'s data, so each field is reported once against its own size and
    ``meta.total_bytes`` stays the full response's size (#134)"""
    if total is None:
        return cap_envelope(settled, cap, rerun)
    if written_size(settled) <= cap.bytes:
        return settled
    kept = len(cut.warnings)
    if tuple(settled.warnings[:kept]) != tuple(cut.warnings):
        raise AssertionError("settling must only append warnings to the cut envelope")
    whole = dataclasses.replace(
        settled,
        data=full.data,
        warnings=(*full.warnings, *settled.warnings[kept:]),
        extra_meta=full.extra_meta,
    )
    return cut_envelope(whole, cap, rerun, total=total)[0]


def shrink(
    data: object, fits: Callable[[object, list[Cut]], bool]
) -> tuple[object, list[Cut]] | None:
    """A copy of ``data`` cut until ``fits`` accepts it, with the cuts made; None when
    even the smallest cut does not fit. ``fits`` sees the candidate and its cuts."""
    data = copy.deepcopy(data)
    cuts: list[Cut] = []
    visited: set[FieldPath] = set()
    while (target := _target(data, visited)) is not None:
        path, node = target
        visited.add(path)
        floor = 0 if isinstance(node, str) else 1
        best: int | None = None
        lo, hi = floor, len(node) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            data = _replace(data, path, _prefix(node, mid))
            if fits(data, [*cuts, Cut(path, len(node), mid)]):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        kept = floor if best is None else best
        data = _replace(data, path, _prefix(node, kept))
        cuts.append(Cut(path, len(node), kept))
        if best is not None:
            return data, cuts
    return None


def _truncated(
    envelope: Envelope,
    data: object,
    cuts: list[Cut],
    total: int,
    rerun: Rerun,
    *,
    brief: bool = False,
) -> Envelope:
    meta: dict[str, object] = {**envelope.extra_meta, "truncated": True, "total_bytes": total}
    root = next((c for c in cuts if c.path == ()), None)
    listed = root is not None and data is not None and isinstance(envelope.data, list)
    pagination = envelope.extra_meta.get("pagination")
    if root is not None and listed:
        meta["total_count"], meta["returned_count"] = root.original, root.kept
    if root is not None and listed and rerun.page is not None and isinstance(pagination, dict):
        # The next page starts at the first item the cut dropped (REQ-F-052)
        path, position = rerun.page
        token = dataclasses.replace(position, skip=position.skip + root.kept).encode(path)
        meta["pagination"] = {
            **pagination,
            "returned": root.kept,
            "truncated": True,
            "has_more": True,
            "next_cursor": token,
        }
        if pagination.get("total") is not None:
            meta["total_count"] = pagination["total"]
        page = {LIMIT_FLAG: str(root.kept), CURSOR_FLAG: token}
        meta["truncation_hint"] = (
            shlex.join(with_flags(rerun.argv, page))
            if rerun.argv is not None
            else f"call again with limit {root.kept} and cursor {token}"
        )
    elif rerun.argv is not None:
        # A rerun's meta differs by a few bytes (duration_ms), so the hint leaves slack
        room = str(total + SLACK)
        meta["truncation_hint"] = shlex.join(with_flags(rerun.argv, {MAX_OUTPUT_FLAG: room}))
    elif brief:
        meta["truncation_hint"] = (
            f"the full response is {total} bytes, over {env_var(rerun.app_name)}"
        )
    else:
        # An exec line or an MCP call has no argv of its own to repeat
        meta["truncation_hint"] = (
            f"ask for less data (a filter or a smaller page); the full response is {total} "
            f"bytes, over the cap that --max-output or {env_var(rerun.app_name)} sets"
        )
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
    if is_binary(node):
        return []  # a cut base64 value would not decode: a list drops it whole (REQ-F-017)
    if isinstance(node, dict):
        return list(node.items())
    if isinstance(node, list):
        return list(enumerate(node))
    return []


def _cuttable(node: object) -> bool:
    if is_binary(node):
        return False
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
