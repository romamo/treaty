"""The plain fallback: flat lines when neither the command nor the app renders plain

Nested values become dotted paths (``release.tag: 1.3.9``), so every line is one item. A
top-level list of objects whose values are all scalars is an aligned table instead: a
header row of the field names, then one row per object.
Input is the JSON-ready ``data`` of an envelope, after secret redaction. A value loses its
terminal escapes, as in the JSON envelope (REQ-F-007); a key, which is never rewritten, and
a value's other controls are shown as escapes.
"""

from __future__ import annotations

import dataclasses
import typing
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from ._envelope import strip_escapes, visible
from ._out import is_binary, out_spec
from ._types import (
    is_dataclass_type,
    resolve_alias,
    strip_optional,
    type_hints,
    union_members,
)

if typing.TYPE_CHECKING:
    from ._adapters import OutputAdapters

_ESCAPES = str.maketrans({"\n": "\\n", "\t": "\\t"})

NO_ROWS = "(no rows)"
"""The plain text of an empty list of objects"""
ELLIPSIS = "\u2026"
GAP = "  "
"""Between two columns"""
MIN_CUT = 4
"""The narrowest a cut text column gets: three cells and the ellipsis"""


@dataclass(frozen=True, slots=True)
class Layout:
    """What an output type of ``list[T]``, ``T`` a dataclass or a class an output adapter
    writes, such as a pydantic model, declares of the table"""

    order: tuple[str, ...] = ()
    """``T``'s fields in declaration order; other keys of ``data`` follow, as first seen"""
    numeric: frozenset[str] = frozenset()
    """Fields of int, float, or Decimal, right-aligned; a Decimal is text in ``data``"""
    hidden: frozenset[str] = frozenset()
    """Fields declared ``Out(table=False)``"""
    rows: bool = False
    """The output is a list of objects, so an empty one prints ``(no rows)``"""


NO_LAYOUT = Layout()


def layout_of(output_type: object, adapters: OutputAdapters | None = None) -> Layout:
    """The table a command's ``list[T]`` output declares; ``NO_LAYOUT`` for any other.
    A ``T`` one of ``adapters`` writes gives its numeric columns from its schema, and its
    columns follow the keys its dump writes, since the normalized schema sorts them"""
    base, _ = strip_optional(resolve_alias(output_type))
    origin = typing.get_origin(base)
    args = typing.get_args(base)
    if origin in (list, Sequence) and len(args) == 1:
        item: object = args[0]
    elif origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        item = args[0]
    else:
        return NO_LAYOUT
    item, _ = strip_optional(resolve_alias(item))
    if members := union_members(item):
        # A list of a union: each member's columns, in the order the members are named
        layouts = [_item_layout(m, adapters) for m in members]
        if not all(one.rows for one in layouts):
            return NO_LAYOUT
        return Layout(
            order=tuple(dict.fromkeys(k for one in layouts for k in one.order)),
            numeric=frozenset().union(*(one.numeric for one in layouts)),
            hidden=frozenset().union(*(one.hidden for one in layouts)),
            rows=True,
        )
    return _item_layout(item, adapters)


def _item_layout(item: object, adapters: OutputAdapters | None) -> Layout:
    """What ``T`` of a ``list[T]`` output declares of the table"""
    if adapters is not None and isinstance(item, type) and adapters.for_type(item) is not None:
        properties = adapters.node(item).get("properties", {})
        return Layout(
            numeric=frozenset(k for k, v in properties.items() if _numeric_schema(v)),
            rows=True,
        )
    if not is_dataclass_type(item):
        return NO_LAYOUT
    assert isinstance(item, type)
    hints = type_hints(item)
    fields = dataclasses.fields(item)
    return Layout(
        order=tuple(f.name for f in fields),
        numeric=frozenset(f.name for f in fields if _numeric_type(hints[f.name])),
        hidden=frozenset(f.name for f in fields if not out_spec(f).table),
        rows=True,
    )


def _numeric_type(tp: object) -> bool:
    base, _ = strip_optional(resolve_alias(tp))
    return (
        isinstance(base, type)
        and issubclass(base, (int, float, Decimal))
        and not issubclass(base, bool)
    )


def _numeric_schema(node: object) -> bool:
    """A property of integer or number, alone or with null"""
    if not isinstance(node, Mapping):
        return False
    branches = node.get("anyOf")
    if isinstance(branches, list):
        kinds = [b.get("type") if isinstance(b, Mapping) else None for b in branches]
        return _numeric_kind(kinds)
    kind = node.get("type")
    return _numeric_kind(kind if isinstance(kind, list) else [kind])


def _numeric_kind(kinds: list[object]) -> bool:
    present = [k for k in kinds if k != "null"]
    return len(present) == 1 and present[0] in ("integer", "number")


def table_width(env: Mapping[str, str]) -> int | None:
    """The most cells a table line takes: ``COLUMNS`` when it is a positive integer, else
    no limit, as when it is unset or holds anything else"""
    raw = env.get("COLUMNS", "")
    if not (raw.isascii() and raw.isdigit()) or int(raw) == 0:
        return None
    return int(raw)


def render_plain(data: object, layout: Layout = NO_LAYOUT, width: int | None = None) -> str:
    """A top-level list of flat objects renders as a table, cut to ``width`` cells; any
    other list as a sequence of events, the way a stream prints them"""
    if isinstance(data, list):
        if not data and layout.rows:
            return NO_ROWS + "\n"
        table = render_table(data, layout, width)
        if table is not None:
            return table
        return "".join(render_event(item) for item in data)
    return "".join(_lines(data, ""))


