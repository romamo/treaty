"""The network values a run needs without ``ctx.http``'s client: ``--proxy`` and
``--no-proxy`` parsing, the run's proxy configuration, ``ctx.network``, and the response
and failure types, kept apart from ``_http`` so ``import treaty`` never loads
``http.client`` and ``ssl`` (#360)."""

from __future__ import annotations

import json as jsonlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from ._envelope import NetworkContext, proxy_without_userinfo
from ._errors import CliExit, ParseError
from ._timeout import Timeout
from ._values import ExitCodeName

PROXY_FLAG = "proxy"
NO_PROXY_FLAG = "no-proxy"
CA_BUNDLE_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
"""The CA bundle variables, the first set one wins"""


def _proxy_problem(raw: str) -> str | None:
    """Why ``raw`` is no proxy URL ``ctx.http`` can use; None when it is one"""
    parts = urlsplit(raw)
    try:
        parts.port  # noqa: B018 - reading the port is what checks it
    except ValueError:
        return "has a port that is not a number from 0 to 65535"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "is not an http:// or https:// proxy URL; SOCKS proxies are not supported"
    return None


def parse_proxy(raw: object) -> str:
    """``--proxy``: an ``http://`` or ``https://`` URL with a host"""
    if not isinstance(raw, str):
        raise ParseError("'proxy' expects a URL", context={"flag": PROXY_FLAG})
    problem = _proxy_problem(raw)
    if problem is not None:
        raise ParseError(
            f"--proxy {proxy_without_userinfo(raw)!r} {problem}",
            context={"flag": PROXY_FLAG, "value": proxy_without_userinfo(raw)},
            suggestion="pass --proxy http://host:port",
        )
    return raw


def _env(env: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """The first set of ``NAME`` and ``name``, with the spelling it was found under"""
    for key in (name, name.lower()):
        value = env.get(key)
        if value:
            return key, value
    return None


def bypassed(url: SplitResult, no_proxy: str) -> bool:
    """``NO_PROXY`` lists the URL's host: ``*``, the host, a domain it is under (with or
    without a leading dot), or ``host:port``, the scheme's port when the URL names none;
    an IPv6 address is bare or in brackets, ``[::1]:8080`` with a port"""
    host = (url.hostname or "").lower()
    url_port = url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)
    for item in no_proxy.lower().replace(" ", "").split(","):
        if item == "*":
            return True
        if item.startswith("["):  # [::1] or [::1]:8080
            name, _, rest = item[1:].partition("]")
            port = rest.removeprefix(":")
        elif item.count(":") > 1:  # a bare IPv6 address, which takes no port
            name, port = item, ""
        else:
            name, _, port = item.partition(":")
        name = name.lstrip(".")
        if not name or (port and str(url_port) != port):
            continue
        if host == name or host.endswith("." + name):
            return True
    return False


_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class Route:
    """How one URL goes out: through ``proxy``, from ``source``, or directly"""

    proxy: str | None
    source: str | None


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """The run's proxy settings: the environment, then ``--proxy`` or ``--no-proxy``"""

    env: Mapping[str, str]
    flag_proxy: str | None = None
    no_proxy_flag: bool = False

    @property
    def no_proxy(self) -> str | None:
        """The hosts that bypass the proxy, as ``NO_PROXY`` lists them"""
        if self.no_proxy_flag:
            return "*"
        found = _env(self.env, "NO_PROXY")
        return None if found is None else found[1]

    def route(self, url: str) -> Route:
        if self.no_proxy_flag:
            return Route(None, f"--{NO_PROXY_FLAG}")
        if self.flag_proxy is not None:
            return Route(self.flag_proxy, f"--{PROXY_FLAG}")
        parts = urlsplit(url)
        if _env(self.env, f"{parts.scheme.upper()}_PROXY") is None:
            return Route(None, None)
        bypass = _env(self.env, "NO_PROXY")
        if bypass and bypassed(parts, bypass[1]):
            return Route(None, bypass[0])
        return self._env_route(parts.scheme)

    def _env_route(self, scheme: str) -> Route:
        """The ``<SCHEME>_PROXY`` variable's proxy, NO_PROXY aside; a ``host:port`` value
        is read as curl reads it, and one that is no http proxy URL exits 4"""
        found = _env(self.env, f"{scheme.upper()}_PROXY")
        if found is None:
            return Route(None, None)
        name, raw = found
        proxy = raw if "://" in raw else f"http://{raw}"
        problem = _proxy_problem(proxy)
        if problem is not None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"{name} {proxy_without_userinfo(raw)!r} {problem}",
                code="PROXY_INVALID",
                context={"variable": name, "value": proxy_without_userinfo(raw)},
                fix_required=f"set {name} to http://host:port, or unset it",
            )
        return Route(proxy, name)

    def by_scheme(self) -> dict[str, str]:
        """``{"http": ..., "https": ...}`` as ``ctx.http`` would go out for each scheme
        before ``NO_PROXY``: empty under ``--no-proxy`` or a ``NO_PROXY`` of ``*``"""
        if self.no_proxy_flag:
            return {}
        if self.flag_proxy is not None:
            return dict.fromkeys(_SCHEMES, self.flag_proxy)
        bypass = _env(self.env, "NO_PROXY")
        if bypass and "*" in bypass[1].replace(" ", "").split(","):
            return {}
        routes = {scheme: self._env_route(scheme).proxy for scheme in _SCHEMES}
        return {scheme: proxy for scheme, proxy in routes.items() if proxy is not None}

    def ca_bundle(self) -> tuple[str, str] | None:
        """The CA bundle variable that is set, first of ``CA_BUNDLE_VARS``, and its value"""
        return next(((n, v) for n in CA_BUNDLE_VARS if (v := self.env.get(n))), None)

    def child_env(self) -> dict[str, str]:
        """The variables ``ctx.run`` children get, so ``--proxy`` and ``--no-proxy`` reach
        them too"""
        if self.no_proxy_flag:
            return {"NO_PROXY": "*", "no_proxy": "*"}
        if self.flag_proxy is None:
            return {}
        names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        return dict.fromkeys(names, self.flag_proxy)


