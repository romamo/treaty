"""The ``effect`` contract for mutating and destructive commands (REQ-C-003, REQ-C-004).

Live runs report ``created``, ``updated``, ``deleted``, or ``noop``; dry runs
report a ``would_*`` preview such as ``would_delete``. Registration checks
that the output type can carry the field, and every successful run is checked
for a value that matches its mode.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from typing import Any

from ._types import is_dataclass_type, strip_optional

LIVE_EFFECTS = frozenset({"created", "updated", "deleted", "noop"})
_PREVIEW_RE = re.compile(r"would_[a-z][a-z_]*")


def can_carry_effect(output_type: object) -> bool:
    """A dataclass with an ``effect`` field, or a dict checked at run time"""
    base, _ = strip_optional(output_type)
    if is_dataclass_type(base):
        assert isinstance(base, type)
        return any(f.name == "effect" for f in dataclasses.fields(base))
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


def effect_problem(data: object, preview: bool) -> str | None:
    """Why ``data`` breaks the contract, or ``None`` when it holds"""
    effect = data.get("effect") if isinstance(data, dict) else None
    if not isinstance(effect, str):
        return "response data has no string 'effect' field"
    if preview and not _PREVIEW_RE.fullmatch(effect):
        return f"dry run reported effect {effect!r}; previews use a would_* value"
    if not preview and effect not in LIVE_EFFECTS:
        return f"effect {effect!r} is not one of {', '.join(sorted(LIVE_EFFECTS))}"
    return None
