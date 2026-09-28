"""Update checks that never delay a run (REQ-F-029, REQ-O-020).

Treaty ships no checker, so core stays free of network code; an app passes one with
``App(update_check=)``. It runs only for a person at a terminal: stdin and stdout are
terminals, ``CI`` is unset, ``<APP>_NO_UPDATE`` is unset, and ``--no-update-check`` is
absent. Even then the run reads only the answer cached in ``update.json`` of the state
directory; a daemon thread refreshes it at most once a day, for a later run.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ._atomic import write_atomic
from ._env import NO_UPDATE, app_var
from ._values import InvalidValue, ToolVersion

REFRESH_SECONDS = 86_400
"""How old the cached answer may be before a run refreshes it in the background"""
CHECK_TIMEOUT_SECONDS = 5.0
UPDATE_FILE = "update.json"


class UpdateCheck(Protocol):
    """How an app learns its latest release; ``latest`` returns its semver version, or
    None when it cannot tell. It runs on a daemon thread, never on a run's path."""

    def latest(self, current: str, timeout: float) -> str | None: ...


def check_allowed(app_name: str, env: Mapping[str, str], *, interactive: bool, flag: bool) -> bool:
    """A person at a terminal, outside CI, who did not turn the check off"""
    off = flag or bool(env.get(app_var(app_name, NO_UPDATE.key))) or bool(env.get("CI"))
    return interactive and not off


def available(check: UpdateCheck, current: str, state: Path) -> str | None:
    """The cached latest version when it is newer than ``current``; a cache older than a
    day, or none, is refreshed on a daemon thread and answers only a later run"""
    path = state / UPDATE_FILE
    cached = _read(path)
    if cached is None or time.time() - cached[0] > REFRESH_SECONDS:
        threading.Thread(
            target=_refresh, args=(check, current, path), name="treaty-update", daemon=True
        ).start()
    latest = None if cached is None else cached[1]
    if latest is None or _precedence(latest) <= _precedence(ToolVersion(current)):
        return None
    return latest.value


def _refresh(check: UpdateCheck, current: str, path: Path) -> None:
    latest = check.latest(current, CHECK_TIMEOUT_SECONDS)
    if latest is not None:
        # Semver, or a PEP 440 pre-release such as PyPI lists; anything else is a bug, on stderr
        latest = ToolVersion.of_release(latest).value
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps({"checked_at": time.time(), "latest": latest}))


def _read(path: Path) -> tuple[float, ToolVersion | None] | None:
    """The cache, or None when it is missing or not one treaty wrote"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    checked, latest = raw.get("checked_at"), raw.get("latest")
    if not isinstance(checked, (int, float)) or isinstance(checked, bool):
        return None
    if latest is None:
        return float(checked), None
    try:
        return float(checked), ToolVersion(str(latest))
    except InvalidValue:
        return None


def _precedence(version: ToolVersion) -> tuple[int, int, int, int]:
    """Semver order of the core version; a pre-release sorts below its release"""
    core = version.value.partition("+")[0]
    numbers, dash, _ = core.partition("-")
    major, minor, patch = (int(p) for p in numbers.split("."))
    return major, minor, patch, 0 if dash else 1
