"""Typed, layered config: the app's settings dataclass, read once per run (REQ-F-028).

``App(settings=Settings)`` names a frozen dataclass whose fields all have defaults; a
handler asks for it by annotating a parameter with the class. Each field takes the first
value found, highest precedence first:

1. ``<APP>_<FIELD>`` in the environment, then the field's ``Flag(env=(...))`` names in order
2. The config files: ``--config PATH`` alone, or else the project file ``./.<app>.toml``
   and then the user file (``$XDG_CONFIG_HOME/<app>/config.toml``)
3. The field's default

Files are TOML, or JSON for a ``--config`` path ending in ``.json``. A file may hold
``[contexts.<name>]`` tables that overlay its top level, selected by ``--context`` or its
``current_context`` key. ``--no-config`` reads no file; env vars still apply. An unknown
key, a wrong type, or an unreadable file exits 2 with ``CONFIG_INVALID`` (02-D3).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ._atomic import retry_sharing_violation
from ._config import local_config, user_config
from ._env import CONFIG, CONTEXT, INSTANCE_ID, KNOWN, app_var
from ._envnames import EnvName, check_env_names, read_env
from ._errors import ParseError, RegistrationError, SchemaError, UserCodeError, user_code
from ._flags import FLAG_META, apply_scalar, coerce_text
from ._parse import check_json_base
from ._paths import check_path
from ._redact import REDACTED, replacer, secret_name
from ._scalars import ScalarRegistry
from ._schema import to_jsonable
from ._types import Classified, FlagType, classify, type_hints
from ._values import InstanceId, InvalidValue

CONTEXTS_KEY = "contexts"
CURRENT_CONTEXT_KEY = "current_context"
INVALID = "CONFIG_INVALID"


@dataclass(frozen=True, slots=True)
class Setting:
    name: str
    classified: Classified
    default: object
    declared_secret: bool | None = None
    """``Flag(..., secret=...)`` on the field; None infers it from the name"""
    env: tuple[EnvName, ...] = ()
    """``Flag(..., env=...)``: variables read after ``<APP>_<NAME>``, in order"""

    @property
    def secret(self) -> bool:
        """Declared, or inferred from the name as for args: shown as ``[REDACTED]`` by
        --show-config and kept out of the config hash. A boolean or an enum is never
        inferred one"""
        if self.declared_secret is not None:
            return self.declared_secret
        item = self.classified.item
        plain = (FlagType.BOOLEAN, FlagType.ENUM)
        if self.classified.flag_type in plain or (item is not None and item.flag_type in plain):
            return False
        return secret_name(self.name)


@dataclass(frozen=True, slots=True)
class SettingsSpec:
    """The app's settings dataclass: its shape checked at ``App(settings=)``, its field
    types once the app is in use, after ``app.scalar`` has registered the classes they name"""

    cls: type
    fields: tuple[Setting, ...]

    @staticmethod
    def check(settings: object, app_name: str) -> str:
        """What ``App(settings=)`` checks before any scalar is registered: a frozen
        dataclass whose fields have defaults, no framework names, and ``env`` names no
        other field or framework option reads. Returns how its errors name it"""
        where = f"App(settings={getattr(settings, '__qualname__', settings)!r})"
        if not (isinstance(settings, type) and dataclasses.is_dataclass(settings)):
            raise RegistrationError(f"{where}: settings is a frozen dataclass type")
        params = getattr(settings, "__dataclass_params__", None)
        if params is None or not params.frozen:
            raise RegistrationError(f"{where}: declare it @dataclass(frozen=True)")
        framework = {v.key for v in KNOWN} | {CONTEXTS_KEY, CURRENT_CONTEXT_KEY}
        for f in dataclasses.fields(settings):
            if f.name in framework:
                raise RegistrationError(
                    f"{where}: field {f.name!r} is a framework name; a config file must not "
                    "set framework options (02-D5), so rename it"
                )
            if f.default is dataclasses.MISSING:
                how = (
                    "default=, not default_factory=; a collection is a tuple with default=()"
                    if f.default_factory is not dataclasses.MISSING
                    else "a default"
                )
                raise RegistrationError(f"{where}: field {f.name!r} needs {how}")
            declared = f.metadata.get(FLAG_META)
            if declared is None:
                continue
            # Only what settings enforce: a pattern or a size would pass unchecked
            ignored = [
                name
                for name, value in (
                    ("positional", declared.positional),
                    ("short", declared.short),
                    ("pattern", declared.pattern),
                    ("pattern_type", declared.pattern_type),
                    ("max_bytes", declared.max_bytes),
                    ("multiline", declared.multiline),
                    ("from_stdin", declared.from_stdin),
                    ("deprecated", declared.deprecated),
                )
                if value not in (None, False)
            ]
            if ignored:
                raise RegistrationError(
                    f"{where}: field {f.name!r}: a setting takes default, description, "
                    f"secret, and env from Flag(...), not {', '.join(ignored)}"
                )
        SettingsSpec._check_env(where, settings, app_name)
        return where

    @staticmethod
    def _check_env(where: str, settings: type, app_name: str) -> None:
        """Each variable sets one value: a declared name is no field's ``<APP>_<NAME>``,
        no framework variable, and not declared by two fields"""
        fields = dataclasses.fields(settings)
        taken = {app_var(app_name, v.key): f"the framework's {v.key}" for v in KNOWN}
        taken |= {app_var(app_name, f.name): f"setting {f.name!r}" for f in fields}
        for f in fields:
            declared = f.metadata.get(FLAG_META)
            if declared is None or not declared.env:
                continue
            own = app_var(app_name, f.name)
            others = {k: v for k, v in taken.items() if k != own}
            check_env_names(f"{where}: field {f.name!r}", own, declared.env, others, default=own)
            taken |= {n.name: f"setting {f.name!r}" for n in declared.env}

    @classmethod
    def inspect(cls, settings: type, scalars: ScalarRegistry, app_name: str) -> SettingsSpec:
        """The shape, then each field's type, a class registered in ``scalars`` included"""
        where = cls.check(settings, app_name)
        hints = type_hints(settings)
        fields: list[Setting] = []
        for f in dataclasses.fields(settings):
            try:
                classified = classify(hints[f.name], scalars)
            except SchemaError as exc:
                raise RegistrationError(f"{where}: field {f.name!r}: {exc}") from None
            declared = f.metadata.get(FLAG_META)
            secret = None if declared is None else declared.secret
            env = () if declared is None else declared.env
            if secret and classified.flag_type is FlagType.BOOLEAN:
                raise RegistrationError(
                    f"{where}: field {f.name!r}: a boolean cannot hold a secret"
                )
            fields.append(Setting(f.name, classified, f.default, secret, env))
        return cls(settings, tuple(fields))


