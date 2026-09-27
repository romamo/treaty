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


@app.command("scratch", description="Use the temp directory", danger_level="safe", exit_codes=())
def scratch(args: NoArgs, ctx: Ctx) -> dict[str, object]:
    note = ctx.tmp_dir / "note.txt"
    note.write_text("x")
    temp = ctx.temp_file(".json")
    code = "import os, tempfile; print(os.environ['TMPDIR'], tempfile.gettempdir())"
    child_tmp, child_gettempdir = ctx.run([sys.executable, "-c", code]).stdout.split()
    return {
        "tmp_dir": ctx.tmp_dir,
        "temp_file": temp,
        "child_tmpdir": child_tmp,
        "child_gettempdir": child_gettempdir,
        "dir_mode": ctx.tmp_dir.stat().st_mode & 0o777,
        "file_mode": temp.stat().st_mode & 0o777,
    }


@dataclass(frozen=True, slots=True)
class Keep:
    keep: int = Flag(default=300, description="Seconds the file is kept")


@app.command("report", description="Write a report file", danger_level="safe", exit_codes=())
def report(args: Keep, ctx: Ctx) -> dict[str, object]:
    path = ctx.output_file("report.json", keep_seconds=args.keep)
    path.write_text("{}")
    return {
        "output_file": path,
        "file_mode": path.stat().st_mode & 0o777,
        "dir_mode": path.parent.stat().st_mode & 0o777,
    }


WATCH_PIDS = """
import os, sys, time
path = os.path.join(os.environ["TMPDIR"], "children.pids")
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    if os.path.exists(path) and str(os.getpid()) in open(path).read().split():
        break
    time.sleep(0.01)
print(open(path).read(), end="")
print(os.getpid())
"""


@app.command("kids", description="A child reads the pid file", danger_level="safe", exit_codes=())
def kids(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    return {"stdout": ctx.run([sys.executable, "-c", WATCH_PIDS]).stdout}


@dataclass(frozen=True, slots=True)
class Hold:
    pid_file: Path = Flag(description="Where the child writes its pid")


@app.command("hold", description="A child that waits", danger_level="safe", exit_codes=())
def hold(args: Hold, ctx: Ctx) -> dict[str, str]:
    code = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    ctx.tmp_dir  # noqa: B018 - the session directory exists before the signal
    ctx.run([sys.executable, "-c", code, str(args.pid_file)])
    return {}


if __name__ == "__main__":
    app.main()
