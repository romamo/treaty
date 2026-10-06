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
from treaty._envelope import clean, open_escape, strip_escapes, terminal_text
from treaty._redact import replacer
from treaty._stdout import LineBuffer

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
    # A beat before the handler's first ctx.progress reports the default status (#375)
    assert set(beats) <= {"running", "connecting with [REDACTED]"}, beats
    assert "connecting with [REDACTED]" in beats, beats
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


def test_a_secret_an_escape_splits_inside_an_unended_c1_osc_never_reaches_the_envelope() -> None:
    """``strip_escapes`` takes an unended 8-bit OSC (``\\x9d``) and the rest of the text
    with it, so the bare text held no secret; ``clean`` keeps the ``\\x9d`` and takes only
    the 7-bit reset out, and the envelope had the secret whole"""
    app = App("c1ctl", version="1.0.0")

    @app.command("talk", description="Warn", danger_level="safe", exit_codes=())
    def talk(args: Login, ctx: Ctx) -> dict[str, bool]:
        ctx.warn("TOKEN_SEEN", f"\x9d{split(args.token)}")
        raise ValueError(f"\x9d{split(args.token)}")

    out, err = io.StringIO(), io.StringIO()
    app.run(
        ["talk", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={"C1CTL_TOKEN": SECRET},
        isatty=False,
    )
    assert not leaks(json.dumps(json.loads(out.getvalue()), ensure_ascii=False))
    assert not leaks(err.getvalue()), err.getvalue()


@pytest.mark.parametrize(
    "text",
    [
        f"\x9d{split(SECRET)}",
        f"x\x9d\x1b[0m{SECRET}",
        f"\x9b\x1b[0m1m{split(SECRET)}",
        f"{SECRET[:5]}\x1b\x1b[0m{SECRET[5:]}",
        f"{SECRET[:5]}\x1b(B\x1b[m{SECRET[5:]}",
        f"{SECRET[:5]}\x1b7{SECRET[5:]}",
        f"{SECRET[:5]}\x1bPq#0\x1b\\{SECRET[5:]}",
        f"{SECRET[:5]}\x1b_app\x07{SECRET[5:]}",
    ],
)
def test_no_cleaning_of_the_redacted_text_makes_a_secret_whole(text: str) -> None:
    """What ``clean``, ``strip_escapes``, or a terminal's text keeps of the redacted text
    holds no secret, whatever escapes were around it"""
    shown = replacer({SECRET})(text)
    views = [shown, clean(shown), strip_escapes(shown), terminal_text(shown, color=True)]
    assert not any(leaks(str(view)) for view in views), views


@pytest.mark.parametrize(
    "escape", ["\x1b(B", "\x1b(B\x1b[m", "\x1b7", "\x1b=", "\x1bPq#0\x1b\\", "\x1b_app\x07"]
)
def test_an_escape_a_terminal_hides_whole_is_taken_out_whole(escape: str) -> None:
    """``tput sgr0`` writes ``\\x1b(B\\x1b[m``: a terminal shows none of it, so neither does
    the cleaned text, and a secret it splits is whole there as on the terminal. What
    ``App.call`` passes to a host's stream as written is redacted too (#261)"""
    assert strip_escapes(f"red{escape}text") == "redtext"
    assert clean(f"red{escape}text") == "redtext"
    raw = LineBuffer(clean=False).add(f"{split_by(escape)}\n", replacer({SECRET}), color=False)
    assert not leaks(raw) and not leaks(strip_escapes(raw)), raw


@pytest.mark.parametrize("head", ["\x1b(", "\x1b ", "\x1bP", "\x1bPq#0", "\x1b_app"])
def test_a_write_that_ends_inside_such_an_escape_holds_it(head: str) -> None:
    """Cleaned on its own, the rest of the escape in the next write would be text"""
    assert open_escape(f"red{head}") == 3


def split_by(escape: str) -> str:
    return SECRET[:5] + escape + SECRET[5:]