def settings_env_names(settings: type | None) -> frozenset[str]:
    """Every name the settings fields declare with ``Flag(env=)``, read without
    inspecting the field types, so an audit runs even when one names an unknown class"""
    if settings is None:
        return frozenset()
    return frozenset(
        n.name
        for f in dataclasses.fields(settings)
        if (declared := f.metadata.get(FLAG_META)) is not None
        for n in declared.env
    )


def plain_settings_env(settings: type | None) -> dict[str, str]:
    """The names plain settings declare with ``Flag(env=)``, with the field each sets:
    ``--show-config`` prints their values. A setting is secret when declared so or, left
    undeclared, when its name says so; read without inspecting the field types"""
    if settings is None:
        return {}
    plain: dict[str, str] = {}
    for f in dataclasses.fields(settings):
        declared = f.metadata.get(FLAG_META)
        if declared is None or not declared.env:
            continue
        secret = secret_name(f.name) if declared.secret is None else declared.secret
        if not secret:
            plain |= dict.fromkeys((n.name for n in declared.env), f"plain setting {f.name!r}")
    return plain


def settings_env_taken(settings: type | None, app_name: str) -> dict[str, str]:
    """Every variable the settings read, prefixed or declared, with the field it sets"""
    if settings is None:
        return {}
    taken: dict[str, str] = {}
    for f in dataclasses.fields(settings):
        declared = f.metadata.get(FLAG_META)
        names = [
            app_var(app_name, f.name),
            *(() if declared is None else (n.name for n in declared.env)),
        ]
        taken |= dict.fromkeys(names, f"setting {f.name!r}")
    return taken


