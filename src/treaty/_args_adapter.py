"""Argument models other than dataclasses, such as pydantic's ``BaseModel``, through an
explicit registration: ``app.args_adapter(BaseModel, schema=..., validate=...)``, the
input-side counterpart of ``app.output_adapter`` (#102).

``schema`` gives the model's JSON Schema, which becomes a frozen dataclass of ``Flag`` and
``Arg`` fields, so argv, ``--raw-payload``, ``exec``, ``App.call``, MCP, help, completion,
``--schema``, and ``--validate-only`` read the command as they read any args dataclass.
Once phase 1 has parsed the values, they go to ``validate`` as JSON values; what it
returns is what the handler receives, and a ``ValueError`` it raises is phase-1 errors,
exit 2. treaty imports no model library: the two callables are the whole contract.
"""

from __future__ import annotations

import dataclasses
import keyword
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from ._errors import ParseError, RegistrationError
from ._flags import Arg, Flag, flag_name
from ._resources import refuse_async
from ._scalars import ScalarRegistry

MODEL_ATTRIBUTE = "_treaty_model"
"""Where a parsed args instance keeps the model ``validate`` built from it"""
EXTRA_KEY = "treaty"
"""The JSON Schema key of a property's treaty options, as pydantic's
``Field(json_schema_extra={"treaty": {...}})`` writes it"""
_FLAG_OPTIONS = frozenset(
    {
        "short",
        "pattern",
        "pattern_type",
        "secret",
        "multiline",
        "max_bytes",
        "from_stdin",
        "audit",
    }
)
_ARG_OPTIONS = frozenset({"pattern", "pattern_type", "secret", "from_stdin", "multiline", "audit"})
_PATH_FORMATS = frozenset({"path", "file-path", "directory-path"})

Validate = Callable[[type, dict[str, object]], object]
SchemaOf = Callable[[type], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ArgsAdapter:
    """``app.args_adapter(base, schema=, validate=)``: commands whose first parameter is
    annotated with a subclass of ``base`` take their arguments from its JSON Schema"""

    base: type
    schema: SchemaOf
    validate: Validate

    def __post_init__(self) -> None:
        if not isinstance(self.base, type):
            raise RegistrationError(
                f"args_adapter takes a class such as pydantic.BaseModel, not {self.base!r}"
            )
        if dataclasses.is_dataclass(self.base):
            raise RegistrationError(
                f"args_adapter({self.base.__qualname__}): a dataclass is an args type already"
            )
        for name in ("schema", "validate"):
            fn = getattr(self, name)
            if not callable(fn):
                raise RegistrationError(
                    f"args_adapter({self.base.__qualname__}): {name}= takes a function"
                )
            refuse_async(fn, f"args_adapter({self.base.__qualname__}) {name}=")


class ArgsAdapters:
    """The app's args adapters; no two cover the same class"""

    def __init__(self) -> None:
        self._adapters: list[ArgsAdapter] = []

    def register(self, adapter: ArgsAdapter, scalars: ScalarRegistry) -> ArgsAdapter:
        where = f"args_adapter({adapter.base.__qualname__})"
        for known in self._adapters:
            if issubclass(adapter.base, known.base) or issubclass(known.base, adapter.base):
                raise RegistrationError(
                    f"{where}: overlaps args_adapter({known.base.__qualname__}); a class "
                    "takes its arguments one way"
                )
        if scalars.get(adapter.base) is not None:
            raise RegistrationError(f"{where}: {adapter.base.__qualname__} is a registered scalar")
        self._adapters.append(adapter)
        return adapter

    def for_class(self, cls: object) -> ArgsAdapter | None:
        if not isinstance(cls, type):
            return None
        return next((a for a in self._adapters if issubclass(cls, a.base)), None)


@dataclass(frozen=True, slots=True)
class ArgsModel:
    """A command's args model, and the dataclass phase 1 parses its arguments into"""

    model: type
    adapter: ArgsAdapter
    fields_type: type

    @classmethod
    def build(cls, model: type, adapter: ArgsAdapter) -> ArgsModel:
        return cls(model, adapter, _fields_dataclass(model, adapter))

    def instance(self, args: object) -> object:
        """What the handler receives for the parsed ``args``"""
        return getattr(args, MODEL_ATTRIBUTE)


def _fields_dataclass(model: type, adapter: ArgsAdapter) -> type:
    where = model.__qualname__
    schema = adapter.schema(model)
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping) or schema.get("type") != "object":
        raise RegistrationError(
            f"{where}: the args adapter's schema= returned no JSON Schema object with properties"
        )
    defs: dict[str, Any] = {**schema.get("definitions", {}), **schema.get("$defs", {})}
    required = schema.get("required", [])
    specs: list[tuple[str, object, Any]] = []
    absent: set[str] = set()
    secrets: set[str] = set()
    for name, prop in properties.items():
        at = f"{where}.{name}"
        if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
            raise RegistrationError(
                f"{at}: an argument is named by a Python identifier; give the schema the "
                "field names, such as model_json_schema(by_alias=False)"
            )
        if not isinstance(prop, Mapping):
            raise RegistrationError(f"{at}: its schema is not a JSON object")
        annotation, secret = _annotation(prop, defs, at)
        options = _options(prop, at)
        positional = bool(options.pop("positional", False))
        allowed = _ARG_OPTIONS if positional else _FLAG_OPTIONS
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise RegistrationError(
                f"{at}: {EXTRA_KEY} options {unknown} are not "
                f"{'Arg' if positional else 'Flag'} options; it takes {sorted(allowed)}"
            )
        if secret:
            options.setdefault("secret", True)
        if options.get("secret"):
            secrets.add(name)
        description = prop.get("description") or prop.get("title") or name
        default: object
        if name in required:
            default = MISSING
        elif "default" in prop:
            default = _python_default(prop["default"], annotation)
            if default is None:
                annotation = annotation | None  # type: ignore[operator]
        else:
            # Neither required nor a default: the model's own default_factory fills it
            default = None
            annotation = annotation | None  # type: ignore[operator]
            absent.add(name)
        declare = Arg if positional else Flag
        declared = declare(description=description, default=default, **options)
        specs.append((name, annotation, declared))
    namespace = {"__post_init__": _validator(model, adapter, frozenset(absent), frozenset(secrets))}
    fields_type = dataclasses.make_dataclass(
        model.__name__,
        specs,
        frozen=True,
        kw_only=True,
        namespace=namespace,
        module=model.__module__,
    )
    fields_type.__qualname__ = model.__qualname__
    fields_type.__doc__ = f"The arguments of {model.__qualname__}, as phase 1 parses them"
    return fields_type


