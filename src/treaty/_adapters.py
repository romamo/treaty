"""Output adapters: a class family treaty learns to describe and write through callables.

``app.output_adapter(BaseModel, schema=..., dump=...)`` registers one base class, and
every subclass of it may be returned, nested in a dataclass field, or held in a list. The
adapter's ``schema(cls)`` gives the class's JSON Schema and ``dump(obj)`` its JSON value;
treaty imports nothing from the library behind them.

The schema is brought to the form treaty writes for a dataclass: ``$defs`` inlined,
``prefixItems`` as draft-07 ``items``, every key required and no other key allowed. The
stable-output rules are checked on it when a command names the class: an output
collection is never null (REQ-F-074) unless the adapter writes a null one as empty
(``none_as_empty=True``), and the ``Out`` options are read from the keys ``x-sort-key``,
``x-ordered``, ``x-volatile``, ``x-high-entropy``, and ``x-external`` of a property (for
pydantic, ``Field(json_schema_extra={"x-volatile": True})``). Each is optional, so a
class the app does not own works with the defaults.
"""

from __future__ import annotations

import copy
import json
import typing
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ._errors import RegistrationError, SchemaError
from ._redact import secret_field

JsonSchema = dict[str, Any]

OPTION_KEYS: Mapping[str, type] = {
    "x-sort-key": str,
    "x-ordered": bool,
    "x-volatile": bool,
    "x-high-entropy": bool,
    "x-external": bool,
}
"""The ``Out`` options a property of an adapted class may declare, and their JSON kinds"""

_REF_PREFIX = "#/$defs/"
_SORTABLE = frozenset({"string", "integer"})
_BUILTIN = (str, int, float, bool, bytes, Path, list, tuple, dict, set, frozenset, object)


@dataclass(frozen=True, slots=True)
class OutputAdapter:
    """How every subclass of ``base`` is described and written as command output"""

    base: type
    schema: Callable[[type], Mapping[str, Any]]
    """The JSON Schema of a subclass, such as ``cls.model_json_schema(mode="serialization")``"""
    dump: Callable[[Any], object]
    """An instance as JSON values, such as ``obj.model_dump(mode="json")``"""
    none_as_empty: bool = False
    """A null list or dict is written as ``[]`` or ``{}``, so ``list[T] | None`` is allowed"""

    def __post_init__(self) -> None:
        if not isinstance(self.base, type) or typing.get_origin(self.base) is not None:
            raise RegistrationError(f"output_adapter: base must be a class, got {self.base!r}")
        name = self.base.__qualname__
        if self.base in _BUILTIN or issubclass(self.base, Enum):
            raise RegistrationError(
                f"output_adapter: {name} is a built-in output type and cannot be adapted"
            )
        if hasattr(self.base, "__dataclass_fields__"):
            raise RegistrationError(
                f"output_adapter: {name} is a dataclass, which treaty writes itself"
            )
        if not callable(self.schema):
            raise RegistrationError(f"output_adapter({name}): schema must be callable")
        if not callable(self.dump):
            raise RegistrationError(f"output_adapter({name}): dump must be callable")
        if not isinstance(self.none_as_empty, bool):
            raise RegistrationError(f"output_adapter({name}): none_as_empty is True or False")


