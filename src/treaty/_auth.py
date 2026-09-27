"""Credentials: what a command needs from the active credential, and login commands.

Treaty never stores, refreshes, or validates a token. The app answers one question
through ``Credentials.active_scopes``: which scopes the active credential holds, or
``None`` when no one is logged in. From that the framework gates every
``requires_auth=True`` command before its handler runs (REQ-C-029), warns when the
credential holds more than the command needs, and serves ``check-permissions``
(REQ-O-047).

A login command declares ``auth="browser"`` or ``auth="device"`` (REQ-C-021). Both get
``--headless`` and ``--token-env-var NAME`` (REQ-O-033) and read a pre-acquired token into
``ctx.token``; a browser login that cannot reach a person needs one, or exits 4.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from ._errors import CliExit, RegistrationError
from ._values import ExitCodeName, Scope

if TYPE_CHECKING:
    from ._context import Ctx

HEADLESS_FLAG = "headless"
TOKEN_ENV_FLAG = "token-env-var"
OVER_PRIVILEGED = "CREDENTIAL_OVER_PRIVILEGED"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Credentials(Protocol):
    """The app's view of the active credential; user code, like a handler"""

    def active_scopes(self, ctx: Ctx) -> Iterable[Scope | str] | None:
        """The scopes the active credential holds; None when no one is logged in"""
        ...


class AuthKind(StrEnum):
    BROWSER = "browser"
    """Needs a person at a browser; headless runs need a pre-acquired token"""
    DEVICE = "device"
    """Device code flow: prints a code and a URL, works without a terminal"""

    @property
    def headless_supported(self) -> bool:
        return self is AuthKind.DEVICE


def is_env_var_name(name: str) -> bool:
    """A variable name as a shell can export it"""
    return _ENV_NAME.fullmatch(name) is not None


def scope_set(value: Iterable[Scope | str] | None) -> frozenset[Scope] | None:
    """``active_scopes`` as value objects; a bare string would be read letter by letter"""
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError("active_scopes returned a str; return a collection of scopes")
    return frozenset(s if isinstance(s, Scope) else Scope(s) for s in value)


def names(scopes: Iterable[Scope]) -> list[str]:
    return sorted(s.value for s in scopes)


@dataclass(frozen=True, slots=True)
class Coverage:
    """How the active credential compares with what one command requires"""

    required: tuple[Scope, ...]
    active: frozenset[Scope]

    @property
    def missing(self) -> list[Scope]:
        return [s for s in self.required if s not in self.active]

    @property
    def excess(self) -> list[str]:
        """Scopes held beyond the required ones; empty for a scopeless credential"""
        return names(self.active - set(self.required))

    @property
    def over_privileged(self) -> bool:
        """A strict superset of the required scopes; a lacking credential is not one"""
        return not self.missing and bool(self.excess)

    def report(self, path: str) -> dict[str, object]:
        return {
            "command": path,
            "required_scopes": [s.value for s in self.required],
            "active_scopes": names(self.active),
            "over_privileged": self.over_privileged,
        }


def not_logged_in(path: str, login: Sequence[str]) -> CliExit:
    fix = f"log in with: {login[0]}" if login else "log in, then retry"
    return CliExit(
        ExitCodeName("AUTH_REQUIRED"),
        f"Command {path} needs a logged-in credential, and there is none",
        context={"command": path},
        fix_required=fix,
    )


def insufficient(path: str, coverage: Coverage, exit_name: str) -> CliExit:
    """``PERMISSION_DENIED`` when a command runs, ``AUTH_REQUIRED`` from check-permissions"""
    missing = [s.value for s in coverage.missing]
    return CliExit(
        ExitCodeName(exit_name),
        f"The active credential lacks {', '.join(missing)}, which {path} requires",
        code="INSUFFICIENT_SCOPES",
        context={**coverage.report(path), "missing_scopes": missing},
        fix_required=f"use a credential granted {', '.join(missing)}",
    )


def check_declaration(
    path: str,
    *,
    requires_auth: bool,
    required_scopes: Sequence[Scope],
    auth: AuthKind | None,
    token_env_vars: Sequence[str],
    streaming: bool,
) -> None:
    if requires_auth and not required_scopes:
        raise RegistrationError(
            f"{path}: requires_auth=True needs required_scopes=[...], the minimal scopes the "
            "command uses (REQ-C-029)"
        )
    if auth is not None and streaming:
        raise RegistrationError(f"{path}: a login command returns one result; drop streaming=True")
    if token_env_vars and auth is None:
        raise RegistrationError(f"{path}: token_env_vars is for login commands; add auth=...")
    bad = [n for n in token_env_vars if not is_env_var_name(n)]
    if bad:
        raise RegistrationError(f"{path}: token_env_vars {bad} are not environment variable names")
