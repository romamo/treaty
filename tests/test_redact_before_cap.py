"""Text is redacted before it is cut (#274). The ``THIRD_PARTY_STDOUT`` warning redacts
what was printed before it cuts it to ``TEXT_CAP``: a declared secret the cut splits is
whole when redacted, so no part of it reaches the envelope, stderr, a ``-vv`` trace, or the
audit log. ``SHELL_STRING_PROHIBITED`` quotes the shell string whole, as it is redacted."""

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Flag
from treaty._stdout import TEXT_CAP, TEXT_HELD, quote

SECRET = "Zq9!hunter2-s3cret-Xy"
PAD = "." * (TEXT_CAP - 5)
"""Padding that puts the cap five characters into the secret"""
LONG = "Lq7#" + "k" * 96
"""A secret ten times as long as ``[REDACTED]``: text full of it shrinks as redacted"""


def assert_no_prefix(text: str, secret: str = SECRET) -> None:
    """No prefix of the secret four characters or longer: every longer one holds this one"""
    assert secret[:4] not in text, text[-200:]


def test_a_cut_text_loses_the_tail_a_secret_may_start_in() -> None:
    """Text cut at ``TEXT_HELD`` may end in part of a secret, which redaction leaves; the
    redaction shrinks what comes before it, and the cap would take that part in"""
    text = (LONG * (TEXT_HELD // len(LONG) + 1))[:TEXT_HELD]

    def redact(value: str) -> str:
        return value.replace(LONG, "[REDACTED]")

    shown = quote(text, redact, cut=True)[:TEXT_CAP]
    assert shown.startswith("[REDACTED]")
    assert_no_prefix(shown, LONG)
    assert LONG[:4] in quote(text, redact, cut=False)  # what the cut left


@dataclass(frozen=True, slots=True)
class Args:
    token: str = Flag(default="", description="API token", secret=True)


WRITES: dict[str, Callable[[str], None]] = {
    "one-write": lambda token: print(PAD + token),
    "split": lambda token: (
        sys.stdout.write(PAD + token[:5]),
        sys.stdout.write(token[5:] + "\n"),
    ),
    "lines": lambda token: (print(PAD[:100]), print(PAD[101:] + token)),
}


@pytest.mark.parametrize("how", list(WRITES))
@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_a_printed_secret_the_cap_splits_is_redacted(
    how: str, verbosity: str, tmp_path: Path
) -> None:
    app = App("probe", version="1.0.0")

    @app.command("leak", description="Print the secret", danger_level="safe", exit_codes=())
    def leak(args: Args, ctx: Ctx) -> dict[str, bool]:
        WRITES[how](args.token)
        return {"ok": True}

    log = tmp_path / "audit.jsonl"
    out, err = io.StringIO(), io.StringIO()
    env = {"PROBE_TOKEN": SECRET, "PROBE_AUDIT_LOG": str(log)}
    app.run(
        ["leak", "--token-from-env", "PROBE_TOKEN", verbosity],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
        env=env,
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    [warning] = envelope["warnings"]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    text = warning["context"]["text"]
    assert len(text) <= TEXT_CAP and text.startswith("....")
    assert_no_prefix(text)
    assert_no_prefix(out.getvalue())
    assert_no_prefix(err.getvalue())
    assert_no_prefix(log.read_text(encoding="utf-8"))


APP = """
import os, sys
from dataclasses import dataclass
from treaty import App, Ctx, Flag, intercept_stdout

below = intercept_stdout()
app = App("leaky", version="1.0.0")
CAP = int(os.environ["CAP"])
PAD = "." * (CAP - 5)


@dataclass(frozen=True, slots=True)
class Login:
    token: str = Flag(description="API token", secret=True)


def fd(token):
    os.write(1, (PAD + token + "\\n").encode())


def fd_split(token):
    os.write(1, (PAD + token[:5]).encode())
    os.write(1, (token[5:] + "\\n").encode())


def both(token):
    # The stand-in's text and descriptor 1's are joined, then cut: the cap falls in the
    # second, five characters into the secret
    print(PAD[:1000], flush=True)
    os.write(1, (PAD[1001:] + token + "\\n").encode())


def flood(token):
    os.write(1, (token * (int(os.environ["HELD"]) // len(token) + 50)).encode())


WRITES = {"fd": fd, "fd-split": fd_split, "both": both, "flood": flood}


@app.command("leak", description="Write the secret", danger_level="safe", exit_codes=())
def leak(args: Login, ctx: Ctx) -> dict[str, bool]:
    WRITES[os.environ["LEAK"]](args.token)
    return {"ok": True}


app.main()
"""


@pytest.mark.parametrize("how", ["fd", "fd-split", "both", "flood"])
@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_a_secret_written_to_descriptor_1_the_cap_splits_is_redacted(
    how: str, verbosity: str, tmp_path: Path
) -> None:
    secret = LONG if how == "flood" else SECRET
    log = tmp_path / "audit.jsonl"
    env = {
        **os.environ,
        "LEAK": how,
        "CAP": str(TEXT_CAP),
        "HELD": str(TEXT_HELD),
        "LEAKY_TOKEN": secret,
        "LEAKY_AUDIT_LOG": str(log),
        "PYTHONUTF8": "1",
    }
    for name in ("FORCE_COLOR", "TREATY_FORMAT"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-", "leak", "--token-from-env", "LEAKY_TOKEN", verbosity],
        input=APP.encode(),
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    out, err = proc.stdout.decode(), proc.stderr.decode()
    assert proc.returncode == 0, err
    envelope = json.loads(out)
    [warning] = envelope["warnings"]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    text = warning["context"]["text"]
    assert len(text) <= TEXT_CAP and text.startswith("[REDACTED]" if secret == LONG else "....")
    for seen in (text, out, err, log.read_text(encoding="utf-8")):
        assert_no_prefix(seen, secret)


def test_a_shell_string_is_quoted_whole_for_redaction() -> None:
    """A shell string cut before the envelope redacts it would leave the first part of a
    secret the cut split"""
    app = App("probe", version="1.0.0")

    @app.command("shell", description="Run a shell string", danger_level="safe", exit_codes=())
    def shell(args: Args, ctx: Ctx) -> dict[str, bool]:
        line = "curl -H " + "x" * 187 + args.token
        ctx.run(line)  # type: ignore[arg-type]
        return {"ok": True}

    out, err = io.StringIO(), io.StringIO()
    app.run(
        ["shell", "--token-from-env", "PROBE_TOKEN"],
        stdin=io.StringIO(""),
        stdout=out,
        stderr=err,
        env={"PROBE_TOKEN": SECRET, "PROBE_AUDIT_LOG": "0"},
        isatty=False,
    )
    error = json.loads(out.getvalue())["error"]
    assert error["code"] == "SHELL_STRING_PROHIBITED"
    assert error["context"]["argv"].endswith("x[REDACTED]")
    assert_no_prefix(out.getvalue())
    assert_no_prefix(err.getvalue())
