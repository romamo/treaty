from dataclasses import dataclass
from pathlib import Path

import fixture_follow_helpers

from treaty import App, Ctx, Flag, NoArgs

from . import net, ops  # noqa: F401  net is loaded as the app would have it at run time

app = App("pkgctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Args:
    directory: Path = Flag(default=Path("."), description="Directory")


@dataclass(frozen=True, slots=True)
class Out:
    value: str


@app.command("token", description="Read a token", danger_level="safe", exit_codes=())
def token(args: Args, ctx: Ctx) -> Out:
    return Out(ops.read_token())


@app.command("outside", description="Call another package", danger_level="safe", exit_codes=())
def outside(args: Args, ctx: Ctx) -> Out:
    return Out(fixture_follow_helpers.enter(args.directory))


@app.command("pull", description="Pull through a local import", danger_level="safe", exit_codes=())
def pull(args: NoArgs, ctx: Ctx) -> Out:
    return Out(str(ops.pull()))


@app.command("beyond", description="Import past the package", danger_level="safe", exit_codes=())
def beyond(args: NoArgs, ctx: Ctx) -> Out:
    ops.beyond()
    return Out("")
