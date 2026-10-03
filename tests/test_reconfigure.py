"""Every stand-in treaty puts on ``sys.stdout``, ``sys.stderr``, or ``sys.stdin`` takes
``TextIOWrapper.reconfigure``'s keywords, as libraries such as ansible-core assume (#288),
and refuses any other as the real stream does. A reconfigure never opens a path around
secret redaction. The scripts go on stdin, as Windows limits a command line's length."""

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Flag, NoArgs
from treaty._prompt import NoPromptStdin

SECRET = "hünter2-sécret"
"""Not ASCII, so an ascii stream with ``errors="replace"`` would show it as ``h?nter2``"""


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="Token", secret=True)


def reconfiguring_app() -> App:
    app = App("printer", version="1.0.0")

    @app.command("login", description="Log in", danger_level="safe", exit_codes=())
    def login(args: Login, ctx: Ctx) -> dict[str, str]:
        sys.stdout.reconfigure(encoding="ascii", errors="replace")
        print(f"using {args.api_token}")
        try:
            sys.stdout.reconfigure(bogus=True)  # type: ignore[call-arg]
        except TypeError as exc:
            refused = str(exc)
        return {"refused": refused}

    @app.command("read", description="Read a line", danger_level="safe", exit_codes=())
    def read(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        try:
            sys.stdin.reconfigure(bogus=True)  # type: ignore[call-arg]
        except TypeError as exc:
            refused = str(exc)
        return {"line": sys.stdin.readline(), "refused": refused}

    return app


def run(argv: list[str], stdin: str = "") -> tuple[int, dict[str, object], str]:
    out, err = io.StringIO(), io.StringIO()
    code = reconfiguring_app().run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        env={"TOKEN": SECRET},
        isatty=False,
    )
    return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()


def test_a_handler_reconfiguring_stdout_still_has_its_printed_secret_redacted() -> None:
    code, envelope, both = run(["login", "--api-token-from-env", "TOKEN", "--verbose"])
    assert code == 0, both
    assert "unexpected keyword argument 'bogus'" in str(envelope["data"])
    assert SECRET not in both and "h?nter2" not in both and "nter2" not in both
    warning = envelope["warnings"][0]  # type: ignore[index]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    assert "[REDACTED]" in warning["context"]["text"]


def test_a_handler_reconfiguring_stdin_still_reads_it() -> None:
    code, envelope, both = run(["read"], stdin="a line\n")
    assert code == 0, both
    data = envelope["data"]
    assert isinstance(data, dict) and data["line"] == "a line\n"
    assert "unexpected keyword argument 'bogus'" in data["refused"]


def test_the_no_prompt_stdin_passes_a_reconfigure_to_the_stream_it_wraps() -> None:
    wrapped = io.TextIOWrapper(io.BytesIO(b"x\n"), encoding="utf-8")
    stdin = NoPromptStdin(wrapped)
    stdin.reconfigure(errors="replace")
    assert wrapped.errors == "replace"
    with pytest.raises(TypeError):
        stdin.reconfigure(bogus=True)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        stdin.reconfigure("utf-8")  # type: ignore[misc]
    with pytest.raises(LookupError):
        stdin.reconfigure(encoding="no-such-codec")
    with pytest.raises(ValueError, match="illegal newline"):
        stdin.reconfigure(newline="x")


@pytest.mark.parametrize(
    "keywords",
    [
        {"encoding": "locale"},
        {"encoding": "rot13"},
        {"encoding": b"utf-8"},
        {"errors": 1},
        {"newline": "x"},
        {"line_buffering": "yes"},
        {"line_buffering": 2},
    ],
)
def test_a_stand_in_takes_and_refuses_what_the_real_stream_does(
    keywords: dict[str, object],
) -> None:
    """``encoding="locale"`` passes and a codec that is not a text encoding is refused, as
    ``TextIOWrapper.reconfigure`` has them"""
    real = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        real.reconfigure(**keywords)  # type: ignore[arg-type]
    except (TypeError, ValueError, LookupError) as exc:
        refused: type[BaseException] | None = type(exc)
    else:
        refused = None
    stand_in = NoPromptStdin(io.StringIO())  # no reconfigure of its own: checks alone
    if refused is None:
        stand_in.reconfigure(**keywords)  # type: ignore[arg-type]
    else:
        with pytest.raises(refused):
            stand_in.reconfigure(**keywords)  # type: ignore[arg-type]


def run_script(script: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-"],
        input=script.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


CALL = f"""
import sys
from dataclasses import dataclass
from treaty import App, Ctx, Flag


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


app = App("libctl", version="1.0.0")


@app.command("show", description="Print", danger_level="safe", exit_codes=())
def show(args: Login, ctx: Ctx) -> dict[str, bool]:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
    print("out", args.api_token)
    print("err", args.api_token, file=sys.stderr)
    return {{"ok": True}}


env = {{"LIBCTL_API_TOKEN": {SECRET!r}, "LIBCTL_AUDIT_LOG": "0"}}
envelope = app.call("show", {{}}, env=env)
print(envelope.ok, sys.stderr.errors)
"""


def test_reconfigure_under_app_call_reaches_the_hosts_stream_and_leaks_no_secret() -> None:
    proc = run_script(CALL)
    assert proc.returncode == 0, proc.stderr
    both = (proc.stdout + proc.stderr).decode()
    assert SECRET not in both and "nter2" not in both
    assert "err [REDACTED]" in both
    # sys.stderr's stand-in passed errors= on to the host's own stderr
    assert proc.stdout.decode().replace("\r\n", "\n").endswith("True replace\n")


ANSIBLE = """
import sys
from treaty import App, Ctx, NoArgs

app = App("inventory", version="1.0.0")


@app.command("show", description="Read the inventory", danger_level="safe", exit_codes=())
def show(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
    from ansible.utils.display import Display

    Display()
    return {"ok": True}


sys.argv = ["inventory", "show", "--format", "json"]
app.main()
"""


def test_ansible_display_set_up_in_a_handler_warns_nothing() -> None:
    pytest.importorskip("ansible.utils.display")
    proc = run_script(ANSIBLE)
    assert proc.returncode == 0, proc.stderr
    assert b"WARNING" not in proc.stderr, proc.stderr
    assert json.loads(proc.stdout)["data"] == {"ok": True}
