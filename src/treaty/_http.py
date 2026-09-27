"""``ctx.http``: a small HTTP client that goes out the way the environment says (REQ-F-036).

Only ``has_network_io=True`` commands get it. It reads ``HTTPS_PROXY``, ``HTTP_PROXY``,
and ``NO_PROXY`` (either case) from the run's environment, never ``os.environ``, sends
Basic proxy auth from a proxy URL's ``user:password@``, and verifies TLS against
``REQUESTS_CA_BUNDLE``, else ``SSL_CERT_FILE``, else the system store. ``--proxy URL``
overrides the variables and ``--no-proxy`` connects directly (REQ-O-019). Every request
waits at most what is left of the command's timeout.

A failed request ends the run with ``error.network_context`` saying how it went out
(REQ-F-037): exit 12 ``CONNECTION_FAILED`` or ``TLS_VERIFY_FAILED``, exit 10 ``TIMEOUT``,
exit 12 ``UPSTREAM_UNAVAILABLE`` for 502, 503, and 504, and, when the command declares
them, exit 8 ``UNAUTHENTICATED`` for 401, exit 7 ``PERMISSION_DENIED`` for 403, and exit
11 ``RATE_LIMITED`` for 429 (REQ-F-063). Any other status is returned. On a ``retry=``
command, connection failures, timeouts, and 502 to 504 are retried through ``ctx.retry``'s
budget, so ``meta.retries`` counts them.
"""

from __future__ import annotations

import base64
import http.client
import json as jsonlib
import shlex
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import SplitResult, unquote, urlsplit

from ._envelope import NetworkContext, without_userinfo
from ._errors import CliExit, ParseError
from ._retry import Retrier
from ._values import ExitCodeName
from ._verbosity import trace

PROXY_FLAG = "proxy"
NO_PROXY_FLAG = "no-proxy"
CA_BUNDLE_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
"""The CA bundle variables, the first set one wins"""
DEFAULT_RETRY_AFTER_MS = 1000
"""A 429 without a readable ``Retry-After`` still says to wait (REQ-C-014)"""


