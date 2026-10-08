"""``Arg`` and ``Flag`` field markers and the ``FieldInfo`` derived from them."""

from __future__ import annotations

import dataclasses
import keyword
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._deprecation import Deprecated
from ._envnames import EnvName, env_names, env_var_entries
from ._errors import ParseError, RegistrationError, SchemaError
from ._paths import PATTERN_TYPE, check_path
from ._redact import REDACTED, secret_name
from ._scalars import (
    BUILT_IN_TEXT,
    PATTERN_TYPES,
    PRESET_PATTERNS,
    ScalarRegistry,
    ScalarSpec,
    anchored,
    built_in,
    check_pattern_publishable,
    matches_preset,
)
from ._secrets import source_flags
from ._types import Classified, FlagType, ObjectHook, classify, type_hints

if TYPE_CHECKING:
    from ._schema import JsonSchema

_META = "treaty"
FLAG_META = _META
"""The field metadata key a Flag or Arg declaration is stored under"""
UNAUDITED = "omitted from the audit log"
"""How a manifest entry, which allows no extra keys, marks a field declared ``audit=False``"""
# REQ-F-044: characters refused in text values, by their rejected_pattern name
_CONTROL_CHARS = {"\n": "newline", "\r": "carriage_return", "\x00": "null_byte"}


@dataclass(frozen=True, slots=True)
class FlagSpec:
    description: str
    positional: bool = False
    short: str | None = None
    pattern: str | None = None
    secret: bool | None = None
    multiline: bool = False
    max_bytes: int | None = None
    pattern_type: str | None = None
    """A REQ-C-020 preset such as ``alphanumeric_id``, without registering a scalar"""
    from_stdin: bool = False
    """The literal ``-`` reads the value from stdin (REQ-O-006)"""
    deprecated: Deprecated | None = None
    """Still accepted, with a ``DEPRECATED_FLAG`` warning naming the replacement (REQ-F-075)"""
    audit: bool = True
    """False writes the value as ``[OMITTED]`` in the audit log; it is still a plain value"""
    dry_run: bool = False
    """The command's dry-run switch under its own name, such as a wrapped tool's ``--check``"""
    confirm: bool = False
    """The inverse switch: the command previews, as a dry run, unless this flag is set"""
    env: tuple[EnvName, ...] = ()
    """Variables read after ``<APP>_<NAME>``, in order, such as ``BEANCOUNT_FILE``"""

    def __post_init__(self) -> None:
        if not self.description:
            raise RegistrationError("every flag needs a description")
        if not isinstance(self.audit, bool):
            raise RegistrationError(f"audit is True or False, not {self.audit!r}")
        if not isinstance(self.dry_run, bool):
            raise RegistrationError(f"dry_run is True or False, not {self.dry_run!r}")
        if not isinstance(self.confirm, bool):
            raise RegistrationError(f"confirm is True or False, not {self.confirm!r}")
        if self.confirm and self.dry_run:
            raise RegistrationError(
                "confirm=True and dry_run=True are opposite switches; a flag is one of them"
            )
        if self.confirm and self.env:
            # A variable left in the environment would confirm every later run unseen
            raise RegistrationError(
                "confirm=True takes no env=: the confirmation is passed on each run, never "
                "read from a variable"
            )
        if self.pattern_type is not None and self.pattern_type not in PATTERN_TYPES:
            raise RegistrationError(
                f"pattern_type={self.pattern_type!r} is not one of "
                f"{', '.join(sorted(PATTERN_TYPES))}; Path fields get filepath on their own"
            )
        if self.pattern is not None and self.pattern_type is not None:
            raise RegistrationError("pattern and pattern_type are mutually exclusive")
        if self.deprecated is not None and not isinstance(self.deprecated, Deprecated):
            raise RegistrationError("deprecated takes treaty.Deprecated(since=...)")
        if self.max_bytes is not None and (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 1
        ):
            raise RegistrationError(f"max_bytes is a positive whole number, not {self.max_bytes!r}")
        if self.short is not None and len(self.short) != 1:
            raise RegistrationError(f"short flag must be one character, got {self.short!r}")
        if self.pattern is not None:
            check_pattern_publishable(self.pattern, "Flag")


def _field(spec: FlagSpec, default: object) -> Any:
    """The dataclass field carrying ``spec``, with ``default`` when one is given"""
    if isinstance(default, (list, dict, set)):
        raise RegistrationError(
            "mutable defaults are not allowed; use a tuple, or types.MappingProxyType(...) "
            "for a mapping"
        )
    if default is MISSING:
        return field(metadata={_META: spec})
    return field(default=default, metadata={_META: spec})


