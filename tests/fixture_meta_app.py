"""A tool run as a real process by test_response_meta: its cwd and children are its own"""

import sys
from dataclasses import dataclass

from treaty import App, Ctx, NoArgs

app = App("metactl", version="2.4.1", description="Report where it runs")


@dataclass(frozen=True, slots=True)
class Where:
    root: str | None
    child_trace: str


@app.command(
    "where",
    description="Show the project root and the trace a child sees",
    danger_level="safe",
    exit_codes=(),
    project_root=("marker.toml",),
)
def where(args: NoArgs, ctx: Ctx) -> Where:
    child = ctx.run([sys.executable, "-c", "import os; print(os.environ.get('TOOL_TRACE_ID', ''))"])
    root = None if ctx.project_root is None else str(ctx.project_root)
    return Where(root, child.stdout.strip())


if __name__ == "__main__":
    app.main()
