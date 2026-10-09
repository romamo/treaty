"""``treaty-mcp module:app`` ends as an app's own ``mcp serve`` does (REQ-C-032, D-13,
#418): closing stdin exits 0 with no envelope, and SIGINT exits 130 and SIGTERM 143 with
the CANCELLED envelope on the last line of stderr, while stdin stays open."""

import json
import signal
from pathlib import Path

import pytest
from conftest import needs_posix_signals
from test_mcp_serve import Server

pytest.importorskip("mcp")

TREATY_MCP = [
    "-c",
    "import sys; from treaty._mcp import main; sys.exit(main())",
    "fixture_treaty_mcp_app:app",
]


@pytest.fixture
def server(tmp_path: Path) -> Server:
    started = Server([], tmp_path, command=TREATY_MCP)
    started.initialize()
    answer = started.request("tools/call", {"name": "ping", "arguments": {}})
    assert answer["result"]["structuredContent"]["data"] == {"pong": "yes"}  # type: ignore[index]
    return started


def assert_only_the_protocol_on_stdout(server: Server) -> None:
    assert server.lines
    for line in server.lines:
        assert json.loads(line)["jsonrpc"] == "2.0"


def test_closing_stdin_exits_0_with_no_envelope_on_stderr(server: Server) -> None:
    assert server.close() == 0, server.stderr()
    assert server.json_lines() == [], server.stderr()
    assert "ping printed this" in server.stderr()  # the stray print reached stderr
    assert "Traceback" not in server.stderr()
    assert_only_the_protocol_on_stdout(server)


@needs_posix_signals
@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_stops_the_server_with_128_plus_n_and_the_envelope_last(
    server: Server, sig: signal.Signals, code: int
) -> None:
    """Stdin stays open: the signal alone stops the server, which the stdin reader and the
    server thread never hold up"""
    server.proc.send_signal(sig)
    assert server.wait() == code, server.stderr()
    envelope = server.envelope()
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "CANCELLED"  # type: ignore[index]
    assert envelope["error"]["context"]["signal"] == sig.name  # type: ignore[index]
    assert envelope["meta"]["exit_code"] == code  # type: ignore[index]
    assert server.json_lines()[-1] == server.stderr().strip().splitlines()[-1]
    assert "Traceback" not in server.stderr()
    assert_only_the_protocol_on_stdout(server)
