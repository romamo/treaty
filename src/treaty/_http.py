"""``ctx.http``: a small HTTP client that goes out the way the environment says (REQ-F-036).

Only ``has_network_io=True`` commands get it. It reads ``HTTPS_PROXY``, ``HTTP_PROXY``,
and ``NO_PROXY`` (either case) from the run's environment, never ``os.environ``, sends
Basic proxy auth from a proxy URL's ``user:password@``, and verifies TLS against
``REQUESTS_CA_BUNDLE``, else ``SSL_CERT_FILE``, else the system store. ``--proxy URL``
overrides the variables and ``--no-proxy`` connects directly (REQ-O-019). Every request
ends by ``ctx.remaining``'s deadline, the whole of it and not only each socket operation:
a server that trickles its answer has the connection shut at the deadline, and the
request fails with exit 10 ``TIMEOUT`` while the handler still has the reserve before the
hard limit to return what it has (#244).

A failed request ends the run with ``error.network_context`` saying how it went out
(REQ-F-037): exit 12 ``CONNECTION_FAILED`` or ``TLS_VERIFY_FAILED``, exit 10 ``TIMEOUT``,
exit 12 ``UPSTREAM_UNAVAILABLE`` for 502, 503, and 504, and, when the command declares
them, exit 8 ``UNAUTHENTICATED`` for 401, exit 7 ``PERMISSION_DENIED`` for 403, and exit
11 ``RATE_LIMITED`` for 429 (REQ-F-063). Any other status is returned. On a ``retry=``
command, connection failures, timeouts, and 502 to 504 are retried through ``ctx.retry``'s
budget, so ``meta.retries`` counts them; a ``Retry-After`` lengthens the delay. A POST or
PATCH may have taken effect before it failed, so it is retried only when it never reached
the server: a refused connection or an unknown host.

A redirect to another origin (scheme, host, or port) carries only the content
negotiation headers: ``Authorization``, cookies, and any custom header such as an API key
stay with the origin the caller addressed.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import http.client
import json as jsonlib
import math
import shlex
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import SplitResult, unquote, urlsplit

from ._envelope import NetworkContext, proxy_without_userinfo, without_userinfo
from ._errors import CliExit, ParseError
from ._retry import Retrier, retry_after
from ._timeout import Timeout
from ._values import ExitCodeName
from ._verbosity import trace

PROXY_FLAG = "proxy"
NO_PROXY_FLAG = "no-proxy"
CA_BUNDLE_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
"""The CA bundle variables, the first set one wins"""
DEFAULT_RETRY_AFTER_MS = 1000
"""A 429 without a readable ``Retry-After`` still says to wait (REQ-C-014)"""
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
"""RFC 9110 9.2.2: sending one twice has the effect of sending it once"""
CROSS_ORIGIN_HEADERS = frozenset({"Accept", "Accept-encoding", "Accept-language", "User-agent"})
"""The headers a redirect to another origin keeps, in urllib's capitalization"""


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


