"""An app served by ``treaty-mcp fixture_treaty_mcp_app:app`` (#418): a command that
prints to stdout, which must reach stderr, never the protocol."""

from treaty import App, Ctx, NoArgs

app = App("pingctl", version="1.0.0", description="Answers pings")


@app.command("ping", description="Answer a ping", danger_level="safe", exit_codes=())
def ping(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    print("ping printed this")  # a stray print: stderr, never the protocol
    return {"pong": "yes"}
