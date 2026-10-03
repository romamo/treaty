"""An app whose ``mcp serve`` serves a different set of commands per startup mode, and an
approval a person runs, registered ``mcp=False`` (#281). Every handler that runs leaves
its name in the file ``SELECTCTL_RAN`` names, so a test sees what ran."""

import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from treaty import App, Ctx, Flag, McpServe, McpTool, NoArgs


def ran(name: str) -> None:
    marker = os.environ.get("SELECTCTL_RAN")
    if marker is not None:
        with Path(marker).open("a", encoding="utf-8") as f:
            f.write(name + "\n")


@dataclass(frozen=True, slots=True)
class ServeArgs:
    mode: Literal["project", "repository", "none", "unknown", "clash"] = Flag(
        default="project",
        description="project serves every command; repository the brownfield ones and the "
        "approval, which mcp=False keeps off anyway; none only the provided tool; unknown "
        "a path the app lacks; clash a provided tool named like the approval",
    )


SELECTED: Mapping[str, Collection[str] | None] = {
    "project": None,
    "repository": ("fleet", "observe.logs", "approve"),
    "none": (),
    "unknown": ("fleet", "observe.missing"),
    "clash": ("fleet",),
}


def select(args: ServeArgs) -> Collection[str] | None:
    return SELECTED[args.mode]


@dataclass(frozen=True, slots=True)
class Previewed:
    previewed: bool


def preview(arguments: Mapping[str, object], ctx: Ctx) -> Previewed:
    ran("preview")
    return Previewed(True)


def provide(args: ServeArgs, ctx: Ctx) -> list[McpTool]:
    name = {"none": "preview", "clash": "approve"}.get(args.mode)
    if name is None:
        return []
    schema: dict[str, object] = {"type": "object", "properties": {}}
    return [McpTool(name=name, description="Preview", input_schema=schema, handler=preview)]


app = App(
    "selectctl",
    version="1.0.0",
    description="Select control",
    mcp=McpServe(args=ServeArgs, commands=select, tools=provide),
)


@app.command("fleet", description="List the fleet", danger_level="safe", exit_codes=())
def fleet(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    ran("fleet")
    return {"fleet": "ok"}


@app.command("observe.logs", description="Read the logs", danger_level="safe", exit_codes=())
def logs(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    ran("observe.logs")
    return {"logs": "ok"}


@app.command("deploy", description="Deploy", danger_level="mutating", exit_codes=())
def deploy(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    ran("deploy")
    return {"effect": "updated"}


@app.command(
    "approve",
    description="Approve a proposed operation; a person runs this",
    danger_level="mutating",
    exit_codes=(),
    mcp=False,
)
def approve(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    ran("approve")
    return {"effect": "updated"}


# The old name of the approval answers REDIRECTED on the command line
app.redirect("ok", to="approve")


if __name__ == "__main__":
    app.main()
