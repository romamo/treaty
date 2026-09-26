"""JSON Schema draft-07 generation and JSON serialization, both dependency-free.

Supports the subset the framework needs: scalars, ``Literal`` strings, string or
integer ``Enum``, ``X | None``, ``list[T]``, ``tuple[T, ...]``, fixed ``tuple[A, B]``,
``dict[str, T]``,
``object`` for unconstrained values, and nested dataclasses.
"""

from __future__ import annotations

import contextvars
import dataclasses
import math
import types
import typing
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from ._errors import SchemaError
from ._scalars import ScalarRegistry
from ._types import is_dataclass_type, strip_optional

JsonSchema = dict[str, Any]


def schema_for(tp: object, scalars: ScalarRegistry) -> JsonSchema:
    """Return a draft-07 schema fragment for a supported annotation"""
    if tp is object or tp is Any:
        return {}
    if tp is types.NoneType:
        return {"type": "null"}
    base, optional = strip_optional(tp)
    schema = _schema_for_base(base, scalars)
    if optional:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _schema_for_base(base: object, scalars: ScalarRegistry) -> JsonSchema:
    origin = typing.get_origin(base)
    if origin is typing.Literal:
        values = typing.get_args(base)
        if not all(isinstance(v, str) for v in values):
            raise SchemaError(f"Literal values must all be strings: {base!r}")
        return {"type": "string", "enum": list(values)}
    if origin is tuple and (args := typing.get_args(base)) and args[-1] is not Ellipsis:
        # A fixed-length tuple: one schema per position (draft-07 tuple validation)
        return {
            "type": "array",
            "items": [schema_for(a, scalars) for a in args],
            "additionalItems": False,
            "minItems": len(args),
            "maxItems": len(args),
        }
    if origin in (list, tuple):
        args = typing.get_args(base)
        item = args[0] if args else object
        return {"type": "array", "items": schema_for(item, scalars)}
    if origin is dict:
        key, value = typing.get_args(base)
        if key is not str:
            raise SchemaError(f"dict keys must be str: {base!r}")
        return {"type": "object", "additionalProperties": schema_for(value, scalars)}
    if isinstance(base, type):
        if base is bool:
            return {"type": "boolean"}
        if base is int:
            return {"type": "integer"}
        if base is float:
            return {"type": "number"}
        if base is str or base is Path:
            return {"type": "string"}
        if (spec := scalars.get(base)) is not None:
            return spec.json_schema()
        if issubclass(base, Enum):
            return _enum_schema(base)
        if dataclasses.is_dataclass(base):
            return _dataclass_schema(base, scalars)
    raise SchemaError(f"unsupported annotation {base!r}; register a class with app.scalar(...)")


def _enum_schema(cls: type[Enum]) -> JsonSchema:
    """The JSON type of the members' values, which is what ``to_jsonable`` emits"""
    if issubclass(cls, Flag):
        # Members combine (READ | WRITE), so any non-negative integer can be emitted
        return {"type": "integer", "minimum": 0}
    values = [m.value for m in cls]
    if all(isinstance(v, str) for v in values):
        return {"type": "string", "enum": values}
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return {"type": "integer", "enum": values}
    raise SchemaError(f"{cls.__qualname__}: enum values must be all strings or all integers")


_BUILDING: contextvars.ContextVar[frozenset[type]] = contextvars.ContextVar(
    "treaty_schema_building", default=frozenset()
)


def _dataclass_schema(cls: type, scalars: ScalarRegistry) -> JsonSchema:
    building = _BUILDING.get()
    if cls in building:
        raise SchemaError(f"{cls.__qualname__} refers to itself; recursive outputs have no schema")
    token = _BUILDING.set(building | {cls})
    try:
        return _dataclass_fields_schema(cls, scalars)
    finally:
        _BUILDING.reset(token)


def _dataclass_fields_schema(cls: type, scalars: ScalarRegistry) -> JsonSchema:
    hints = typing.get_type_hints(cls)
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        properties[f.name] = schema_for(hints[f.name], scalars)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
    schema: JsonSchema = {
        "type": "object",
        "title": cls.__name__,
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def is_payload_type(tp: object) -> bool:
    """True when values of this type serialize to a JSON object, array, or null"""
    base, _ = strip_optional(tp)
    if base is types.NoneType:
        return True
    origin = typing.get_origin(base)
    return origin in (list, tuple, dict) or is_dataclass_type(base)


def to_jsonable(value: object, scalars: ScalarRegistry) -> object:
    """Convert handler output into plain JSON types; fails on anything else"""
    # A registered scalar first: a str or int subclass has its own serialize=
    if (spec := scalars.for_value(value)) is not None:
        return to_jsonable(spec.serialize(value), scalars)
    if isinstance(value, float) and not math.isfinite(value):
        raise SchemaError(f"{value!r} is not a finite number, and JSON has no NaN or Infinity")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 10_000:
        try:
            str(value)
        except ValueError:
            raise SchemaError("an integer too long to write as JSON") from None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, scalars) for v in value]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaError(f"dict keys must be str, got {type(k).__name__}")
            out[k] = to_jsonable(v, scalars)
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name), scalars) for f in dataclasses.fields(value)
        }
    raise SchemaError(
        f"cannot serialize {type(value).__name__} to JSON; register it with app.scalar(...)"
    )
