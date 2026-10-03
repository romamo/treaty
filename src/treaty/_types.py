"""Classification of Python annotations into the spec's flag and schema types.

Shared by the flag inspector and the JSON Schema generator so both agree on
which annotations are supported.
"""

from __future__ import annotations

import annotationlib
import dataclasses
import inspect
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path

from ._errors import RegistrationError, SchemaError
from ._scalars import BUILT_IN, ScalarRegistry, ScalarSpec

if typing.TYPE_CHECKING:
    from ._flags import FieldInfo


class FlagType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    ENUM = "enum"
    OBJECT = "object"
    """A frozen dataclass argument, carried as a JSON object"""


_SCALARS: dict[type, FlagType] = {
    bool: FlagType.BOOLEAN,
    int: FlagType.INTEGER,
    float: FlagType.NUMBER,
    str: FlagType.STRING,
}


@dataclass(frozen=True, slots=True)
class Classified:
    """One annotation reduced to a flag type plus what is needed to parse it"""

    flag_type: FlagType
    optional: bool
    base: object
    enum_values: tuple[str, ...] = ()
    enum_cls: type[Enum] | None = None
    item: Classified | None = None
    path: bool = False
    """A ``pathlib.Path`` string: hardened against traversal and encoded bytes"""
    scalar: ScalarSpec | None = None
    """A registered custom scalar: parsed through its spec after the base type"""
    members: tuple[FieldInfo, ...] = ()
    """An object's fields, in declaration order: what its JSON object's keys hold"""
    values: tuple[Classified, ...] = ()
    """A mapping's value types, ``dict[str, V]``: one, or each scalar of a union, tried in
    declaration order; empty for anything but a mapping"""

    @property
    def is_map(self) -> bool:
        """A ``dict[str, V]``: a JSON object of any string keys, each value a V"""
        return bool(self.values)

    @property
    def object_cls(self) -> type:
        """The frozen dataclass an object value is built as"""
        if self.flag_type is not FlagType.OBJECT or not isinstance(self.base, type):
            raise TypeError(f"{self.base!r} is not an object type")
        return self.base


ObjectHook = Callable[[type], Classified]
"""Reads a dataclass annotation as an object, where an argument may be one"""


def type_hints(obj: object) -> dict[str, typing.Any]:
    """``typing.get_type_hints``, but an annotation naming what is not defined at runtime,
    such as a class imported only under ``TYPE_CHECKING`` (annotations are lazy, so the
    module still imports), is a RegistrationError that lists every such name"""
    try:
        return typing.get_type_hints(obj)
    except NameError as exc:
        lazy = typing.get_type_hints(obj, format=annotationlib.Format.FORWARDREF)
        owner = getattr(obj, "__qualname__", repr(obj))
        entries: list[str] = []
        names: set[str] = set()
        for key, tp in lazy.items():
            undefined = list(dict.fromkeys(_undefined(tp)))
            if not undefined:
                continue
            names.update(undefined)
            if isinstance(obj, type):
                label = f"{owner}.{key}"
            else:
                label = f"{owner}: " + ("the return" if key == "return" else f"parameter {key}")
            entries.append(f"{label} names {', '.join(undefined)}")
        if not entries:
            entries.append(f"{owner}: an annotation names {exc.name}")
            names.add(str(exc.name))
        which = "that name is" if len(names) == 1 else "those names are"
        pronoun = "it" if len(names) == 1 else "them"
        raise RegistrationError(
            f"{'; '.join(entries)}; {which} not defined at runtime: import {pronoun} outside "
            "TYPE_CHECKING, since treaty reads annotations at registration to build the manifest"
        ) from None


def signature(fn: typing.Callable[..., object]) -> inspect.Signature:
    """``inspect.signature`` for parameter names and kinds, which leaves the annotations
    unevaluated, so ``type_hints`` is where an undefined name is reported"""
    return inspect.signature(fn, annotation_format=annotationlib.Format.FORWARDREF)


def _undefined(tp: object) -> typing.Iterator[str]:
    """The names a ``FORWARDREF``-format annotation could not resolve, nested ones too"""
    if isinstance(tp, annotationlib.ForwardRef):
        yield tp.__forward_arg__
        return
    for arg in typing.get_args(tp):
        yield from _undefined(arg)


def resolve_alias(tp: object) -> object:
    """``type Port = int`` (PEP 695) names its value; follow aliases of aliases too"""
    while True:
        if isinstance(tp, typing.TypeAliasType):
            tp = tp.__value__
            continue
        origin = typing.get_origin(tp)
        if isinstance(origin, typing.TypeAliasType):
            # A generic alias applied to arguments: type Vec[T] = list[T]; Vec[int]
            tp = origin.__value__[typing.get_args(tp)]
            continue
        return tp