def Flag(
    *,
    description: str,
    default: Any = MISSING,
    short: str | None = None,
    pattern: str | None = None,
    secret: bool | None = None,
    multiline: bool = False,
    max_bytes: int | None = None,
    pattern_type: str | None = None,
    from_stdin: bool = False,
    deprecated: Deprecated | None = None,
    audit: bool = True,
    dry_run: bool = False,
    confirm: bool = False,
    env: Sequence[str | EnvName] = (),
) -> Any:
    """Declare a named ``--flag`` on an arguments dataclass

    ``pattern`` is a regex the whole value must match; ``pattern_type`` is a preset
    instead: ``alphanumeric_id``, ``uuid``, ``semver``, or ``url`` (REQ-C-020).
    ``secret`` keeps the value out of every error; ``None`` infers it from the name.
    ``multiline`` lets a text field such as a message body contain newlines.
    ``max_bytes`` is the most UTF-8 bytes a text value may have, such as a backend
    column's size: a longer one exits 2 with ``FIELD_TOO_LARGE`` before the handler runs.
    ``from_stdin=True`` makes ``--flag -`` read the value from stdin, one item per line
    for an array, as in ``tool get --format id | tool delete --id -``.
    ``deprecated=Deprecated("1.4.0", replacement="new-flag")`` keeps the flag working
    with a warning on every run that passes it.
    ``audit=False`` writes the value as ``[OMITTED]`` in the audit log, such as a message
    body too private to keep; unlike ``secret``, argv and error messages still carry it.
    ``dry_run=True`` on a boolean makes it the command's dry run in place of a field
    named ``dry_run``, so a wrapper keeps the tool's own ``--check`` or ``--noop``.
    ``confirm=True`` on a boolean, such as ``yes: bool = Flag(confirm=True, ...)``, is the
    inverse: a mutating command previews unless it is passed, with ``meta.dry_run`` true
    and a ``would_*`` effect as under ``--dry-run``; its default is False.
    ``env=("IBKR_FLEX_TOKEN",)`` reads those variables, in order, when the flag is not
    passed, after ``<APP>_<NAME>``, which a flag or setting that declares ``env`` reads
    first (REQ-F-073). ``EnvName("OLD", deprecated=Deprecated("1.4.0"))`` still reads one, with a
    warning naming what to use instead.
    """
    spec = FlagSpec(
        description,
        short=short,
        pattern=pattern,
        secret=secret,
        multiline=multiline,
        max_bytes=max_bytes,
        pattern_type=pattern_type,
        from_stdin=from_stdin,
        deprecated=deprecated,
        audit=audit,
        dry_run=dry_run,
        confirm=confirm,
        env=env_names(env),
    )
    if confirm and default is MISSING:
        default = False  # unconfirmed is the preview
    if confirm and default is not False:
        raise RegistrationError(
            f"confirm=True makes the command preview unless the flag is passed, so its "
            f"default is False, not {default!r}"
        )
    return _field(spec, default)


