"""The ``mcp serve`` values every run reads: its command path, the confirmation key, and the
bindings a served command runs with. Kept apart from ``_mcp_serve``, which only an app
with ``App(mcp=)`` or the MCP server loads, so ``import treaty`` stays light (#360)."""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._framework import CONFIRM_FLAG
from ._values import CommandPath, StdioProtocol

if TYPE_CHECKING:
    from ._app import App
    from ._command import Command

MCP_GROUP = CommandPath("mcp")
MCP_SERVE_PATH = MCP_GROUP.child("serve")
MCP_STDIO = StdioProtocol("mcp-stdio")
"""The protocol ``mcp serve`` serves: CommandEntry.protocol (REQ-C-032)"""
CONFIRM_KEY = CONFIRM_FLAG.replace("-", "_")
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"


def protocol_command(app: App, path: CommandPath) -> bool:
    """Whether the command at ``path`` serves a protocol on stdout, as ``mcp serve`` does"""
    command = app.commands.get(path)
    return command is not None and command.protocol is not None


@dataclass(frozen=True, slots=True)
class Bindings:
    """The argument values ``McpServe(bind=)`` fixed for a server run, by payload key, as
    JSON gives them (#285)"""

    values: Mapping[str, object] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", types.MappingProxyType(dict(self.values)))

    def for_command(self, command: Command) -> Mapping[str, object]:
        """The bound values of the fields ``command`` has"""
        keys = {f.key for f in command.fields}
        return types.MappingProxyType({k: v for k, v in self.values.items() if k in keys})


NO_BINDINGS = Bindings()
