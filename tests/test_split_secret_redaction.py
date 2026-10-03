"""A declared secret that a terminal escape splits, such as ``hunte\\x1b[0mr2...``, is whole
once the escape is cleaned away: the heartbeat status, ctx.log and ctx.progress lines, a
crash's message, and a warning are redacted after cleaning too, not only before (#277)"""

import io
import json
import re
from dataclasses import dataclass

import pytest
from conftest import CountedLines

from treaty import App, Ctx, Flag
from treaty._redact import replacer

SECRET = "hunter2-s3cr3t-qzx9"
HEARTBEAT = re.compile(r"\[(\d+)s\] (.*)")


@dataclass(frozen=True, slots=True)
class Login:
    token: str = Flag(description="API token", secret=True)


def split(secret: str) -> str:
    """``secret`` with an SGR reset inside it, past its first four characters"""
    return secret[:5] + "\x1b[0m" + secret[5:]


def leaks(text: str) -> bool:
    """Whether ``text`` holds the secret, or its first or last four characters"""
    return SECRET[:4] in text or SECRET[-4:] in text


def test_a_heartbeat_status_with_a_secret_an_escape_splits_never_shows_it() -> None:
    err = CountedLines(lambda line: HEARTBEAT.fullmatch(line) is not None)
    app = App("beatctl", version="1.0.0")

    @app.command("sync", description="Sync", danger_level="safe",
                 exit_codes=(), heartbeat=True, timeout=30)  # fmt: skip
    def sync(args: Login, ctx: Ctx) -> dict[str, bool]:
        ctx.progress(f"connecting with {split(args.token)}")
        err.wait_for(err.seen + 2)
        return {"done": True}

    code = app.run(
        ["sync", "--heartbeat-interval", "0.05"],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=err,
        env={"BEATCTL_TOKEN": SECRET},
        isatty=False,
    )
    beats = [m[2] for m in map(HEARTBEAT.fullmatch, err.getvalue().splitlines()) if m]
    assert code == 0 and len(beats) >= 2
    assert all(beat == "connecting with [REDACTED]" for beat in beats), beats
    assert not leaks(err.getvalue()), err.getvalue()


def logging_app() -> App:
    app = App("logctl", version="1.0.0")

    @app.command("talk", description="Log", danger_level="safe", exit_codes=())
    def talk(args: Login, ctx: Ctx) -> dict[str, bool]:
        ctx.progress(f"status {split(args.token)}")
        ctx.log(f"log {split(args.token)}", key=split(args.token))
        ctx.warn("TOKEN_SEEN", f"warned {split(args.token)}", key=split(args.token))
        return {"ok": True}

    @app.command("crash", description="Crash", danger_level="safe", exit_codes=())
    def crash(args: Login, ctx: Ctx) -> dict[str, bool]:
        raise ValueError(f"boom {split(args.token)}")

    return app


@pytest.mark.parametrize("command", ["talk", "crash"])
@pytest.mark.parametrize("fmt", ["json", "plain"])
@pytest.mark.parametrize("isatty", [False, True])
def test_log_lines_warnings_and_crashes_never_show_a_secret_an_escape_splits(
    command: str, fmt: str, isatty: bool
) -> None:
    """Off a terminal escapes go and the secret would be whole on stderr; on a color
    terminal the reset stays, but the person reads the secret whole all the same"""
    out, err = io.StringIO(), io.StringIO()
    logging_app().run(
        [command, "--format", fmt, "--verbose"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={"LOGCTL_TOKEN": SECRET},
        isatty=isatty,
    )
    for text in (out.getvalue(), err.getvalue()):
        assert not leaks(text), text
        assert not leaks(re.sub(r"\x1b\[[0-9;]*m", "", text)), text
    if command == "talk" and fmt == "json":
        envelope = json.loads(out.getvalue())
        assert envelope["warnings"][0]["message"] == "warned [REDACTED]"
        assert envelope["warnings"][0]["context"] == {"key": "[REDACTED]"}


def test_the_redaction_keeps_escapes_unless_they_hide_a_secret() -> None:
    redact = replacer({SECRET})
    assert redact(f"\x1b[31mred\x1b[0m {SECRET}") == "\x1b[31mred\x1b[0m [REDACTED]"
    assert redact(f"\x1b[31mred\x1b[0m {split(SECRET)}") == "red [REDACTED]"
    assert redact(f"\x9b31m{SECRET[:6]}\x9d0;t\x9c{SECRET[6:]}") == "[REDACTED]"
