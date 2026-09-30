"""The plain fallback: flat lines when neither the command nor the app renders plain

Nested values become dotted paths (``release.tag: 1.3.9``), so every line is one item.
Input is the JSON-ready ``data`` of an envelope, after secret redaction. A value loses its
terminal escapes, as in the JSON envelope (REQ-F-007); a key, which is never rewritten, and
a value's other controls are shown as escapes.
"""

from __future__ import annotations

from ._envelope import strip_escapes, visible
from ._out import is_binary

_ESCAPES = str.maketrans({"\n": "\\n", "\t": "\\t"})


def render_plain(data: object) -> str:
    """A top-level array renders as a sequence of events, the way a stream prints them"""
    if isinstance(data, list):
        return "".join(render_event(item) for item in data)
    return "".join(_lines(data, ""))


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
