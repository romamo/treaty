"""JSON Schema draft-07 generation and JSON serialization, both dependency-free.

Supports the subset the framework needs: scalars, string or integer ``Literal``, string or
integer ``Enum``, ``X | None``, ``list[T]``, ``tuple[T, ...]``, fixed ``tuple[A, B]``,
``dict[str, T]``,
``object`` for unconstrained values, nested dataclasses, ``datetime``, ``date``,
``time``, and ``Decimal`` as strings, and ``bytes`` and ``Binary`` as a base64 wrapper.

An output schema (``output=True``) lists every key of a dataclass as required, since
every key is always written, and refuses a nullable collection: empty is ``[]`` or ``{}``,
never ``null`` (REQ-F-074). A dataclass that holds itself, such as a tree node, is a
``$ref`` to its ``$defs`` entry wherever it does (see ``_refs``); an argument type cannot.
"""

from __future__ import annotations

import base64
import contextvars
import dataclasses
import datetime as dt
import json
import math
import re
import types
import typing
from collections.abc import Sequence
from decimal import Decimal
from enum import Enum, Flag
from pathlib import Path
from typing import Any

from ._errors import RegistrationError, SchemaError
from ._flags import FLAG_META
from ._out import Binary, External, out_spec
from ._redact import secret_field
from ._refs import DEFS_KEY, DEFS_PREFIX, defs_of, free_name, rename_refs, with_defs
from ._scalars import DATE_TEXT, DATETIME_TEXT, DECIMAL_TEXT, ScalarRegistry
from ._types import is_dataclass_type, literal_kind, resolve_alias, strip_optional, type_hints

JsonSchema = dict[str, Any]


def schema_for(tp: object, scalars: ScalarRegistry, *, output: bool = False) -> JsonSchema:
    """Return a draft-07 schema fragment for a supported annotation; ``output`` for what a
    handler returns, rather than what a command accepts"""
    if output and _DEFS.get() is None:
        return _output_root(tp, scalars)
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
        kind = "integer" if literal_kind(base) is int else "string"
        return {"type": kind, "enum": list(typing.get_args(base))}
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
        value = resolve_alias(value)
        if typing.get_origin(value) in (types.UnionType, typing.Union) and (
            types.NoneType not in typing.get_args(value)
        ):
            # dict[str, str | int]: a value of any of the scalars (#299)
            branches = [schema_for(v, scalars, output=output) for v in typing.get_args(value)]
            return {"type": "object", "additionalProperties": {"anyOf": branches}}
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
        if output and base in _TEMPORAL:
            # Before an app's own scalar of the class, as it always was, so no schema lock
            # of a date output drifts; unlike Decimal, whose output an app scalar replaces
            return {"type": "string", "format": _TEMPORAL[base]}
        if (spec := scalars.get(base)) is not None:
            # Before the built-ins it may replace, such as Decimal and an argument's date,
            # as serialization and parsing are
            return spec.json_schema()
        if base in _TEMPORAL:
            temporal: JsonSchema = {"type": "string", "format": _TEMPORAL[base]}
            if base not in _TEMPORAL_ARGUMENT:
                return temporal
            # An argument also carries the pattern its parser holds the text to
            return {**temporal, "pattern": _TEMPORAL_ARGUMENT[base]}
        if output and scalars.adapters.for_type(base) is not None:
            # An output adapter writes a class; reading one in is another registration
            return _lift_defs(scalars.adapters.schema(base), base)
        if base is Decimal:
            schema: JsonSchema = {"type": "string", "pattern": DECIMAL_PATTERN}
            # An argument says format: decimal; output keeps the schema locks recorded
            return schema if output else {**schema, "format": "decimal"}
        if issubclass(base, Enum):
            return _enum_schema(base)
        if dataclasses.is_dataclass(base):
            return _dataclass_schema(base, scalars, output)
    raise SchemaError(
        f"unsupported annotation {base!r}; register a class with app.scalar(...), or a "
        "model family with app.output_adapter(...)"
    )


# REQ-F-005: dates and times travel as ISO 8601 text, keyed by exact class
_TEMPORAL: dict[object, str] = {dt.datetime: "date-time", dt.date: "date", dt.time: "time"}
# Built-in date and datetime arguments: RFC 3339 text, a datetime with an offset
_TEMPORAL_ARGUMENT: dict[object, str] = {
    dt.date: f"^{DATE_TEXT}$",
    dt.datetime: f"^{DATETIME_TEXT}$",
}
# A Decimal is written and read as fixed-point text, so no float rounding touches it
DECIMAL_PATTERN = f"^{DECIMAL_TEXT}$"


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


