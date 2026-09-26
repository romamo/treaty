"""``Arg`` and ``Flag`` field markers and the ``FieldInfo`` derived from them."""

from __future__ import annotations

import dataclasses
import math
import re
import typing
from dataclasses import MISSING, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ._errors import ParseError, RegistrationError
from ._paths import PATTERN_TYPE, check_path
from ._scalars import ScalarRegistry, ScalarSpec
from ._secrets import source_flags
from ._types import Classified, FlagType, classify

_META = "treaty"
REDACTED = "[REDACTED]"
# REQ-F-034: names containing these are treated as secrets unless declared otherwise
_SECRET_NAME_PARTS = ("token", "secret", "password", "key", "credential", "auth")


@dataclass(frozen=True, slots=True)
class FlagSpec:
    description: str
    positional: bool = False
    short: str | None = None
    pattern: str | None = None
    secret: bool | None = None

    def __post_init__(self) -> None:
        if not self.description:
            raise RegistrationError("every flag needs a description")
        if self.short is not None and len(self.short) != 1:
            raise RegistrationError(f"short flag must be one character, got {self.short!r}")
        if self.pattern is not None:
            re.compile(self.pattern)


def Flag(
    *,
    description: str,
    default: Any = MISSING,
    short: str | None = None,
    pattern: str | None = None,
    secret: bool | None = None,
) -> Any:
    """Declare a named ``--flag`` on an arguments dataclass

    ``secret`` keeps the value out of every error; ``None`` infers it from the name.
    """
    spec = FlagSpec(description, short=short, pattern=pattern, secret=secret)
    if isinstance(default, (list, dict, set)):
        raise RegistrationError("mutable defaults are not allowed; use a tuple")
    if default is MISSING:
        return field(metadata={_META: spec})
    return field(default=default, metadata={_META: spec})


def Arg(*, description: str, pattern: str | None = None, secret: bool | None = None) -> Any:
    """Declare a positional argument on an arguments dataclass"""
    spec = FlagSpec(description, positional=True, pattern=pattern, secret=secret)
    return field(metadata={_META: spec})


@dataclass(frozen=True, slots=True)
class FieldInfo:
    name: str
    flag: str
    classified: Classified
    required: bool
    default: object
    spec: FlagSpec

    @property
    def flag_type(self) -> FlagType:
        return self.classified.flag_type

    @property
    def positional(self) -> bool:
        return self.spec.positional

    @property
    def path(self) -> bool:
        """True for ``Path`` fields and arrays of them"""
        item = self.classified.item
        return self.classified.path or (item is not None and item.path)

    @property
    def scalar(self) -> ScalarSpec | None:
        """The registered custom scalar of the field or its array items, if any"""
        item = self.classified.item
        return self.classified.scalar if item is None else item.scalar

    @property
    def secret(self) -> bool:
        """Declared or name-inferred; a boolean can never be a secret"""
        if self.spec.secret is not None:
            return self.spec.secret
        if self.flag_type is FlagType.BOOLEAN:
            return False
        lowered = self.name.lower()
        return any(part in lowered for part in _SECRET_NAME_PARTS)

    @property
    def env_flag(self) -> str:
        return source_flags(self.flag)[0]

    @property
    def file_flag(self) -> str:
        return source_flags(self.flag)[1]

    def exposed_flags(self) -> tuple[str, ...]:
        """Flag names the parser accepts for this field: the secret variants, or the name"""
        return source_flags(self.flag) if self.secret else (self.flag,)

    def scrub(self, exc: ParseError) -> ParseError:
        """Replace an echoed value with ``[REDACTED]`` when this field holds a secret"""
        if self.secret and "value" in exc.context:
            exc.context["value"] = REDACTED
        if self.secret and exc.suggestion is not None:
            # A suggestion rebuilds the value (a decoded or resolved path)
            exc.suggestion = f"fix the value behind --{self.env_flag} or --{self.file_flag}"
        return exc

    def parse(self, raw: str) -> object:
        """Coerce one raw token into the field's scalar or array item type"""
        target = self.classified.item if self.flag_type is FlagType.ARRAY else self.classified
        if target is None:
            raise RegistrationError(f"{self.name}: array without item type")
        try:
            self.check_pattern(raw)
            return _coerce(target, raw, self.flag, secret=self.secret)
        except ParseError as exc:
            raise self.scrub(exc) from None

    def check_pattern(self, raw: str) -> None:
        """``Flag(pattern=)`` for argv tokens and JSON strings alike (REQ-C-020)"""
        if self.spec.pattern is not None and not re.fullmatch(self.spec.pattern, raw):
            raise ParseError(
                f"value for {self.flag!r} does not match pattern",
                context={"flag": self.flag, "value": raw, "pattern": self.spec.pattern},
            )

    def to_flag_entries(self) -> dict[str, dict[str, object]]:
        """Manifest entries keyed by exposed flag; a secret shows only its two sources"""
        if not self.secret:
            return {self.flag: self.to_flag_entry()}
        what = self.spec.description
        return {
            self.env_flag: {
                "type": "string",
                "required": False,
                "description": f"Name of the environment variable holding: {what}",
            },
            self.file_flag: {
                "type": "string",
                "required": False,
                "description": f"Path of the file holding: {what}",
                "pattern_type": PATTERN_TYPE,
            },
        }

    def to_flag_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "type": self.flag_type.value,
            "required": self.required,
            "description": self.spec.description,
        }
        if self.default is not MISSING and self.default is not None:
            entry["default"] = _jsonable_default(self.default, self.scalar)
        if self.flag_type is FlagType.ENUM:
            entry["enum_values"] = list(self.classified.enum_values)
        if self.spec.short is not None:
            entry["short"] = self.spec.short
        if self.spec.pattern is not None:
            entry["pattern"] = self.spec.pattern
        if self.path:
            entry["pattern_type"] = PATTERN_TYPE
        if (scalar := self.scalar) is not None:
            if scalar.pattern is not None:
                entry["pattern"] = scalar.pattern
            if scalar.pattern_type is not None:
                entry["pattern_type"] = scalar.pattern_type
        return entry

    def to_positional_entry(self) -> dict[str, object]:
        """``PositionalEntry`` (ManifestResponse 3.0): an array positional takes the rest"""
        variadic = self.flag_type is FlagType.ARRAY
        target = self.classified.item if variadic else self.classified
        assert target is not None, "array fields always carry an item type"
        entry: dict[str, object] = {
            "name": self.name,
            "type": target.flag_type.value,
            "required": self.required,
            "description": self.spec.description,
        }
        if target.flag_type is FlagType.ENUM:
            entry["enum_values"] = list(target.enum_values)
        if variadic:
            entry["variadic"] = True
        return entry


