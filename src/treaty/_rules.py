"""Conditional argument rules (REQ-C-026): cross-field requirements the manifest can show.

``requires=[RequiredWhen("format", "csv", then=("separator",))]`` says what an args
``__post_init__`` would otherwise say only on a failing call. The rules are checked in
phase 1 on the fields the caller supplied, before ``__post_init__`` runs, so a
``DefaultWhenAbsent`` default is what it sees.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ._errors import ParseError, RegistrationError
from ._flags import FieldInfo, jsonable_default
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


Rule = RequiredWhen | Excludes | DefaultWhenAbsent


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

    def to_json(self) -> dict[str, object]:
        """The manifest ``ConditionalRule``"""
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
            case _:
                raise RegistrationError(
                    f"{where}: requires takes RequiredWhen, Excludes, or DefaultWhenAbsent, "
                    f"not {rule!r}"
                )
        if any(o is bound[-1].field for o in bound[-1].others):
            raise RegistrationError(f"{where}: a rule on --{bound[-1].field.flag} names itself")
    return tuple(bound)


def _typed(field: FieldInfo, value: object, where: str) -> tuple[object, object]:
    """``value`` as the field would parse it from argv, and its JSON form"""
    if field.flag_type is FlagType.ARRAY:
        raise RegistrationError(f"{where}: requires cannot compare the array --{field.flag}")
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
