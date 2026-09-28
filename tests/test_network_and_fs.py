"""Network and filesystem utilities (workstream 10): REQ-F-036, F-037, F-061, F-063 (the
HTTP half), O-019, O-040. Every HTTP test talks to a real server on a local port."""

import base64
import contextlib
import io
import json
import os
import shlex
import socket
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from treaty import App, Ctx, Exit, Flag, HttpResponse, NoArgs, RegistrationError, Retry
from treaty._audit import Finding, audit
from treaty._http import Http, NetworkFailure, ProxyConfig
from treaty._profile import probes_for
from treaty._retry import Retrier
from treaty._values import ExitCodeName

DATA = Path(__file__).resolve().parent / "data"
CERT = DATA / "test-server.pem"
"""Self-signed for 127.0.0.1 and localhost, a test fixture only"""
KEY = DATA / "test-server.key"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


# Servers


class Origin(BaseHTTPRequestHandler):
    """``/status/N`` answers N, ``/slow`` waits 2 s, ``/flaky`` answers 503 once, as
    ``/flaky-after`` does with ``Retry-After: 3``, and anything else 200 with the path in
    JSON"""

    server: Recording

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.path == "/slow":
            time.sleep(2)
        status = 200
        if self.path.startswith("/status/"):
            status = int(self.path.rsplit("/", 1)[1])
        if self.path in ("/flaky", "/flaky-after") and self.server.flaky:
            self.server.flaky -= 1
            status = 503
        body = json.dumps({"via": "origin", "path": self.path}).encode()
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "7")
        if status == 503 and self.path == "/flaky-after":
            self.send_header("Retry-After", "3")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


