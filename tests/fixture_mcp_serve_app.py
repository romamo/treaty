"""An app with its own ``mcp serve`` (#239): startup flags, a setup that refuses a missing
project, a resource it acquires, and a command that prints to stdout. With ``--catalog``,
the operations a JSON file lists are served as tools of their own (#240)."""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Ctx, Exit, Flag, McpServe, McpTool, NoArgs


@dataclass(frozen=True, slots=True)
class ServeArgs:
    project: Path = Flag(description="Project directory the tools work in")
    token: str | None = Flag(default=None, description="Gateway token", secret=True)
    catalog: Path | None = Flag(default=None, description="Operations catalog, a JSON file")


class Project:
    """A resource setup asks for; its release is written to stderr"""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def acquire(cls, args: ServeArgs, ctx: Ctx) -> Project:
        return cls(args.project)

    def release(self) -> None:
        sys.stderr.write(f"released {self.root.name}\n")


def setup(args: ServeArgs, ctx: Ctx, project: Project) -> None:
    print("setup printed this")  # a stray print: stderr, never the protocol
    if not project.root.is_dir():
        # The token in the message is redacted wherever the envelope goes
        raise Exit.PROJECT_INVALID(
            f"no project at {project.root} for {args.token}",
            context={"project": str(project.root)},
        )


@dataclass(frozen=True, slots=True)
class Decision:
    operation: str
    target: str
    applied: bool


def operation(name: str) -> Callable[[Mapping[str, object], Ctx], Decision]:
    def preview(arguments: Mapping[str, object], ctx: Ctx) -> Decision:
        print("previewing", name)  # a stray print: stderr, never the protocol
        target = arguments["target"]
        assert isinstance(target, str)
        if target == "missing":
            raise Exit.NOT_FOUND(f"no target {target}", context={"target": target})
        return Decision(name, target, applied=False)

    return preview


def provide(args: ServeArgs, ctx: Ctx) -> list[McpTool]:
    """One tool per operation the catalog lists: its name, description, and risk"""
    if args.catalog is None:
        return []
    tools = []
    for entry in json.loads(args.catalog.read_text(encoding="utf-8")):
        risky = entry["risk"] == "destructive"
        tools.append(
            McpTool(
                name=entry["name"],
                description=entry["description"],
                input_schema={
                    "type": "object",
                    "properties": {"target": {"type": "string", "minLength": 1}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
                handler=operation(entry["name"]),
                # A call only previews: read-only, yet it stands for a destructive action
                read_only=True,
                destructive=risky,
                exit_codes=("NOT_FOUND",),
            )
        )
    return tools


def instructions(args: ServeArgs) -> str:
    return f"Preview operations in {args.project.name}; a person approves them."


app = App(
    "servectl",
    version="1.2.0",
    description="Serve control",
    mcp=McpServe(
        args=ServeArgs,
        setup=setup,
        exit_codes=("PROJECT_INVALID",),
        tools=provide,
        instructions=instructions,
    ),
)
app.exit_code("PROJECT_INVALID", 80, description="The project directory is missing",
              retryable=False, side_effects="none")  # fmt: skip


@dataclass(frozen=True, slots=True)
class EchoArgs:
    text: str = Flag(description="Text to echo")


@app.command("echo", description="Echo the text, printing it too", danger_level="safe",
             exit_codes=())  # fmt: skip
def echo(args: EchoArgs, ctx: Ctx) -> dict[str, str]:
    print("echo printed", args.text)  # a stray print: stderr, never the protocol
    os.write(1, b"echo wrote to descriptor 1\n")  # C code or a child: stderr too
    return {"text": args.text}


@app.command("ping", description="Answer pong", danger_level="safe", exit_codes=())
def ping(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"pong": "pong"}


@app.command("read-stdin", description="Read stdin, in the handler and in a child",
             danger_level="safe", exit_codes=())  # fmt: skip
def read_stdin(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    # Neither may block on, or take, the client's requests: both read an empty pipe
    child = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    done = subprocess.run(child, capture_output=True, timeout=5, check=True)
    return {"handler": sys.stdin.read(), "child": done.stdout.decode()}


if __name__ == "__main__":
    app.main()
