"""What a type checker sees through treaty's decorators: a handler keeps its signature, so
mypy reports a direct call that no longer matches it"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

FORCED_COLOR = ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE")

SNIPPET = """
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("t", version="1.0.0")
group = app.group("g", description="Group")


@dataclass(frozen=True, slots=True)
class Args:
    x: int = Flag(default=0, description="x")


@app.command("go", description="Go", danger_level="safe", exit_codes=())
def go(args: Args, ctx: Ctx, extra: str) -> dict[str, int]:
    return {"x": args.x}


@group.command("sub", description="Sub", danger_level="safe", exit_codes=())
def sub(args: Args, ctx: Ctx, extra: str) -> dict[str, int]:
    return {"x": args.x}


def caller(ctx: Ctx) -> None:
    go(Args(), ctx)
    sub(Args(), ctx)
"""


def test_a_decorated_handler_keeps_its_signature_for_mypy(tmp_path: Path) -> None:
    mypy = shutil.which("mypy", path=str(Path(sys.executable).parent))
    assert mypy is not None, "mypy is not installed in this environment"
    (tmp_path / "snippet.py").write_text(textwrap.dedent(SNIPPET))
    # plain text whatever the caller's shell forces: FORCE_COLOR=3 makes mypy color its output
    env = {k: v for k, v in os.environ.items() if k not in FORCED_COLOR}
    done = subprocess.run(
        [mypy, "--strict", "--no-incremental", "--no-color-output", "snippet.py"],
        cwd=tmp_path,
        env={**env, "NO_COLOR": "1", "MYPY_FORCE_COLOR": "0"},
        capture_output=True,
        text=True,
    )
    missing = [
        line for line in done.stdout.splitlines() if 'Missing positional argument "extra"' in line
    ]
    assert len(missing) == 2, done.stdout