class Proxy(BaseHTTPRequestHandler):
    """A forward proxy: answers plain HTTP itself and tunnels CONNECT to the target"""

    server: Recording

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        body = json.dumps({"via": "proxy", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        host, port = self.path.rsplit(":", 1)
        upstream = socket.create_connection((host, int(port)))
        self.send_response(200)
        self.end_headers()
        self.close_connection = True
        back = threading.Thread(target=_pipe, args=(upstream, self.connection), daemon=True)
        back.start()
        _pipe(self.connection, upstream)
        back.join(5)
        upstream.close()

    def log_message(self, format: str, *args: object) -> None:
        pass


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    with contextlib.suppress(ConnectionError):
        while data := source.recv(65536):
            sink.sendall(data)
    with contextlib.suppress(OSError):  # the other side may have closed first
        sink.shutdown(socket.SHUT_WR)


class Recording(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        self.flaky = 0

    @property
    def url(self) -> str:
        scheme = "https" if isinstance(self.socket, ssl.SSLSocket) else "http"
        return f"{scheme}://127.0.0.1:{self.server_port}"


@contextlib.contextmanager
def serving(handler: type[BaseHTTPRequestHandler], *, tls: bool = False) -> Iterator[Recording]:
    server = Recording(handler)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def origin() -> Iterator[Recording]:
    with serving(Origin) as server:
        yield server


@pytest.fixture
def tls_origin() -> Iterator[Recording]:
    with serving(Origin, tls=True) as server:
        yield server


@pytest.fixture
def proxy() -> Iterator[Recording]:
    with serving(Proxy) as server:
        yield server


@pytest.fixture
def refused() -> str:
    """A local URL nothing listens on"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# The app

CHILD = "import os; e = os.environ; print(e.get('HTTPS_PROXY', '') + '|' + e.get('NO_PROXY', ''))"


@dataclass(frozen=True, slots=True)
class Fetch:
    url: str = Flag(description="URL to get")


@dataclass(frozen=True, slots=True)
class Got:
    status: int
    body: str


def net_app() -> App:
    app = App("netctl", version="1.0.0")

    @app.command(
        "get", description="Get a URL", danger_level="safe", exit_codes=(), has_network_io=True
    )
    def get(args: Fetch, ctx: Ctx) -> Got:
        response = ctx.http.get(args.url)
        return Got(response.status, response.text())

    @app.command(
        "authed",
        description="Get a URL that needs credentials",
        danger_level="safe",
        exit_codes=("AUTH_REQUIRED", "PERMISSION_DENIED", "RATE_LIMITED"),
        has_network_io=True,
    )
    def authed(args: Fetch, ctx: Ctx) -> Got:
        response = ctx.http.get(args.url)
        return Got(response.status, response.text())

    @app.command(
        "sturdy",
        description="Get a URL, retrying",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        retry=Retry(retries=2, delay_ms=1),
    )
    def sturdy(args: Fetch, ctx: Ctx) -> Got:
        response = ctx.http.get(args.url)
        return Got(response.status, response.text())

    @app.command(
        "refuse", description="Fail on its own", danger_level="safe", exit_codes=("NOT_FOUND",)
    )
    def refuse(args: NoArgs, ctx: Ctx) -> None:
        raise Exit.NOT_FOUND("no such thing")

    @app.command(
        "child",
        description="Report the child's proxy",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
    )
    def child(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        out = ctx.run([sys.executable, "-c", CHILD]).stdout.strip()
        return {"seen": out}

    @app.command("local", description="No network", danger_level="safe", exit_codes=())
    def local(args: NoArgs, ctx: Ctx) -> None:
        return None

    return app


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, **(env or {})},
    )
    return code, json.loads(out.getvalue())


def error_of(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


def network_of(envelope: dict[str, object]) -> dict[str, object]:
    network = error_of(envelope)["network_context"]
    assert isinstance(network, dict)
    return network


def body_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    decoded = json.loads(str(data["body"]))
    assert isinstance(decoded, dict)
    return decoded


# REQ-F-036: proxy and CA bundle variables


def test_when_https_proxy_is_set_all_outbound_https_requests_use_that_proxy(
    tls_origin: Recording, proxy: Recording
) -> None:
    env = {"HTTPS_PROXY": proxy.url, "REQUESTS_CA_BUNDLE": str(CERT)}
    code, envelope = run(net_app(), ["get", "--url", f"{tls_origin.url}/users"], env)
    assert code == 0, envelope
    assert body_of(envelope) == {"via": "origin", "path": "/users"}
    assert [(m, p) for m, p, _ in proxy.seen] == [
        ("CONNECT", f"127.0.0.1:{tls_origin.server_port}")
    ]


def test_when_http_proxy_is_set_plain_http_requests_use_that_proxy(
    origin: Recording, proxy: Recording
) -> None:
    code, envelope = run(net_app(), ["get", "--url", f"{origin.url}/a"], {"http_proxy": proxy.url})
    assert code == 0
    assert body_of(envelope) == {"via": "proxy", "path": f"{origin.url}/a"}
    assert origin.seen == []


def test_when_no_proxy_is_localhost_requests_to_localhost_bypass_the_proxy(
    origin: Recording, proxy: Recording
) -> None:
    env = {"HTTP_PROXY": proxy.url, "NO_PROXY": "example.com,localhost"}
    url = f"http://localhost:{origin.server_port}/direct"
    code, envelope = run(net_app(), ["get", "--url", url], env)
    assert code == 0
    assert body_of(envelope)["via"] == "origin"
    assert proxy.seen == []


def test_when_requests_ca_bundle_is_set_the_specified_ca_bundle_is_used(
    tls_origin: Recording,
) -> None:
    url = f"{tls_origin.url}/secure"
    code, envelope = run(net_app(), ["get", "--url", url], {"REQUESTS_CA_BUNDLE": str(CERT)})
    assert code == 0 and body_of(envelope)["via"] == "origin"
    code, envelope = run(net_app(), ["get", "--url", url], {"SSL_CERT_FILE": str(CERT)})
    assert code == 0
    # Without it the system store does not know the test CA
    code, envelope = run(net_app(), ["get", "--url", url])
    assert code == 12
    error = error_of(envelope)
    assert error["code"] == "TLS_VERIFY_FAILED"
    assert error["retryable"] is False  # until the CA bundle changes
    assert "REQUESTS_CA_BUNDLE" in str(error["fix_required"])
    assert network_of(envelope)["ssl_verify"] is True


def test_an_unreadable_ca_bundle_exits_4_naming_the_variable(tls_origin: Recording) -> None:
    env = {"REQUESTS_CA_BUNDLE": str(DATA / "missing.pem")}
    code, envelope = run(net_app(), ["get", "--url", tls_origin.url], env)
    assert code == 4
    assert error_of(envelope)["code"] == "CA_BUNDLE_INVALID"
    assert error_of(envelope)["context"] == {
        "variable": "REQUESTS_CA_BUNDLE",
        "path": str(DATA / "missing.pem"),
    }


def test_a_command_author_needs_no_proxy_handling_code_and_proxy_credentials_are_sent(
    tls_origin: Recording, origin: Recording, proxy: Recording
) -> None:
    # The handler is only ctx.http.get(url); credentials in the proxy URL become Basic auth
    authed = f"http://agent:s%40fe@127.0.0.1:{proxy.server_port}"
    env = {"HTTPS_PROXY": authed, "HTTP_PROXY": authed, "REQUESTS_CA_BUNDLE": str(CERT)}
    assert run(net_app(), ["get", "--url", tls_origin.url], env)[0] == 0
    assert run(net_app(), ["get", "--url", origin.url], env)[0] == 0
    expected = "Basic " + base64.b64encode(b"agent:s@fe").decode()
    assert [h.get("Proxy-Authorization") for _, _, h in proxy.seen] == [expected, expected]
    assert all("Proxy-Authorization" not in h for _, _, h in tls_origin.seen)


# REQ-O-019: --proxy and --no-proxy


def test_proxy_flag_routes_all_http_requests_through_that_proxy(
    origin: Recording, proxy: Recording, refused: str
) -> None:
    env = {"HTTP_PROXY": refused}  # overridden
    code, envelope = run(net_app(), ["get", "--url", origin.url, "--proxy", proxy.url], env)
    assert code == 0
    assert body_of(envelope)["via"] == "proxy"


def test_no_proxy_flag_results_in_direct_connections_regardless_of_https_proxy(
    tls_origin: Recording, refused: str
) -> None:
    env = {"HTTPS_PROXY": refused, "REQUESTS_CA_BUNDLE": str(CERT)}
    code, envelope = run(net_app(), ["get", "--url", tls_origin.url, "--no-proxy"], env)
    assert code == 0
    assert body_of(envelope)["via"] == "origin"


def test_the_proxy_url_used_is_reflected_in_network_context_in_error_responses(
    refused: str,
) -> None:
    host = refused.removeprefix("http://")
    code, envelope = run(
        net_app(),
        ["get", "--url", "http://example.test/x", "--proxy", f"http://u:secret@{host}"],
    )
    assert code == 12
    network = network_of(envelope)
    assert network["proxy_used"] == refused  # userinfo removed
    assert network["proxy_source"] == "--proxy"
    assert "secret" not in json.dumps(envelope)


def test_the_flags_are_absent_on_commands_that_declare_no_network_io() -> None:
    app = net_app()
    code, envelope = run(app, ["local", "--schema"])
    flags = envelope["data"]["flags"]  # type: ignore[index]
    assert "proxy" not in flags and "no-proxy" not in flags
    code, envelope = run(app, ["get", "--schema"])
    flags = envelope["data"]["flags"]  # type: ignore[index]
    assert flags["proxy"]["type"] == "string" and flags["no-proxy"]["type"] == "boolean"
    assert run(app, ["local", "--no-proxy"])[0] == 2


def test_proxy_and_no_proxy_together_exit_2() -> None:
    code, envelope = run(
        net_app(), ["get", "--url", "http://x", "--proxy", "http://p:1", "--no-proxy"]
    )
    assert code == 2
    assert "contradict" in str(error_of(envelope)["message"])


def test_a_socks_proxy_exits_2_before_anything_runs() -> None:
    code, envelope = run(net_app(), ["get", "--url", "http://x", "--proxy", "socks5://p:1080"])
    assert code == 2
    assert "SOCKS" in str(error_of(envelope)["message"])


def test_proxy_and_no_proxy_reach_ctx_run_children() -> None:
    code, envelope = run(net_app(), ["child", "--proxy", "http://p:3128"])
    assert code == 0 and envelope["data"] == {"seen": "http://p:3128|"}
    code, envelope = run(net_app(), ["child", "--no-proxy"], {"HTTPS_PROXY": "http://p:1"})
    assert code == 0 and envelope["data"] == {"seen": "http://p:1|*"}


# REQ-F-037: error.network_context


def test_a_connection_failure_error_includes_network_context_proxy_used(
    refused: str, proxy: Recording
) -> None:
    env = {"HTTP_PROXY": proxy.url, "NO_PROXY": "127.0.0.1"}
    code, envelope = run(net_app(), ["get", "--url", f"{refused}/x"], env)
    assert code == 12
    error = error_of(envelope)
    assert error["code"] == "CONNECTION_FAILED" and error["retryable"] is True
    network = network_of(envelope)
    assert "proxy_used" in network
    assert network["url"] == f"{refused}/x"
    assert network["no_proxy"] == "127.0.0.1"
    assert network["proxy_source"] == "NO_PROXY"


def test_when_no_proxy_is_configured_proxy_used_is_null_not_absent(refused: str) -> None:
    code, envelope = run(net_app(), ["get", "--url", refused])
    assert code == 12
    network = network_of(envelope)
    assert network["proxy_used"] is None and network["no_proxy"] is None


def test_network_context_suggestion_contains_an_executable_shell_command(refused: str) -> None:
    code, envelope = run(net_app(), ["get", "--url", f"{refused}/a b"])
    assert code == 12
    suggestion = str(network_of(envelope)["suggestion"])
    assert shlex.split(suggestion) == ["curl", "-v", f"{refused}/a b"]
    host = refused.removeprefix("http://")
    code, envelope = run(net_app(), ["get", "--url", "http://x.test/", "--proxy", f"http://{host}"])
    assert shlex.split(str(network_of(envelope)["suggestion"])) == [
        "curl",
        "-v",
        "--proxy",
        refused,
        "http://x.test/",
    ]


def test_the_network_context_block_is_absent_for_non_network_errors() -> None:
    code, envelope = run(net_app(), ["refuse"])
    assert code == 5
    assert "network_context" not in error_of(envelope)


def test_a_timeout_exits_10_with_network_context(origin: Recording) -> None:
    http = Http(ProxyConfig({}), deadline=time.monotonic() + 0.3, retrier=None, declared=())
    with pytest.raises(NetworkFailure) as caught:
        http.get(f"{origin.url}/slow")
    assert caught.value.name == ExitCodeName("TIMEOUT") and caught.value.code == "TIMEOUT"
    assert caught.value.network.url == f"{origin.url}/slow"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_an_upstream_gateway_failure_exits_12_with_the_status(
    origin: Recording, status: int
) -> None:
    code, envelope = run(net_app(), ["get", "--url", f"{origin.url}/status/{status}"])
    assert code == 12
    assert error_of(envelope)["code"] == "UPSTREAM_UNAVAILABLE"
    assert network_of(envelope)["status_code"] == status


def test_other_statuses_are_returned_to_the_handler(origin: Recording) -> None:
    for status in (404, 500):
        code, envelope = run(net_app(), ["get", "--url", f"{origin.url}/status/{status}"])
        assert code == 0 and envelope["data"]["status"] == status  # type: ignore[index]


# REQ-F-063: 401 and 403 from ctx.http


def test_a_401_exits_8_unauthenticated_when_the_command_declares_it(origin: Recording) -> None:
    code, envelope = run(net_app(), ["authed", "--url", f"{origin.url}/status/401"])
    assert code == 8
    error = error_of(envelope)
    assert error["code"] == "UNAUTHENTICATED" and error["retryable"] is False
    assert network_of(envelope)["status_code"] == 401


def test_a_403_exits_7_permission_denied_when_the_command_declares_it(origin: Recording) -> None:
    code, envelope = run(net_app(), ["authed", "--url", f"{origin.url}/status/403"])
    assert code == 7 and error_of(envelope)["code"] == "PERMISSION_DENIED"


def test_a_429_exits_11_with_retry_after_ms_from_the_header(origin: Recording) -> None:
    code, envelope = run(net_app(), ["authed", "--url", f"{origin.url}/status/429"])
    assert code == 11
    assert error_of(envelope)["retry_after_ms"] == 7000


def test_an_undeclared_401_is_the_handlers_to_decide(origin: Recording) -> None:
    code, envelope = run(net_app(), ["get", "--url", f"{origin.url}/status/401"])
    assert code == 0 and envelope["data"]["status"] == 401  # type: ignore[index]


# X5: ctx.http retries through ctx.retry


def test_http_retries_count_in_meta_retries(origin: Recording) -> None:
    origin.flaky = 1
    code, envelope = run(net_app(), ["sturdy", "--url", f"{origin.url}/flaky"])
    assert code == 0
    assert envelope["meta"]["retries"] == 1  # type: ignore[index]


def test_a_retried_request_goes_through_the_proxy_again(
    tls_origin: Recording, proxy: Recording
) -> None:
    tls_origin.flaky = 1
    env = {"HTTPS_PROXY": proxy.url, "REQUESTS_CA_BUNDLE": str(CERT)}
    code, envelope = run(net_app(), ["sturdy", "--url", f"{tls_origin.url}/flaky"], env)
    assert code == 0 and envelope["meta"]["retries"] == 1  # type: ignore[index]
    assert [m for m, _, _ in proxy.seen] == ["CONNECT", "CONNECT"]


def test_a_503s_retry_after_lengthens_the_backoffs_wait(origin: Recording) -> None:
    origin.flaky = 1
    waits: list[float] = []
    policy = Retry(retries=2, delay_ms=1)
    retrier = Retrier(policy, retries=2, delay_ms=1, deadline=None, sleep=waits.append)
    http = Http(ProxyConfig({}), deadline=None, retrier=retrier, declared=())
    assert http.get(f"{origin.url}/flaky-after").status == 200
    assert waits == [3.0] and retrier.count == 1


def test_exhausted_http_retries_keep_network_context(refused: str) -> None:
    code, envelope = run(net_app(), ["sturdy", "--url", refused])
    assert code == 12
    error = error_of(envelope)
    assert error["code"] == "CONNECTION_FAILED"
    assert error["retries_exhausted"] == 2 and error["retryable"] is False
    assert envelope["meta"]["retries"] == 2  # type: ignore[index]
    assert network_of(envelope)["url"] == refused


def test_http_response_decodes_text_and_json(origin: Recording) -> None:
    http = Http(ProxyConfig({}), deadline=None, retrier=None, declared=())
    response = http.post(f"{origin.url}/p", json={"a": 1})
    assert isinstance(response, HttpResponse)
    assert response.json() == {"via": "origin", "path": "/p"}
    assert response.headers["content-type"] == "application/json"
    assert origin.seen[0][2]["Content-Type"] == "application/json"
    with pytest.raises(ValueError, match="http"):
        http.get("file:///etc/passwd")


def test_ctx_http_needs_has_network_io() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="has_network_io"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Fetch, ctx: Ctx) -> None:
            ctx.http.get(args.url)


def test_network_commands_may_exit_12_undeclared() -> None:
    code, envelope = run(net_app(), ["get", "--schema"])
    assert "12" in envelope["data"]["exit_codes"]  # type: ignore[index]
    code, envelope = run(net_app(), ["local", "--schema"])
    assert "12" not in envelope["data"]["exit_codes"]  # type: ignore[index]


# REQ-F-061 and REQ-O-040: ctx.walk


def _symlinks_supported(tmp: Path) -> bool:
    try:
        (tmp / ".probe").symlink_to(tmp, target_is_directory=True)
    except OSError:
        return False
    (tmp / ".probe").unlink()
    return True


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    if not _symlinks_supported(tmp_path):
        pytest.skip("this system cannot create symlinks")
    return tmp_path


@dataclass(frozen=True, slots=True)
class Target:
    path: Path = Flag(description="Directory to delete")


def fs_app() -> App:
    app = App("fsctl", version="1.0.0")

    @app.command(
        "delete",
        description="Count what a recursive delete would remove",
        danger_level="safe",
        exit_codes=(),
        recursive_traversal=True,
    )
    def delete(args: Target, ctx: Ctx) -> dict[str, object]:
        walk = ctx.walk(args.path)
        deepest = max((entry.depth for entry in walk), default=0)
        return {
            "deleted_count": walk.count,
            "symlinks_skipped": walk.symlinks_skipped,
            "deepest": deepest,
        }

    @app.command("flat", description="No traversal", danger_level="safe", exit_codes=())
    def flat(args: NoArgs, ctx: Ctx) -> None:
        return None

    return app


def deep(root: Path, levels: int) -> Path:
    """``root/d1/d2/.../dN`` with a file at the bottom"""
    where = root
    for i in range(1, levels + 1):
        where = where / f"d{i}"
    where.mkdir(parents=True)
    (where / "leaf.txt").write_text("x")
    return root


def looped(root: Path) -> Path:
    """``a/b/c/link_back`` pointing at ``a``, beside two files"""
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "one.txt").write_text("1")
    (root / "a" / "b" / "two.txt").write_text("2")
    (root / "a" / "b" / "c" / "link_back").symlink_to(root / "a", target_is_directory=True)
    return root / "a"


def test_a_recursive_delete_on_a_directory_containing_a_circular_symlink_exits_4_symlink_loop(
    tree: Path,
) -> None:
    code, envelope = run(fs_app(), ["delete", "--path", str(looped(tree))])
    assert code == 4
    error = error_of(envelope)
    assert error["code"] == "SYMLINK_LOOP" and error["retryable"] is False
    assert "no-follow-symlinks" in str(error["hint"])


def test_the_loop_error_includes_path_and_loop_target(tree: Path) -> None:
    root = looped(tree)
    code, envelope = run(fs_app(), ["delete", "--path", str(root)])
    context = error_of(envelope)["context"]
    assert context == {
        "path": str(root / "b" / "c" / "link_back"),
        "loop_target": str(root),
        "completed_count": 2,  # b and c, depth first in name order
    }


def test_max_depth_10_limits_traversal_to_10_directory_levels(tmp_path: Path) -> None:
    root = deep(tmp_path / "ten", 9)  # d9/leaf.txt is level 10
    code, envelope = run(fs_app(), ["delete", "--path", str(root), "--max-depth", "10"])
    assert code == 0 and envelope["data"]["deepest"] == 10  # type: ignore[index]
    root = deep(tmp_path / "eleven", 10)
    code, envelope = run(fs_app(), ["delete", "--path", str(root), "--max-depth", "10"])
    assert code == 4 and error_of(envelope)["code"] == "DEPTH_EXCEEDED"


def test_a_directory_without_circular_symlinks_traverses_normally_to_full_depth(
    tree: Path,
) -> None:
    root = deep(tree / "t", 30)
    # Two links to one directory are not a loop (10-D1)
    (root / "x").symlink_to(root / "d1" / "d2", target_is_directory=True)
    (root / "y").symlink_to(root / "d1" / "d2", target_is_directory=True)
    code, envelope = run(fs_app(), ["delete", "--path", str(root)])
    assert code == 0
    data = envelope["data"]
    assert data["deepest"] == 31  # type: ignore[index]
    assert data["deleted_count"] == 31 + 2 * 30  # type: ignore[index]


def test_no_follow_symlinks_skips_symlinks_and_completes_without_following_circular_refs(
    tree: Path,
) -> None:
    code, envelope = run(fs_app(), ["delete", "--path", str(looped(tree)), "--no-follow-symlinks"])
    assert code == 0
    assert envelope["data"] == {"deleted_count": 5, "symlinks_skipped": 1, "deepest": 3}


def test_max_depth_3_exits_4_with_depth_exceeded_if_the_tree_is_deeper_than_3_levels(
    tmp_path: Path,
) -> None:
    root = deep(tmp_path, 5)
    code, envelope = run(fs_app(), ["delete", "--path", str(root), "--max-depth", "3"])
    assert code == 4
    error = error_of(envelope)
    assert error["code"] == "DEPTH_EXCEEDED"
    assert error["hint"] == "Use --max-depth to adjust the limit"
    assert error["context"] == {"max_depth": 3, "path": str(root / "d1" / "d2" / "d3" / "d4")}


def test_without_no_follow_symlinks_inode_tracking_provides_the_loop_protection(
    tree: Path,
) -> None:
    root = looped(tree)
    assert run(fs_app(), ["delete", "--path", str(root)])[0] == 4
    assert run(fs_app(), ["delete", "--path", str(root), "--no-follow-symlinks"])[0] == 0


def test_schema_for_recursive_commands_lists_no_follow_symlinks_and_max_depth() -> None:
    code, envelope = run(fs_app(), ["delete", "--schema"])
    flags = envelope["data"]["flags"]  # type: ignore[index]
    assert flags["no-follow-symlinks"] == {
        "type": "boolean",
        "required": False,
        "default": False,
        "description": "Walk without entering symlinks; they are listed and counted as skipped",
    }
    assert flags["max-depth"]["type"] == "integer" and flags["max-depth"]["default"] == 50
    code, envelope = run(fs_app(), ["flat", "--schema"])
    flags = envelope["data"]["flags"]  # type: ignore[index]
    assert "max-depth" not in flags and "no-follow-symlinks" not in flags


def test_max_depth_below_1_exits_2(tmp_path: Path) -> None:
    code, envelope = run(fs_app(), ["delete", "--path", str(tmp_path), "--max-depth", "0"])
    assert code == 2


def test_ctx_walk_needs_recursive_traversal() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="recursive_traversal"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Target, ctx: Ctx) -> None:
            ctx.walk(args.path)


# Audit rules and conformance probes


def findings(app: App) -> list[Finding]:
    return [f for r in audit(app, app.name, limit=50).rules for f in r.findings]


def test_audit_flags_a_hand_rolled_walk_and_a_direct_http_call() -> None:
    import os as os_module
    import urllib.request

    app = App("t", version="1.0.0")

    @app.command("scan", description="Scan", danger_level="safe", exit_codes=())
    def scan(args: Target, ctx: Ctx) -> dict[str, int]:
        return {"n": sum(1 for _ in os_module.walk(args.path))}

    @app.command("tidy", description="Tidy", danger_level="safe", exit_codes=())
    def tidy(args: Target, ctx: Ctx) -> dict[str, int]:
        return {"n": len(list(args.path.rglob("*")))}

    @app.command(
        "pull", description="Pull", danger_level="safe", exit_codes=(), has_network_io=True
    )
    def pull(args: Fetch, ctx: Ctx) -> dict[str, int]:
        with urllib.request.urlopen(args.url, timeout=ctx.timeout.seconds) as r:
            return {"status": r.status}

    found = {(f.rule, f.command): f for f in findings(app)}
    assert found["recursive-traversal", "scan"].fix == (
        "recursive_traversal=True, then for entry in ctx.walk(root): ..."
    )
    assert ("recursive-traversal", "tidy") in found
    assert found["http-client", "pull"].fix == "response = ctx.http.get(url)"
    rules = {f.rule for f in (*findings(fs_app()), *findings(net_app()))}
    assert not rules & {"recursive-traversal", "http-client"}


def test_conformance_probes_refuse_a_socks_proxy_and_a_zero_depth() -> None:
    app = App("probectl", version="1.0.0")

    @app.command(
        "ping", description="Ping", danger_level="safe", exit_codes=(), has_network_io=True
    )
    def ping(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"status": ctx.http.get("http://127.0.0.1:9/").status}

    @app.command(
        "walk", description="Walk", danger_level="safe", exit_codes=(), recursive_traversal=True
    )
    def walk(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"count": sum(1 for _ in ctx.walk("."))}

    kinds = {p.name: p.kind for p in probes_for(app)}
    assert kinds["walk --max-depth 0"] == kinds["ping --proxy socks5"] == "invalid"