def Arg(
    *,
    description: str,
    default: Any = MISSING,
    pattern: str | None = None,
    secret: bool | None = None,
    pattern_type: str | None = None,
    from_stdin: bool = False,
    multiline: bool = False,
    audit: bool = True,
) -> Any:
    """Declare a positional argument on an arguments dataclass

    ``default`` makes it optional, as in ``query: str | None = Arg(default=None, ...)``:
    an optional positional may follow a required one, never precede it.
    ``from_stdin=True`` makes the literal ``-`` read it from stdin, ``multiline``
    lets it contain newlines, and ``audit=False`` keeps it out of the audit log, as on
    ``Flag``.
    """
    spec = FlagSpec(
        description,
        positional=True,
        pattern=pattern,
        secret=secret,
        pattern_type=pattern_type,
        from_stdin=from_stdin,
        multiline=multiline,
        audit=audit,
    )
    return _field(spec, default)


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
    def key(self) -> str:
        """The field's key in a JSON payload (an exec line, an MCP call): the flag with
        underscores, less a keyword's trailing ``_``"""
        return self.flag.replace("-", "_")

    @property
    def positional(self) -> bool:
        return self.spec.positional

    @property
    def shown(self) -> str:
        """The field as argv takes it, for ``--help`` and messages: ``<query>`` for a
        positional, ``--figi`` for a flag (#331)"""
        return f"<{self.flag}>" if self.positional else f"--{self.flag}"

    @property
    def spelled(self) -> str:
        """Every way argv takes the field, for a message about it (#358): ``<query>``
        for a positional, ``--name/-n`` for a flag with a short form, and a secret's
        ``--token-from-env/--token-from-file``"""
        if self.positional:
            return self.shown
        if self.secret:
            return "/".join(f"--{f}" for f in self.exposed_flags())
        return f"--{self.flag}" + (f"/-{self.spec.short}" if self.spec.short else "")

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
        """Declared or name-inferred; a boolean can never be a secret, and an enum's values
        are public, so neither is inferred one"""
        if self.spec.secret is not None:
            return self.spec.secret
        item = self.classified.item
        public = (FlagType.BOOLEAN, FlagType.ENUM, FlagType.OBJECT)
        if self.classified.is_map:
            # A mapping's values carry no names of their own, so its name speaks for them,
            # as a tuple's does: api_keys: dict[str, str] holds secrets (#299)
            if all(v.flag_type in public or v.int_values for v in self.classified.values):
                return False
            return secret_name(self.name)
        target = self.classified if item is None else item
        if target.flag_type in public or target.int_values:
            return False  # an object's own fields say which of them hold a secret
        return secret_name(self.name)

    @property
    def object_type(self) -> Classified | None:
        """The object type of an object field, or of an array field's items"""
        target = self.classified.item if self.flag_type is FlagType.ARRAY else self.classified
        return target if target is not None and target.flag_type is FlagType.OBJECT else None

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
            if target.flag_type in (FlagType.INTEGER, FlagType.NUMBER):
                # The number's own text, as the JSON route checks it: 1.50 is 1.5 on both
                value = coerce_text(target, raw, self.flag, secret=self.secret)
                self.check_pattern(str(value))
                return value
            if target.flag_type is FlagType.STRING:
                self.check_text(raw)
            self.check_pattern(raw)
            return coerce_text(target, raw, self.flag, secret=self.secret)
        except ParseError as exc:
            raise self.scrub(exc) from None

    def check_text(self, raw: str) -> None:
        """REQ-F-044: a line break or null byte in a text value is refused in phase 1, since
        it can end a command line or a log record wherever the value is passed on. A
        ``multiline`` field accepts line breaks; a secret, never echoed or passed as an
        argument, is exempt."""
        self.check_size(raw)
        if self.secret:
            return
        refuse_line_breaks(self.flag, raw, multiline=self.spec.multiline)

    def check_size(self, raw: str) -> None:
        """``Flag(max_bytes=)``: the error names the sizes, never the value (REQ-F-064)"""
        limit = self.spec.max_bytes
        if limit is None:
            return
        size = len(raw.encode("utf-8", "surrogatepass"))
        if size > limit:
            raise ParseError(
                f"value for {self.flag!r} is {size} bytes, over its limit of {limit}",
                code="FIELD_TOO_LARGE",
                context={"field": self.flag, "max_bytes": limit, "actual_bytes": size},
                suggestion=f"shorten --{self.flag} to at most {limit} UTF-8 bytes",
            )

    def check_pattern(self, raw: str) -> None:
        """``Flag(pattern=)`` and ``Flag(pattern_type=)`` for argv tokens and JSON strings
        alike; the error names the flag and what it expects (REQ-C-020)"""
        if self.spec.pattern is not None and not re.fullmatch(self.spec.pattern, raw):
            raise ParseError(
                f"value for {self.flag!r} does not match pattern",
                context={"flag": self.flag, "value": raw, "pattern": self.spec.pattern},
            )
        preset = self.spec.pattern_type
        if preset is not None and not matches_preset(preset, raw):
            context: dict[str, object] = {"flag": self.flag, "value": raw, "pattern_type": preset}
            if preset in PRESET_PATTERNS:
                context["pattern"] = anchored(PRESET_PATTERNS[preset])
            raise ParseError(
                f"value for {self.flag!r} does not match the {preset} pattern",
                context=context,
                suggestion=_PRESET_HINTS[preset],
            )

    def to_flag_entries(
        self, own_env: str | None, prop: JsonSchema
    ) -> dict[str, dict[str, object]]:
        """Manifest entries keyed by exposed flag; a secret shows only its two sources.
        ``own_env`` is the ``<APP>_<NAME>`` a plain flag that declares ``env`` reads first;
        ``prop`` is the field's property in the args schema"""
        if not self.secret:
            return {self.flag: self.to_flag_entry(own_env, prop)}
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

    @property
    def int_choices(self) -> str | None:
        """An integer ``Literal``'s values as the entry's description states them (#327);
        ManifestResponse 3.18's integer ``enum_values`` are optional, and not emitted"""
        target = self.classified.item if self.flag_type is FlagType.ARRAY else self.classified
        if target is None or not target.int_values:
            return None
        each = "each " if target is self.classified.item else ""
        return f"{each}one of {', '.join(map(str, target.int_values))}"

    def to_flag_entry(self, own_env: str | None, prop: JsonSchema) -> dict[str, object]:
        description = self.spec.description
        if self.spec.multiline:
            # FlagEntry allows no extra keys, so the opt-out is stated in the description
            description = f"{description} (may contain newlines)"
        if self.spec.max_bytes is not None:
            description = f"{description} (at most {self.spec.max_bytes} bytes)"
        if self.spec.from_stdin:
            description = f"{description} (- reads it from stdin)"
        if (choices := self.int_choices) is not None:
            description = f"{description} ({choices})"
        if not self.spec.audit:
            description = f"{description} ({UNAUDITED})"
        if (old := self.spec.deprecated) is not None:
            instead = "" if old.replacement is None else f"; use --{old.replacement}"
            description = f"{description} (deprecated since {old.since}{instead})"
        entry: dict[str, object] = {
            "type": self.flag_type.value,
            # A variable may supply it, as --x-from-env does a secret's
            "required": self.required and not self.spec.env,
            "description": description,
        }
        if self.object_type is not None:
            # ManifestResponse 3.7: one argv token of JSON text, as the schema describes it;
            # an array flag's schema is one item's
            entry["schema"] = object_schema(
                prop["items"] if self.flag_type is FlagType.ARRAY else prop, self.flag
            )
        if self.spec.env:
            # ManifestResponse 3.4: in precedence order, the prefixed name first (REQ-F-073)
            if own_env is None:
                raise RegistrationError(f"--{self.flag} declares env= but reads no <APP>_<NAME>")
            entry["env_vars"] = env_var_entries(own_env, self.spec.env)
        if self.default is not MISSING and self.default is not None:
            entry["default"] = jsonable_value(self.default, self.classified)
        if self.flag_type is FlagType.ENUM:
            entry["enum_values"] = list(self.classified.enum_values)
        if self.spec.short is not None:
            entry["short"] = self.spec.short
        if self.spec.pattern is not None:
            entry["pattern"] = anchored(self.spec.pattern)
        if self.spec.pattern_type is not None:
            entry["pattern_type"] = self.spec.pattern_type
        if self.path:
            entry["pattern_type"] = PATTERN_TYPE
        if (scalar := self.scalar) is not None:
            if scalar.pattern is not None:
                entry["pattern"] = anchored(scalar.pattern)
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
            "description": self.spec.description
            + ("" if (choices := self.int_choices) is None else f" ({choices})")
            + (" (- reads it from stdin)" if self.spec.from_stdin else "")
            + ("" if self.spec.audit else f" ({UNAUDITED})"),
        }
        if target.flag_type is FlagType.ENUM:
            entry["enum_values"] = list(target.enum_values)
        if variadic:
            entry["variadic"] = True
        return entry


