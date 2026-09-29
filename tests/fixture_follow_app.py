"""An app whose handlers keep their I/O in helpers, for the audit's call following."""

from dataclasses import dataclass
from pathlib import Path

import fixture_follow_helpers as helpers

from treaty import App, Ctx, Flag, NoArgs

app = App("followctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Args:
    directory: Path = Flag(default=Path("."), description="Directory")


@dataclass(frozen=True, slots=True)
class Out:
    value: str


@app.command("go", description="Enter a directory", danger_level="safe", exit_codes=())
def go(args: Args, ctx: Ctx) -> Out:
    return Out(helpers.enter(args.directory))


@app.command("deep", description="Too deep to follow", danger_level="safe", exit_codes=())
def deep(args: Args, ctx: Ctx) -> Out:
    helpers.first(args.directory)
    return Out("")


@app.command("encode", description="Only stdlib below", danger_level="safe", exit_codes=())
def encode(args: Args, ctx: Ctx) -> Out:
    return Out(helpers.encode(str(args.directory)))


@app.command("proxy", description="Call a proxy", danger_level="safe", exit_codes=())
def proxy(args: NoArgs, ctx: Ctx) -> Out:
    helpers.missing()  # type: ignore[attr-defined]
    return Out(helpers.lazy.run("x") + helpers.lazy("x"))


@app.command("peek", description="Call a lambda", danger_level="safe", exit_codes=())
def peek(args: NoArgs, ctx: Ctx) -> Out:
    return Out(str(helpers.fetch_lambda("https://example.com")))


@app.command("note", description="Quote a slug", danger_level="safe", exit_codes=())
def note(args: NoArgs, ctx: Ctx) -> Out:
    return Out(helpers.slug("a b"))


@app.command("pull", description="Call a cached fetch", danger_level="safe", exit_codes=())
def pull(args: NoArgs, ctx: Ctx) -> Out:
    return Out(str(helpers.cached_fetch("https://example.com")))


@app.command("alias", description="Call an aliased import", danger_level="safe", exit_codes=())
def alias(args: NoArgs, ctx: Ctx) -> Out:
    return Out(str(helpers.aliased_fetch("https://example.com")))


@app.command(
    "refresh",
    description="Declared, but untimed",
    danger_level="safe",
    exit_codes=(),
    has_network_io=True,
)
def refresh(args: NoArgs, ctx: Ctx) -> Out:
    return Out(str(helpers.untimed_fetch("https://example.com")))