_SCHEMES = ("http", "https")


class NetworkSettings:
    """``ctx.network``: the proxy, CA bundle, and deadline the run resolved, for a client
    other than ``ctx.http``, such as a library's own ``requests.Session``, to go out the
    same way. ``--proxy`` and ``--no-proxy`` win over the run's environment, as for
    ``ctx.http``. A proxy URL keeps its ``user:password@`` for the client to authenticate
    with; the repr removes it"""

    __slots__ = ("_config", "_remaining")

    def __init__(self, config: ProxyConfig, remaining: Callable[[], float | None]) -> None:
        self._config = config
        self._remaining = remaining

    def timeout(self, own: Timeout | float) -> float | None:
        """The ``timeout=`` for one call of the client: its ``own`` timeout, cut to
        ``ctx.remaining`` so the call cannot outlive ``--timeout`` (REQ-C-012). None only
        when neither limits the call. With no time left it exits 10 ``TIMEOUT`` before
        the call goes out, as ``ctx.run`` does"""
        seconds = _timeout(own).seconds
        left = self._remaining()
        if left is None:
            return seconds
        if left <= 0:
            raise CliExit(
                ExitCodeName("TIMEOUT"),
                "No time is left on the command's deadline for another request",
            )
        return left if seconds is None else min(seconds, left)

    def fits(self, attempt: Timeout | float) -> bool:
        """Whether one more attempt taking up to ``attempt`` seconds, its backoff
        included, still ends before the command's deadline: a retry budget stops when it
        does not, so the handler answers with the last failure instead of ``TIMEOUT``.
        Always true without a limit; an attempt of ``Timeout(None)`` fits only then"""
        seconds = _timeout(attempt).seconds
        left = self._remaining()
        return left is None or (seconds is not None and seconds <= left)

    @property
    def proxies(self) -> dict[str, str]:
        """A ``requests``-style mapping, ``{"http": url, "https": url}``, holding the
        schemes that go through a proxy: ``--proxy`` for both, else ``HTTP_PROXY`` and
        ``HTTPS_PROXY`` (either case); empty under ``--no-proxy`` or a ``NO_PROXY`` of
        ``*``. ``NO_PROXY``'s hosts are not in it: ``proxy_for(url)`` applies them. A
        proxy variable that is no http proxy URL exits 4 ``PROXY_INVALID``"""
        return self._config.by_scheme()

    def proxy_for(self, url: str) -> str | None:
        """The proxy ``ctx.http`` would use for ``url``, ``NO_PROXY`` applied; None for a
        direct connection"""
        return self._config.route(url).proxy

    @property
    def ca_bundle(self) -> Path | None:
        """The CA bundle to verify TLS against: ``REQUESTS_CA_BUNDLE``, else
        ``SSL_CERT_FILE``; None for the system store"""
        found = self._config.ca_bundle()
        return None if found is None else Path(found[1])

    def __repr__(self) -> str:
        try:
            shown = repr({k: proxy_without_userinfo(v) for k, v in self.proxies.items()})
        except CliExit as exc:  # a repr names the problem rather than raising it
            shown = f"<{exc.code}>"
        return f"NetworkSettings(proxies={shown}, ca_bundle={self.ca_bundle!r})"


def _timeout(value: Timeout | float) -> Timeout:
    """``value`` as a ``Timeout``: seconds above 0, or a ``Timeout`` as it is"""
    if isinstance(value, Timeout):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected seconds or a treaty.Timeout, not {type(value).__name__}")
    try:
        seconds = float(value)
    except OverflowError:
        seconds = math.inf  # a huge integer: Timeout refuses it as out of range
    return Timeout(seconds)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What ``ctx.http`` got back; ``headers`` keys are lowercase"""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> object:
        return jsonlib.loads(self.body)


class NetworkFailure(CliExit):
    """A failed ``ctx.http`` request, with ``error.network_context`` (REQ-F-037)"""

    def __init__(
        self,
        name: str,
        message: str,
        *,
        code: str,
        network: NetworkContext,
        transient: bool,
        permanent: bool = False,
        unsent: bool = False,
        retry_after_ms: int | None = None,
        fix_required: str | None = None,
    ) -> None:
        super().__init__(
            ExitCodeName(name),
            message,
            code=code,
            retry_after_ms=retry_after_ms,
            fix_required=fix_required,
        )
        self.network = network
        self.transient = transient
        """``ctx.retry`` tries again: a connection failure, a timeout, or 502 to 504"""
        self.permanent = permanent
        """Not retryable whatever the exit code says: a certificate failure"""
        self.unsent = unsent
        """The request never reached a server: a refused connection or an unknown host"""
        self.retried: int | None = None
        """The retries ``ctx.retry`` made before this one, when it gave up"""
