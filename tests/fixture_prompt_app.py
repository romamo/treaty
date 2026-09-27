"""An app whose commands ask a person, for interactivity tests; runnable as a tool."""

import sys
from dataclasses import dataclass

from treaty import App, Ctx, Flag, NoArgs

app = App("askctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class InitArgs:
    name: str | None = Flag(default=None, description="Project name")


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    overwrite: bool


@app.command(
    "init",
    description="Create a project, asking for what is missing",
    danger_level="safe",
    exit_codes=(),
    interactive=True,
)
def init(args: InitArgs, ctx: Ctx) -> Project:
    name = args.name if args.name is not None else ctx.prompt("Project name", flag="name")
    return Project(name=name, overwrite=ctx.confirm("Overwrite existing files?"))


@app.command(
    "status",
    description="Show status; declares prompts but never asks",
    danger_level="safe",
    exit_codes=(),
    interactive=True,
)
def status(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"status": "ok"}


@dataclass(frozen=True, slots=True)
class CommitArgs:
    message: str | None = Flag(default=None, description="Commit message", multiline=True)


@app.command(
    "commit",
    description="Record a message, from --message or an editor",
    danger_level="safe",
    exit_codes=(),
    editor_alternatives=["message"],
)
def commit(args: CommitArgs, ctx: Ctx) -> dict[str, str]:
    message = args.message if args.message is not None else ctx.edit("# Describe the change\n")
    return {"message": message}


@app.command("ask", description="Read an answer with input()", danger_level="safe", exit_codes=())
def ask(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    try:
        answer = input("Continue? ")
    except Exception:  # noqa: BLE001 - the stray catch-all the guard must survive
        answer = "swallowed"
    return {"answer": answer}


@app.command("slurp", description="Read piped data", danger_level="safe", exit_codes=())
def slurp(args: NoArgs, ctx: Ctx) -> dict[str, list[str]]:
    return {"lines": sys.stdin.read().splitlines()}


if __name__ == "__main__":
    app.main()