def object_schema(prop: JsonSchema, flag: str) -> JsonSchema:
    """The object schema of one value of an object flag: an optional field's property
    is ``anyOf`` the object and null, and argv never passes null"""
    branches = prop.get("anyOf")
    if isinstance(branches, list):
        kept = [b for b in branches if b != {"type": "null"}]
        if len(kept) != 1:
            raise RegistrationError(f"--{flag}: its schema has no single object branch: {prop}")
        [prop] = kept
    if prop.get("type") != "object":
        raise RegistrationError(f"--{flag}: its schema is not an object: {prop}")
    return prop


_PRESET_HINTS = {
    "alphanumeric_id": "use letters, digits, - and _ only, starting with a letter or digit; "
    "no /, ., ?, #, or %",
    "uuid": "pass a UUID such as 123e4567-e89b-12d3-a456-426614174000",
    "semver": "pass a version such as 1.2.3",
    "url": "pass an http or https URL with a host",
}


def object_shape(target: Classified) -> str:
    """An object's keys and their types for help and the manifest, as in
    ``{account: string, number: decimal, memo?: string}``; ``?`` marks an optional key"""
    if target.is_map:
        return "{<key>: " + "|".join(object_shape(v) for v in target.values) + "}"
    if target.flag_type is FlagType.OBJECT:
        keys = (
            f"{m.name}{'' if m.required else '?'}: {object_shape(m.classified)}"
            for m in target.members
        )
        return "{" + ", ".join(keys) + "}"
    if target.flag_type is FlagType.ARRAY:
        assert target.item is not None, "array fields always carry an item type"
        return f"[{object_shape(target.item)}]"
    if target.flag_type is FlagType.ENUM:
        return "|".join(target.enum_values)
    if target.int_values:
        return "|".join(map(str, target.int_values))
    if built_in(target.scalar):
        assert target.scalar is not None
        return BUILT_IN_TEXT[target.scalar.cls][2]
    return "path" if target.path else target.flag_type.value


def jsonable_value(value: object, target: Classified) -> object:
    """A default as JSON, each field of an object by its own type"""
    if value is None:
        return None
    if target.flag_type is FlagType.ARRAY and target.item is not None:
        assert isinstance(value, tuple)
        return [jsonable_value(v, target.item) for v in value]
    if target.is_map:
        assert isinstance(value, Mapping)
        return {k: jsonable_value(v, map_branch(target, v)) for k, v in value.items()}
    if target.flag_type is FlagType.OBJECT:
        return {
            m.name: jsonable_value(getattr(value, m.name), m.classified) for m in target.members
        }
    return jsonable_default(value, target.scalar)


def map_branch(target: Classified, value: object) -> Classified:
    """The value type of a mapping that a parsed value belongs to: a registered scalar by
    its class, else the first, since the others serialize by the value's own type"""
    for branch in target.values:
        if branch.scalar is not None and isinstance(value, branch.scalar.cls):
            return branch
    return target.values[0]


def jsonable_default(value: object, scalar: ScalarSpec | None) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [jsonable_default(v, scalar) for v in value]
    if scalar is not None and isinstance(value, scalar.cls):
        return jsonable_default(scalar.serialize(value), None)
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
        if built_in(spec):
            what, hint, _ = BUILT_IN_TEXT[spec.cls]
            raise ParseError(
                f"value for {flag!r} is not {what}", context={**ctx, **detail}, suggestion=hint
            )
        raise ParseError(f"value for {flag!r} {problem}", context={**ctx, **detail})
    try:
        return spec.parse(base_value)
    except ParseError as exc:
        # The parser's own rejection: its message and suggestion are the ones to show,
        # unless they could quote a secret
        if secret:
            raise ParseError(
                f"value for {flag!r} is not a valid {spec.cls.__name__}", context=ctx
            ) from None
        exc.context.setdefault("flag", flag)  # so the field is not also reported missing
        raise
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


