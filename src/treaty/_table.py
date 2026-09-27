"""Delimited text for ``--format tsv`` and ``--format csv``: a header row, then one row
per item (REQ-O-001)."""

from __future__ import annotations

import csv
import io
import json

from ._command import Renderer


def table(delimiter: str) -> Renderer:
    """A renderer writing ``data`` as rows under a header: a list of objects is one row
    per object, an object is one row, and any other item is a ``value`` column. Nested
    values are compact JSON; ``null`` is an empty cell. ``tsv`` is built in; offer CSV
    with ``app.format(Format.CSV, render=table(","))``."""
    if len(delimiter) != 1:
        raise ValueError("a table delimiter is one character")

    def render(data: object) -> str:
        items = data if isinstance(data, list) else [data]
        rows = [item if isinstance(item, dict) else {"value": item} for item in items]
        header = list(dict.fromkeys(key for row in rows for key in row))
        if not header:
            return ""
        out = io.StringIO()
        writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow([_cell(row.get(key)) for key in header])
        return out.getvalue()

    return render


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)
