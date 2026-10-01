"""Conditional argument rules (REQ-C-026): cross-field requirements the manifest can show.

``requires=[RequiredWhen("format", "csv", then=("separator",))]`` says what an args
``__post_init__`` would otherwise say only on a failing call. The rules are checked in
phase 1 on the fields the caller supplied, before ``__post_init__`` runs, so a
``DefaultWhenAbsent`` default is what it sees.

``RequiresAny`` and ``RequiresOne`` have no manifest ``ConditionalRule`` shape, so they
appear in ``--schema`` as ``requires_groups`` and as ``anyOf``/``oneOf`` of the
``raw_payload_schema``, in ``--help``, and in the MCP tool description.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo, jsonable_default
from ._schema import JsonSchema
from ._types import FlagType


@dataclass(frozen=True, slots=True)
class RequiredWhen:
    """When ``--flag`` has ``value``, every flag in ``then`` is required"""

    flag: str
    value: object
    then: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Excludes:
    """When ``--flag`` is given, no flag in ``prohibited`` may be"""

    flag: str
    prohibited: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DefaultWhenAbsent:
    """When ``--flag`` is not given, ``--target`` defaults to ``default`` instead"""

    flag: str
    target: str
    default: object


@dataclass(frozen=True, slots=True)
class RequiresAny:
    """At least one flag in ``fields`` is required"""

    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequiresOne:
    """Exactly one flag in ``fields`` is required: ``RequiresAny`` with the flags
    mutually exclusive"""

    fields: tuple[str, ...]


Rule = RequiredWhen | Excludes | DefaultWhenAbsent | RequiresAny | RequiresOne


@dataclass(frozen=True, slots=True)
class BoundRule:
    """A rule with its flags resolved to fields and its value parsed, at registration"""

    rule: Rule
    field: FieldInfo
    others: tuple[FieldInfo, ...]
    """``then``, ``prohibited``, or the one ``target``"""
    value: object = None
    """The parsed ``value`` or ``default``"""
    json_value: object = None

    @property
    def group(self) -> bool:
        """A ``RequiresAny`` or ``RequiresOne``: no manifest ``ConditionalRule`` shape"""
        return isinstance(self.rule, (RequiresAny, RequiresOne))

    @property
    def flags(self) -> tuple[FieldInfo, ...]:
        """Every field the rule names, the first one first"""
        return (self.field, *self.others)

    def describe(self) -> str:
        """The rule as a sentence, for ``--help`` and the MCP tool description"""
        names = [f"--{f.flag}" for f in self.others]
        match self.rule:
            case RequiredWhen():
                when = f"--{self.field.flag}"
                if self.json_value is not True:  # a boolean switch takes no value on argv
                    when += f" {_spelled(self.json_value)}"
                return f"{when} requires {_listed(names, 'and')}"
            case Excludes():
                return f"--{self.field.flag} excludes {_listed(names, 'and')}"
            case DefaultWhenAbsent():
                target = self.others[0].flag
                default = _spelled(self.json_value)
                return f"without --{self.field.flag}, --{target} defaults to {default}"
            case RequiresAny():
                return f"pass at least one of {_listed(self.flag_names, 'or')}"
            case RequiresOne():
                return f"pass exactly one of {_listed(self.flag_names, 'or')}"

    @property
    def flag_names(self) -> list[str]:
        """``--flag`` for every field the rule names"""
        return [f"--{f.flag}" for f in self.flags]

    def to_json(self) -> dict[str, object]:
        """The manifest ``ConditionalRule``, or for a group ``{"any_of": [...]}`` or
        ``{"one_of": [...]}``, which only ``--schema`` shows"""
        names = [f.flag for f in self.others]
        match self.rule:
            case RequiredWhen():
                return {
                    "if_flag": self.field.flag,
                    "if_value": self.json_value,
                    "then_required": names,
                }
            case Excludes():
                return {"if_flag": self.field.flag, "prohibited": names}
            case DefaultWhenAbsent():
                return {
                    "if_flag": self.field.flag,
                    "target_flag": names[0],
                    "default": self.json_value,
                }
            case RequiresAny():
                return {"any_of": [f.flag for f in self.flags]}
            case RequiresOne():
                return {"one_of": [f.flag for f in self.flags]}

    def json_schema(self) -> JsonSchema:
        """A group as JSON Schema over the payload keys: ``anyOf`` or ``oneOf`` of one
        branch per flag, each true only when the caller gave that flag"""
        assert self.group
        branches = [_given_schema(f) for f in self.flags]
        return {"anyOf" if isinstance(self.rule, RequiresAny) else "oneOf": branches}


def _spelled(value: object) -> str:
    """A rule's value as argv spells it: ``csv``, ``true``, ``5``"""
    return value if isinstance(value, str) else json.dumps(value)


