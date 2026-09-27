"""JSON Schema draft-07 generation and JSON serialization, both dependency-free.

Supports the subset the framework needs: scalars, ``Literal`` strings, string or
integer ``Enum``, ``X | None``, ``list[T]``, ``tuple[T, ...]``, fixed ``tuple[A, B]``,
``dict[str, T]``,
``object`` for unconstrained values, nested dataclasses, ``datetime``, ``date``,
``time``, and ``Decimal`` as strings, and ``bytes`` and ``Binary`` as a base64 wrapper.

An output schema (``output=True``) lists every key of a dataclass as required, since
every key is always written, and refuses a nullable collection: empty is ``[]`` or ``{}``,
never ``null`` (REQ-F-074).
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import datetime as dt
import math
import types
import typing
from decimal import Decimal
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from ._errors import RegistrationError, SchemaError
from ._out import Binary, out_spec
from ._scalars import ScalarRegistry
from ._types import is_dataclass_type, resolve_alias, strip_optional

JsonSchema = dict[str, Any]


def schema_for(tp: object, scalars: ScalarRegistry, *, output: bool = False) -> JsonSchema:
    """Return a draft-07 schema fragment for a supported annotation; ``output`` for what a
    handler returns, rather than what a command accepts"""
    tp = resolve_alias(tp)
    if tp is object or tp is Any:
        return {}
    if tp is types.NoneType:
        return {"type": "null"}
    base, optional = strip_optional(tp)
    if output and optional and typing.get_origin(base) in (list, tuple, dict):
        _nullable_collection(repr(tp), base)
    schema = _schema_for_base(base, scalars, output)
    if optional:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _nullable_collection(where: str, base: object) -> typing.NoReturn:
    empty = "dict" if typing.get_origin(base) is dict else "list"
    raise RegistrationError(
        f"{where}: an output collection is never null, since empty is [] or {{}}; drop "
        f"| None and write {base!r} = treaty.Out(default_factory={empty}) (REQ-F-074)"
    )


BINARY_SCHEMA: JsonSchema = {
    "type": "object",
    "title": "Binary",
    "properties": {
        "type": {"type": "string", "enum": ["binary"]},
        "encoding": {"type": "string", "enum": ["base64"]},
        "value": {"type": "string", "contentEncoding": "base64"},
        "size_bytes": {"type": "integer", "minimum": 0},
        "content_type": {"type": "string"},
    },
    "required": ["type", "encoding", "value", "size_bytes"],
    "additionalProperties": False,
}
"""REQ-F-017: how ``bytes`` and ``treaty.Binary`` values are written"""


def _schema_for_base(base: object, scalars: ScalarRegistry, output: bool) -> JsonSchema:
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
            "items": [schema_for(a, scalars, output=output) for a in args],
            "additionalItems": False,
            "minItems": len(args),
            "maxItems": len(args),
        }
    if origin in (list, tuple):
        args = typing.get_args(base)
        item = args[0] if args else object
        return {"type": "array", "items": schema_for(item, scalars, output=output)}
    if origin is dict:
        key, value = typing.get_args(base)
        if key is not str:
            raise SchemaError(f"dict keys must be str: {base!r}")
        return {"type": "object", "additionalProperties": schema_for(value, scalars, output=output)}
    if base is bytes or base is Binary:
        return dict(BINARY_SCHEMA)
    if isinstance(base, type):
        if base is bool:
            return {"type": "boolean"}
        if base is int:
            return {"type": "integer"}
        if base is float:
            return {"type": "number"}
        if base is str or base is Path:
            return {"type": "string"}
        if base in _TEMPORAL:
            return {"type": "string", "format": _TEMPORAL[base]}
        if base is Decimal:
            return {"type": "string", "pattern": DECIMAL_PATTERN}
        if (spec := scalars.get(base)) is not None:
            return spec.json_schema()
        if issubclass(base, Enum):
            return _enum_schema(base)
        if dataclasses.is_dataclass(base):
            return _dataclass_schema(base, scalars, output)
    raise SchemaError(f"unsupported annotation {base!r}; register a class with app.scalar(...)")


# REQ-F-005: dates and times travel as ISO 8601 text, keyed by exact class
_TEMPORAL: dict[object, str] = {dt.datetime: "date-time", dt.date: "date", dt.time: "time"}
# A Decimal is written as fixed-point text, so no float rounding touches it
DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"


def _temporal(value: dt.date | dt.time) -> str:
    """ISO 8601, with ``Z`` for UTC; a naive datetime names no instant, so it is refused"""
    if isinstance(value, dt.datetime) and value.utcoffset() is None:
        raise SchemaError(
            f"a naive datetime {value.isoformat()}; give it a tzinfo, such as datetime.UTC"
        )
    text = value.isoformat()
    return text.removesuffix("+00:00") + "Z" if text.endswith("+00:00") else text


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise SchemaError(f"{value!r} is not a finite number, and JSON has no NaN or Infinity")
    return format(value, "f")


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


def _dataclass_schema(cls: type, scalars: ScalarRegistry, output: bool) -> JsonSchema:
    building = _BUILDING.get()
    if cls in building:
        raise SchemaError(f"{cls.__qualname__} refers to itself; recursive outputs have no schema")
    token = _BUILDING.set(building | {cls})
    try:
        return _dataclass_fields_schema(cls, scalars, output)
    finally:
        _BUILDING.reset(token)


def _dataclass_fields_schema(cls: type, scalars: ScalarRegistry, output: bool) -> JsonSchema:
    """Input: a field with a default may be left out. Output: every key is written, so
    each is required, but a ``volatile`` one, which ``--stable-output`` leaves out"""
    hints = typing.get_type_hints(cls)
    properties: dict[str, JsonSchema] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        base, optional = strip_optional(hints[f.name])
        if output and optional and typing.get_origin(base) in (list, tuple, dict):
            _nullable_collection(f"{cls.__qualname__}.{f.name}", base)
        prop = schema_for(hints[f.name], scalars, output=output)
        spec = out_spec(f)
        if output and spec.ordered:
            prop = {**prop, "x-ordered": True}
        if output and spec.volatile:
            prop = {**prop, "x-volatile": True}
        properties[f.name] = prop
        if output and not spec.volatile:
            required.append(f.name)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
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


def binary_json(data: bytes, content_type: str | None = None) -> dict[str, object]:
    """REQ-F-017: bytes as a wrapper object whose ``value`` decodes back to them"""
    out: dict[str, object] = {
        "type": "binary",
        "encoding": "base64",
        "value": base64.b64encode(data).decode("ascii"),
        "size_bytes": len(data),
    }
    if content_type is not None:
        out["content_type"] = content_type
    return out


def to_jsonable(value: object, scalars: ScalarRegistry, *, base: Path) -> object:
    """Convert handler output into plain JSON types; fails on anything else. A relative
    ``Path`` is joined to ``base``, the run's working directory (REQ-F-040)"""
    # A registered scalar first: a str or int subclass has its own serialize=
    if (spec := scalars.for_value(value)) is not None:
        return to_jsonable(spec.serialize(value), scalars, base=base)
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
        return str(value if value.is_absolute() else base / value)
    if isinstance(value, bytes):
        return binary_json(value)
    if isinstance(value, Binary):
        return binary_json(value.data, value.content_type)
    if isinstance(value, (dt.date, dt.time)):
        return _temporal(value)
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, scalars, base=base) for v in value]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaError(f"dict keys must be str, got {type(k).__name__}")
            out[k] = to_jsonable(v, scalars, base=base)
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name), scalars, base=base)
            for f in dataclasses.fields(value)
        }
    raise SchemaError(
        f"cannot serialize {type(value).__name__} to JSON; register it with app.scalar(...)"
    )