_INTEGER = re.compile(r"-?[0-9]+")
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?")
"""JSON's number, leading zeros allowed: argv has no reason to refuse ``007``"""


def coerce_text(target: Classified, raw: str, flag: str, *, secret: bool) -> object:
    base = _coerce_base(target, raw, flag)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, flag, secret=secret)


def _coerce_base(target: Classified, raw: str, flag: str) -> object:
    match target.flag_type:
        case FlagType.STRING:
            return check_path(raw, flag) if target.path else raw
        case FlagType.INTEGER:
            # ASCII digits only, as JSON writes them: int() also takes 1_000, " 7", +3, and
            # other scripts' digits, which the published schema does not
            if not _INTEGER.fullmatch(raw):
                raise ParseError(
                    f"{flag!r} expects an integer", context={"flag": flag, "value": raw}
                )
            return check_int_value(target, int(raw), flag)
        case FlagType.NUMBER:
            if not _NUMBER.fullmatch(raw):
                raise ParseError(f"{flag!r} expects a number", context={"flag": flag, "value": raw})
            number = float(raw)
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
        case FlagType.OBJECT:
            raise ParseError(f"{flag!r} expects a JSON object", context={"flag": flag})


def check_int_value(target: Classified, value: int, flag: str) -> int:
    """``value`` when the field takes any integer or an integer ``Literal`` holds it"""
    if target.int_values and value not in target.int_values:
        allowed = list(target.int_values)
        raise ParseError(
            f"{flag!r} must be one of {', '.join(map(str, allowed))}",
            context={"flag": flag, "value": value, "allowed": allowed},
        )
    return value


def refuse_line_breaks(flag: str, raw: str, *, multiline: bool = False) -> None:
    """REQ-F-044's check of one text value at ``flag``: no null byte, and no line break
    unless ``multiline``"""
    for char, name in _CONTROL_CHARS.items():
        if char in raw and not (multiline and char in "\n\r"):
            raise ParseError(
                f"value for {flag!r} contains a {name.replace('_', ' ')}",
                context={"flag": flag, "value": raw, "rejected_pattern": name},
                suggestion=f"pass --{flag} as a single line",
            )


def _check_secret_field(cls: type, info: FieldInfo) -> None:
    """REQ-C-016: a secret is a named scalar that arrives via env var or file, never argv"""
    where = f"{cls.__qualname__}.{info.name}"
    how = "declared secret" if info.spec.secret else "inferred a secret from its name"
    if info.positional:
        raise RegistrationError(
            f"{where}: {how}; secrets cannot be positional, declare it with "
            f"Flag(...) to get --{info.env_flag} and --{info.file_flag}, or pass "
            "secret=False when it holds no secret (REQ-C-016)"
        )
    if info.flag_type in (FlagType.BOOLEAN, FlagType.ARRAY, FlagType.OBJECT):
        raise RegistrationError(
            f"{where}: {how}, but a {info.flag_type.value} cannot hold a secret; pass secret=False"
        )
    if info.spec.short is not None:
        raise RegistrationError(f"{where}: a secret cannot have a short flag")


_POSITIONAL_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _checked_default(target: Classified, default: object, where: str) -> object:
    """A default of the field's own type, so the manifest never publishes a contradiction;
    an enum's value becomes its member, as a parsed argument would"""
    if default is None and target.optional:
        return None
    if target.scalar is not None:
        if not isinstance(default, target.scalar.cls):
            raise RegistrationError(
                f"{where}: default {default!r} is not a {target.scalar.cls.__qualname__}"
            )
        if built_in(target.scalar):
            # As its text, so the default is what a parsed argument would be: -0 is 0, and
            # a datetime is no date default, nor a naive one a datetime default
            text = target.scalar.serialize(default)
            if target.scalar.violation(text):
                what = BUILT_IN_TEXT[target.scalar.cls][0]
                raise RegistrationError(f"{where}: default {default!r} is not {what}")
            return target.scalar.parse(text)
        return default
    if target.is_map:
        return _checked_map_default(target, default, where)
    match target.flag_type:
        case FlagType.OBJECT:
            if not isinstance(default, target.object_cls):
                raise RegistrationError(
                    f"{where}: default {default!r} is not a {target.object_cls.__qualname__}"
                )
            return default
        case FlagType.ARRAY:
            assert target.item is not None
            if not isinstance(default, tuple):
                raise RegistrationError(f"{where}: an array default must be a tuple")
            return tuple(_checked_default(target.item, d, where) for d in default)
        case FlagType.ENUM:
            value = default.value if isinstance(default, Enum) else default
            if value not in target.enum_values:
                raise RegistrationError(
                    f"{where}: default {default!r} is not one of {list(target.enum_values)}"
                )
            return target.enum_cls(value) if target.enum_cls is not None else value
        case FlagType.BOOLEAN:
            ok = isinstance(default, bool)
        case FlagType.INTEGER:
            ok = isinstance(default, int) and not isinstance(default, bool)
            if ok and target.int_values and default not in target.int_values:
                raise RegistrationError(
                    f"{where}: default {default!r} is not one of {list(target.int_values)}"
                )
        case FlagType.NUMBER:
            ok = isinstance(default, (int, float)) and not isinstance(default, bool)
        case FlagType.STRING:
            if target.path and isinstance(default, str):
                return Path(default)  # what a parsed argument would be
            ok = isinstance(default, Path) if target.path else isinstance(default, str)
    if not ok:
        raise RegistrationError(
            f"{where}: default {default!r} does not match the field's type "
            f"({target.flag_type.value})"
        )
    return default


