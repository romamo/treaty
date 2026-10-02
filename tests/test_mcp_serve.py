"""An app's own ``mcp serve`` (#239): startup flags, failures before serving on stderr,
the protocol alone on stdout, and a clean stop on EOF or a signal."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import needs_posix_signals

from treaty import App, Ctx, Flag, McpServe, RegistrationError

pytest.importorskip("mcp")

HERE = Path(__file__).resolve().parent
SERVECTL = HERE / "fixture_mcp_serve_app.py"
TOKEN = "gw-secret-0123456789"
WAIT = 30.0


class Server:
    """``servectl mcp serve`` in a subprocess: JSON-RPC lines in on stdin, out on stdout,
    each stdout line kept for the check that nothing else ever reaches it"""

    def __init__(
        self,
        args: list[str],
        tmp: Path,
        env: dict[str, str] | None = None,
        stdout: int = subprocess.PIPE,
    ) -> None:
        self.stderr_path = tmp / "stderr.txt"
        self.audit = tmp / "audit.jsonl"
        environ = {k: v for k, v in os.environ.items() if not k.startswith("SERVECTL_")}
        environ |= {"SERVECTL_AUDIT_LOG": str(self.audit), **(env or {})}
        environ.pop("FORCE_COLOR", None)
        self._err = self.stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVECTL), "mcp", "serve", *args],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=self._err,
            cwd=HERE,
            env=environ,
        )
        self.lines: list[bytes] = []
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        if self.proc.stdout is not None:
            threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        for line in iter(stdout.readline, b""):
            self.lines.append(line)
            self._queue.put(line)
        self._queue.put(None)

    def send(self, message: dict[str, object]) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(message).encode() + b"\n")
        stdin.flush()

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._id += 1
        message: dict[str, object] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            line = self._queue.get(timeout=WAIT)
            assert line is not None, "the server closed stdout before answering"
            answer = json.loads(line)
            if answer.get("id") == self._id:
                assert isinstance(answer, dict)
                return answer

    def initialize(self) -> dict[str, object]:
        answer = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer

    def close(self) -> int:
        """Close stdin, the client leaving, and wait for the exit code"""
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.close()
        return self.wait()

    def wait(self) -> int:
        code = self.proc.wait(timeout=WAIT)
        self._err.close()
        return code

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8")

    def envelope(self) -> dict[str, object]:
        """The run's envelope: the last stderr line"""
        last = self.stderr().strip().splitlines()[-1]
        loaded = json.loads(last)
        assert isinstance(loaded, dict)
        return loaded

    def audit_entries(self) -> list[dict[str, object]]:
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text().splitlines()]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def server(tmp_path: Path, project: Path) -> Iterator[Server]:
    started = Server(["--project", str(project)], tmp_path, env={"SERVECTL_TOKEN": TOKEN})
    yield started
    if started.proc.poll() is None:
        started.proc.kill()
        started.wait()


def _result(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    assert isinstance(result, dict), answer
    return result


def test_serves_the_commands_and_only_the_protocol_reaches_stdout(server: Server) -> None:
    init = _result(server.initialize())
    assert init["serverInfo"] == {"name": "servectl", "version": "1.2.0"}
    tools = _result(server.request("tools/list"))["tools"]
    assert isinstance(tools, list)
    names = {t["name"] for t in tools}
    assert {"echo", "ping", "manifest"} <= names
    assert "mcp_serve" not in names  # the server is no tool of its own
    called = _result(server.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}}))
    assert called["isError"] is False
    assert called["structuredContent"]["data"] == {"text": "hi"}  # type: ignore[index]
    assert server.close() == 0
    # Every stdout line is a JSON-RPC message: the handler's print() and its write to
    # descriptor 1 went to stderr
    assert server.lines
    for line in server.lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
    envelope = server.envelope()
    assert envelope["ok"] is True
    assert envelope["data"] == {"stopped_by": "eof", "tool_calls": 1}
    warning = envelope["warnings"][0]  # type: ignore[index]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    assert "echo printed hi" in warning["context"]["text"]
    assert "setup printed this" in warning["context"]["text"]
    errors = server.stderr()
    assert "released proj" in errors  # setup's resource was released as the run ended
    assert TOKEN not in errors


def test_a_handler_or_child_reading_stdin_reads_its_end_not_the_protocol(
    server: Server,
) -> None:
    server.initialize()
    called = _result(server.request("tools/call", {"name": "read-stdin", "arguments": {}}))
    body = called["structuredContent"]
    assert isinstance(body, dict)
    # On failure, the envelope (a child's timeout, say) and the server's stderr say why
    assert body["data"] == {"handler": "", "child": ""}, (body, server.stderr())
    # The client's next request still reaches the server
    assert (
        _result(server.request("tools/call", {"name": "ping", "arguments": {}}))["isError"] is False
    )
    assert server.close() == 0


def test_the_client_closing_stdout_stops_the_server_cleanly(tmp_path: Path, project: Path) -> None:
    read, write = os.pipe()
    os.close(read)  # the client stops reading: the first answer meets a closed pipe
    try:
        started = Server(["--project", str(project)], tmp_path, stdout=write)
    finally:
        os.close(write)
    started.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
    )
    try:
        code = started.wait()
    except subprocess.TimeoutExpired:
        started.proc.kill()
        started.proc.wait(timeout=WAIT)
        pytest.fail(f"the server did not stop; its stderr:\n{started.stderr()}")
    assert code == 0, started.stderr()
    envelope = started.envelope()
    assert envelope["data"] == {"stopped_by": "eof", "tool_calls": 0}, started.stderr()
    assert "Traceback" not in started.stderr()


