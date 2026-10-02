"""A declared secret written straight to descriptor 1, by C code or a child, reaches stderr
redacted, as printed text does (#254); stdout still carries only the envelope."""

import json
import os
import subprocess
import sys

import pytest

SECRET = "hunter2-s3cret"

APP = f"""
import os, subprocess, sys
from dataclasses import dataclass
from treaty import App, Ctx, Flag, intercept_stdout

below = intercept_stdout()

app = App("leaky", version="1.0.0")
SECRET = {SECRET!r}


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="API token", secret=True)


def child(code):
    return [sys.executable, "-c", code]


WRITES = {{
    "once": lambda ctx: os.write(1, b"token=" + SECRET.encode() + b"\\n"),
    "split": lambda ctx: (
        os.write(1, b"token=" + SECRET[:5].encode()),
        os.write(1, SECRET[5:].encode() + b"\\n"),
    ),
    "unended": lambda ctx: os.write(1, b"token=" + SECRET.encode()),
    "binary": lambda ctx: os.write(1, b"\\xff" + SECRET.encode() + b"\\xfe\\n"),
    "child": lambda ctx: subprocess.run(
        child(f"import sys; sys.stdout.write('token=' + {{SECRET!r}} + '\\\\n')"), check=True
    ),
    "split-child": lambda ctx: subprocess.run(
        child(
            "import os, time; os.write(1, b'token=' + " + repr(SECRET[:5].encode()) + ")\\n"
            "time.sleep(0.2); os.write(1, " + repr(SECRET[5:].encode()) + " + b'\\\\n')"
        ),
        check=True,
    ),
    "ctx-run": lambda ctx: ctx.run(
        child(f"import os; os.write(1, b'token=' + {{SECRET.encode()!r}} + b'\\\\n')")
    ),
}}


@app.command("leak", description="Write the token to descriptor 1", danger_level="safe",
             exit_codes=())
def leak(args: Login, ctx: Ctx) -> dict[str, str]:
    WRITES[os.environ["LEAK"]](ctx)
    return {{"status": "ok"}}


if sys.argv[1] == "call":
    envelope = app.call("leak", {{"api_token_from_env": "LEAKY_TOKEN"}})
    sys.stderr.write("call ok " + str(envelope.ok) + "\\n")
    below.close()
else:
    app.main()
"""


def leaky(argv: list[str], how: str, stdin: str | None = None) -> tuple[bytes, bytes]:
    env = {**os.environ, "LEAK": how, "LEAKY_TOKEN": SECRET, "PYTHONUTF8": "1"}
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


def envelopes(stdout: bytes) -> list[dict[str, object]]:
    """Every line of stdout is an envelope, and nothing else is there"""
    return [json.loads(line) for line in stdout.decode().splitlines()]


def assert_redacted(err: bytes) -> None:
    assert SECRET.encode() not in err
    assert SECRET[:5].encode() not in err and SECRET[5:].encode() not in err
    assert b"token=[REDACTED]" in err


@pytest.mark.parametrize("how", ["once", "split", "unended", "child", "split-child", "ctx-run"])
def test_a_secret_written_to_descriptor_1_reaches_stderr_redacted(how: str) -> None:
    out, err = leaky(["leak", "--api-token-from-env", "LEAKY_TOKEN", "--format", "json"], how)
    [envelope] = envelopes(out)
    assert envelope["ok"] is True
    assert SECRET not in json.dumps(envelope)
    if how == "ctx-run":
        # The child's stdout is captured, never on descriptor 1
        assert SECRET.encode() not in err
    else:
        assert_redacted(err)


def test_bytes_that_are_not_utf8_around_a_secret_pass_through_redacted() -> None:
    out, err = leaky(["leak", "--api-token-from-env", "LEAKY_TOKEN", "--format", "json"], "binary")
    assert len(envelopes(out)) == 1
    assert b"\xff[REDACTED]\xfe\n" in err


@pytest.mark.parametrize("how", ["once", "split", "unended", "child"])
def test_each_exec_line_redacts_what_it_wrote_to_descriptor_1(how: str) -> None:
    line = json.dumps({"_cmd": "leak", "api_token_from_env": "LEAKY_TOKEN"})
    out, err = leaky(["exec"], how, stdin=f"{line}\n{line}\n")
    assert [e["ok"] for e in envelopes(out)] == [True, True]
    assert SECRET.encode() not in err
    assert err.count(b"token=[REDACTED]") == 2


@pytest.mark.parametrize("how", ["once", "split", "unended", "child"])
def test_app_call_under_an_interceptor_redacts_what_it_wrote_to_descriptor_1(how: str) -> None:
    out, err = leaky(["call"], how)
    assert out == b""
    assert b"call ok True\n" in err
    assert_redacted(err)


READER = """
import os
from treaty._stdout import intercept_stdout, redact_with

redact_with(lambda text: text.replace({secret!r}, "[REDACTED]"))
below = intercept_stdout()
below.color = False
{body}
below.close()
"""


def read(body: str) -> bytes:
    """What reaches stderr when the reader is fed ``body``'s reads directly, so a split falls
    between two reads every time"""
    proc = subprocess.run(
        [sys.executable, "-c", READER.format(secret=SECRET, body=body)],
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert proc.stdout == b""
    return proc.stderr


def test_a_secret_split_across_two_reads_of_the_pipe_is_redacted() -> None:
    first, second = b"token=" + SECRET[:5].encode(), SECRET[5:].encode() + b"\nnext"
    body = f"below._pass_on({first!r})\nbelow._pass_on({second!r})\nbelow._pass_on(b' line\\n')"
    assert read(body) == b"token=[REDACTED]\nnext line\n"


def test_a_line_ended_by_a_carriage_return_is_passed_on_at_once() -> None:
    # A progress line rewrites itself with "\r": it reaches stderr before the next arrives
    body = "below._pass_on(b'50%\\r')\nos.write(2, b'|')\nbelow._pass_on(b'done')"
    assert read(body) == b"50%\r|done"


def test_a_line_longer_than_the_cap_is_passed_on_without_its_end() -> None:
    body = "below._pass_on(b'x' * 70000)\nos.write(2, b'|')"
    assert read(body) == b"x" * 70000 + b"|"


def test_a_secret_a_color_splits_is_redacted_with_the_colors_gone() -> None:
    written = b"token=" + SECRET[:5].encode() + b"\x1b[31m" + SECRET[5:].encode() + b"\n"
    assert read(f"below.color = True\nbelow._pass_on({written!r})") == b"token=[REDACTED]\n"
