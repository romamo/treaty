"""First-run setup as an explicit ``init`` command, never a side effect (REQ-F-076).

Treaty's own first-run work needs none: config reads create nothing, and the state
directory is made only by a keyed run, which fails with a structured ``STATE_*`` error.
An app with setup that can fail (a directory, a keypair, a download) passes
``App(init=...)``: the ``init`` built-in runs it, and every other command of the app exits
4 with ``INIT_REQUIRED`` until ``initialized`` holds.
"""

from __future__ import annotations

import errno
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ._errors import CliExit
from ._values import ExitCodeName

if TYPE_CHECKING:
    from ._context import Ctx

INIT_COMMAND = "init"


class Init(Protocol):
    """The app's one-time setup; user code, like a handler"""

    def initialized(self, ctx: Ctx) -> bool:
        """Whether setup has run; cheap, with no side effects"""
        ...

    def run(self, ctx: Ctx) -> None:
        """Do the setup; an ``OSError`` becomes ``INIT_FAILED`` naming the reason"""
        ...


@dataclass(frozen=True, slots=True)
class Initialized:
    effect: str
    """``created`` on the run that set up, ``noop`` after"""
    initialized: bool
    already_initialized: bool


def required(app_name: str) -> CliExit:
    """What a command answers before ``init`` has run"""
    return CliExit(
        ExitCodeName("PRECONDITION"),
        f"Run '{app_name} init' before first use",
        code="INIT_REQUIRED",
        fix_command=f"{app_name} init",
    )


def failed(app_name: str, exc: OSError) -> CliExit:
    """A setup failure, by the class of fix it needs: permissions, disk, network, or io"""
    if isinstance(exc, PermissionError):
        reason = "permissions"
    elif isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        reason = "network"
    elif exc.errno in (errno.ENOSPC, errno.EDQUOT):
        reason = "disk"
    else:
        reason = "io"
    return CliExit(
        ExitCodeName("GENERAL_ERROR"),
        f"{app_name} init failed: {exc.strerror or exc}",
        code="INIT_FAILED",
        context={"reason": reason, "error": type(exc).__name__},
        fix_required=f"fix the {reason} problem, then rerun '{app_name} init'",
    )


def run_init(setup: Init, app_name: str, ctx: Ctx) -> Initialized:
    """The ``init`` built-in: idempotent, and exit 0 with ``already_initialized`` after"""
    if setup.initialized(ctx):
        return Initialized("noop", initialized=True, already_initialized=True)
    try:
        setup.run(ctx)
    except OSError as exc:
        raise failed(app_name, exc) from exc
    return Initialized("created", initialized=True, already_initialized=False)
