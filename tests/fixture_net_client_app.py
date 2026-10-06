"""Network commands whose own client is configured from ctx.network, as the README shows
(issue 356): the http-client rule only advises, so --strict passes."""

from dataclasses import dataclass

import fixture_net_helpers as net

from treaty import App, Ctx, Flag

app = App("netclient", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Fetch:
    url: str = Flag(description="URL to get")


@dataclass(frozen=True, slots=True)
class Got:
    status: int


@app.command(
    "session",
    description="Get a URL with a requests.Session configured from ctx.network",
    danger_level="safe",
    exit_codes=(),
    has_network_io=True,
    external=True,
)
def session(args: Fetch, ctx: Ctx) -> Got:
    client = requests.Session()  # noqa: F821
    client.trust_env = False  # treaty already read the environment
    client.proxies = ctx.network.proxies
    bundle = ctx.network.ca_bundle
    client.verify = True if bundle is None else str(bundle)
    return Got(client.get(args.url, timeout=ctx.network.timeout(30)).status_code)


@app.command(
    "helper",
    description="Get a URL through a helper module configured from ctx.network",
    danger_level="safe",
    exit_codes=(),
    has_network_io=True,
    external=True,
)
def helper(args: Fetch, ctx: Ctx) -> Got:
    net.use_network(ctx.network.proxies, ctx.network.ca_bundle)
    return Got(net.get(args.url))