def _jsonable_default(value: object, scalar: ScalarSpec | None) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable_default(v, scalar) for v in value]
    if scalar is not None and isinstance(value, scalar.cls):
        return _jsonable_default(scalar.serialize(value), None)
    if isinstance(value, float) and not math.isfinite(value):
        raise RegistrationError(f"default {value!r} is not a finite number")
    if value is not None and not isinstance(value, (bool, int, float, str)):
        raise RegistrationError(f"default {value!r} cannot be listed in the manifest")
    return value


def apply_scalar(
    spec: ScalarSpec, base_value: object, flag: str, *, secret: bool = False
) -> object:
    """Check the declared constraint, then hand the base value to the registered parser

    A parser's message may quote the value, so a secret's error leaves it out.
    """
    ctx: dict[str, object] = {"flag": flag, "value": base_value, "scalar": spec.cls.__name__}
    violation = spec.violation(base_value)
    if violation is not None:
        problem, detail = violation
        raise ParseError(f"value for {flag!r} {problem}", context={**ctx, **detail})
    try:
        return spec.parse(base_value)
    except (TypeError, ValueError) as exc:
        if secret:
            raise ParseError(
                f"value for {flag!r} is not a valid {spec.cls.__name__}", context=ctx
            ) from None
        raise ParseError(
            f"value for {flag!r} is not a valid {spec.cls.__name__}: {exc}",
            context={**ctx, "cause": str(exc)},
        ) from None
    except Exception as exc:  # noqa: BLE001 - a scalar's parse= is user code
        # Not a declared rejection, but still nothing ran: exit 2 naming the parser's error
        raise ParseError(
            f"the {spec.cls.__name__} parser for {flag!r} raised {type(exc).__name__}",
            context={**ctx, "value": REDACTED if secret else base_value},
            suggestion=f"{spec.cls.__name__}'s parse= should raise ValueError for bad input",
        ) from None


def _coerce(target: Classified, raw: str, flag: str, *, secret: bool) -> object:
    base = _coerce_base(target, raw, flag)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, flag, secret=secret)


def _coerce_base(target: Classified, raw: str, flag: str) -> object:
    match target.flag_type:
        case FlagType.STRING:
            return check_path(raw, flag) if target.path else raw
        case FlagType.INTEGER:
            try:
                return int(raw)
            except ValueError:
                raise ParseError(
                    f"{flag!r} expects an integer", context={"flag": flag, "value": raw}
                ) from None
        case FlagType.NUMBER:
            try:
                number = float(raw)
            except ValueError:
                raise ParseError(
                    f"{flag!r} expects a number", context={"flag": flag, "value": raw}
                ) from None
            if not math.isfinite(number):
                # JSON has no NaN or Infinity, and they defeat minimum/maximum checks
                raise ParseError(
                    f"{flag!r} expects a finite number", context={"flag": flag, "value": raw}
                )
            return number
        case FlagType.ENUM:
            if raw not in target.enum_values:
                raise ParseError(
                    f"{flag!r} must be one of {', '.join(target.enum_values)}",
                    context={"flag": flag, "value": raw, "allowed": list(target.enum_values)},
                )
            return target.enum_cls(raw) if target.enum_cls is not None else raw
        case FlagType.BOOLEAN:
            lowered = raw.lower()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no"):
                return False
            raise ParseError(
                f"{flag!r} expects true or false", context={"flag": flag, "value": raw}
            )
        case FlagType.ARRAY:
            raise RegistrationError("nested arrays are not supported")