def _proxy_problem(raw: str) -> str | None:
    """Why ``raw`` is no proxy URL ``ctx.http`` can use; None when it is one"""
    parts = urlsplit(raw)
    try:
        parts.port
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
            f"--proxy {without_userinfo(raw)!r} {problem}",
            context={"flag": PROXY_FLAG, "value": without_userinfo(raw)},
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
    without a leading dot), or ``host:port``"""
    host = (url.hostname or "").lower()
    for item in no_proxy.lower().replace(" ", "").split(","):
        if item == "*":
            return True
        name, _, port = item.partition(":")
        name = name.lstrip(".")
        if not name or (port and str(url.port) != port):
            continue
        if host == name or host.endswith("." + name):
            return True
    return False


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
        found = _env(self.env, f"{parts.scheme.upper()}_PROXY")
        if found is None:
            return Route(None, None)
        bypass = _env(self.env, "NO_PROXY")
        if bypass and bypassed(parts, bypass[1]):
            return Route(None, bypass[0])
        name, raw = found
        proxy = raw if "://" in raw else f"http://{raw}"  # host:port, as curl reads it
        problem = _proxy_problem(proxy)
        if problem is not None:
            raise CliExit(
                ExitCodeName("PRECONDITION"),
                f"{name} {without_userinfo(raw)!r} {problem}",
                code="PROXY_INVALID",
                context={"variable": name, "value": without_userinfo(raw)},
                fix_required=f"set {name} to http://host:port, or unset it",
            )
        return Route(proxy, name)

    def child_env(self) -> dict[str, str]:
        """The variables ``ctx.run`` children get, so ``--proxy`` and ``--no-proxy`` reach
        them too"""
        if self.no_proxy_flag:
            return {"NO_PROXY": "*", "no_proxy": "*"}
        if self.flag_proxy is None:
            return {}
        names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        return dict.fromkeys(names, self.flag_proxy)


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
        self.retried: int | None = None
        """The retries ``ctx.retry`` made before this one, when it gave up"""


class _Exhausted(Exception):
    """Carries a transient failure through ``Retrier.call``"""

    def __init__(self, failure: NetworkFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _give_up(exc: BaseException, retried: int) -> BaseException:
    assert isinstance(exc, _Exhausted)
    exc.failure.retried = retried
    return exc.failure


class _Proxied(urllib.request.BaseHandler):
    """Sends each request, redirects included, the way ``ProxyConfig`` routes it; urllib's
    own ``ProxyHandler`` would read ``os.environ`` and the system settings instead"""

    handler_order = 100

    def __init__(self, proxies: ProxyConfig) -> None:
        self.proxies = proxies

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        # A redirect copies the previous hop's headers: its proxy's credentials must not
        # reach a host that bypasses the proxy, or another proxy
        req.remove_header("Proxy-authorization")
        route = self.proxies.route(req.full_url)
        if route.proxy is not None:
            parts = urlsplit(route.proxy)
            if parts.username is not None:
                user = unquote(parts.username)
                password = unquote(parts.password or "")
                token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
                req.add_unredirected_header("Proxy-Authorization", f"Basic {token}")
            host = parts.hostname or ""
            req.set_proxy(f"{host}:{parts.port}" if parts.port else host, parts.scheme)
        return req

    https_request = http_request


class Http:
    """``ctx.http`` of one run"""

    def __init__(
        self,
        proxies: ProxyConfig,
        *,
        deadline: float | None,
        retrier: Retrier | None,
        declared: Collection[ExitCodeName],
    ) -> None:
        self.proxies = proxies
        self.deadline = deadline
        self.retrier = retrier
        self.declared = frozenset(declared)
        self._opener: urllib.request.OpenerDirector | None = None

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResponse:
        return self.request("GET", url, headers=headers)

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        json: object = None,
    ) -> HttpResponse:
        return self.request("POST", url, headers=headers, body=body, json=json)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        json: object = None,
    ) -> HttpResponse:
        """Send one request and return the response; ``json=`` sends a JSON body"""
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"ctx.http takes http:// and https:// URLs, not {url!r}")
        if body is not None and json is not None:
            raise ValueError("pass body= or json=, not both")
        sent = dict(headers or {})
        if json is not None:
            body = jsonlib.dumps(json).encode()
            sent.setdefault("Content-Type", "application/json")

        def once() -> HttpResponse:
            # A new Request each time: sending one routes it through the proxy in place
            return self._send(urllib.request.Request(url, data=body, headers=sent, method=method))

        if self.retrier is None:
            return once()

        def attempt() -> HttpResponse:
            try:
                return once()
            except NetworkFailure as failure:
                if failure.transient:
                    raise _Exhausted(failure) from failure
                raise

        return self.retrier.call(attempt, on=(_Exhausted,), give_up=_give_up)

    def _send(self, request: urllib.request.Request) -> HttpResponse:
        url = request.full_url
        timeout = None
        if self.deadline is not None:
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                raise self._failure(url, "TIMEOUT", "TIMEOUT", "the command's timeout ran out")
        started = time.perf_counter()
        try:
            with self.opener().open(request, timeout=timeout) as reply:
                response = HttpResponse(reply.status, _headers(reply.headers), reply.read())
            failed = False
        except urllib.error.HTTPError as exc:
            response = HttpResponse(exc.code, _headers(exc.headers), exc.read())
            exc.close()
            failed = True
        except urllib.error.URLError as exc:
            raise self._reason(url, exc.reason) from exc
        except (TimeoutError, http.client.HTTPException, OSError) as exc:
            raise self._reason(url, exc) from exc
        # REQ-O-008: --debug shows each request; the writer redacts Authorization
        trace(
            "http request",
            method=request.get_method(),
            url=without_userinfo(url),
            headers=dict(request.header_items()),
            status=response.status,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return self._status(url, response) if failed else response

    def opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            found = next(
                ((n, v) for n in CA_BUNDLE_VARS if (v := self.proxies.env.get(n))),
                None,
            )
            try:
                context = ssl.create_default_context(cafile=None if found is None else found[1])
            except OSError as exc:  # FileNotFoundError, or ssl.SSLError on a bad bundle
                assert found is not None
                raise CliExit(
                    ExitCodeName("PRECONDITION"),
                    f"{found[0]} names a CA bundle that cannot be read: {exc}",
                    code="CA_BUNDLE_INVALID",
                    context={"variable": found[0], "path": found[1]},
                    fix_required=f"point {found[0]} at a PEM file of CA certificates",
                ) from exc
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _Proxied(self.proxies),
                urllib.request.HTTPSHandler(context=context),
            )
        return self._opener

    def _reason(self, url: str, reason: object) -> NetworkFailure:
        if isinstance(reason, TimeoutError):
            return self._failure(url, "TIMEOUT", "TIMEOUT", "the request timed out")
        if isinstance(reason, ssl.SSLCertVerificationError):
            return self._failure(
                url,
                "UNAVAILABLE",
                "TLS_VERIFY_FAILED",
                f"the server's certificate failed verification: {reason.verify_message}",
                permanent=True,
                fix_required="set REQUESTS_CA_BUNDLE or SSL_CERT_FILE to the CA bundle that "
                "signed the server's certificate",
            )
        return self._failure(
            url, "UNAVAILABLE", "CONNECTION_FAILED", f"could not connect: {reason}"
        )

    def _status(self, url: str, response: HttpResponse) -> HttpResponse:
        status = response.status
        retry_after = _retry_after_ms(response.headers.get("retry-after"))
        if status in (502, 503, 504):
            raise self._failure(
                url,
                "UNAVAILABLE",
                "UPSTREAM_UNAVAILABLE",
                f"the server answered {status}",
                status=status,
                retry_after_ms=retry_after,
            )
        mapped = {
            401: ("AUTH_REQUIRED", "UNAUTHENTICATED", "the server needs credentials (401)"),
            403: ("PERMISSION_DENIED", "PERMISSION_DENIED", "the server refused access (403)"),
            429: ("RATE_LIMITED", "RATE_LIMITED", "the server is rate limiting (429)"),
        }.get(status)
        # A status the command cannot exit with is the handler's to decide
        if mapped is None or ExitCodeName(mapped[0]) not in self.declared:
            return response
        name, code, message = mapped
        fixes = {
            401: "supply valid credentials, then retry",
            403: "use a credential that has access to this resource",
        }
        raise self._failure(
            url,
            name,
            code,
            message,
            status=status,
            retry_after_ms=(retry_after or DEFAULT_RETRY_AFTER_MS) if status == 429 else None,
            fix_required=fixes.get(status),
        )

    def _failure(
        self,
        url: str,
        name: str,
        code: str,
        message: str,
        *,
        status: int | None = None,
        permanent: bool = False,
        retry_after_ms: int | None = None,
        fix_required: str | None = None,
    ) -> NetworkFailure:
        route = self.proxies.route(url)
        curl = ["curl", "-v"]
        if route.proxy is not None:
            curl += ["--proxy", without_userinfo(route.proxy)]
        elif self.proxies.no_proxy_flag:
            curl += ["--noproxy", "*"]
        curl.append(without_userinfo(url))
        network = NetworkContext(
            url=url,
            proxy_used=route.proxy,
            proxy_source=route.source,
            no_proxy=self.proxies.no_proxy,
            ssl_verify=True,
            suggestion=shlex.join(curl),
            status_code=status,
        )
        host = urlsplit(url).hostname or url
        return NetworkFailure(
            name,
            f"Request to {host} failed: {message}",
            code=code,
            network=network,
            transient=code in ("CONNECTION_FAILED", "TIMEOUT", "UPSTREAM_UNAVAILABLE"),
            permanent=permanent,
            retry_after_ms=retry_after_ms,
            fix_required=fix_required,
        )


def _headers(message: object) -> dict[str, str]:
    """Response headers by lowercase name; a repeated header joined with ``, ``"""
    out: dict[str, str] = {}
    items = message.items() if isinstance(message, http.client.HTTPMessage) else ()
    for key, value in items:
        name = key.lower()
        out[name] = f"{out[name]}, {value}" if name in out else value
    return out


def _retry_after_ms(raw: str | None) -> int | None:
    """``Retry-After`` in milliseconds: delay seconds or an HTTP date"""
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw) * 1000
    try:
        when = parsedate_to_datetime(raw)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, int((when - datetime.now(UTC)).total_seconds() * 1000))
