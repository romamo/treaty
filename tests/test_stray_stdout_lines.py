"""Printed text reaches stderr a line at a time, redacted whole, as descriptor 1's does
(#256): a secret split across two ``sys.stdout.write`` calls is caught, and each line of a
multi-line secret, such as a PEM key, is redacted wherever a line at a time is written."""

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Envelope, Flag

SECRET = "hunter2-s3cret"
PEM = "-----BEGIN TEST KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END TEST KEY-----"
PEM_LINES = PEM.splitlines()
ENV = {"PROBE_TOKEN": SECRET, "PROBE_KEY": PEM}


@dataclass(frozen=True, slots=True)
class Args:
    token: str = Flag(default="", description="API token", secret=True)
    key: str = Flag(default="", description="Private key", secret=True)


WRITES: dict[str, Callable[[Args, Ctx], None]] = {
    "split": lambda args, ctx: (
        sys.stdout.write("token=" + args.token[:5]),
        sys.stdout.write(args.token[5:] + "\n"),
    ),
    "unended": lambda args, ctx: print("token=" + args.token, end=""),
    "pem-lines": lambda args, ctx: [print(line) for line in args.key.splitlines()],
    "pem-split": lambda args, ctx: [
        sys.stdout.write(part)
        for line in args.key.splitlines()
        for part in (line[:9], line[9:], "\n")
    ],
    "waits": lambda args, ctx: (
        sys.stdout.write("partial "),
        ctx.log("between"),
        sys.stdout.write("line\n"),
    ),
    "progress": lambda args, ctx: (
        sys.stdout.write("50%\r"),
        ctx.log("between"),
        sys.stdout.write("done\n"),
    ),
}


def probe_app(how: str) -> App:
    app = App("probe", version="1.0.0")

    @app.command("leak", description="Print the secrets", danger_level="safe", exit_codes=())
    def leak(args: Args, ctx: Ctx) -> dict[str, bool]:
        WRITES[how](args, ctx)
        return {"ok": True}

    return app


def run(how: str, argv: list[str], stdin: str = "") -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    probe_app(how).run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env=ENV, isatty=False
    )
    return out.getvalue(), err.getvalue()


LEAK = ["leak", "--token-from-env", "PROBE_TOKEN", "--key-from-env", "PROBE_KEY"]


def assert_no_secret(text: str) -> None:
    assert SECRET not in text and SECRET[:5] not in text and SECRET[5:] not in text
    for line in PEM_LINES:
        assert line not in text
        assert line[9:] not in text  # the rest of a line split in two writes


@pytest.mark.parametrize("how", ["split", "unended"])
@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_a_secret_split_across_writes_reaches_stderr_redacted(how: str, verbosity: str) -> None:
    out, err = run(how, [*LEAK, verbosity])
    envelope = json.loads(out)
    assert envelope["ok"] is True
    assert_no_secret(out + err)
    assert "token=[REDACTED]" in err  # under -vv, in the "stdout write" trace
    [warning] = envelope["warnings"]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    assert warning["context"]["text"] == "token=[REDACTED]"


@pytest.mark.parametrize("how", ["pem-lines", "pem-split"])
def test_each_line_of_a_multi_line_secret_is_redacted(how: str) -> None:
    out, err = run(how, [*LEAK, "-v"])
    assert_no_secret(out + err)
    assert err.count("[REDACTED]") == 3


@pytest.mark.parametrize("how", ["split", "unended", "pem-split"])
def test_each_exec_line_flushes_and_redacts_its_own_print(how: str) -> None:
    line = json.dumps(
        {"_cmd": "leak", "token_from_env": "PROBE_TOKEN", "key_from_env": "PROBE_KEY"}
    )
    out, err = run(how, ["exec", "-v"], stdin=f"{line}\n{line}\n")
    assert [json.loads(e)["ok"] for e in out.splitlines()] == [True, True]
    assert_no_secret(out + err)


def test_app_call_redacts_each_printed_line_of_a_multi_line_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = probe_app("pem-lines").call("leak", {"key_from_env": "PROBE_KEY"}, env=ENV)
    assert isinstance(called, Envelope) and called.ok
    host = capsys.readouterr().out  # App.call leaves the host's stdout where it was
    assert_no_secret(host)
    assert host.count("[REDACTED]") == 3


def test_a_line_without_its_end_waits_for_the_end() -> None:
    """The timing #256 sets: a partial line reaches stderr once its line ends, or at the
    next envelope, so a log line written in between comes first"""
    _, err = run("waits", [*LEAK, "-v"])
    assert err.index("between") < err.index("partial line\n")


def test_a_carriage_return_passes_the_line_on_at_once() -> None:
    _, err = run("progress", [*LEAK, "-v"])
    assert err.index("50%\r") < err.index("between") < err.index("done\n")


APP = """
import os, subprocess, sys, time
from dataclasses import dataclass
from treaty import App, Ctx, Flag, intercept_stdout

below = intercept_stdout()
app = App("leaky", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Login:
    key: str = Flag(description="Private key", secret=True)


def apart(lines, write):
    # Each line on its own, so the reader never has the whole key in one read
    for line in lines:
        write(line)
        time.sleep(0.1)


CHILD = (
    "import os, sys, time\\n"
    "for line in sys.argv[1].splitlines():\\n"
    "    os.write(1, line.encode() + b'\\\\n'); time.sleep(0.1)"
)
WRITES = {
    "print": lambda key: apart(key.splitlines(), lambda line: print(line, flush=True)),
    "fd": lambda key: apart(key.splitlines(), lambda line: os.write(1, line.encode() + b"\\n")),
    "child": lambda key: subprocess.run([sys.executable, "-c", CHILD, key], check=True),
}


@app.command("leak", description="Write the key a line at a time", danger_level="safe",
             exit_codes=())
def leak(args: Login, ctx: Ctx) -> dict[str, str]:
    WRITES[os.environ["LEAK"]](args.key)
    return {"status": "ok"}


if sys.argv[1] == "call":
    envelope = app.call("leak", {"key_from_env": "LEAKY_KEY"})
    sys.stderr.write("call ok " + str(envelope.ok) + "\\n")
    below.close()
else:
    app.main()
"""


def leaky(argv: list[str], how: str, stdin: str | None = None) -> tuple[bytes, bytes]:
    env = {**os.environ, "LEAK": how, "LEAKY_KEY": PEM, "PYTHONUTF8": "1"}
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-c", APP, *argv],
        input=None if stdin is None else stdin.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, proc.stderr


@pytest.mark.parametrize("how", ["print", "fd", "child"])
@pytest.mark.parametrize("entry", ["main", "exec", "call"])
def test_a_multi_line_secret_written_a_line_at_a_time_is_redacted(how: str, entry: str) -> None:
    line = json.dumps({"_cmd": "leak", "key_from_env": "LEAKY_KEY"})
    argv, stdin = {
        "main": (["leak", "--key-from-env", "LEAKY_KEY", "-v"], None),
        "exec": (["exec", "-v"], f"{line}\n{line}\n"),
        "call": (["call"], None),
    }[entry]
    out, err = leaky(argv, how, stdin)
    text = (out + err).decode()
    # Lines read together are redacted as the whole key, lines read apart one by one
    assert "[REDACTED]" in err.decode()
    for pem_line in PEM_LINES:
        assert pem_line not in text