def _check_secret_field(cls: type, info: FieldInfo) -> None:
    """REQ-C-016: a secret is a named scalar that arrives via env var or file, never argv"""
    where = f"{cls.__qualname__}.{info.name}"
    how = "declared secret" if info.spec.secret else "inferred a secret from its name"
    if info.positional:
        raise RegistrationError(
            f"{where}: {how}; secrets cannot be positional, declare it with "
            f"Flag(...) to get --{info.env_flag} and --{info.file_flag} (REQ-C-016)"
        )
    if info.flag_type in (FlagType.BOOLEAN, FlagType.ARRAY):
        raise RegistrationError(
            f"{where}: {how}, but a {info.flag_type.value} cannot hold a secret; pass secret=False"
        )
    if info.spec.short is not None:
        raise RegistrationError(f"{where}: a secret cannot have a short flag")


_POSITIONAL_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _check_flag_names(cls: type, infos: list[FieldInfo]) -> None:
    """Flags the parser derives must not collide with another field's flag"""
    flags = {i.flag: i for i in infos}
    for info in infos:
        derived: list[str] = []
        if info.flag_type is FlagType.BOOLEAN:
            derived.append(f"no-{info.flag}")  # --no-cache negates cache
        if info.secret:
            derived.extend((info.env_flag, info.file_flag))
        for flag in derived:
            if flag in flags and flags[flag] is not info:
                raise RegistrationError(
                    f"{cls.__qualname__}: field {flags[flag].name!r} takes --{flag}, which "
                    f"{info.name!r} needs; rename one of them"
                )


def _check_positionals(cls: type, positionals: list[FieldInfo]) -> None:
    """Layouts the parser and ``PositionalEntry`` can express: one variadic, last"""
    for info in positionals:
        if not _POSITIONAL_NAME.fullmatch(info.name):
            raise RegistrationError(
                f"{cls.__qualname__}.{info.name}: positional names are lowercase letters, "
                "digits, and underscores, starting with a letter"
            )
    variadic = [i for i in positionals if i.flag_type is FlagType.ARRAY]
    if len(variadic) > 1 or (variadic and positionals[-1] is not variadic[0]):
        raise RegistrationError(
            f"{cls.__qualname__}.{variadic[0].name}: an array positional takes every "
            "remaining value, so only the last positional may be one"
        )


def inspect_fields(cls: type, scalars: ScalarRegistry) -> tuple[FieldInfo, ...]:
    """Read an arguments dataclass into ordered ``FieldInfo`` records"""
    if not dataclasses.is_dataclass(cls):
        raise RegistrationError(f"{cls.__qualname__} must be a dataclass")
    hints = typing.get_type_hints(cls)
    infos: list[FieldInfo] = []
    seen_optional_positional = False
    for f in dataclasses.fields(cls):
        spec = f.metadata.get(_META)
        if not isinstance(spec, FlagSpec):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: declare fields with Flag(...) or Arg(...)"
            )
        classified = classify(hints[f.name], scalars)
        item = classified.item
        if spec.pattern is not None and (classified.path or (item is not None and item.path)):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: Path fields get the filepath preset; "
                "pattern= is not allowed on them (REQ-C-020)"
            )
        scalar = classified.scalar if item is None else item.scalar
        if spec.pattern is not None and scalar is not None:
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: {scalar.cls.__qualname__} declares its own "
                "constraints in app.scalar(...); pattern= is not allowed on it"
            )
        default: object = f.default
        if f.default_factory is not MISSING:
            raise RegistrationError(f"{cls.__qualname__}.{f.name}: default_factory is not allowed")
        if classified.flag_type is FlagType.BOOLEAN:
            if spec.positional:
                raise RegistrationError(
                    f"{cls.__qualname__}.{f.name}: booleans cannot be positional"
                )
            if default is MISSING:
                raise RegistrationError(
                    f"{cls.__qualname__}.{f.name}: boolean flags need a default"
                )
        if spec.positional and (classified.optional or default is not MISSING):
            seen_optional_positional = True
        elif spec.positional and seen_optional_positional:
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: required positional after optional positional"
            )
        required = default is MISSING and not classified.optional
        info = FieldInfo(
            name=f.name,
            flag=f.name.replace("_", "-"),
            classified=classified,
            required=required,
            default=default,
            spec=spec,
        )
        if info.secret:
            _check_secret_field(cls, info)
        infos.append(info)
    _check_positionals(cls, [i for i in infos if i.positional])
    _check_flag_names(cls, infos)
    shorts = [i.spec.short for i in infos if i.spec.short is not None]
    if len(shorts) != len(set(shorts)):
        raise RegistrationError(f"{cls.__qualname__}: duplicate short flags {shorts}")
    return tuple(infos)
