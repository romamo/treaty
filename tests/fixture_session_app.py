"""An app for session and process hygiene tests (workstream 09); runnable as a tool."""

import os
import sys
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Arg, Ctx, Flag, NoArgs

app = App("sessionctl", version="1.0.0")


@app.suppress_update_notifier
def quiet_mylib(env: MutableMapping[str, str]) -> None:
    env["MYLIB_NO_UPDATE"] = "1"


@dataclass(frozen=True, slots=True)
class Child:
    code: str = Flag(description="Python source the child runs", multiline=True)


@app.command("child", description="Run a Python child", danger_level="safe", exit_codes=())
def child(args: Child, ctx: Ctx) -> dict[str, str]:
    done = ctx.run([sys.executable, "-c", args.code], check=False)
    return {"stdout": done.stdout, "stderr": done.stderr}


@app.command(
    "child-localized",
    description="Run a Python child in the user's locale",
    danger_level="safe",
    exit_codes=(),
    preserve_locale=True,
)
def child_localized(args: Child, ctx: Ctx) -> dict[str, str]:
    done = ctx.run([sys.executable, "-c", args.code], check=False)
    return {"stdout": done.stdout, "stderr": done.stderr}


@app.command(
    "environ", description="Read this process's environment", danger_level="safe", exit_codes=()
)
def environ(args: NoArgs, ctx: Ctx) -> dict[str, str | None]:
    names = ("CI", "NO_UPDATE_NOTIFIER", "MYLIB_NO_UPDATE", "LC_ALL", "LC_NUMERIC")
    return {name: os.environ.get(name) for name in names}


@dataclass(frozen=True, slots=True)
class Where:
    file: Path = Arg(description="A file to read")


@app.command("read", description="Read a file", danger_level="safe", exit_codes=())
def read(args: Where, ctx: Ctx) -> dict[str, object]:
    return {"file": args.file, "text": args.file.read_text(), "cwd": ctx.cwd}


@app.command("pwd", description="Where a child starts", danger_level="safe", exit_codes=())
def pwd(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    done = ctx.run([sys.executable, "-c", "import os; print(os.getcwd())"])
    return {"child": done.stdout.strip(), "process": os.getcwd()}


@dataclass(frozen=True, slots=True)
class Move:
    to: Path = Arg(description="Where to change to")


@app.command("chdir", description="Change directory", danger_level="safe", exit_codes=())
def chdir(args: Move, ctx: Ctx) -> dict[str, str]:
    os.chdir(args.to)
    return {"now": os.getcwd()}


if __name__ == "__main__":
    app.main()