def test_one_audit_entry_per_server_run_with_the_secret_redacted(server: Server) -> None:
    server.initialize()
    server.request("tools/call", {"name": "ping", "arguments": {}})
    assert server.close() == 0
    entries = server.audit_entries()
    served = [e for e in entries if e["command"] == "mcp.serve"]
    assert len(served) == 1
    assert served[0]["exit_code"] == 0
    assert served[0]["args"]["token"] == "[REDACTED]"  # type: ignore[index]
    # Each tool call is an invocation of its command, logged as over treaty-mcp
    assert [e["command"] for e in entries if e["command"] != "mcp.serve"] == ["ping"]
    assert TOKEN not in server.audit.read_text()


def test_a_failure_before_serving_answers_on_stderr_and_writes_nothing_to_stdout(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nowhere"
    started = Server(["--project", str(missing)], tmp_path, env={"SERVECTL_TOKEN": TOKEN})
    assert started.close() == 80
    assert started.lines == []
    envelope = started.envelope()
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PROJECT_INVALID"  # type: ignore[index]
    assert TOKEN not in started.stderr()
    assert [e["exit_code"] for e in started.audit_entries()] == [80]


def test_a_bad_startup_flag_exits_2_on_stderr(tmp_path: Path) -> None:
    started = Server(["--no-such-flag"], tmp_path)
    assert started.close() == 2
    assert started.lines == []
    envelope = started.envelope()
    assert envelope["meta"]["exit_code"] == 2  # type: ignore[index]
    assert envelope["meta"]["command"] == "mcp.serve"  # type: ignore[index]


@needs_posix_signals
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_server_with_exit_0(server: Server, sig: signal.Signals) -> None:
    server.initialize()
    server.request("tools/list")
    server.proc.send_signal(sig)
    assert server.wait() == 0
    envelope = server.envelope()
    assert envelope["data"] == {"stopped_by": sig.name, "tool_calls": 0}
    served = [e for e in server.audit_entries() if e["command"] == "mcp.serve"]
    assert [e["exit_code"] for e in served] == [0]
    assert "released proj" in server.stderr()


# In-process


def _servectl() -> App:
    sys.path.insert(0, str(HERE))
    try:
        from fixture_mcp_serve_app import app
    finally:
        sys.path.remove(str(HERE))
    return app


def test_schema_lists_the_startup_flags_with_the_secret_from_env() -> None:
    out = _run(_servectl(), ["mcp", "serve", "--schema"])
    schema = json.loads(out)["data"]
    flags = schema["flags"]
    assert flags["project"]["required"] is True
    assert {"token-from-env", "token-from-file"} <= set(flags)
    assert "token" not in flags  # a secret is never a value on argv
    assert schema["secret_env_vars"] == ["SERVECTL_TOKEN"]


def test_the_manifest_says_stdout_carries_the_protocol() -> None:
    manifest = json.loads(_run(_servectl(), ["manifest"]))["data"]
    entry = manifest["commands"]["mcp.serve"]
    assert "Stdout carries the MCP protocol" in entry["description"]
    assert entry["exit_codes"]["80"]["name"] == "PROJECT_INVALID"


def test_exec_and_app_call_refuse_it_before_setup_runs(project: Path) -> None:
    envelope = _servectl().call("mcp.serve", {"project": str(project)})
    assert envelope.error is not None
    assert envelope.error.code == "NEEDS_STDIO"
    assert envelope.exit_code == 4


def _run(app: App, argv: list[str]) -> str:
    import io

    out = io.StringIO()
    app.run(argv, stdout=out, stderr=io.StringIO(), env={})
    return out.getvalue()


# Registration


@dataclass(frozen=True, slots=True)
class Startup:
    level: int = Flag(default=1, description="Level")


def test_registration_refuses_what_is_no_mcp_serve() -> None:
    with pytest.raises(RegistrationError, match="args dataclass"):
        McpServe(args=int)
    with pytest.raises(RegistrationError, match="treaty.McpServe"):
        App("x", version="1.0.0", mcp="serve")  # type: ignore[arg-type]
    with pytest.raises(RegistrationError, match="must take"):
        App("x", version="1.0.0", mcp=McpServe(args=Startup, setup=lambda: None))


def test_an_unregistered_exit_code_fails_at_first_use() -> None:
    app = App("x", version="1.0.0", mcp=McpServe(exit_codes=("NOPE",)))
    with pytest.raises(RegistrationError, match="NOPE is not registered"):
        _run(app, ["version"])


def test_no_mcp_serve_without_mcp_serve_declared() -> None:
    app = App("x", version="1.0.0")
    assert all(p.value != "mcp.serve" for p in app.commands)


class Conn:
    @classmethod
    def acquire(cls, args: Startup, ctx: Ctx) -> Conn:
        return cls()


def test_setup_gets_its_resources() -> None:
    def setup(args: Startup, ctx: Ctx, conn: Conn) -> None:
        return None

    app = App("x", version="1.0.0", mcp=McpServe(args=Startup, setup=setup))
    assert app.commands[next(p for p in app.commands if p.value == "mcp.serve")].resources == (
        Conn,
    )