def _listed(names: Sequence[str], conjunction: str) -> str:
    """``--a``, ``--a and --b``, ``--a, --b, or --c``"""
    if len(names) < 3:
        return f" {conjunction} ".join(names)
    return f"{', '.join(names[:-1])}, {conjunction} {names[-1]}"


def _given_schema(field: FieldInfo) -> JsonSchema:
    """The payload carries ``field`` as given: a boolean when true, anything else when
    not null, as ``_present`` decides"""
    key = field.flag.replace("-", "_")
    value: JsonSchema = (
        {"const": True} if field.flag_type is FlagType.BOOLEAN else {"not": {"type": "null"}}
    )
    return {"required": [key], "properties": {key: value}}


def bind_rules(
    requires: Sequence[object], fields: Sequence[FieldInfo], where: str
) -> tuple[BoundRule, ...]:
    """Resolve every rule against the command's fields; a typo or a wrong-typed value
    fails registration"""
    if isinstance(requires, (str, bytes)) or not isinstance(requires, Sequence):
        raise RegistrationError(f"{where}: requires is a list of RequiredWhen, Excludes, ...")
    by_flag = {f.flag: f for f in fields}

    def resolve(name: object, *, optional: bool = False) -> FieldInfo:
        found = by_flag.get(name) if isinstance(name, str) else None
        if found is None:
            raise RegistrationError(
                f"{where}: requires names {name!r}, which is not a flag of the command; "
                f"use one of {sorted(by_flag)}"
            )
        if optional and found.required:
            raise RegistrationError(
                f"{where}: requires makes --{found.flag} conditional, but it is always required; "
                "give it a default"
            )
        if found.secret:
            raise RegistrationError(f"{where}: requires cannot name the secret --{found.flag}")
        return found

    def names(value: object, what: str) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (tuple, list)) or not value:
            raise RegistrationError(f"{where}: {what} is a non-empty tuple of flag names")
        return tuple(value)

    bound: list[BoundRule] = []
    for rule in requires:
        match rule:
            case RequiredWhen(flag=flag, value=value, then=then):
                field = resolve(flag)
                others = tuple(resolve(n, optional=True) for n in names(then, "then"))
                parsed, as_json = _typed(field, value, where)
                bound.append(BoundRule(rule, field, others, parsed, as_json))
            case Excludes(flag=flag, prohibited=prohibited):
                field = resolve(flag)
                others = tuple(resolve(n, optional=True) for n in names(prohibited, "prohibited"))
                bound.append(BoundRule(rule, field, others))
            case DefaultWhenAbsent(flag=flag, target=target, default=default):
                field = resolve(flag)
                other = resolve(target, optional=True)
                parsed, as_json = _typed(other, default, where)
                bound.append(BoundRule(rule, field, (other,), parsed, as_json))
            case RequiresAny(fields=group) | RequiresOne(fields=group):
                kind = type(rule).__name__
                flags = names(group, "fields")
                if len(flags) < 2:
                    raise RegistrationError(
                        f"{where}: {kind} names {len(flags)} flag; it needs two or more, "
                        "and a single one is a Flag without a default"
                    )
                if len(set(flags)) != len(flags):
                    raise RegistrationError(f"{where}: {kind} names a flag twice: {flags}")
                first, *rest = (resolve(n, optional=True) for n in flags)
                bound.append(BoundRule(rule, first, tuple(rest)))
            case _:
                raise RegistrationError(
                    f"{where}: requires takes RequiredWhen, Excludes, DefaultWhenAbsent, "
                    f"RequiresAny, or RequiresOne, not {rule!r}"
                )
        if any(o is bound[-1].field for o in bound[-1].others):
            raise RegistrationError(f"{where}: a rule on --{bound[-1].field.flag} names itself")
    return tuple(bound)


