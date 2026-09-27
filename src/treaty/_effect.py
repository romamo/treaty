"""The ``effect`` contract for mutating and destructive commands (REQ-C-003, REQ-C-004).

Live runs report ``created``, ``updated``, ``deleted``, or ``noop``; dry runs
report a ``would_*`` preview such as ``would_delete``, and a destructive dry run
also says what it would change in ``would_affect``. Registration checks that the
output type can carry the fields, and every successful run is checked for values
that match its mode.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from dataclasses import dataclass
from typing import Any

from ._types import is_dataclass_type, strip_optional

LIVE_EFFECTS = frozenset({"created", "updated", "deleted", "noop"})
_PREVIEW_RE = re.compile(r"would_[a-z][a-z_]*")


@dataclass(frozen=True, slots=True)
class Affects:
    """What a destructive dry run would change: ``would_affect`` in its output (REQ-C-004)"""

    summary: str
    """One line for a person, such as: Rolls api back from 1.4.0 to 1.3.9"""
    resources: tuple[str, ...]
    """Identifiers of what would change, such as ``("service/api",)``"""
    count: int


def can_carry(output_type: object, name: str) -> bool:
    """A dataclass with the field ``name``, or a dict checked at run time"""
    base, _ = strip_optional(output_type)
    if is_dataclass_type(base):
        assert isinstance(base, type)
        return any(f.name == name for f in dataclasses.fields(base))
    return base is dict or typing.get_origin(base) is dict


def with_replay_effect(schema: dict[str, Any]) -> dict[str, Any]:
    """An idempotent replay reports ``effect: "noop"``, so a closed ``effect`` enum in the
    output schema must admit it, or the replay breaks the schema agents validate against"""
    if "anyOf" in schema:
        return {**schema, "anyOf": [with_replay_effect(s) for s in schema["anyOf"]]}
    effect = schema.get("properties", {}).get("effect")
    if not isinstance(effect, dict):
        return schema
    properties = {**schema["properties"], "effect": _admit_noop(effect)}
    return {**schema, "properties": properties}


def _admit_noop(effect: dict[str, Any]) -> dict[str, Any]:
    """``Literal[...]`` is an enum; ``Literal[...] | None`` wraps it in anyOf"""
    if "anyOf" in effect:
        return {**effect, "anyOf": [_admit_noop(s) for s in effect["anyOf"]]}
    if "enum" not in effect or "noop" in effect["enum"]:
        return effect
    return {**effect, "enum": [*effect["enum"], "noop"]}


def effect_problem(data: object, preview: bool, *, destructive: bool = False) -> str | None:
    """Why ``data`` breaks the contract, or ``None`` when it holds"""
    effect = data.get("effect") if isinstance(data, dict) else None
    if not isinstance(effect, str):
        return "response data has no string 'effect' field"
    if preview and not _PREVIEW_RE.fullmatch(effect):
        return f"dry run reported effect {effect!r}; previews use a would_* value"
    if not preview and effect not in LIVE_EFFECTS:
        return f"effect {effect!r} is not one of {', '.join(sorted(LIVE_EFFECTS))}"
    if preview and destructive and affects_summary(data) is None:
        return "destructive dry run has no would_affect with a summary; return treaty.Affects"
    return None


def affects_summary(data: object) -> str | None:
    """``would_affect.summary`` of a response's data, when it has one"""
    affects = data.get("would_affect") if isinstance(data, dict) else None
    summary = affects.get("summary") if isinstance(affects, dict) else None
    return summary if isinstance(summary, str) and summary else None