def strip_optional(tp: object) -> tuple[object, bool]:
    tp = resolve_alias(tp)
    origin = typing.get_origin(tp)
    if origin is not types.UnionType and origin is not typing.Union:
        return tp, False
    members = [a for a in typing.get_args(tp) if a is not types.NoneType]
    if len(members) != 1 or len(members) == len(typing.get_args(tp)):
        raise SchemaError(f"unsupported union {tp!r}; only 'X | None' is allowed")
    return resolve_alias(members[0]), True


def classify(tp: object, scalars: ScalarRegistry, objects: ObjectHook | None = None) -> Classified:
    """``objects`` reads a dataclass, or the items of an array of them, as an object;
    without it, as for settings, a dataclass is an unsupported annotation"""
    base, optional = strip_optional(tp)
    origin = typing.get_origin(base)
    if origin is typing.Literal:
        values = typing.get_args(base)
        if not values or not all(isinstance(v, str) for v in values):
            raise SchemaError(f"Literal values must all be strings: {base!r}")
        return Classified(FlagType.ENUM, optional, base, enum_values=tuple(values))
    if origin in (list, tuple):
        args = typing.get_args(base)
        if origin is tuple:
            if len(args) != 2 or args[1] is not Ellipsis:
                raise SchemaError(f"only homogeneous 'tuple[T, ...]' is supported: {base!r}")
        elif len(args) != 1:
            raise SchemaError(f"list needs exactly one item type: {base!r}")
        item = classify(args[0], scalars, objects)
        if item.flag_type in (FlagType.ARRAY, FlagType.BOOLEAN) or item.optional:
            raise SchemaError(f"array items must be scalars: {base!r}")
        return Classified(FlagType.ARRAY, optional, base, item=item)
    if origin is dict:
        return Classified(FlagType.OBJECT, optional, base, values=_map_values(base, scalars))
    if base is dict or origin is Mapping or base is Mapping:
        raise SchemaError(f"unsupported mapping {tp!r}; {MAP_SHAPE}")
    if isinstance(base, type):
        if base in _SCALARS:
            return Classified(_SCALARS[base], optional, base)
        if base is Path:
            return Classified(FlagType.STRING, optional, base, path=True)
        if (spec := scalars.get(base)) is not None:
            return Classified(_SCALARS[spec.base], optional, base, scalar=spec)
        if objects is not None and dataclasses.is_dataclass(base):
            return replace(objects(base), optional=optional)
        if (built := _BUILT_IN.get(base)) is not None:
            # Decimal, date, and datetime, built in as text; an app's own app.scalar above
            # wins. Keyed by exact class: a datetime is a date, but not a date argument
            return Classified(FlagType.STRING, optional, base, scalar=built)
        if issubclass(base, Enum):
            members = list(base)
            if not all(isinstance(m.value, str) for m in members):
                raise SchemaError(f"enum members must have string values: {base!r}")
            return Classified(
                FlagType.ENUM,
                optional,
                base,
                enum_values=tuple(m.value for m in members),
                enum_cls=base,
            )
    raise SchemaError(f"unsupported annotation {tp!r}; register a class with app.scalar(...)")


_BUILT_IN: dict[type, ScalarSpec] = {spec.cls: spec for spec in BUILT_IN}


MAP_SHAPE = (
    "a mapping is dict[str, V], V one of str, int, float, bool, Decimal, date, datetime, "
    "Path, an enum, a Literal, or a class registered with app.scalar(...), or a union of "
    "them, such as dict[str, str | int]"
)


def _map_values(tp: object, scalars: ScalarRegistry) -> tuple[Classified, ...]:
    """The value types of ``dict[str, V]``: V, or each member of a union of scalars"""
    args = typing.get_args(tp)
    if len(args) != 2 or resolve_alias(args[0]) is not str:
        raise SchemaError(f"unsupported mapping {tp!r}; {MAP_SHAPE}")
    value = resolve_alias(args[1])
    union = typing.get_origin(value) in (types.UnionType, typing.Union)
    members = typing.get_args(value) if union else (value,)
    if types.NoneType in members:
        raise SchemaError(
            f"unsupported mapping {tp!r}: a value cannot be null; leave the key out, and "
            f"make the whole mapping optional with | None if it may be absent; {MAP_SHAPE}"
        )
    out: list[Classified] = []
    for member in members:
        try:
            classified = classify(member, scalars)
        except SchemaError:
            raise SchemaError(f"unsupported mapping {tp!r}; {MAP_SHAPE}") from None
        if classified.flag_type in (FlagType.ARRAY, FlagType.OBJECT):
            raise SchemaError(f"unsupported mapping {tp!r}; {MAP_SHAPE}")
        out.append(classified)
    return tuple(out)


def is_dataclass_type(tp: object) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)
