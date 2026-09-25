"""JSON Schema draft-07 generation and JSON serialization, both dependency-free.

Supports the subset the framework needs: scalars, ``Literal`` and ``Enum``
strings, ``X | None``, ``list[T]``, ``tuple[T, ...]``, ``dict[str, T]``,
``object`` for unconstrained values, and nested dataclasses.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from enum import Enum
from pathlib import Path
from typing import Any

from ._errors import SchemaError
from ._types import is_dataclass_type, strip_optional

JsonSchema = dict[str, Any]


def schema_for(tp: object) -> JsonSchema:
    """Return a draft-07 schema fragment for a supported annotation"""
    if tp is object or tp is Any:
        return {}
    if tp is types.NoneType:
        return {"type": "null"}
    base, optional = strip_optional(tp)
    schema = _schema_for_base(base)
    if optional:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _schema_for_base(base: object) -> JsonSchema:
    origin = typing.get_origin(base)
    if origin is typing.Literal:
        values = typing.get_args(base)
        if not all(isinstance(v, str) for v in values):
            raise SchemaError(f"Literal values must all be strings: {base!r}")
        return {"type": "string", "enum": list(values)}
    if origin in (list, tuple):
        args = typing.get_args(base)
        item = args[0] if args else object
        return {"type": "array", "items": schema_for(item)}
    if origin is dict:
        key, value = typing.get_args(base)
        if key is not str:
            raise SchemaError(f"dict keys must be str: {base!r}")
        return {"type": "object", "additionalProperties": schema_for(value)}
    if isinstance(base, type):
        if base is bool:
            return {"type": "boolean"}
        if base is int:
            return {"type": "integer"}
        if base is float:
            return {"type": "number"}
        if base is str or base is Path:
            return {"type": "string"}
        if issubclass(base, Enum):
            return {"type": "string", "enum": [m.value for m in base]}
        if dataclasses.is_dataclass(base):
            return _dataclass_schema(base)
    raise SchemaError(f"unsupported annotation {base!r}")


def _dataclass_schema(cls: type) -> JsonSchema:
    hints = typing.get_type_hints(cls)
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        properties[f.name] = schema_for(hints[f.name])
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


def to_jsonable(value: object) -> object:
    """Convert handler output into plain JSON types; fails on anything else"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaError(f"dict keys must be str, got {type(k).__name__}")
            out[k] = to_jsonable(v)
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    raise SchemaError(f"cannot serialize {type(value).__name__} to JSON")