@dataclass(frozen=True, slots=True)
class ConfigOptions:
    """Where this run reads config: the global flags, else their environment variables"""

    config: Path | None = None
    context: str | None = None
    no_config: bool = False
    instance_id: InstanceId | None = None


@dataclass(frozen=True, slots=True)
class Resolved:
    """The settings of one run and where each value came from"""

    value: object | None
    """The settings instance; None without ``App(settings=)``"""
    effective: Mapping[str, object]
    """Every field as JSON, secrets included"""
    sources: Mapping[str, str]
    """Per field: ``env:<VAR>``, ``file:<abs path>``, or ``default``"""
    files: tuple[Path, ...]
    """The files read, highest precedence first: ``meta.config_sources``"""
    candidates: tuple[Path, ...]
    """Every file this run would read, present or not, highest first"""
    context: str | None
    options: ConfigOptions
    secrets: frozenset[str] = frozenset()
    """The fields whose values are secret: never shown, never hashed"""

    def _public(self) -> dict[str, object]:
        return {k: REDACTED if k in self.secrets else v for k, v in self.effective.items()}

    @property
    def hash(self) -> str:
        """``meta.effective_config_hash``: 12 hex of sha256 over the merged settings, secret
        values left out: a short secret would be recoverable from its hash"""
        text = json.dumps(self._public(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def meta(self) -> dict[str, object]:
        extra: dict[str, object] = {
            "config_sources": [str(p) for p in self.files],
            "effective_config_hash": self.hash,
        }
        if self.context is not None:
            extra["context"] = self.context
        if self.options.instance_id is not None:
            extra["instance_id"] = self.options.instance_id.value
        return extra

    def show(self) -> dict[str, object]:
        """``--show-config``: the values, secrets redacted, with their sources (REQ-O-015)"""
        return {
            "effective_config": self._public(),
            "sources": dict(self.sources),
            "precedence_order": ["env-vars", *(str(p) for p in self.candidates), "defaults"],
            "context": self.context,
        }


EMPTY = Resolved(None, {}, {}, (), (), None, ConfigOptions())


def options(
    app_name: str,
    env: Mapping[str, str],
    *,
    config: str | None = None,
    context: str | None = None,
    no_config: bool = False,
    instance_id: str | None = None,
) -> ConfigOptions:
    """The flags, each falling back to its ``<APP>_`` variable (REQ-O-024, REQ-O-036); an
    empty variable is unset, but an empty flag, such as ``--config=``, is an error"""
    for flag, value in (("config", config), ("context", context), ("instance-id", instance_id)):
        if value == "":
            raise ParseError(
                f"--{flag} is empty",
                context={"flag": flag},
                suggestion=f"pass a value to --{flag}, or leave the flag out",
            )
    config_var, context_var, instance_var = (
        app_var(app_name, v.key) for v in (CONFIG, CONTEXT, INSTANCE_ID)
    )
    raw_config, config_source = (config, "config") if config else (env.get(config_var), config_var)
    raw_instance, instance_source = (
        (instance_id, "instance-id") if instance_id else (env.get(instance_var), instance_var)
    )
    instance: InstanceId | None = None
    if raw_instance:
        try:
            instance = InstanceId(raw_instance)
        except InvalidValue as exc:
            raise ParseError(
                f"{instance_source}: {exc}",
                context={"flag": "instance-id", "source": instance_source, "value": raw_instance},
            ) from None
    return ConfigOptions(
        config=check_path(raw_config, config_source) if raw_config else None,
        context=context or env.get(context_var) or None,
        no_config=no_config,
        instance_id=instance,
    )


def resolve(
    spec: SettingsSpec | None,
    app_name: str,
    opts: ConfigOptions,
    env: Mapping[str, str],
    cwd: Path,
    scalars: ScalarRegistry,
) -> Resolved:
    """Read the layers once; a ``ParseError`` with ``CONFIG_INVALID`` names what is wrong"""
    if opts.config is not None:
        path = opts.config if opts.config.is_absolute() else cwd / opts.config
        candidates: tuple[Path, ...] = (path,)
    else:
        user = user_config(app_name, env, opts.instance_id)
        candidates = (local_config(app_name, cwd), *([] if user is None else [user]))
    if opts.no_config or spec is None:
        candidates = ()
    # A --config file that does not exist yet reads as empty: a fresh session's file,
    # which its first config write creates
    loaded = [(p, _load(p)) for p in candidates if p.exists()]
    context = opts.context or next(
        (str(d[CURRENT_CONTEXT_KEY]) for _, d in loaded if CURRENT_CONTEXT_KEY in d), None
    )
    # The selected context outranks every file's top level: the project file's context,
    # the user file's, then the project's top level and the user's
    chosen = [] if context is None else [(p, _contexts(p, d).get(context, {})) for p, d in loaded]
    layers = [*chosen, *((p, _top(d)) for p, d in loaded)]
    # A context only selects among files that are read: --no-config, or an app without
    # settings, reads none, so an <APP>_CONTEXT set for other runs has nothing to miss
    if context is not None and candidates:
        available = sorted({n for p, d in loaded for n in _contexts(p, d)})
        if context not in available:
            raise ParseError(
                f"no context {context!r} in the config files",
                code="CONTEXT_UNKNOWN",
                context={"context": context, "available": available},
                suggestion="pass --context with one of the available names",
            )
    if spec is None:
        return dataclasses.replace(EMPTY, context=context, options=opts)
    known = {s.name for s in spec.fields}
    for path, layer in layers:
        for key in layer:
            if key not in known:
                raise _invalid(path, key, f"unknown key {key!r}", known=sorted(known))
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for s in spec.fields:
        found_env = read_env(app_var(app_name, s.name), s.env, env)
        if found_env is not None:
            var, text = found_env
            values[s.name] = _from_text(s, text, var)
            sources[s.name] = f"env:{var}"
            continue
        found = next(((p, layer[s.name]) for p, layer in layers if s.name in layer), None)
        if found is None:
            values[s.name] = s.default
            sources[s.name] = "default"
            continue
        path, raw = found
        values[s.name] = _from_file(s, raw, path)
        sources[s.name] = f"file:{path}"
    for s in spec.fields:
        # A relative Path setting means the run's directory, --cwd included, as a flag does
        value = values[s.name]
        if s.classified.path and isinstance(value, Path) and not value.is_absolute():
            values[s.name] = cwd / value
        item = s.classified.item
        if item is not None and item.path and isinstance(value, tuple):
            values[s.name] = tuple(
                cwd / v if isinstance(v, Path) and not v.is_absolute() else v for v in value
            )
    secrets = frozenset(s.name for s in spec.fields if s.secret)
    try:
        value = user_code(lambda: spec.cls(**values))
    except UserCodeError as err:
        # A ValueError in __post_init__ is how a dataclass refuses a value; read before
        # routing, it must not take help and version down with a traceback (REQ-F-068)
        exc = err.cause
        why = (
            str(exc)
            if isinstance(exc, (ParseError, InvalidValue))
            else f"{type(exc).__name__}: {exc}"
        )
    else:
        effective = {k: to_jsonable(v, scalars, base=cwd) for k, v in values.items()}
        files = tuple(p for p, _ in loaded)
        return Resolved(value, effective, sources, files, candidates, context, opts, secrets)
    spellings = {form for name in secrets for form in _spellings(values[name], scalars)}
    why = replacer(spellings)(why)
    raise ParseError(f"settings are invalid: {why}", code=INVALID, context={"sources": sources})


def _spellings(value: object, scalars: ScalarRegistry) -> set[str]:
    """A secret setting's value, or each item of a tuple, as text; a registered scalar by
    its serialized form too, the text its instance was parsed from. Replaced as the run's
    secrets are, the longest first and one an escape splits too (#277)"""
    found: set[str] = set()
    for item in value if isinstance(value, tuple) else (value,):
        spec = scalars.for_value(item)
        for form in (item,) if spec is None else (spec.serialize(item), item):
            if form is not None and str(form):
                found.add(str(form))
    return found


def _invalid(path: Path, key: str | None, why: str, **extra: object) -> ParseError:
    context: dict[str, object] = {"path": str(path), **extra}
    if key is not None:
        context["key"] = key
    return ParseError(
        f"config file {path}: {why}",
        code=INVALID,
        context=context,
        suggestion="fix the file, or pass --no-config to ignore every config file",
    )


def _load(path: Path) -> dict[str, object]:
    try:
        text = retry_sharing_violation(lambda: path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(path, None, f"cannot read it: {exc.__class__.__name__}") from None
    try:
        data = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
    except (ValueError, RecursionError) as exc:
        # Decode errors, an integer past the digit limit, and nesting past the stack
        raise _invalid(
            path, None, f"not valid {path.suffix.lstrip('.') or 'TOML'}: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise _invalid(path, None, "the top level is not a table")
    current = data.get(CURRENT_CONTEXT_KEY)
    if current is not None and not isinstance(current, str):
        raise _invalid(path, CURRENT_CONTEXT_KEY, "current_context names a context")
    _contexts(path, data)
    return data


def _contexts(path: Path, data: Mapping[str, object]) -> dict[str, dict[str, object]]:
    contexts = data.get(CONTEXTS_KEY, {})
    if not isinstance(contexts, dict) or not all(isinstance(v, dict) for v in contexts.values()):
        raise _invalid(path, CONTEXTS_KEY, "contexts holds one table per context name")
    return contexts


def _top(data: Mapping[str, object]) -> dict[str, object]:
    """The file's settings outside any context"""
    return {k: v for k, v in data.items() if k not in (CONTEXTS_KEY, CURRENT_CONTEXT_KEY)}


def _from_text(setting: Setting, raw: str, var: str) -> object:
    """An env value, parsed as argv text; an array is comma-separated"""
    target = setting.classified
    try:
        if target.flag_type is FlagType.ARRAY:
            assert target.item is not None
            return tuple(
                coerce_text(target.item, v, var, secret=setting.secret) for v in raw.split(",")
            )
        return coerce_text(target, raw, var, secret=setting.secret)
    except ParseError as exc:
        raise ParseError(
            f"{var}: {exc.message}",
            code=INVALID,
            context={"source": var, "key": setting.name},
            suggestion=f"fix or unset {var}",
        ) from None


def _from_file(setting: Setting, raw: object, path: Path) -> object:
    target = setting.classified
    try:
        if raw is None and target.optional:
            return None
        if target.flag_type is FlagType.ARRAY:
            if not isinstance(raw, list) or target.item is None:
                raise ParseError(f"{setting.name!r} expects an array")
            return tuple(_from_json(setting, target.item, v) for v in raw)
        return _from_json(setting, target, raw)
    except ParseError as exc:
        raise _invalid(path, setting.name, exc.message) from None


def _from_json(setting: Setting, target: Classified, raw: object) -> object:
    """A file value as its JSON type, then through its registered scalar's checks and parse"""
    base = check_json_base(target, raw, setting.name)
    if target.scalar is None:
        return base
    return apply_scalar(target.scalar, base, setting.name, secret=setting.secret)
