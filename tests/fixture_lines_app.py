"""An app whose commands read stdin line by line (#33); runnable as a tool."""

from collections.abc import Iterator
from dataclasses import dataclass

from treaty import App, Ctx, NoArgs

# A 0.5 s idle limit was outlasted on a loaded Windows runner (#172); the idle-timeout
# tests pass a short --timeout of their own
app = App("linectl", version="1.0.0", default_timeout=30, max_line_bytes=64)


@dataclass(frozen=True, slots=True)
class Line:
    n: int
    text: str


@dataclass(frozen=True, slots=True)
class Count:
    lines: int
    first: str | None


@app.command(
    "upper",
    description="Each input line in upper case, as it arrives",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    stdin_input="lines",
)
def upper(args: NoArgs, ctx: Ctx) -> Iterator[Line]:
    for n, text in enumerate(ctx.stdin_lines, 1):
        yield Line(n, text.upper())


@app.command(
    "tally",
    description="One event, the number of input lines, once the input ends",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    stdin_input="lines",
)
def tally(args: NoArgs, ctx: Ctx) -> Iterator[Count]:
    lines = list(ctx.stdin_lines)
    yield Count(len(lines), lines[0] if lines else None)


@app.command(
    "count",
    description="Count the input lines",
    danger_level="safe",
    exit_codes=(),
    stdin_input="lines",
    timeout=10,
)
def count(args: NoArgs, ctx: Ctx) -> Count:
    lines = list(ctx.stdin_lines)
    return Count(len(lines), lines[0] if lines else None)


@app.command(
    "payload",
    description="Count a stdin payload",
    danger_level="safe",
    exit_codes=(),
    stdin_input=True,
)
def payload(args: NoArgs, ctx: Ctx) -> Count:
    text = ctx.stdin_text or ""
    return Count(len(text.splitlines()), None)


@app.command(
    "events",
    description="The lines of a stdin payload, one event each",
    danger_level="safe",
    exit_codes=(),
    streaming=True,
    stdin_input=True,
)
def events(args: NoArgs, ctx: Ctx) -> Iterator[Line]:
    for n, text in enumerate((ctx.stdin_text or "").splitlines(), 1):
        yield Line(n, text)


if __name__ == "__main__":
    app.main()
