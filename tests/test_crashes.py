"""Every exit carries an envelope: handler bugs, broken results, and bad input streams"""

import io
import json
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Exit, Flag, NoArgs, RegistrationError
from treaty._errors import CliExit
from treaty._signals import Cancellation, Cancelled, CancelSignal
from treaty._values import ExitCodeName


@dataclass(frozen=True, slots=True)
class Login:
    token: str = Flag(description="API token")


def crash_app(*, default_timeout: float | None = 60.0) -> App:
    app = App("crashctl", version="1", default_timeout=default_timeout)

    @app.command("boom", description="Raises a bug")
    def boom(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise RuntimeError("handler bug")

    @app.command("login", description="Leaks its secret into an exception")
    def login(args: Login, ctx: Ctx) -> dict[str, str]:
        raise RuntimeError(f"bad token {args.token}")

    @app.command("opaque", description="Returns something JSON cannot hold")
    def opaque(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"x": object()}

    @app.command("where", description="Exits with a Path in its context")
    def where(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.NOT_FOUND("missing", context={"path": Path("/srv/app")})

    @app.command("done", description="Raises SUCCESS")
    def done(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.SUCCESS("done")

    @app.command("scalar", description="Exits with scalar data")
    def scalar(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise CliExit(ExitCodeName("NOT_FOUND"), "missing", data="oops")

    @app.command("swallow", description="Catches everything while being cancelled")
    def swallow(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        try:
            signal.raise_signal(signal.SIGTERM)
        except Exception:  # noqa: BLE001 - the handler bug under test
            return {"swallowed": "yes"}
        return {"swallowed": "no"}

    @app.command("tail", description="Crashes mid-stream", streaming=True)
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        raise RuntimeError("stream bug")

    return app


def run(
    app: App, argv: list[str], *, stdin: io.StringIO | None = None, env: dict | None = None
) -> tuple[int, list[dict], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdin=stdin, stdout=out, stderr=err, env=env or {}, isatty=False)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    validator = spec_validator("response-envelope")
    for line in lines:
        validator.validate(line)
    return code, lines, err.getvalue()


def test_handler_bug_is_general_error_with_traceback_on_stderr() -> None:
    code, [env], err = run(crash_app(), ["boom"])
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"
    assert env["error"]["context"] == {"command": "boom", "exception": "RuntimeError"}
    assert "Traceback" in err and "handler bug" in err


def test_handler_bug_without_timeout_thread_is_caught_too() -> None:
    code, [env], _ = run(crash_app(default_timeout=None), ["boom"])
    assert code == 1 and env["error"]["code"] == "HANDLER_CRASHED"


def test_crash_redacts_secret_values() -> None:
    code, [env], err = run(
        crash_app(), ["login", "--token-from-env", "TOK"], env={"TOK": "s3cr3t-value"}
    )
    assert code == 1 and "s3cr3t-value" not in env["error"]["message"]
    assert "s3cr3t-value" not in err and "[REDACTED]" in err


def test_exec_continues_past_a_crashing_line_with_ignore_errors() -> None:
    plan = io.StringIO('{"_cmd": "boom"}\n{"_cmd": "version"}\n')
    code, lines, _ = run(crash_app(), ["exec", "--ignore-errors"], stdin=plan)
    assert code == 1 and [line["ok"] for line in lines] == [False, True]


def test_stream_crash_ends_with_a_terminal_envelope() -> None:
    code, lines, _ = run(crash_app(), ["tail"])
    assert code == 1 and lines[0]["data"] == {"n": 1}
    assert lines[-1]["error"]["code"] == "HANDLER_CRASHED" and lines[-1]["meta"]["partial"]


@pytest.mark.parametrize(
    ("command", "code"),
    [("opaque", "INVALID_OUTPUT"), ("done", "INVALID_EXIT"), ("scalar", "INVALID_EXIT")],
)
def test_broken_results_are_general_error(command: str, code: str) -> None:
    exit_code, [env], _ = run(crash_app(), [command])
    assert exit_code == 1 and env["error"]["code"] == code


def test_exit_context_is_serialized() -> None:
    code, [env], _ = run(crash_app(), ["where"])
    assert code != 0 and env["error"]["code"] == "NOT_FOUND"
    assert env["error"]["context"] == {"path": "/srv/app"}


def test_signal_cannot_be_swallowed_by_handler_code() -> None:
    code, [env], _ = run(crash_app(default_timeout=None), ["swallow"])
    assert code == 143 and env["error"]["code"] == "CANCELLED"


def test_signal_outside_a_handler_waits_for_the_next_one() -> None:
    cancellation = Cancellation()
    sig = CancelSignal("SIGTERM", 143)
    cancellation.handle(sig, io.StringIO())  # while serializing a finished result
    with pytest.raises(Cancelled):
        with cancellation.armed():
            pytest.fail("the held signal must raise before the next handler starts")


@pytest.mark.parametrize("value", ["inf", "1e12", "nan"])
def test_out_of_range_timeout_is_arg_error(value: str) -> None:
    app = App("t", version="1")

    @dataclass(frozen=True, slots=True)
    class Host:
        host: str = Arg(description="Host")

    @app.command("ping", description="Ping", has_network_io=True)
    def ping(args: Host, ctx: Ctx) -> dict[str, str]:
        return {"host": args.host}

    code, [env], _ = run(app, ["ping", "h", "--timeout", value])
    assert code == 2 and env["error"]["context"]["flag"] == "timeout"


def test_exec_rejects_stdin_that_is_not_utf8() -> None:
    raw = io.BytesIO(b'\xff\xfe{"_cmd":"version"}\n')
    stdin = io.TextIOWrapper(raw, encoding="utf-8", errors="surrogateescape")
    code, [env], _ = run(crash_app(), ["exec"], stdin=stdin)  # type: ignore[arg-type]
    assert code == 2 and env["error"]["code"] == "STDIN_NOT_UTF8"


def test_example_that_is_not_a_shell_command_is_rejected() -> None:
    app = App("t", version="1")
    with pytest.raises(RegistrationError, match="not a valid shell command"):

        @app.command("x", description="X", examples=[("Broken", "t x --name 'unclosed")])
        def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}