@dataclass(slots=True)
class OutputAdapters:
    """The adapters of one app, and the normalized schema of each class they have described"""

    _adapters: list[OutputAdapter] = field(default_factory=list)
    _schemas: dict[type, JsonSchema] = field(default_factory=dict)

    def register(self, adapter: OutputAdapter) -> OutputAdapter:
        for other in self._adapters:
            if issubclass(adapter.base, other.base) or issubclass(other.base, adapter.base):
                raise RegistrationError(
                    f"output_adapter: {adapter.base.__qualname__} overlaps the adapter for "
                    f"{other.base.__qualname__}; one class family has one adapter"
                )
        self._adapters.append(adapter)
        return adapter

    def __len__(self) -> int:
        return len(self._adapters)

    def __iter__(self) -> Iterator[OutputAdapter]:
        return iter(self._adapters)

    def for_type(self, tp: object) -> OutputAdapter | None:
        if not isinstance(tp, type) or typing.get_origin(tp) is not None:
            return None
        return next((a for a in self._adapters if issubclass(tp, a.base)), None)

    def for_value(self, value: object) -> OutputAdapter | None:
        return next((a for a in self._adapters if isinstance(value, a.base)), None)

    def node(self, cls: type) -> JsonSchema:
        """The normalized schema of ``cls``, shared: callers never change it"""
        cached = self._schemas.get(cls)
        if cached is not None:
            return cached
        adapter = self.for_type(cls)
        assert adapter is not None, cls
        raw = adapter.schema(cls)
        if not isinstance(raw, Mapping):
            raise RegistrationError(
                f"{cls.__qualname__}: the output adapter's schema() returned "
                f"{type(raw).__name__}, not a JSON Schema object"
            )
        try:
            # A copy the adapter cannot change later, and proof it is JSON
            plain = json.loads(json.dumps(raw, sort_keys=True))
        except (TypeError, ValueError) as exc:
            raise RegistrationError(
                f"{cls.__qualname__}: the output adapter's schema() is not JSON ({exc})"
            ) from None
        schema = _Normalizer(cls.__qualname__, plain, adapter.none_as_empty).run()
        if schema.get("type") not in ("object", "array"):
            raise RegistrationError(
                f"{cls.__qualname__}: the output adapter's schema() describes "
                f"{schema.get('type')!r}, not a JSON object or array"
            )
        self._schemas[cls] = schema
        return schema

    def schema(self, cls: type) -> JsonSchema:
        """The normalized schema of ``cls``, a copy to embed in a command's schema"""
        return copy.deepcopy(self.node(cls))

    def dump(self, value: object, adapter: OutputAdapter) -> object:
        """``value`` as JSON values, checked against its schema: every key written, and a
        null collection written as empty when the adapter says so"""
        cls = type(value)
        node = self.node(cls)
        return _fit(adapter.dump(value), node, cls.__qualname__, adapter.none_as_empty)


