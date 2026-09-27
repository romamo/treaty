"""Config writes: which file a command may change, and changing it safely (REQ-C-025).

A ``config_write_scope="local"`` command writes ``./.<app>.toml`` in the working
directory, or with ``--global`` the user file ``$XDG_CONFIG_HOME/<app>/config.toml``
(``~/.config/<app>/config.toml``). A ``"global"`` command writes only the user file and
requires ``--global``. Writes go through ``ctx.write_config``: atomic, locked when global,
and a global write adds a ``GLOBAL_CONFIG_MODIFIED`` warning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ._atomic import exclusive, write_atomic

GLOBAL_FLAG = "global"


class ConfigScope(StrEnum):
    LOCAL = "local"
    """The project file, or the user file with ``--global``"""
    GLOBAL = "global"
    """Only the user file, and only with ``--global``"""


def user_config(app_name: str, env: Mapping[str, str]) -> Path | None:
    """The user's config file, or None without ``XDG_CONFIG_HOME`` or ``HOME``"""
    if xdg := env.get("XDG_CONFIG_HOME"):
        return Path(xdg) / app_name / "config.toml"
    if home := env.get("HOME"):
        return Path(home) / ".config" / app_name / "config.toml"
    return None


def local_config(app_name: str) -> Path:
    return Path.cwd() / f".{app_name}.toml"


@dataclass(frozen=True, slots=True)
class ConfigFile:
    """The one config file a run may write"""

    path: Path
    is_global: bool
    warn: Callable[[str, str, Mapping[str, object]], None]

    def write(self, text: str) -> Path:
        if not self.is_global:
            write_atomic(self.path, text)
            return self.path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Other tools and sessions share the user file: one writer at a time
        with exclusive(self.path.with_name(self.path.name + ".lock")):
            write_atomic(self.path, text)
        self.warn(
            "GLOBAL_CONFIG_MODIFIED",
            "Wrote to the global config file",
            {"path": str(self.path)},
        )
        return self.path
