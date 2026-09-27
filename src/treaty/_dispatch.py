"""``DispatchRequest`` lines: route by ``_cmd``; ``_opts`` and the payload become field values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._errors import ParseError
from ._json5 import Unreadable, loads_forgiving
from ._values import CommandPath, InvalidValue

Scalar = bool | str | int | float


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    path: CommandPath
    opts: Mapping[str, Scalar] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)


def invalid_json(what: str, exc: Unreadable, context: Mapping[str, object]) -> ParseError:
    """``INVALID_JSON`` (REQ-F-059), with ``corrected_input`` when a repair reads it"""
    ctx = {**context, "cause": str(exc)}
    if exc.position is not None:
        ctx["position"] = exc.position
    if exc.corrected is None:
        return ParseError(f"{what} is not valid JSON", code="INVALID_JSON", context=ctx)
    return ParseError(
        f"{what} could not be normalized to valid JSON",
        code="INVALID_JSON",
        context={**ctx, "corrected_input": exc.corrected},
        suggestion="reissue with the corrected_input value, if it says what you meant",
    )


def parse_dispatch_line(line: str, line_no: int) -> DispatchRequest:
    ctx: dict[str, object] = {"line": line_no}
    try:
        raw = loads_forgiving(line)
    except Unreadable as exc:
        raise invalid_json(f"line {line_no}", exc, ctx) from None
    except ValueError as exc:
        raise ParseError(
            f"line {line_no}: invalid JSON", context={**ctx, "cause": str(exc)}
        ) from None
    if not isinstance(raw, dict):
        raise ParseError(f"line {line_no}: expected a JSON object", context=ctx)
    cmd = raw.pop("_cmd", None)
    if not isinstance(cmd, str):
        raise ParseError(f"line {line_no}: _cmd must be a string", context=ctx)
    try:
        path = CommandPath(cmd)
    except InvalidValue as exc:
        raise ParseError(f"line {line_no}: {exc}", context={**ctx, "_cmd": cmd}) from None
    opts_raw = raw.pop("_opts", {})
    if not isinstance(opts_raw, dict):
        raise ParseError(f"line {line_no}: _opts must be an object", context=ctx)
    opts: dict[str, Scalar] = {}
    for key, value in opts_raw.items():
        if not isinstance(value, (bool, str, int, float)):
            raise ParseError(
                f"line {line_no}: _opts.{key} must be a scalar", context={**ctx, "opt": key}
            )
        opts[str(key)] = value
    return DispatchRequest(path=path, opts=opts, payload=raw)