class _Normalizer:
    def __init__(self, name: str, schema: JsonSchema, none_as_empty: bool) -> None:
        self.name = name
        defs = schema.pop("$defs", None)
        legacy = schema.pop("definitions", None)
        self.defs: dict[str, JsonSchema] = {**(legacy or {}), **(defs or {})}
        self.root = schema
        self.none_as_empty = none_as_empty

    def run(self) -> JsonSchema:
        return self.normalize(self.inline(self.root, ()), self.name)

    def inline(self, node: object, stack: tuple[str, ...]) -> Any:
        """Every ``$ref`` replaced by its definition; a class that holds itself has no
        finite schema, as a recursive dataclass has none"""
        if isinstance(node, list):
            return [self.inline(n, stack) for n in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
                raise RegistrationError(
                    f"{self.name}: $ref {ref!r} does not point into $defs, which treaty inlines"
                )
            target = ref.removeprefix(_REF_PREFIX)
            if target in stack:
                raise RegistrationError(
                    f"{self.name}: {target} refers to itself; recursive outputs have no schema"
                )
            if target not in self.defs:
                raise RegistrationError(f"{self.name}: $ref {ref!r} names no definition")
            resolved = self.inline(self.defs[target], (*stack, target))
            siblings = {k: self.inline(v, stack) for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings}
        out: JsonSchema = {}
        for key, value in node.items():
            if key == "discriminator" and isinstance(value, dict):
                # Its mapping names $defs, which are gone once inlined
                out[key] = {k: v for k, v in value.items() if k != "mapping"}
            else:
                out[key] = self.inline(value, stack)
        return out

    def normalize(self, node: JsonSchema, where: str) -> JsonSchema:
        node = dict(node)
        for key, kind in OPTION_KEYS.items():
            if key in node and type(node[key]) is not kind:
                raise RegistrationError(
                    f"{where}: {key} is {'a string' if kind is str else 'true or false'}, "
                    f"got {node[key]!r}"
                )
        node = self.not_null(node, where)
        for key in ("anyOf", "oneOf", "allOf"):
            if isinstance(node.get(key), list):
                node[key] = [self.normalize(b, where) for b in node[key]]
        if "prefixItems" in node:
            prefix = node.pop("prefixItems")
            rest = node.pop("items", False)
            node["items"] = prefix
            node["additionalItems"] = rest
        items = node.get("items")
        if isinstance(items, dict):
            node["items"] = self.normalize(items, f"{where}[]")
        elif isinstance(items, list):
            node["items"] = [self.normalize(i, f"{where}[]") for i in items]
        if isinstance(node.get("additionalItems"), dict):
            node["additionalItems"] = self.normalize(node["additionalItems"], f"{where}[]")
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            node["additionalProperties"] = self.normalize(extra, f"{where}[]")
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["properties"] = {
                name: self.property(name, prop, f"{where}.{name}")
                for name, prop in properties.items()
            }
            # Every key is written, as on a dataclass, but a volatile one, which
            # --stable-output leaves out; a key the schema does not list is never written
            required = [n for n, p in node["properties"].items() if not p.get("x-volatile")]
            if required:
                node["required"] = required
            else:
                node.pop("required", None)
            node.setdefault("additionalProperties", False)
        self.check_order(node, where)
        return node

    def property(self, name: str, prop: object, where: str) -> JsonSchema:
        if not isinstance(prop, dict):
            raise RegistrationError(f"{where}: property schema is {prop!r}, not an object")
        out = self.normalize(prop, where)
        if "x-high-entropy" not in out and (
            _has_format(out, "password") or (secret_field(name) and _textual(out))
        ):
            out["x-high-entropy"] = True  # REQ-F-058: a summary unless --unmask
        return out

    def not_null(self, node: JsonSchema, where: str) -> JsonSchema:
        """REQ-F-074: an output collection is never null; with ``none_as_empty`` the null
        branch goes, since the adapter writes ``[]`` or ``{}`` instead"""
        kinds = node.get("type")
        if isinstance(kinds, list) and "null" in kinds:
            others = [k for k in kinds if k != "null"]
            if any(k in ("array", "object") for k in others) and not _closed_object(node):
                self.nullable(where)
                node["type"] = others[0] if len(others) == 1 else others
                node.pop("default", None)
            return node
        for key in ("anyOf", "oneOf"):
            branches = node.get(key)
            if not isinstance(branches, list):
                continue
            nulls = [b for b in branches if isinstance(b, dict) and b.get("type") == "null"]
            rest = [b for b in branches if b not in nulls]
            if not nulls or not any(_collection(b) for b in rest):
                continue
            self.nullable(where)
            del node[key]
            node.pop("default", None)
            if len(rest) == 1:
                return {**node, **rest[0]}
            node[key] = rest
        return node

    def nullable(self, where: str) -> None:
        if not self.none_as_empty:
            raise RegistrationError(
                f"{where}: an output collection is never null, since empty is [] or {{}}; "
                "pass none_as_empty=True to app.output_adapter(...) to write a null one as "
                "empty, or drop | None from the field (REQ-F-074)"
            )

    def check_order(self, node: JsonSchema, where: str) -> None:
        """``x-sort-key`` names a string or integer property of the array's object items;
        ``x-ordered`` keeps an array or untyped content in the handler's order"""
        key = node.get("x-sort-key")
        if key is not None and node.get("x-ordered"):
            raise RegistrationError(
                f"{where}: x-sort-key orders the array, x-ordered keeps it; pick one"
            )
        arrays = any(_is_kind(b, "array") for b in branches(node))
        if node.get("x-ordered") and not (arrays or untyped(node)):
            raise RegistrationError(f"{where}: x-ordered is for arrays")
        if key is None:
            return
        array = next((b for b in branches(node) if _is_kind(b, "array")), None)
        items = array.get("items") if array is not None else None
        prop = None
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            prop = items["properties"].get(key)
        if not isinstance(prop, dict) or not _sortable(prop):
            raise RegistrationError(
                f"{where}: x-sort-key={key!r} must name a string or integer property of the "
                "array's object items"
            )


def branches(node: JsonSchema) -> list[JsonSchema]:
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, list):
            return [b for o in options if isinstance(o, dict) for b in branches(o)]
    return [node]


def _is_kind(node: JsonSchema, kind: str) -> bool:
    kinds = node.get("type")
    return kind in kinds if isinstance(kinds, list) else kinds == kind


def _has_format(node: JsonSchema, fmt: str) -> bool:
    return any(b.get("format") == fmt for b in branches(node))


def _textual(node: JsonSchema) -> bool:
    """A string, or an array of them, possibly nullable"""
    for b in branches(node):
        if _is_kind(b, "string"):
            return True
        items = b.get("items")
        if _is_kind(b, "array") and isinstance(items, dict) and _textual(items):
            return True
    return False


def _sortable(prop: JsonSchema) -> bool:
    options = [b for b in branches(prop) if not _is_kind(b, "null")]
    return len(options) == 1 and options[0].get("type") in _SORTABLE


