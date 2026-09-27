"""The response envelope written on every exit in JSON mode."""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import IO

from ._errors import RegistrationError

# Deeper than this, echoed input is cut: an agent needs the shape, not 1,000 brackets
_MAX_DEPTH = 32


def json_safe(value: object, depth: int = 0) -> object:
    """Error context is diagnostic and holds whatever a parser or handler put there:
    rejected input (NaN, an int too long for ``str()``, deep nesting) or objects such
    as a Decimal or Path. Everything becomes JSON; unknown objects become their text."""
    if depth > _MAX_DEPTH:
        return "..."
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, int):
        if value.bit_length() > 1000:
            return f"an integer of {value.bit_length()} bits"
        return int(value)  # an IntEnum or other subclass as a plain number
    if isinstance(value, Enum):
        return json_safe(value.value, depth + 1)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v, depth + 1) for v in value]
    return str(value)


# REQ-F-007: CSI (colors, cursor movement), OSC (titles, links), other two-byte escapes,
# and a stray ESC
_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]?")
# REQ-F-016: a null byte or a lone surrogate is not valid UTF-8 text
_INVALID = re.compile(r"[\x00\ud800-\udfff]")


def clean(value: object) -> object:
    """Every string value of a JSON value without terminal escapes, and valid UTF-8 once
    encoded: whatever a handler or a library returned, the envelope stays plain text. Keys
    are left alone, so two keys never collapse into one"""
    if isinstance(value, str):
        return _INVALID.sub("\ufffd", _ESCAPES.sub("", value))
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def sentence(text: str) -> str:
    """An error message as a complete sentence (REQ-C-013): a lowercase first letter is
    capitalized and closing punctuation is added when missing. Escapes go first, so a
    colored message is judged by its text."""
    text = _ESCAPES.sub("", text).strip()
    if text[:1].islower():
        text = text[0].upper() + text[1:]
    if text and text[-1] not in ".!?":
        text += "."
    return text


_RETRY = "retry the same command; it had no side effects"


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
    alternatives: Sequence[Mapping[str, str]] | None = None
    """Flags that replace an editor the run could not open (REQ-F-055)"""
    hint: str | None = None
    """The flag that avoids this failure, such as ``--input-file`` (REQ-F-054)"""
    auth_methods: Sequence[Mapping[str, str]] | None = None
    """Ways to log in without a browser, such as a token variable (REQ-O-033)"""
    retries_exhausted: int | None = None
    """Retries ``ctx.retry`` made before giving up (REQ-F-078)"""

    def __post_init__(self) -> None:
        # One place, so framework and author messages alike read as sentences (REQ-C-013)
        object.__setattr__(self, "message", sentence(self.message))
        if self.errors is not None:
            items = [
                {**e, "message": sentence(str(e["message"]))} if "message" in e else e
                for e in self.errors
            ]
            object.__setattr__(self, "errors", tuple(items))
        if self.suggestion is None and (self.retryable or self.fix_required is not None):
            # A recoverable error always names its next step
            object.__setattr__(self, "suggestion", self.fix_required or _RETRY)

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
        if self.retries_exhausted is not None:
            out["retries_exhausted"] = self.retries_exhausted
        if self.hint is not None:
            out["hint"] = self.hint
        if self.phase is not None:
            out["phase"] = self.phase
        if self.errors is not None:
            out["errors"] = [json_safe(dict(e)) for e in self.errors]
        if self.alternatives is not None:
            out["alternatives"] = [dict(a) for a in self.alternatives]
        if self.auth_methods is not None:
            out["auth_methods"] = [dict(a) for a in self.auth_methods]
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


ENVELOPE_SCHEMA_VERSION = "1.0"
"""``meta.schema_version`` of a response no command answered, such as an unknown command"""


@dataclass(frozen=True, slots=True)
class Meta:
    """The framework's ``meta`` keys: volatile by definition, so ``data`` stays safe to
    cache and diff (REQ-F-021). Optional keys are absent, never null."""

    duration_ms: int
    request_id: str | None
    """None under ``--stable-output``, which leaves it out (REQ-O-007)"""
    command: str
    """The command's path as the manifest keys it, or the app name when none resolved"""
    timestamp: str | None
    """ISO 8601 UTC time the invocation started; None under ``--stable-output``"""
    schema_version: str
    """``MAJOR.MINOR`` of the command's output contract, or of the one pinned"""
    tool_version: str
    cwd: str
    """The working directory, as ``pwd`` prints it"""
    trace_id: str | None = None
    """``TOOL_TRACE_ID``, when set"""
    project_root: str | None = None
    """The directory holding a ``project_root=`` marker, when one was found"""
    retries: int = 0
    """Retries ``ctx.retry`` made; 0 is left out"""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "duration_ms": self.duration_ms,
            "command": self.command,
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "cwd": self.cwd,
        }
        if self.request_id is not None:
            out["request_id"] = self.request_id
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        if self.trace_id is not None:
            out["trace_id"] = self.trace_id
        if self.project_root is not None:
            out["project_root"] = self.project_root
        if self.retries:
            out["retries"] = self.retries
        return out


@dataclass(frozen=True, slots=True)
class Envelope:
    exit_code: int
    data: object
    error: ErrorDetail | None
    meta: Meta
    warnings: Sequence[WarningDetail] = ()
    extra_meta: Mapping[str, object] = field(default_factory=dict)
    """Keys a response adds besides the framework's, such as ``pagination``"""

    def __post_init__(self) -> None:
        if (self.exit_code == 0) != (self.error is None):
            raise RegistrationError("envelope: error must be present exactly when exit_code != 0")
        if self.data is not None and not isinstance(self.data, (dict, list)):
            raise RegistrationError("envelope: data must be an object, array, or null")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def cleaned(self) -> Envelope:
        """``data`` as ``clean`` leaves it; the rest is cleaned by ``to_json``"""
        return dataclasses.replace(self, data=clean(self.data))

    def to_json(self) -> dict[str, object]:
        meta: dict[str, object] = {"exit_code": self.exit_code, **self.meta.to_json()}
        meta.update(self.extra_meta)
        return {
            "ok": self.ok,
            # Cleaned once in cleaned(), before the byte cap serializes it again and again
            "data": self.data,
            "error": None if self.error is None else clean(self.error.to_json()),
            "warnings": clean([w.to_json() for w in self.warnings]),
            "meta": clean(meta),
        }


def serialize(envelope: Envelope) -> str:
    return json.dumps(envelope.to_json(), separators=(",", ":"), sort_keys=True)


def write_envelope(envelope: Envelope, stream: IO[str]) -> None:
    stream.write(serialize(envelope))
    stream.write("\n")
    stream.flush()
