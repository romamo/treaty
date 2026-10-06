"""A network helper module whose client takes ctx.network's settings from module state,
set once by the handler, as a wrapped library's module would (issue 356). Read by the
audit, never run: requests is not installed."""

from pathlib import Path

_proxies: dict[str, str] | None = None
_ca_bundle: Path | None = None


def use_network(proxies: dict[str, str], ca_bundle: Path | None) -> None:
    global _proxies, _ca_bundle
    _proxies, _ca_bundle = dict(proxies), ca_bundle


def get(url: str) -> int:
    with requests.Session() as session:  # noqa: F821
        if _proxies is not None:
            session.trust_env = False
            session.proxies = dict(_proxies)
            session.verify = True if _ca_bundle is None else str(_ca_bundle)
        return int(session.get(url, timeout=5).status_code)