class _Exhausted(Exception):
    """Carries a transient failure through ``Retrier.call``"""

    def __init__(self, failure: NetworkFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _give_up(exc: BaseException, retried: int) -> BaseException:
    assert isinstance(exc, _Exhausted)
    exc.failure.retried = retried
    return exc.failure


def _asked(exc: BaseException) -> float | None:
    """The seconds a failure's ``Retry-After`` asks for; one too long to sleep gives up"""
    assert isinstance(exc, _Exhausted)
    return retry_after(exc.failure)


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return (
        scheme,
        (parts.hostname or "").lower(),
        parts.port or {"http": 80, "https": 443}.get(scheme),
    )


class _Redirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirects, except that a hop to another origin drops the caller's
    credentials: urllib copies every header to wherever the server points"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in [n for n in new.headers if n not in CROSS_ORIGIN_HEADERS]:
                new.remove_header(name)
        return new


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


_CURRENT = threading.local()
"""The ``_Cutoff`` of the request this thread is sending; urllib builds a request's
connections on the thread that sends it, redirects included"""


class _Cutoff:
    """Shuts one request's connections when its time runs out: urllib's timeout bounds
    each socket operation, so a server that keeps sending a little at a time could hold a
    request well past the deadline (#244). Used as a context manager around the request,
    which it makes this thread's current one"""

    def __init__(self, seconds: float | None) -> None:
        self._lock = threading.Lock()
        self._connections: list[http.client.HTTPConnection] = []
        self._sockets: list[socket.socket] = []
        """Each connection's socket, kept: urllib drops ``connection.sock`` once the
        response arrives, while the response still reads from it"""
        self._stopped = False
        self.fired = False
        """The deadline came while the request was running"""
        self._timer = None if seconds is None else threading.Timer(seconds, self._fire)
        if self._timer is not None:
            self._timer.daemon = True

    def __enter__(self) -> _Cutoff:
        _CURRENT.cutoff = self
        if self._timer is not None:
            self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
        del _CURRENT.cutoff

    def track(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._connections.append(connection)

    def connected(self, connection: http.client.HTTPConnection) -> None:
        """Keep a connected socket; one that connected after the deadline is shut at once"""
        sock = connection.sock
        assert isinstance(sock, socket.socket)
        with self._lock:
            self._sockets.append(sock)
            if self.fired:
                _shut(sock)

    def stop(self) -> bool:
        """End the watch, the request being over; whether the deadline came first"""
        with self._lock:
            self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
        return self.fired

    def _fire(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self.fired = True
            # A connection still connecting has a socket it has not handed over yet
            for sock in [*self._sockets, *(c.sock for c in self._connections)]:
                _shut(sock)


def _shut(sock: socket.socket | None) -> None:
    """Shut a connection both ways, which wakes a read blocked on it on any thread. The
    plain socket's shutdown, also for TLS: ``SSLSocket.shutdown`` drops its TLS state
    under the reading thread"""
    if sock is None:
        return
    # Closed, or no longer connected: the request it carried is already over
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


def _cutoff() -> _Cutoff:
    found = _CURRENT.cutoff
    assert isinstance(found, _Cutoff)
    return found


class _Connection(http.client.HTTPConnection):
    """An HTTP connection its request's ``_Cutoff`` can shut"""

    def __init__(self, host: str, *, cutoff: _Cutoff, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._cutoff = cutoff
        cutoff.track(self)

    def connect(self) -> None:
        super().connect()
        self._cutoff.connected(self)


class _TLSConnection(http.client.HTTPSConnection):
    """An HTTPS connection its request's ``_Cutoff`` can shut"""

    def __init__(self, host: str, *, cutoff: _Cutoff, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._cutoff = cutoff
        cutoff.track(self)

    def connect(self) -> None:
        super().connect()
        self._cutoff.connected(self)


class _CutHTTP(urllib.request.HTTPHandler):
    """urllib's ``http://`` handler on connections the request's ``_Cutoff`` tracks"""

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(functools.partial(_Connection, cutoff=_cutoff()), req)


class _CutHTTPS(urllib.request.HTTPSHandler):
    """urllib's ``https://`` handler on connections the request's ``_Cutoff`` tracks"""

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self.tls = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        connection = functools.partial(_TLSConnection, cutoff=_cutoff())
        return self.do_open(connection, req, context=self.tls)


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

        idempotent = method.upper() in IDEMPOTENT_METHODS

        def attempt() -> HttpResponse:
            try:
                return once()
            except NetworkFailure as failure:
                if failure.transient and (idempotent or failure.unsent):
                    raise _Exhausted(failure) from failure
                raise

        return self.retrier.call(attempt, on=(_Exhausted,), give_up=_give_up, wait=_asked)

    def _send(self, request: urllib.request.Request) -> HttpResponse:
        url = request.full_url
        timeout = None
        if self.deadline is not None:
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                raise self._failure(url, "TIMEOUT", "TIMEOUT", "the command's timeout ran out")
        started = time.perf_counter()
        with _Cutoff(timeout) as cutoff:
            try:
                response, failed = self._exchange(request, timeout)
            except (TimeoutError, http.client.HTTPException, OSError) as exc:
                # URLError is an OSError; a connection shut at the deadline fails as
                # whatever the read was doing, and is a timeout all the same
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                failure = self._timed_out(url) if cutoff.stop() else self._reason(url, reason)
                self._trace(request, started, error=failure.code)
                raise failure from exc
        if cutoff.fired:
            # A body read to the end of a connection shut early would look complete
            failure = self._timed_out(url)
            self._trace(request, started, error=failure.code)
            raise failure
        self._trace(request, started, status=response.status)
        return self._status(url, response) if failed else response

    def _exchange(
        self, request: urllib.request.Request, timeout: float | None
    ) -> tuple[HttpResponse, bool]:
        """The response, and whether urllib raised it as an ``HTTPError``"""
        try:
            with self.opener().open(request, timeout=timeout) as reply:
                return HttpResponse(reply.status, _headers(reply.headers), reply.read()), False
        except urllib.error.HTTPError as exc:
            response = HttpResponse(exc.code, _headers(exc.headers), exc.read())
            exc.close()
            return response, True

    def _timed_out(self, url: str) -> NetworkFailure:
        return self._failure(url, "TIMEOUT", "TIMEOUT", "the request timed out")

    def _trace(self, request: urllib.request.Request, started: float, **outcome: object) -> None:
        """REQ-O-008: --debug shows each request, answered (``status``) or not (``error``);
        the writer redacts Authorization"""
        trace(
            "http request",
            method=request.get_method(),
            url=without_userinfo(request.full_url),
            headers=dict(request.header_items()),
            **outcome,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            found = self.proxies.ca_bundle()
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
                _Redirects(),
                _CutHTTP(),
                _CutHTTPS(context),
            )
        return self._opener

    def _reason(self, url: str, reason: object) -> NetworkFailure:
        if isinstance(reason, TimeoutError):
            return self._timed_out(url)
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
            url,
            "UNAVAILABLE",
            "CONNECTION_FAILED",
            f"could not connect: {reason}",
            unsent=isinstance(reason, (ConnectionRefusedError, socket.gaierror)),
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
        unsent: bool = False,
        retry_after_ms: int | None = None,
        fix_required: str | None = None,
    ) -> NetworkFailure:
        route = self.proxies.route(url)
        curl = ["curl", "-v"]
        if route.proxy is not None:
            curl += ["--proxy", proxy_without_userinfo(route.proxy)]
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
            unsent=unsent,
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
