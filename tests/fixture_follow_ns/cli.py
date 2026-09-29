"""A namespace-package app, with no ``__init__.py``: its modules are still first-party"""

from fixture_follow_ns import helpers

from treaty import App, Ctx, NoArgs

app = App("nsctl", version="1.0.0")


@app.command("enter", description="Enter a directory", danger_level="safe", exit_codes=())
def enter(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    helpers.enter()
    return {}