class _Defs:
    """The ``$defs`` of one output schema being built: a key for each dataclass that holds
    itself, and the definitions an output adapter's schema brought"""

    def __init__(self) -> None:
        self.keys: dict[type, str] = {}
        self.schemas: dict[str, JsonSchema] = {}

    def key_for(self, cls: type) -> str:
        key = self.keys.get(cls)
        if key is None:
            # Two classes of one name, from two modules or two scopes, get two keys
            taken = {*self.keys.values(), *self.schemas}
            key = cls.__name__ if cls.__name__ not in taken else free_name(cls.__name__, taken)
            self.keys[cls] = key
        return key

    def adopt(self, schema: JsonSchema) -> JsonSchema:
        """``schema`` with its ``$defs`` moved here, renamed where a name is taken by a
        different definition"""
        defs = defs_of(schema)
        body = {k: v for k, v in schema.items() if k != DEFS_KEY}
        if not defs:
            return body
        renames: dict[str, str] = {}
        for name, definition in defs.items():
            taken = {*self.keys.values(), *self.schemas}
            if name in taken and self.schemas.get(name) != rename_refs(definition, renames):
                renames[name] = free_name(name, {*taken, *defs, *renames.values()})
        for name, definition in defs.items():
            self.schemas[renames.get(name, name)] = rename_refs(definition, renames)
        renamed: JsonSchema = rename_refs(body, renames)
        return renamed


_DEFS: contextvars.ContextVar[_Defs | None] = contextvars.ContextVar(
    "treaty_schema_defs", default=None
)


def _output_root(tp: object, scalars: ScalarRegistry) -> JsonSchema:
    """An output schema, with ``$defs`` at its root when a type in it holds itself"""
    defs = _Defs()
    token = _DEFS.set(defs)
    try:
        schema = schema_for(tp, scalars, output=True)
        built: set[type] = set()
        while pending := [c for c in defs.keys if c not in built]:
            for cls in pending:
                # Each definition from the class alone, wherever it was first held
                outer = _BUILDING.set(frozenset({cls}))
                try:
                    definition = _dataclass_fields_schema(cls, scalars, True)
                finally:
                    _BUILDING.reset(outer)
                defs.schemas[defs.keys[cls]] = definition
                built.add(cls)
    finally:
        _DEFS.reset(token)
    return with_defs(schema, defs.schemas)


def _lift_defs(schema: JsonSchema, cls: type) -> JsonSchema:
    """An output adapter's schema, whose ``$defs`` a model that holds itself keeps, with
    them moved to the root of the output schema being built"""
    defs = _DEFS.get()
    if defs is None:
        if DEFS_KEY in schema:
            raise SchemaError(f"{cls.__qualname__}: $defs outside an output schema")
        return schema
    return defs.adopt(schema)


def _dataclass_schema(cls: type, scalars: ScalarRegistry, output: bool) -> JsonSchema:
    building = _BUILDING.get()
    if cls in building:
        defs = _DEFS.get()
        if not output or defs is None:
            raise SchemaError(
                f"{cls.__qualname__} refers to itself; a recursive argument type has no "
                "flag or JSON form to read"
            )
        return {"$ref": DEFS_PREFIX + defs.key_for(cls)}
    token = _BUILDING.set(building | {cls})
    try:
        return _dataclass_fields_schema(cls, scalars, output)
    finally:
        _BUILDING.reset(token)


def _dataclass_fields_schema(cls: type, scalars: ScalarRegistry, output: bool) -> JsonSchema:
    """Input: a field with a default may be left out. Output: every key is written, so
    each is required, but a ``volatile`` one, which ``--stable-output`` leaves out"""
    hints = type_hints(cls)
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
        if output and _masked(f.name, spec.high_entropy, prop):
            prop = {**prop, "x-high-entropy": True}  # REQ-F-058: a summary unless --unmask
        declared = f.metadata.get(FLAG_META)
        if not output and declared is not None:
            prop = {**prop, "description": declared.description}
            if declared.max_bytes is not None and "items" in prop:
                prop["items"] = {**prop["items"], "x-max-bytes": declared.max_bytes}
            elif declared.max_bytes is not None:
                prop["x-max-bytes"] = declared.max_bytes
        properties[f.name] = prop
        if output and not spec.volatile:
            required.append(f.name)
        elif not output and optional:
            continue  # an input's X | None may be left out, as the parser allows
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


def _textual(prop: JsonSchema) -> bool:
    """A string, or an array of them, possibly nullable"""
    options = prop.get("anyOf")
    if isinstance(options, list):
        return any(isinstance(o, dict) and _textual(o) for o in options)
    kind = prop.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "string" in kinds:
        return True
    items = prop.get("items")
    return "array" in kinds and isinstance(items, dict) and _textual(items)


def _masked(name: str, declared: bool | None, prop: JsonSchema) -> bool:
    """Declared ``high_entropy=True``, or text under a credential's name"""
    if declared is not None:
        return declared
    return secret_field(name) and _textual(prop)


