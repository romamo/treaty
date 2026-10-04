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
from typing import TYPE_CHECKING, Any

from ._types import is_dataclass_type, strip_optional, union_members

if TYPE_CHECKING:
    from ._adapters import OutputAdapters

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


def can_carry(output_type: object, name: str, adapters: OutputAdapters | None = None) -> bool:
    """A dataclass with the field ``name``, a class an output adapter writes whose schema
    requires the key ``name``, or a dict checked at run time"""
    base, _ = strip_optional(output_type)
    if members := union_members(base):
        return all(can_carry(m, name, adapters) for m in members)
    if is_dataclass_type(base):
        assert isinstance(base, type)
        return any(f.name == name for f in dataclasses.fields(base))
    node = _adapted_node(base, adapters)
    if node is not None:
        return name in node.get("required", ())
    return base is dict or typing.get_origin(base) is dict


def lists_unrequired(output_type: object, name: str, adapters: OutputAdapters | None) -> bool:
    """An adapted class whose schema lists ``name`` but does not require it: an
    ``x-volatile`` key, which ``--stable-output`` leaves out, so ``data`` may lack it"""
    base, _ = strip_optional(output_type)
    if members := union_members(base):
        return any(lists_unrequired(m, name, adapters) for m in members)
    node = _adapted_node(base, adapters)
    if node is None:
        return False
    properties = node.get("properties")
    listed = isinstance(properties, dict) and name in properties
    return listed and name not in node.get("required", ())


def _adapted_node(base: object, adapters: OutputAdapters | None) -> dict[str, Any] | None:
    """The normalized schema of ``base`` when an output adapter writes it"""
    if adapters is None or adapters.for_type(base) is None:
        return None
    assert isinstance(base, type)
    return adapters.node(base)


def with_replay_effect(schema: dict[str, Any]) -> dict[str, Any]:
    """An idempotent replay reports ``effect: "noop"``, so a closed ``effect`` enum in the
    output schema must admit it, or the replay breaks the schema agents validate against"""
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return {**schema, key: [with_replay_effect(s) for s in schema[key]]}
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


def is_preview(data: object) -> bool:
    """``data`` reports a ``would_*`` effect: a dry run that applied nothing"""
    effect = data.get("effect") if isinstance(data, dict) else None
    return isinstance(effect, str) and _PREVIEW_RE.fullmatch(effect) is not None


def affects_summary(data: object) -> str | None:
    """``would_affect.summary`` of a response's data, when it has one"""
    affects = data.get("would_affect") if isinstance(data, dict) else None
    summary = affects.get("summary") if isinstance(affects, dict) else None
    return summary if isinstance(summary, str) and summary else None
