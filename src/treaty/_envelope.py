"""The response envelope written on every exit in JSON mode."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO

from ._errors import RegistrationError


def json_safe(value: object) -> object:
    """Error context echoes rejected input, which may be NaN or an int too long for
    ``str()``; both become text so the envelope stays valid JSON"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 1000:
        return f"an integer of {value.bit_length()} bits"
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    code: str
    message: str
    retryable: bool
    detail: str | None = None
    cause: str | None = None
    context: Mapping[str, object] = field(default_factory=dict)
    suggestion: str | None = None
    fix_command: str | None = None
    retry_after_ms: int | None = None
    fix_required: str | None = None
    phase: str | None = None
    errors: Sequence[Mapping[str, object]] | None = None
    """Every validation failure of the run (REQ-F-015); present on validation errors only"""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        if self.cause is not None:
            out["cause"] = self.cause
        if self.context:
            out["context"] = json_safe(dict(self.context))
        if self.suggestion is not None:
            out["suggestion"] = self.suggestion
        if self.fix_command is not None:
            out["fix_command"] = self.fix_command
        if self.retry_after_ms is not None:
            out["retry_after_ms"] = self.retry_after_ms
        if self.fix_required is not None:
            out["fix_required"] = self.fix_required
        if self.phase is not None:
            out["phase"] = self.phase
        if self.errors is not None:
            out["errors"] = [json_safe(dict(e)) for e in self.errors]
        return out


@dataclass(frozen=True, slots=True)
class WarningDetail:
    code: str
    message: str
    context: Mapping[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"code": self.code, "message": self.message}
        if self.context:
            out["context"] = dict(self.context)
        return out


@dataclass(frozen=True, slots=True)
class Envelope:
    exit_code: int
    data: object
    error: ErrorDetail | None
    duration_ms: int
    request_id: str
    warnings: Sequence[WarningDetail] = ()
    extra_meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.exit_code == 0) != (self.error is None):
            raise RegistrationError("envelope: error must be present exactly when exit_code != 0")
        if self.data is not None and not isinstance(self.data, (dict, list)):
            raise RegistrationError("envelope: data must be an object, array, or null")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_json(self) -> dict[str, object]:
        meta: dict[str, object] = {
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "request_id": self.request_id,
        }
        meta.update(self.extra_meta)
        return {
            "ok": self.ok,
            "data": self.data,
            "error": None if self.error is None else self.error.to_json(),
            "warnings": [w.to_json() for w in self.warnings],
            "meta": meta,
        }


def serialize(envelope: Envelope) -> str:
    return json.dumps(envelope.to_json(), separators=(",", ":"), sort_keys=True)


def write_envelope(envelope: Envelope, stream: IO[str]) -> None:
    stream.write(serialize(envelope))
    stream.write("\n")
    stream.flush()
