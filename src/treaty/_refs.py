"""``$defs`` and ``$ref`` in output schemas: only a type that holds itself has them.

A recursive output type, such as a tree node whose ``children`` are nodes, has no finite
inlined schema. Its schema is inlined where it first appears, as every other type's is,
and each place it holds itself is ``{"$ref": "#/$defs/<Name>"}``, with the definition in
``$defs`` at the root of the command's output schema. A schema without recursion has no
``$defs``, so it stays as it was, byte for byte.

A walk that follows a schema follows ``$ref`` too: one led by a value (masking, ordering,
fitting) ends with the value, and one led by the schema alone stops at a definition it is
already inside.
"""

from __future__ import annotations

import types
from collections.abc import Iterable, Mapping
from typing import Any

from ._errors import RegistrationError

JsonSchema = dict[str, Any]

DEFS_KEY = "$defs"
DEFS_PREFIX = "#/$defs/"


def ref_name(node: Mapping[str, Any]) -> str | None:
    """The definition a ``{"$ref": "#/$defs/<Name>"}`` node names, or None"""
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith(DEFS_PREFIX):
        return ref.removeprefix(DEFS_PREFIX)
    return None


def defs_of(schema: Mapping[str, Any]) -> Mapping[str, JsonSchema]:
    """The ``$defs`` at the root of an output schema; empty without recursion"""
    defs = schema.get(DEFS_KEY)
    return defs if isinstance(defs, dict) else {}


def deref(node: Mapping[str, Any], defs: Mapping[str, JsonSchema]) -> JsonSchema:
    """``node`` with a ``$ref`` replaced by the definition it names, its own keys, such as
    ``x-ordered``, kept beside; any other node as it is"""
    name = ref_name(node)
    if name is None:
        return node if isinstance(node, dict) else dict(node)
    target = defs.get(name)
    if target is None:
        raise RegistrationError(f"$ref {node['$ref']!r} names no definition in $defs")
    siblings = {k: v for k, v in node.items() if k != "$ref"}
    return {**target, **siblings} if siblings else target


def rename_refs(node: object, renames: Mapping[str, str]) -> Any:
    """A copy of ``node`` with every ``$ref`` to a renamed definition pointing at its new
    name"""
    if isinstance(node, list):
        return [rename_refs(n, renames) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: rename_refs(v, renames) for k, v in node.items()}
    name = ref_name(node)
    if name is not None and name in renames:
        out["$ref"] = DEFS_PREFIX + renames[name]
    return out


def merge_defs(
    schemas: Iterable[JsonSchema],
) -> tuple[list[JsonSchema], dict[str, JsonSchema]]:
    """Each schema without its ``$defs``, and their ``$defs`` merged to go on one root
    around them all: a name two schemas define differently is renamed in the later one"""
    merged: dict[str, JsonSchema] = {}
    out: list[JsonSchema] = []
    for schema in schemas:
        defs = defs_of(schema)
        body = {k: v for k, v in schema.items() if k != DEFS_KEY}
        if not defs:
            out.append(body)
            continue
        renames: dict[str, str] = {}
        for name, definition in defs.items():
            if merged.get(name, definition) != definition:
                renames[name] = free_name(name, merged.keys() | defs.keys())
        for name, definition in defs.items():
            merged[renames.get(name, name)] = rename_refs(definition, renames)
        out.append(rename_refs(body, renames))
    return out, merged


def free_name(name: str, taken: Iterable[str]) -> str:
    """``name`` with the first number suffix no definition has: ``Node_2``, ``Node_3``"""
    used = set(taken)
    n = 2
    while f"{name}_{n}" in used:
        n += 1
    return f"{name}_{n}"


def with_defs(schema: JsonSchema, defs: Mapping[str, JsonSchema]) -> JsonSchema:
    """``schema`` with ``defs`` at its root, sorted by name; unchanged without any"""
    if not defs:
        return schema
    return {**schema, DEFS_KEY: {name: defs[name] for name in sorted(defs)}}


EMPTY_DEFS: Mapping[str, JsonSchema] = types.MappingProxyType({})
"""The ``$defs`` of a schema without recursion"""
