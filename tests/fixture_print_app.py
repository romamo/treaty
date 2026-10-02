"""An app whose handler prints its secret, for the MCP server's redaction test."""

import sys
from dataclasses import dataclass

from treaty import App, Ctx, Flag

app = App("printctl", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


@app.command("show", description="Print the token", danger_level="safe", exit_codes=())
def show(args: Login, ctx: Ctx) -> dict[str, bool]:
    print("out", args.api_token)
    sys.stderr.write("err " + args.api_token + "\n")
    return {"ok": True}