def _checked_map_default(target: Classified, default: object, where: str) -> object:
    """A mapping's default, each value of one of its types. It is kept as given: a
    dataclass refuses a ``dict`` default, so it is read-only, such as a
    ``types.MappingProxyType``, and sharing it between runs is safe"""
    if not isinstance(default, Mapping):
        raise RegistrationError(f"{where}: default {default!r} is not a mapping")
    for key, value in default.items():
        if not isinstance(key, str):
            raise RegistrationError(f"{where}: default key {key!r} is not a str")
        errors: list[RegistrationError] = []
        for branch in target.values:
            try:
                _checked_default(branch, value, f"{where}.{key}")
            except RegistrationError as exc:
                errors.append(exc)
                continue
            break
        else:
            if len(errors) == 1:
                raise errors[0]
            raise RegistrationError(
                f"{where}.{key}: default {value!r} matches none of the value types "
                f"({object_shape(target)})"
            )
    return default


def _check_default_constraints(info: FieldInfo, where: str) -> None:
    """A text default passes the field's own ``pattern``, ``pattern_type``, and
    ``max_bytes``, as a passed value must: the manifest never publishes a default the
    field would refuse; a secret's default stands for no value, not a value"""
    if info.secret:
        return
    default = info.default
    values = default if isinstance(default, tuple) else (default,)
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            info.check_size(value)
            info.check_pattern(value)
        except ParseError as exc:
            raise RegistrationError(f"{where}: default {value!r}: {exc.message}") from None


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


def dry_run_field(fields: Sequence[FieldInfo]) -> FieldInfo | None:
    """The command's dry-run switch: the field marked ``Flag(dry_run=True)``, else a
    boolean field named ``dry_run``"""
    marked = next((f for f in fields if f.spec.dry_run), None)
    if marked is not None:
        return marked
    return next(
        (f for f in fields if f.name == "dry_run" and f.flag_type is FlagType.BOOLEAN), None
    )


def confirm_field(fields: Sequence[FieldInfo]) -> FieldInfo | None:
    """The command's confirmation switch: the field marked ``Flag(confirm=True)``"""
    return next((f for f in fields if f.spec.confirm), None)


def _check_dry_run(cls: type, infos: Sequence[FieldInfo]) -> None:
    """One dry-run switch per command: one marked field, and no ``dry_run`` field beside it;
    a confirmation switch is one too, so it stands alone"""
    confirming = [i.name for i in infos if i.spec.confirm]
    if len(confirming) > 1:
        raise RegistrationError(
            f"{cls.__qualname__}: confirm=True on {confirming}; a command has one "
            "confirmation switch"
        )
    switch = dry_run_field(infos)
    if confirming and switch is not None:
        raise RegistrationError(
            f"{cls.__qualname__}: {confirming[0]} is marked confirm=True, so the command "
            f"previews without it; {switch.name} is a second dry-run switch, drop one"
        )
    marked = [i.name for i in infos if i.spec.dry_run]
    if len(marked) > 1:
        raise RegistrationError(
            f"{cls.__qualname__}: dry_run=True on {marked}; a command has one dry-run switch"
        )
    if marked and marked[0] != "dry_run" and any(i.name == "dry_run" for i in infos):
        raise RegistrationError(
            f"{cls.__qualname__}: {marked[0]} is marked dry_run=True, but a field named "
            "dry_run is also declared; drop one or rename dry_run"
        )


def flag_name(field_name: str) -> str:
    """``dry_run`` is ``--dry-run``; ``for_``, spelled so for Python, is ``--for``"""
    if field_name.endswith("_") and keyword.iskeyword(field_name[:-1]):
        field_name = field_name[:-1]
    return field_name.replace("_", "-")


MAX_OBJECT_DEPTH = 8
"""How deep objects may nest in an argument: postings[0].amount.currency is depth 3"""
_MEMBER_OPTIONS = (
    "positional",
    "short",
    "from_stdin",
    "deprecated",
    "dry_run",
)


def _objects(scalars: ScalarRegistry, outer: tuple[type, ...] = ()) -> ObjectHook:
    """Reads a frozen dataclass inside an argument as an object, ``outer`` holding the
    dataclasses it sits in"""

    def hook(cls: type) -> Classified:
        return _inspect_object(cls, scalars, outer)

    return hook


