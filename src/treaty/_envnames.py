"""Declared variable names: ``Flag(env=(...))`` on a settings field (#7) or a command's
flag (#9).

A settings field or a flag that declares names is read from ``<APP>_<NAME>`` first, then
from each declared name in order, so a tool keeps a variable its users already export,
such as ``BEANCOUNT_FILE`` or ``IBKR_FLEX_TOKEN``; a flag passed on the command line, or a
secret's ``--x-from-env``/``--x-from-file``, comes before both. The prefixed name always
wins over a declared one: it is the one treaty documents, and a stale shared variable
must not shadow it (REQ-F-073). A plain flag without ``env=`` reads no variable.

A declared name may be ``EnvName(..., deprecated=Deprecated(...))``: still read, with a
``DEPRECATED_ENV_VAR`` warning naming what to use instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ._auth import is_env_var_name
from ._deprecation import Deprecated
from ._errors import RegistrationError

DEPRECATED_ENV_VAR = "DEPRECATED_ENV_VAR"
"""The warning a run reading a deprecated declared name gets (REQ-F-075)"""


@dataclass(frozen=True, slots=True)
class EnvName:
    """One name of ``Flag(env=(...))``; ``deprecated=Deprecated("1.4.0")`` keeps it read
    with a warning. ``Deprecated(replacement=)`` is then the variable to use instead:
    ``<APP>_<NAME>`` when left out, or another declared name"""

    name: str
    deprecated: Deprecated | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not is_env_var_name(self.name):
            raise RegistrationError(
                f"env name {self.name!r} is not an environment variable name: letters, "
                "digits, and underscores, not starting with a digit"
            )
        if self.deprecated is not None and not isinstance(self.deprecated, Deprecated):
            raise RegistrationError(
                f"EnvName({self.name!r}, deprecated=...) takes treaty.Deprecated(since=...)"
            )


def replacement(name: EnvName, default: str) -> str:
    """What a deprecation warning says to use: the declared replacement, else ``default``,
    the field's own variable or, for a flag without one, the flag"""
    old = name.deprecated
    return default if old is None or old.replacement is None else old.replacement


def env_names(value: object) -> tuple[EnvName, ...]:
    """``Flag(env=)`` as ``EnvName``s: a sequence of names or ``EnvName``s, no repeats"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RegistrationError(
            f"env takes a tuple of variable names, such as env=('BEANCOUNT_FILE',), not {value!r}"
        )
    names: list[EnvName] = []
    for item in value:
        if isinstance(item, EnvName):
            names.append(item)
        elif isinstance(item, str):
            names.append(EnvName(item))
        else:
            raise RegistrationError(f"env holds variable names or treaty.EnvName, not {item!r}")
    spelled = [n.name for n in names]
    repeated = sorted({n for n in spelled if spelled.count(n) > 1})
    if repeated:
        raise RegistrationError(f"env names {repeated} more than once")
    return tuple(names)


def check_env_names(
    where: str,
    own: str | None,
    names: tuple[EnvName, ...],
    taken: Mapping[str, str],
    *,
    default: str,
) -> None:
    """At registration: no declared name is the field's own ``<APP>_<NAME>``, or a
    variable that ``taken`` says something else reads; a deprecation's replacement is
    ``default`` or a declared name that is not deprecated itself"""
    current = {default} | {n.name for n in names if n.deprecated is None}
    for n in names:
        if n.name == own:
            raise RegistrationError(
                f"{where}: env name {n.name} is already read as the field's own variable; "
                "drop it from env="
            )
        if n.name in taken:
            raise RegistrationError(
                f"{where}: env name {n.name} is already read for {taken[n.name]}; "
                "one variable sets one value"
            )
        instead = replacement(n, default)
        if instead not in current:
            raise RegistrationError(
                f"{where}: EnvName({n.name!r}) names replacement {instead!r}; name {default} "
                "or a declared name that is not deprecated"
            )


def read_env(
    own: str | None, names: tuple[EnvName, ...], env: Mapping[str, str]
) -> tuple[str, str] | None:
    """The first set variable, ``own`` then the declared names in order, and its text; an
    empty variable is unset"""
    for var in (*([] if own is None else [own]), *(n.name for n in names)):
        raw = env.get(var)
        if raw:
            return var, raw
    return None


def declared_text(name: EnvName, what: str, after: str | None, default: str) -> str:
    """How ``--help`` and AGENTS.md describe a declared name: what it sets, the variable
    that outranks it, and its deprecation, whose replacement is ``default`` unless named"""
    text = what if after is None else f"{what}, when {after} is not set"
    old = name.deprecated
    if old is not None:
        text += f" (deprecated since {old.since}; use {replacement(name, default)})"
    return text


def env_var_entries(own: str, names: tuple[EnvName, ...]) -> list[dict[str, object]]:
    """``EnvVarEntry`` items (ManifestResponse 3.4) in precedence order: ``own``, the
    tool-prefixed name, then the declared names, each deprecated one marked"""
    entries: list[dict[str, object]] = [{"name": own}]
    for n in names:
        entry: dict[str, object] = {"name": n.name}
        if n.deprecated is not None:
            entry["deprecated"] = True
        entries.append(entry)
    return entries


def deprecated_name(names: tuple[EnvName, ...], var: str) -> EnvName | None:
    """The deprecated declared name ``var`` is, if it is one"""
    return next((n for n in names if n.name == var and n.deprecated is not None), None)