def _typed(field: FieldInfo, value: object, where: str) -> tuple[object, object]:
    """``value`` as the field would parse it from argv, and its JSON form"""
    if field.flag_type is FlagType.ARRAY:
        raise RegistrationError(f"{where}: requires cannot compare the array --{field.flag}")
    if field.flag_type is FlagType.OBJECT:
        raise RegistrationError(f"{where}: requires cannot compare the object --{field.flag}")
    raw = value.value if isinstance(value, Enum) else value
    if isinstance(raw, bool):
        raw = "true" if raw else "false"
    if not isinstance(raw, (str, int, float)):
        raise RegistrationError(f"{where}: {value!r} is not a value of --{field.flag}")
    try:
        parsed = field.parse(str(raw))
    except ParseError as exc:
        raise RegistrationError(
            f"{where}: {value!r} is not a value of --{field.flag}: {exc}"
        ) from None
    return parsed, jsonable_default(parsed, field.scalar)


def _present(field: FieldInfo, values: Mapping[str, object]) -> bool:
    """Given by the caller: a boolean only when true, anything else when not null"""
    if field.name not in values or values[field.name] is None:
        return False
    return values[field.name] is True if field.flag_type is FlagType.BOOLEAN else True


def check_rules(
    rules: Sequence[BoundRule], values: dict[str, object], failed: set[str]
) -> list[ParseError]:
    """Apply ``DefaultWhenAbsent`` to ``values``, the caller's own, and return every
    violated rule; a flag whose own value failed is left to that error"""
    errors: list[ParseError] = []
    for bound in rules:
        field, rule = bound.field, bound.rule
        if bound.group:
            errors.extend(_check_group(bound, values, failed))
            continue
        if field.flag in failed:
            continue
        match rule:
            case RequiredWhen():
                if not _present(field, values) or values[field.name] != bound.value:
                    continue
                for other in bound.others:
                    if not _present(other, values) and other.flag not in failed:
                        errors.append(
                            ParseError(
                                f"--{field.flag} {bound.json_value} requires --{other.flag}",
                                context={"flag": other.flag, "rule": bound.to_json()},
                                suggestion=f"add --{other.flag}, or change --{field.flag}",
                            )
                        )
            case Excludes():
                if not _present(field, values):
                    continue
                for other in bound.others:
                    if _present(other, values):
                        errors.append(
                            ParseError(
                                f"--{field.flag} and --{other.flag} are mutually exclusive",
                                context={"flag": other.flag, "rule": bound.to_json()},
                                suggestion=f"drop --{other.flag} or --{field.flag}",
                            )
                        )
            case DefaultWhenAbsent():
                [target] = bound.others
                if not _present(field, values) and target.name not in values:
                    values[target.name] = bound.value
    return errors


def _check_group(
    bound: BoundRule, values: Mapping[str, object], failed: set[str]
) -> list[ParseError]:
    """None given fails both kinds; two or more given fails ``RequiresOne``. A flag
    whose own value failed counts as given: the caller meant to pass it"""
    given = [f"--{f.flag}" for f in bound.flags if f.flag in failed or _present(f, values)]
    context: dict[str, object] = {"flags": [f.flag for f in bound.flags], "rule": bound.to_json()}
    if not given:
        suggestion = f"add {_listed(bound.flag_names, 'or')}"
        return [ParseError(bound.describe(), context=context, suggestion=suggestion)]
    if isinstance(bound.rule, RequiresOne) and len(given) > 1:
        return [
            ParseError(
                f"{_listed(given, 'and')} are mutually exclusive; {bound.describe()}",
                context=context,
                suggestion=f"keep one of {_listed(given, 'or')}",
            )
        ]
    return []
