"""Typed, layered config: the app's settings dataclass, read once per run (REQ-F-028).

``App(settings=Settings)`` names a frozen dataclass whose fields all have defaults; a
handler asks for it by annotating a parameter with the class. Each field takes the first
value found, highest precedence first:

1. ``<APP>_<FIELD>`` in the environment
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
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ._config import local_config, user_config
from ._env import CONFIG, CONTEXT, INSTANCE_ID, KNOWN, app_var
from ._errors import ParseError, RegistrationError, SchemaError
from ._flags import REDACTED, SECRET_NAME_PARTS, coerce_text
from ._parse import check_json_base
from ._paths import check_path
from ._scalars import ScalarRegistry
from ._schema import to_jsonable
from ._types import Classified, FlagType, classify
from ._values import InstanceId, InvalidValue

CONTEXTS_KEY = "contexts"
CURRENT_CONTEXT_KEY = "current_context"
INVALID = "CONFIG_INVALID"


@dataclass(frozen=True, slots=True)
class Setting:
    name: str
    classified: Classified
    default: object

    @property
    def secret(self) -> bool:
        """Inferred from the name, as for args: shown as ``[REDACTED]`` by --show-config"""
        lowered = self.name.lower()
        return self.classified.flag_type is not FlagType.BOOLEAN and any(
            part in lowered for part in SECRET_NAME_PARTS
        )


@dataclass(frozen=True, slots=True)
class SettingsSpec:
    """The app's settings dataclass, checked once at ``App(settings=)``"""

    cls: type
    fields: tuple[Setting, ...]

    @classmethod
    def inspect(cls, settings: object, scalars: ScalarRegistry) -> SettingsSpec:
        where = f"App(settings={getattr(settings, '__qualname__', settings)!r})"
        if not (isinstance(settings, type) and dataclasses.is_dataclass(settings)):
            raise RegistrationError(f"{where}: settings is a frozen dataclass type")
        params = getattr(settings, "__dataclass_params__", None)
        if params is None or not params.frozen:
            raise RegistrationError(f"{where}: declare it @dataclass(frozen=True)")
        hints = typing.get_type_hints(settings)
        framework = {v.key for v in KNOWN} | {CONTEXTS_KEY, CURRENT_CONTEXT_KEY}
        fields: list[Setting] = []
        for f in dataclasses.fields(settings):
            if f.name in framework:
                raise RegistrationError(
                    f"{where}: field {f.name!r} is a framework name; a config file must not "
                    "set framework options (02-D5), so rename it"
                )
            if f.default is dataclasses.MISSING:
                raise RegistrationError(f"{where}: field {f.name!r} needs a default")
            try:
                classified = classify(hints[f.name], scalars)
            except SchemaError as exc:
                raise RegistrationError(f"{where}: field {f.name!r}: {exc}") from None
            if classified.scalar is not None or (
                classified.item is not None and classified.item.scalar is not None
            ):
                raise RegistrationError(
                    f"{where}: field {f.name!r}: settings take str, int, float, bool, Path, "
                    "enums, Literal, and tuples of them"
                )
            fields.append(Setting(f.name, classified, f.default))
        return cls(settings, tuple(fields))


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
    """Every field as JSON, secrets included: what the hash covers"""
    sources: Mapping[str, str]
    """Per field: ``env:<VAR>``, ``file:<abs path>``, or ``default``"""
    files: tuple[Path, ...]
    """The files read, highest precedence first: ``meta.config_sources``"""
    candidates: tuple[Path, ...]
    """Every file this run would read, present or not, highest first"""
    context: str | None
    options: ConfigOptions

    @property
    def hash(self) -> str:
        """``meta.effective_config_hash``: 12 hex of sha256 over the merged settings"""
        text = json.dumps(self.effective, sort_keys=True, separators=(",", ":"))
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

    def show(self, spec: SettingsSpec | None) -> dict[str, object]:
        """``--show-config``: the values, secrets redacted, with their sources (REQ-O-015)"""
        secret = {s.name for s in spec.fields if s.secret} if spec is not None else set()
        return {
            "effective_config": {
                k: REDACTED if k in secret else v for k, v in self.effective.items()
            },
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
    """The flags, each falling back to its ``<APP>_`` variable (REQ-O-024, REQ-O-036)"""
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
    layers = [(p, _layer(p, d, context)) for p, d in loaded]
    if context is not None:
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
        var = app_var(app_name, s.name)
        if env.get(var):
            values[s.name] = _from_text(s, env[var], var)
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
    try:
        value = spec.cls(**values)
    except (ParseError, InvalidValue) as exc:
        raise ParseError(
            f"settings are invalid: {exc}", code=INVALID, context={"sources": sources}
        ) from None
    effective = {k: to_jsonable(v, scalars, base=cwd) for k, v in values.items()}
    files = tuple(p for p, _ in loaded)
    return Resolved(value, effective, sources, files, candidates, context, opts)


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
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _invalid(path, None, f"cannot read it: {exc.__class__.__name__}") from None
    try:
        data = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
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


def _layer(path: Path, data: Mapping[str, object], context: str | None) -> dict[str, object]:
    """The file's top level, with the selected context's table over it"""
    layer = {k: v for k, v in data.items() if k not in (CONTEXTS_KEY, CURRENT_CONTEXT_KEY)}
    if context is not None:
        layer |= _contexts(path, data).get(context, {})
    return layer


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
            return tuple(check_json_base(target.item, v, setting.name) for v in raw)
        return check_json_base(target, raw, setting.name)
    except ParseError as exc:
        raise _invalid(path, setting.name, exc.message) from None