def _inspect_object(cls: type, scalars: ScalarRegistry, outer: tuple[type, ...]) -> Classified:
    """An object argument's fields, each checked as a flag's value would be. A field may be
    a plain dataclass field or carry ``Flag(...)`` for its description, ``secret``,
    ``pattern``, ``pattern_type``, ``multiline``, or ``max_bytes``"""
    where = cls.__qualname__
    if cls in outer:
        chain = " -> ".join(c.__qualname__ for c in (*outer, cls))
        raise RegistrationError(f"{where} contains itself ({chain}); an object has a finite shape")
    if len(outer) >= MAX_OBJECT_DEPTH:
        raise RegistrationError(
            f"{where}: objects nest at most {MAX_OBJECT_DEPTH} deep in an argument"
        )
    params = getattr(cls, "__dataclass_params__", None)
    if params is None or not params.frozen:
        raise RegistrationError(
            f"{where}: an object argument is a frozen dataclass; declare it @dataclass(frozen=True)"
        )
    hints = type_hints(cls)
    members: list[FieldInfo] = []
    for f in dataclasses.fields(cls):
        at = f"{where}.{f.name}"
        if not f.init:
            raise RegistrationError(f"{at}: field(init=False) cannot be given as a JSON key")
        spec = f.metadata.get(_META)
        if spec is None:
            spec = FlagSpec(f.name.replace("_", " "))
        elif not isinstance(spec, FlagSpec):
            raise RegistrationError(f"{at}: declare it with Flag(...) or as a plain field")
        given = [o for o in _MEMBER_OPTIONS if getattr(spec, o) not in (None, False)]
        if not spec.audit:
            given.append("audit")
        if given:
            raise RegistrationError(
                f"{at}: a field of an object takes description, secret, pattern, "
                f"pattern_type, multiline, and max_bytes from Flag(...), not {', '.join(given)}"
            )
        try:
            classified = classify(hints[f.name], scalars, _objects(scalars, (*outer, cls)))
        except SchemaError as exc:
            raise RegistrationError(f"{at}: {exc}") from None
        default: object = f.default
        if default is not MISSING:
            default = _checked_default(classified, default, at)
        elif f.default_factory is MISSING and classified.optional:
            default = None  # X | None without a default: an absent key is null
        info = FieldInfo(
            name=f.name,
            flag=f.name,
            classified=classified,
            required=default is MISSING and f.default_factory is MISSING,
            default=default,
            spec=spec,
        )
        _check_value_spec(at, spec, classified, secret=info.secret)
        _check_default_constraints(info, at)
        members.append(info)
    return Classified(FlagType.OBJECT, False, cls, members=tuple(members))


def _check_value_spec(where: str, spec: FlagSpec, classified: Classified, *, secret: bool) -> None:
    """The value options an object's field allows by its type, as an argument's do"""
    item = classified.item
    _check_map_spec(where, spec, classified)
    if spec.pattern is not None and (classified.path or (item is not None and item.path)):
        raise RegistrationError(
            f"{where}: Path fields get the filepath preset; pattern= is not allowed on them "
            "(REQ-C-020)"
        )
    scalar = classified.scalar if item is None else item.scalar
    text_type = item if item is not None else classified
    if spec.pattern_type is not None and (
        text_type.flag_type is not FlagType.STRING or text_type.path or scalar is not None
    ):
        raise RegistrationError(
            f"{where}: pattern_type is for str fields; a Path gets filepath on its own, and a "
            "scalar declares it in app.scalar(...) (REQ-C-020)"
        )
    if spec.pattern is not None and scalar is not None:
        raise RegistrationError(
            f"{where}: {scalar.cls.__qualname__} declares its own constraints in "
            "app.scalar(...); pattern= is not allowed on it"
        )
    if spec.multiline and text_type.flag_type is not FlagType.STRING:
        raise RegistrationError(f"{where}: multiline=True is for str fields only")
    if spec.max_bytes is not None and (
        text_type.flag_type is not FlagType.STRING or text_type.path or secret
    ):
        raise RegistrationError(
            f"{where}: max_bytes is for str fields that are not secrets or paths"
        )


def _check_map_spec(where: str, spec: FlagSpec, classified: Classified) -> None:
    """A mapping's values are checked by their types alone: pattern= has nothing to match"""
    if spec.pattern is not None and classified.is_map:
        raise RegistrationError(
            f"{where}: pattern= is for str fields; a mapping's values are checked by their "
            "type, so register a class with app.scalar(...) to constrain them"
        )


def _secret_paths(target: Classified, where: str) -> list[str]:
    """Where an object holds a secret, declared or by name, as ``postings[].token``"""
    if target.item is not None:
        return _secret_paths(target.item, f"{where}[]")
    paths: list[str] = []
    for m in target.members:
        at = f"{where}.{m.name}"
        paths.extend([at] if m.secret else _secret_paths(m.classified, at))
    return paths


