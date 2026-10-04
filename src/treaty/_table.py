"""Delimited text for ``--format tsv`` and ``--format csv``: a header row, then one row
per item (REQ-O-001)."""

from __future__ import annotations

import csv
import io
import json

from ._command import Renderer
from ._envelope import strip_escapes, visible


def table(delimiter: str) -> Renderer:
    """A renderer writing ``data`` as rows under a header: a list of objects is one row
    per object, an object is one row, and any other item is a ``value`` column. Nested
    values are compact JSON; ``null`` is an empty cell. A tab delimiter writes TSV, which
    has no quoting: a backslash, tab, CR, or LF in a cell is written as a backslash
    escape, as in a Python string literal. Any other delimiter quotes like the ``csv``
    module. A cell loses its terminal escapes, as in the JSON envelope (REQ-F-007), and
    shows any other control as its escape, a header too. ``tsv`` is built in; offer CSV
    with ``app.format(Format.CSV, render=table(","))``. On an ``external=True`` command
    the trust tags are no columns: the ``UNTRUSTED_CONTENT`` warning on stderr says the
    content is untrusted instead (#336).
    """
    if len(delimiter) != 1:
        raise ValueError("a table delimiter is one character")
    return Table(delimiter)


class Table:
    """The renderer ``table`` returns, which treaty tells from an app's own: it hands a
    ``Table`` the data without the trust tags, and an app's renderer the data as the JSON
    envelope has it (#336)"""

    __slots__ = ("delimiter",)

    def __init__(self, delimiter: str) -> None:
        self.delimiter = delimiter

    def __repr__(self) -> str:
        return f"table({self.delimiter!r})"

    def __call__(self, data: object) -> str:
        delimiter = self.delimiter
        items = data if isinstance(data, list) else [data]
        rows = [item if isinstance(item, dict) else {"value": item} for item in items]
        header = list(dict.fromkeys(key for row in rows for key in row))
        if not header:
            return ""
        if delimiter == "\t":
            lines = [header, *([_cell(row.get(key)) for key in header] for row in rows)]
            return "".join("\t".join(map(_escape, line)) + "\n" for line in lines)
        out = io.StringIO()
        writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
        writer.writerow([visible(key, _CSV_KEEP) for key in header])
        for row in rows:
            writer.writerow([visible(_cell(row.get(key)), _CSV_KEEP) for key in header])
        return out.getvalue()


_TSV_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})
# A quoted CSV cell may hold a CRLF line break
_CSV_KEEP = "\r"


def _escape(cell: str) -> str:
    # The backslashes first: the escapes visible() writes are not doubled
    return visible(cell.translate(_TSV_ESCAPES))


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return strip_escapes(str(value))
