"""Config writes: which file a command may change, and changing it safely (REQ-C-025).

A ``config_write_scope="local"`` command writes ``./.<app>.toml`` in the working
directory, or with ``--global`` the user file ``$XDG_CONFIG_HOME/<app>/config.toml``
(``~/.config/<app>/config.toml``). A ``"global"`` command writes only the user file and
requires ``--global``. Under ``--config PATH`` a command writes that file instead. Writes go
through ``ctx.write_config``: atomic and locked, and a global write adds a
``GLOBAL_CONFIG_MODIFIED`` warning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ._atomic import exclusive, write_atomic
from ._values import InstanceId

GLOBAL_FLAG = "global"


class ConfigScope(StrEnum):
    LOCAL = "local"
    """The project file, or the user file with ``--global``"""
    GLOBAL = "global"
    """Only the user file, and only with ``--global``"""


def user_config(
    app_name: str, env: Mapping[str, str], instance: InstanceId | None = None
) -> Path | None:
    """The user's config file, or None without ``XDG_CONFIG_HOME`` or ``HOME``; a relative
    ``XDG_CONFIG_HOME`` is ignored, as the XDG spec says. An ``--instance-id`` has its own
    file under ``instances/<id>/`` (REQ-O-036)"""
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and Path(xdg).is_absolute():
        home = Path(xdg) / app_name
    elif base := env.get("HOME"):
        home = Path(base) / ".config" / app_name
    else:
        return None
    if instance is not None:
        home = home / "instances" / instance.value
    return home / "config.toml"


def local_config(app_name: str, cwd: Path) -> Path:
    """The project file ``./.<app>.toml`` in the run's working directory"""
    return cwd / f".{app_name}.toml"


@dataclass(frozen=True, slots=True)
class ConfigFile:
    """The one config file a run may write"""

    path: Path
    is_global: bool
    warn: Callable[[str, str, Mapping[str, object]], None]

    def write(self, text: str) -> Path:
        if self.is_global:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Other sessions may write the same file at once: one writer at a time (REQ-O-036)
        with exclusive(self.path.with_name(self.path.name + ".lock")):
            write_atomic(self.path, text)
        if not self.is_global:
            return self.path
        self.warn(
            "GLOBAL_CONFIG_MODIFIED",
            "Wrote to the global config file",
            {"path": str(self.path)},
        )
        return self.path
