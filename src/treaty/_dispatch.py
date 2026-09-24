"""``DispatchRequest`` lines: route by ``_cmd`` and turn ``_opts`` into argv."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from ._errors import ParseError
from ._values import CommandPath, InvalidValue

Scalar = bool | str | int | float


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    path: CommandPath
    opts: Mapping[str, Scalar] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)

    def to_argv(self) -> list[str]:
        argv = list(self.path.parts)
        for key, value in self.opts.items():
            if value is True:
                argv.append(f"--{key}")
            elif value is False:
                argv.append(f"--no-{key}")
            else:
                argv.append(f"--{key}={value}")
        return argv


def parse_dispatch_line(line: str, line_no: int) -> DispatchRequest:
    ctx: dict[str, object] = {"line": line_no}
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
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
