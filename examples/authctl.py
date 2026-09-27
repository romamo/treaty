"""Example of credential-gated commands. Run: AUTHCTL_TOKEN=t uv run examples/authctl.py repos list

The fake credential store reads the token from AUTHCTL_TOKEN, its scopes from
AUTHCTL_SCOPES (comma-separated, default repo:read), and an expiry from
AUTHCTL_EXPIRED_AT (ISO 8601). A real app asks its keychain or API.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from treaty import App, Ctx, Expired, NoArgs


class EnvCredentials:
    def active_scopes(self, ctx: Ctx) -> Iterable[str] | Expired | None:
        if not ctx.env.get("AUTHCTL_TOKEN"):
            return None
        expired_at = ctx.env.get("AUTHCTL_EXPIRED_AT")
        if expired_at:
            return Expired(at=datetime.fromisoformat(expired_at))
        return ctx.env.get("AUTHCTL_SCOPES", "repo:read").split(",")


app = App(
    "authctl", version="0.1.0", description="Manage repositories", credentials=EnvCredentials()
)


@dataclass(frozen=True, slots=True)
class Session:
    logged_in: bool
    open_url: str | None = None


@app.command(
    "login",
    description="Log in through the browser, or with a token without one",
    danger_level="safe",
    exit_codes=(),
    auth="browser",
    refreshes_auth=True,
    gui_operations=["browser_open"],
    headless_behavior="emit_in_output",
    examples=[("Log in without a browser", "authctl login --headless")],
)
def login(args: NoArgs, ctx: Ctx) -> Session:
    if ctx.token is not None:
        return Session(logged_in=True)  # a real app checks the token and stores it
    ctx.open_url("https://auth.example.com/authorize?client=authctl")
    return Session(logged_in=False)


repos = app.group("repos", description="Manage repositories")


@repos.command(
    "list",
    description="List the repositories the credential can read",
    danger_level="safe",
    exit_codes=(),
    requires_auth=True,
    required_scopes=["repo:read"],
    examples=[("List repositories", "authctl repos list")],
)
def repos_list(args: NoArgs, ctx: Ctx) -> dict[str, list[str]]:
    return {"repos": ["api", "web"]}


if __name__ == "__main__":
    app.main()