def is_payload_type(tp: object, scalars: ScalarRegistry) -> bool:
    """True when values of this type serialize to a JSON object, array, or null"""
    base, _ = strip_optional(tp)
    if base is types.NoneType:
        return True
    if scalars.adapters.for_type(base) is not None:
        return True  # its schema is checked to be an object or array when it is built
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


MAX_OUTPUT_DEPTH = 400
"""The most arrays and objects one value written as JSON nests: a tree deeper than this
is refused whole, before any of it is written, rather than failing part way through"""


def to_jsonable(value: object, scalars: ScalarRegistry, *, base: Path) -> object:
    """Convert handler output into plain JSON types; fails on anything else. A relative
    ``Path`` is joined to ``base``, the run's working directory (REQ-F-040). A value that
    holds itself, or nests deeper than ``MAX_OUTPUT_DEPTH``, has no JSON form. The
    ``SchemaError`` raised carries in ``at`` the keys and indexes of the part that failed."""
    return _Jsonable(scalars, base).value(value, 0)


_PLAIN_KEY = re.compile(r"[^.\[\]\"\s]+")


def value_path(at: Sequence[str | int]) -> str | None:
    """``at`` spelled as the audit spells a field, with indexes: ``brackets[1].hi``,
    ``[2].hi`` at the top of a list; a key holding ``.``, a bracket, a quote, or a space,
    or an empty one, is quoted, ``["a.b"]``. None at the root. Keys and indexes only:
    a value never appears in it."""
    if not at:
        return None
    out: list[str] = []
    for part in at:
        if isinstance(part, int):
            out.append(f"[{part}]")
        elif _PLAIN_KEY.fullmatch(part):
            out.append(f".{part}" if out else part)
        else:
            out.append(f"[{json.dumps(part)}]")
    return "".join(out)


class _Jsonable:
    def __init__(self, scalars: ScalarRegistry, base: Path) -> None:
        self.scalars = scalars
        self.base = base
        self.open: set[int] = set()
        """The containers being converted, by id, from the root down to the current one"""

    def value(self, value: object, depth: int) -> object:
        scalars = self.scalars
        # A registered scalar first: a str or int subclass has its own serialize=
        if (spec := scalars.for_value(value)) is not None:
            return self.value(spec.serialize(value), depth)
        if (adapter := scalars.adapters.for_value(value)) is not None:
            return self.value(scalars.adapters.dump(value, adapter), depth)
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
            return str(value if value.is_absolute() else self.base / value)
        if isinstance(value, bytes):
            return binary_json(value)
        if isinstance(value, Binary):
            return binary_json(value.data, value.content_type)
        if isinstance(value, (dt.date, dt.time)):
            return _temporal(value)
        if isinstance(value, Decimal):
            return _decimal(value)
        if isinstance(value, External):
            raise SchemaError(
                "treaty.External, which marks a top-level value of error.context only; "
                "Out(external=True) or external=True marks data"
            )
        container = isinstance(value, (list, tuple, dict)) or (
            dataclasses.is_dataclass(value) and not isinstance(value, type)
        )
        if not container:
            raise SchemaError(
                f"cannot serialize {type(value).__name__} to JSON; register it with "
                "app.scalar(...), or its model family with app.output_adapter(...)"
            )
        if id(value) in self.open:
            raise SchemaError(
                f"a {type(value).__name__} that contains itself, which has no JSON form; "
                "return a tree whose children are new objects, or refer to a node by its id"
            )
        if depth >= MAX_OUTPUT_DEPTH:
            raise SchemaError(
                f"a value nested more than {MAX_OUTPUT_DEPTH} arrays and objects deep; "
                "return the tree flat, such as a list of {path, parent, depth} entries in "
                "depth-first order"
            )
        self.open.add(id(value))
        try:
            return self.container(value, depth + 1)
        finally:
            self.open.discard(id(value))

    def container(self, value: object, depth: int) -> object:
        # A try costs nothing until a SchemaError passes it; it gains its key on the way out,
        # so no path is built for a value that converts
        if isinstance(value, (list, tuple)):
            items: list[object] = []
            for i, v in enumerate(value):
                try:
                    items.append(self.value(v, depth))
                except SchemaError as exc:
                    exc.at = (i, *exc.at)
                    raise
            return items
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    raise SchemaError(f"dict keys must be str, got {type(k).__name__}")
                try:
                    out[k] = self.value(v, depth)
                except SchemaError as exc:
                    exc.at = (k, *exc.at)
                    raise
            return out
        assert dataclasses.is_dataclass(value) and not isinstance(value, type)
        fields: dict[str, object] = {}
        for f in dataclasses.fields(value):
            try:
                fields[f.name] = self.value(getattr(value, f.name), depth)
            except SchemaError as exc:
                exc.at = (f.name, *exc.at)
                raise
        return fields