def _options(prop: Mapping[str, Any], at: str) -> dict[str, Any]:
    options = prop.get(EXTRA_KEY, {})
    if not isinstance(options, Mapping):
        raise RegistrationError(f"{at}: {EXTRA_KEY} options are a JSON object, not {options!r}")
    return dict(options)


def _deref(prop: Mapping[str, Any], defs: Mapping[str, Any], at: str) -> Mapping[str, Any]:
    """``{"$ref": "#/$defs/Side"}`` as the definition it names, with the property's own
    keys, such as its description, over it"""
    ref = prop.get("$ref")
    if ref is None:
        all_of = prop.get("allOf")
        if isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], Mapping):
            own = {k: v for k, v in prop.items() if k != "allOf"}
            return {**_deref(all_of[0], defs, at), **own}
        return prop
    name = str(ref).rpartition("/")[2]
    if not str(ref).startswith(("#/$defs/", "#/definitions/")) or name not in defs:
        raise RegistrationError(f"{at}: the schema refers to {ref!r}, which it does not define")
    rest = {k: v for k, v in prop.items() if k != "$ref"}
    return {**_deref(defs[name], defs, at), **rest}


def _annotation(prop: Mapping[str, Any], defs: Mapping[str, Any], at: str) -> tuple[object, bool]:
    """The Python type phase 1 parses a property as, and whether it holds a secret"""
    prop = _deref(prop, defs, at)
    options = prop.get("anyOf") or prop.get("oneOf")
    if isinstance(options, list):
        members = [_deref(o, defs, at) for o in options if isinstance(o, Mapping)]
        rest = [m for m in members if m.get("type") != "null"]
        nullable = len(rest) < len(members)
        kinds = sorted(str(m.get("type")) for m in rest)
        if kinds == ["number", "string"]:
            base: object = Decimal  # pydantic's Decimal: a number or numeric text
            secret = False
        elif len(rest) == 1:
            base, secret = _annotation(rest[0], defs, at)
        else:
            raise RegistrationError(
                f"{at}: a union of {kinds} has no flag form; an argument is one type or "
                "that type or null"
            )
        return (base | None if nullable else base), secret  # type: ignore[operator]
    if "const" in prop:
        values: Sequence[object] = [prop["const"]]
    elif "enum" in prop:
        values = prop["enum"]
    else:
        values = ()
    if values:
        if not all(isinstance(v, str) for v in values):
            raise RegistrationError(
                f"{at}: an enum argument's values are strings, not {list(values)}"
            )
        return Literal[tuple(values)], False
    kind = prop.get("type")
    if kind == "string":
        if prop.get("format") == "password" or prop.get("writeOnly") is True:
            return str, True
        return (Path if prop.get("format") in _PATH_FORMATS else str), False
    if kind == "integer":
        return int, False
    if kind == "number":
        return float, False
    if kind == "boolean":
        return bool, False
    if kind == "array":
        items = prop.get("items")
        if not isinstance(items, Mapping):
            raise RegistrationError(f"{at}: an array argument names its item type")
        item, secret = _annotation(items, defs, at)
        return tuple[item, ...], secret  # type: ignore[valid-type]
    raise RegistrationError(
        f"{at}: a JSON Schema of type {kind!r} has no flag form; an args model's fields are "
        "strings, numbers, booleans, enums, and lists of them"
    )