def inspect_fields(cls: type, scalars: ScalarRegistry) -> tuple[FieldInfo, ...]:
    """Read an arguments dataclass into ordered ``FieldInfo`` records"""
    if not dataclasses.is_dataclass(cls):
        raise RegistrationError(f"{cls.__qualname__} must be a dataclass")
    hints = type_hints(cls)
    infos: list[FieldInfo] = []
    seen_optional_positional = False
    for f in dataclasses.fields(cls):
        spec = f.metadata.get(_META)
        if not isinstance(spec, FlagSpec):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: declare fields with Flag(...) or Arg(...)"
            )
        classified = classify(hints[f.name], scalars, _objects(scalars))
        item = classified.item
        if spec.confirm and classified.flag_type is not FlagType.BOOLEAN:
            # Before the default check, whose False would not match another type
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: confirm=True marks a boolean flag as the "
                f"confirmation; this field is a {classified.flag_type.value}"
            )
        if spec.positional and (
            classified.flag_type is FlagType.OBJECT
            or (item is not None and item.flag_type is FlagType.OBJECT)
        ):
            # A SchemaError, so registration names the command as for any unsupported type
            raise SchemaError(
                f"{cls.__qualname__}.{f.name}: a dataclass positional; declare it with "
                "Flag(...) to take a JSON object per value, or register a class with "
                "app.scalar(...) to take it from one text value"
            )
        _check_map_spec(f"{cls.__qualname__}.{f.name}", spec, classified)
        if spec.pattern is not None and (classified.path or (item is not None and item.path)):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: Path fields get the filepath preset; "
                "pattern= is not allowed on them (REQ-C-020)"
            )
        scalar = classified.scalar if item is None else item.scalar
        text_type = item if item is not None else classified
        if spec.pattern_type is not None and (
            text_type.flag_type is not FlagType.STRING or text_type.path or scalar is not None
        ):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: pattern_type is for str fields; a Path gets "
                "filepath on its own, and a scalar declares it in app.scalar(...) (REQ-C-020)"
            )
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
                f"{cls.__qualname__}.{f.name}: a required positional cannot follow an optional "
                "one, since a value could fill either; give it a default too, or move it first"
            )
        if default is not MISSING:
            default = _checked_default(classified, default, f"{cls.__qualname__}.{f.name}")
        required = default is MISSING and not classified.optional
        info = FieldInfo(
            name=f.name,
            flag=flag_name(f.name),
            classified=classified,
            required=required,
            default=default,
            spec=spec,
        )
        if info.secret:
            _check_secret_field(cls, info)
        if info.object_type is not None and (paths := _secret_paths(classified, info.flag)):
            # REQ-C-016: a JSON object travels on argv, so it cannot carry a secret. A
            # SchemaError, so registration names the command
            raise SchemaError(
                f"{cls.__qualname__}.{f.name}: {', '.join(paths)} would be a secret inside an "
                "object, which travels on argv; make it a top-level Flag(...), read from "
                "--x-from-env or --x-from-file, or pass secret=False if it holds none "
                "(REQ-C-016)"
            )
        text = item if item is not None else classified
        if spec.multiline and text.flag_type is not FlagType.STRING:
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: multiline=True is for str fields only"
            )
        if spec.max_bytes is not None and (
            text.flag_type is not FlagType.STRING or text.path or info.secret
        ):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: max_bytes is for str fields that are not "
                "secrets or paths"
            )
        _check_default_constraints(info, f"{cls.__qualname__}.{f.name}")
        if spec.from_stdin and (info.flag_type is FlagType.BOOLEAN or info.secret):
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: from_stdin=True is for value fields; a boolean "
                "takes no value, and a secret comes from --x-from-env or --x-from-file"
            )
        if spec.dry_run and info.flag_type is not FlagType.BOOLEAN:
            raise RegistrationError(
                f"{cls.__qualname__}.{f.name}: dry_run=True marks a boolean flag as the "
                f"dry run; this field is a {info.flag_type.value}"
            )
        infos.append(info)
    _check_dry_run(cls, infos)
    stdin_fields = [i.name for i in infos if i.spec.from_stdin]
    if len(stdin_fields) > 1:
        raise RegistrationError(
            f"{cls.__qualname__}: from_stdin=True on {stdin_fields}; stdin holds one value, "
            "so at most one field reads it"
        )
    flags = {i.flag for i in infos}
    for info in infos:
        old = info.spec.deprecated
        if old is not None and old.replacement is not None and old.replacement not in flags:
            raise RegistrationError(
                f"{cls.__qualname__}.{info.name}: Deprecated(replacement={old.replacement!r}) "
                f"is not a flag of the same arguments; name one of {sorted(flags)}"
            )
    _check_positionals(cls, [i for i in infos if i.positional])
    _check_flag_names(cls, infos)
    shorts = [i.spec.short for i in infos if i.spec.short is not None]
    if len(shorts) != len(set(shorts)):
        raise RegistrationError(f"{cls.__qualname__}: duplicate short flags {shorts}")
    return tuple(infos)