def render_table(items: list[object], layout: Layout, width: int | None) -> str | None:
    """``items`` as aligned columns, or None unless every item is an object whose shown
    values are all scalars

    Columns follow ``layout.order``, then keys no field declares; a key missing from an
    item is an empty cell, as ``null`` is. Numbers are right-aligned. A line wider than
    ``width`` cuts its widest text column, the rightmost of equals, one cell at a time to
    no less than ``MIN_CUT``, ending a cut value in an ellipsis; a number is never cut, so
    a table of numbers can stay wider. Cell widths count East Asian wide characters as
    two cells and combining marks as none; grapheme clusters such as emoji joined by
    zero-width joiners may still misalign.
    """
    rows: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or is_binary(item):
            return None
        rows.append(item)
    seen = dict.fromkeys(k for row in rows for k in row if k not in layout.hidden)
    header = [k for k in layout.order if k in seen] + [k for k in seen if k not in layout.order]
    if not header or any(
        isinstance(row[k], (dict, list)) for row in rows for k in header if k in row
    ):
        return None
    cells = [[_scalar(row.get(k)) for k in header] for row in rows]
    numeric = [key in layout.numeric or _numbers([row.get(key) for row in rows]) for key in header]
    titles = [_text(key) for key in header]
    widths = [
        max(_width(titles[i]), *(_width(line[i]) for line in cells)) for i in range(len(header))
    ]
    if width is not None:
        _fit(widths, numeric, sum(widths) + len(GAP) * (len(widths) - 1) - width)
    return "".join(_row(line, widths, numeric) + "\n" for line in [titles, *cells])


def _fit(widths: list[int], numeric: list[bool], over: int) -> None:
    """Take ``over`` cells off the text columns, a cell at a time from the widest, the
    rightmost of equals, none below ``MIN_CUT``. Each step lowers every widest column to
    the next width down at once, so the work grows with the columns, not the cells cut"""
    text = [i for i, n in enumerate(numeric) if not n]
    while over > 0:
        top = max((widths[i] for i in text), default=MIN_CUT)
        if top <= MIN_CUT:
            return
        widest = [i for i in text if widths[i] == top]
        floor = max([MIN_CUT, *(widths[i] for i in text if widths[i] < top)])
        rounds, extra = divmod(min(over, (top - floor) * len(widest)), len(widest))
        for rank, i in enumerate(reversed(widest)):
            widths[i] = top - rounds - (1 if rank < extra else 0)
        over -= rounds * len(widest) + extra


def _numbers(values: list[object]) -> bool:
    """An untyped column of numbers: every value a number or null, and one a number"""
    present = [v for v in values if v is not None]
    return bool(present) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in present
    )


def _row(cells: list[str], widths: list[int], numeric: list[bool]) -> str:
    """One line, without the spaces that would trail it"""
    out: list[str] = []
    for cell, width, number in zip(cells, widths, numeric, strict=True):
        shown = cell if number else _cut(cell, width)
        pad = " " * (width - _width(shown))
        out.append(pad + shown if number else shown + pad)
    return GAP.join(out).rstrip(" ")


def _cut(text: str, width: int) -> str:
    """``text`` in at most ``width`` cells, ending in an ellipsis when it was longer"""
    if _width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        cells = _char_width(char)
        if used + cells > width - 1:
            break
        kept.append(char)
        used += cells
    return "".join(kept) + ELLIPSIS


def _width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _char_width(char: str) -> int:
    """Terminal cells: two for East Asian wide and fullwidth, none for a combining mark
    or a format character such as a zero-width space"""
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def render_event(data: object) -> str:
    """One stream event: its lines, then a blank line when it spans several items"""
    text = "".join(_lines(data, ""))
    if isinstance(data, (dict, list)) and text:
        return text + "\n"
    return text


def _lines(value: object, path: str) -> list[str]:
    if is_binary(value):
        assert isinstance(value, dict)
        # Both come from the data, so they lose their escapes as a value does
        kind = (
            f" {_text(strip_escapes(str(value['content_type'])))}"
            if "content_type" in value
            else ""
        )
        size = _text(strip_escapes(str(value["size_bytes"])))
        return [_line(path, f"<binary {size} bytes{kind}>")]
    if isinstance(value, dict):
        if not value:
            return [_line(path, "{}")] if path else []
        return [
            line
            for key, item in value.items()
            for line in _lines(item, f"{path}.{_text(str(key))}" if path else _text(str(key)))
        ]
    if isinstance(value, list):
        if not value:
            return [_line(path, "[]")] if path else []
        return [
            line
            for index, item in enumerate(value)
            for line in _lines(item, f"{path}.{index}" if path else str(index))
        ]
    if value is None and not path:
        return []
    return [_line(path, _scalar(value))]


def _line(path: str, text: str) -> str:
    if not path:
        return f"{text}\n"
    return f"{path}: {text}\n" if text else f"{path}:\n"


def _scalar(value: object) -> str:
    # bool before int: True is an int
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _text(strip_escapes(value))
    raise TypeError(f"plain output takes JSON values, not {type(value).__name__}")


def _text(value: str) -> str:
    """One line's worth: newline and tab as ``\\n`` and ``\\t``, other controls escaped"""
    return visible(value).translate(_ESCAPES)