def _python_default(value: object, annotation: object) -> object:
    """A JSON default as the value phase 1 would parse: a list as a tuple, numeric text as
    a ``Decimal``, a path as a ``Path``"""
    if value is None:
        return None
    base = annotation
    args = typing.get_args(annotation)
    if args and type(None) in args:
        base = next(a for a in args if a is not type(None))
    if isinstance(value, list):
        item = typing.get_args(base)[0] if typing.get_args(base) else object
        return tuple(_python_default(v, item) for v in value)
    if base is Decimal and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    if base is Path and isinstance(value, str):
        return Path(value)
    return value


def _jsonable(value: object) -> object:
    """A parsed value as the JSON value ``validate`` receives"""
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _validator(
    model: type, adapter: ArgsAdapter, absent: frozenset[str], secrets: frozenset[str]
) -> Callable[[object], None]:
    def post_init(self: object) -> None:
        data: dict[str, object] = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if value is None and f.name in absent:
                continue  # the model's default_factory fills it
            data[f.name] = _jsonable(value)
        try:
            built = adapter.validate(model, data)
        except ParseError:
            raise
        except ValueError as exc:
            raise ParseError.combine(list(_issues(exc, secrets))) from None
        object.__setattr__(self, MODEL_ATTRIBUTE, built)

    return post_init


def _issues(exc: ValueError, secrets: frozenset[str]) -> Iterable[ParseError]:
    """A ``ValueError`` from ``validate`` as phase-1 errors: one per entry of its
    ``errors()`` list (``{"loc", "msg", "type", "input"}``, as pydantic's
    ``ValidationError`` has), each at its location; else one error with its message"""
    entries = getattr(exc, "errors", None)
    listed = entries() if callable(entries) else None
    if not isinstance(listed, list) or not listed:
        yield ParseError(str(exc) or type(exc).__name__)
        return
    for entry in listed:
        if not isinstance(entry, Mapping):
            yield ParseError(str(entry))
            continue
        raw_loc = entry.get("loc")
        loc: tuple[object, ...] = tuple(raw_loc) if isinstance(raw_loc, (list, tuple)) else ()
        context: dict[str, object] = {}
        location = _location(loc)
        if location is not None:
            context["field"] = location
        if isinstance(entry.get("type"), str):
            context["type"] = entry["type"]
        secret = bool(loc) and loc[0] in secrets
        if "input" in entry and location is not None and not secret:
            context["value"] = entry["input"]
        yield ParseError(str(entry.get("msg", "invalid value")), context=context)


def _location(loc: Sequence[object]) -> str | None:
    """``("tags", 1)`` as ``tags[1]``, the first part as its flag: ``report-price``"""
    if not loc or not isinstance(loc[0], str):
        return None
    text = flag_name(loc[0])
    for part in loc[1:]:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text
