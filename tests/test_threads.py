"""Concurrent runs of one App, as the MCP server's ``asyncio.to_thread`` tool calls make
them, and as free-threaded CPython (3.14t) runs them truly in parallel (#74)"""

import functools
import io
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag

THREADS = 8
ROUNDS = 2


@dataclass(frozen=True, slots=True)
class Work:
    n: int = Flag(description="Which run this is")
    api_token: str = Flag(description="API token", secret=True)


def token(n: int) -> str:
    return f"sk-live-{n:020d}"


def make_app(barrier: threading.Barrier) -> App:
    app = App("threadctl", version="1.0.0")

    @app.command("work", description="Work", danger_level="safe", exit_codes=())
    def work(args: Work, ctx: Ctx) -> dict[str, int]:
        barrier.wait(timeout=10)  # every run is inside its handler at once
        ctx.log(f"run {args.n} authenticating with {args.api_token}")
        ctx.warn("ODD", f"odd {args.n}", n=args.n)
        print(f"stray {args.n} {args.api_token}")
        barrier.wait(timeout=10)
        return {"n": args.n, "length": len(args.api_token)}

    return app


def run_one(app: App, results: dict[int, tuple[int, str, str]], n: int) -> None:
    out, err = io.StringIO(), io.StringIO()
    argv = ["work", "--n", str(n), "--format", "json", *(["--verbose"] if n % 2 else [])]
    env = {"THREADCTL_API_TOKEN": token(n), "THREADCTL_AUDIT_LOG": "off"}
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=err, env=env, isatty=False)
    results[n] = (code, out.getvalue(), err.getvalue())


def call_one(app: App, results: dict[int, dict[str, object]], n: int) -> None:
    env = {"THREADCTL_API_TOKEN": token(n), "THREADCTL_AUDIT_LOG": "off"}
    envelope = app.call("work", {"n": n}, env=env)
    results[n] = {"ok": envelope.ok, "data": envelope.data, "warnings": envelope.warnings}


def start_all(work: Callable[[int], None]) -> None:
    threads = [threading.Thread(target=work, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()


def test_concurrent_runs_each_get_their_own_envelope_log_and_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    streams = (sys.stdout, sys.stdin)
    for _ in range(ROUNDS):
        app = make_app(threading.Barrier(THREADS))
        results: dict[int, tuple[int, str, str]] = {}
        start_all(functools.partial(run_one, app, results))
        assert sorted(results) == list(range(THREADS))
        for n, (code, out, err) in results.items():
            assert code == 0, err
            lines = out.splitlines()
            assert len(lines) == 1, out  # the envelope alone: no print reached stdout
            envelope = json.loads(lines[0])
            assert envelope["data"] == {"n": n, "length": len(token(n))}
            odd = [w["message"] for w in envelope["warnings"] if w["code"] == "ODD"]
            assert odd == [f"odd {n}"]
            # No run's secret in any run's output, whoever's stderr a print reached
            assert not [t for t in map(token, range(THREADS)) if t in out or t in err], (
                n,
                out,
                err,
            )
            logged = [line for line in err.splitlines() if "authenticating" in line]
            if n % 2:
                assert len(logged) == 1 and f"run {n} authenticating" in logged[0], err
                assert "[REDACTED]" in logged[0]
            else:
                assert logged == [], err
    assert (sys.stdout, sys.stdin) == streams  # the last run out restored them
    assert capsys.readouterr().out == ""


def test_concurrent_app_calls_as_the_mcp_server_makes_them() -> None:
    app = make_app(threading.Barrier(THREADS))
    results: dict[int, dict[str, object]] = {}
    start_all(functools.partial(call_one, app, results))
    assert sorted(results) == list(range(THREADS))
    for n, result in results.items():
        assert result["ok"] is True
        assert result["data"] == {"n": n, "length": len(token(n))}
        warnings = result["warnings"]
        assert isinstance(warnings, tuple)
        assert [w.message for w in warnings if w.code == "ODD"] == [f"odd {n}"]