def _closed_object(node: JsonSchema) -> bool:
    """An object with named properties: a nested model, which may be null, not a dict"""
    return isinstance(node.get("properties"), dict)


def _collection(node: object) -> bool:
    if not isinstance(node, dict):
        return False
    if _is_kind(node, "array"):
        return True
    return _is_kind(node, "object") and not _closed_object(node)


def untyped(node: object) -> bool:
    """Content the schema does not describe: ``{}``, an open object such as
    ``dict[str, Any]``, or an array of it"""
    if not isinstance(node, dict):
        return node is True
    meaningful = {k for k in node if k not in ("title", "description", "default")}
    if not meaningful - set(OPTION_KEYS):
        return True
    if _is_kind(node, "object") and not _closed_object(node):
        return untyped(node.get("additionalProperties", True))
    if _is_kind(node, "array"):
        return untyped(node.get("items", True))
    return False


def branch(node: JsonSchema, value: object) -> JsonSchema:
    """The ``anyOf`` or ``oneOf`` branch of ``node`` that describes ``value``, by its JSON
    kind; an object picks the first object branch its keys and constants fit, as a union
    of models needs, else the first object branch. ``{}`` when none does."""
    options = node.get("anyOf", node.get("oneOf"))
    if isinstance(options, list):
        accepted = [o for o in options if isinstance(o, dict) and _accepts(o, value)]
        if isinstance(value, dict):
            fitting = [o for o in accepted if _fits_object(o, value)]
            accepted = fitting or accepted
        return branch(accepted[0], value) if accepted else {}
    every = node.get("allOf")
    if isinstance(every, list) and len(every) == 1 and isinstance(every[0], dict):
        rest = {k: v for k, v in node.items() if k != "allOf"}
        return branch({**every[0], **rest}, value)
    return node


def _fits_object(node: JsonSchema, value: Mapping[str, object]) -> bool:
    """``value`` has each required key of ``node``, no key a closed ``node`` leaves out,
    and the ``const`` or ``enum`` value each such property names"""
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return True  # a nested union or an open object: decided further down
    if any(key not in value for key in node.get("required", ())):
        return False
    if node.get("additionalProperties") is False and any(k not in properties for k in value):
        return False
    for key, v in value.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue
        if "const" in prop and prop["const"] != v:
            return False
        if isinstance(prop.get("enum"), list) and v not in prop["enum"]:
            return False
    return True


def _accepts(node: JsonSchema, value: object) -> bool:
    kinds = node.get("type")
    if kinds is None:
        return True
    allowed = set(kinds) if isinstance(kinds, list) else {kinds}
    if value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        return bool(allowed & {"integer", "number"})
    elif isinstance(value, float):
        kind = "number"
    elif isinstance(value, str):
        kind = "string"
    elif isinstance(value, list):
        kind = "array"
    else:
        kind = "object"
    return kind in allowed


def _fit(value: object, node: JsonSchema, where: str, none_as_empty: bool) -> object:
    """The dump checked against its schema: each required key written, and with
    ``none_as_empty`` a null collection written as ``[]`` or ``{}``"""
    if value is None and none_as_empty:
        if _is_kind(node, "array"):
            return []
        if _is_kind(node, "object") and not _closed_object(node):
            return {}
    node = branch(node, value)
    if isinstance(value, list):
        items = node.get("items")
        if isinstance(items, list):
            extra = node.get("additionalItems")
            rest = extra if isinstance(extra, dict) else {}
            return [
                _fit(v, items[i] if i < len(items) else rest, f"{where}[{i}]", none_as_empty)
                for i, v in enumerate(value)
            ]
        item = items if isinstance(items, dict) else {}
        return [_fit(v, item, f"{where}[{i}]", none_as_empty) for i, v in enumerate(value)]
    if not isinstance(value, dict):
        return value
    properties = node.get("properties")
    if isinstance(properties, dict):
        for key in node.get("required", ()):
            if key not in value:
                raise SchemaError(
                    f"{where}: the output adapter's dump() left out {key!r}, which its schema "
                    "says is always written"
                )
        if node.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    raise SchemaError(
                        f"{where}: the output adapter's dump() wrote {key!r}, which its "
                        "schema does not list"
                    )
    extra = node.get("additionalProperties")
    rest = extra if isinstance(extra, dict) else {}
    props = properties if isinstance(properties, dict) else {}
    return {k: _fit(v, props.get(k, rest), f"{where}.{k}", none_as_empty) for k, v in value.items()}
