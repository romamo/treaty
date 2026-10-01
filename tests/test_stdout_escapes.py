"""Terminal escapes in what children and C code write to descriptor 1, and in ctx.run's
output: cleaned on stderr as printed text is (#117)."""

import io
import os
import subprocess
import sys

import pytest

from treaty import App, Ctx, NoArgs

OSC52 = "\x1b]52;c;aGVsbG8=\x07"
"""A clipboard write"""

INTERCEPTED = """
import os, subprocess, sys
from treaty._stdout import intercept_stdout

interceptor = intercept_stdout()
if {color} is not None:
    interceptor.color = {color}
{body}
interceptor.close()
"""


def intercepted(body: str, *, color: bool | None = False) -> bytes:
    """What reaches stderr from a process that intercepts descriptor 1 and runs ``body``"""
    script = INTERCEPTED.format(color=color, body=body)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=30,
        check=True,
    )
    assert proc.stdout == b""
    return proc.stderr


def test_a_child_printing_osc_52_leaves_no_raw_escape_on_stderr() -> None:
    child = f"import sys; sys.stdout.write({OSC52 + 'copied'!r} + '\\n')"
    err = intercepted(f"subprocess.run([sys.executable, '-c', {child!r}], check=True)")
    assert b"\x1b" not in err
    assert err.replace(b"\r\n", b"\n") == b"copied\n"


def test_cursor_movement_titles_links_and_c1_controls_go_and_tab_cr_newline_stay() -> None:
    written = (
        b"\x1b[2J\x1b[Hstart\x1b]0;title\x1b\\ \x1b]8;;https://x.test\x07link\x1b]8;;\x07"
        b" \xc2\x9b2Jc1\tend\rover\n"
    )
    err = intercepted(f"os.write(1, {written!r})")
    assert err == b"start link c1\tend\rover\n"


def test_colors_stay_only_where_the_run_may_color() -> None:
    written = b"\x1b[31mred\x1b[0m\x1b[1A\n"
    assert intercepted(f"os.write(1, {written!r})", color=True) == b"\x1b[31mred\x1b[0m\n"
    assert intercepted(f"os.write(1, {written!r})", color=False) == b"red\n"
    # Decided as the run decides: stdout here is a pipe, not a terminal
    assert intercepted(f"os.write(1, {written!r})", color=None) == b"red\n"


def test_an_escape_split_across_two_reads_is_held_until_it_completes() -> None:
    # Fed to the reader directly, so the split falls between the two reads every time
    body = (
        "interceptor._pass_on(b'before \\x1b]0;my ti')\n"
        "interceptor._pass_on(b'tle\\x07after \\xe2\\x82')\n"
        "interceptor._pass_on(b'\\xac\\n')"
    )
    assert intercepted(body) == "before after €\n".encode()


def test_bytes_that_are_not_utf8_pass_through_unchanged() -> None:
    written = b"\xff\xfe latin-1 caf\xe9\n"
    assert intercepted(f"os.write(1, {written!r})") == written


def test_an_unfinished_escape_at_close_is_cleaned_and_nothing_hangs() -> None:
    assert intercepted("os.write(1, b'end\\x1b]0;never closed')") == b"end"
    assert intercepted("os.write(1, b'end\\x1b[')") == b"end"


def test_an_unterminated_osc_is_held_no_longer_than_the_cap() -> None:
    # Held past HELD_CAP, it would take what the next reads bring until the descriptor closed
    body = (
        "interceptor._pass_on(b'\\x1b]0;' + b'x' * 3000)\n"
        "interceptor._pass_on(b'x' * 3000)\n"
        "interceptor._pass_on(b'later\\n')\n"
        "os.write(2, b'|')"
    )
    err = intercepted(body)
    assert b"\x1b" not in err
    assert err == b"later\n|"


def test_the_warning_counts_the_bytes_written_not_the_cleaned_ones() -> None:
    body = "os.write(1, b'\\x1b]0;t\\x07ok\\n')\nprint(interceptor.take(), file=sys.stderr)"
    err = intercepted(body)
    assert err.replace(b"\r\n", b"\n") == b"ok\n('\\x1b]0;t\\x07ok\\n', 9)\n"


# ctx.run: what a child wrote reaches stderr cleaned, in a ctx.log line or an error


def child_app() -> App:
    app = App("children", version="1.0.0")
    code = f"import sys; print({OSC52 + 'out'!r}); print({OSC52 + 'err'!r}, file=sys.stderr)"

    @app.command("follow", description="Stream a child", danger_level="safe", exit_codes=())
    def follow(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.run([sys.executable, "-c", code], stream=True)
        return {}

    @app.command("fail", description="Run a failing child", danger_level="safe", exit_codes=())
    def fail(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.run([sys.executable, "-c", code + "; sys.exit(3)"])
        return {}

    return app


def plain(app: App, command: str) -> str:
    """The run's stderr on a terminal, where ctx.log lines and errors are plain text"""
    err = io.StringIO()
    app.run(
        [command, "--format", "plain"],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=err,
        env={"PATH": os.environ["PATH"]},
        isatty=True,
    )
    return err.getvalue()


def test_a_streamed_childs_escapes_never_reach_stderr() -> None:
    err = plain(child_app(), "follow")
    assert "\x1b]" not in err and "\x07" not in err
    assert sorted(err.split()) == ["err", "out"]


def test_a_failed_childs_stderr_in_the_error_carries_no_escape() -> None:
    err = plain(child_app(), "fail")
    assert "SUBPROCESS_FAILED" in err or "exited with 3" in err, err
    assert "\x1b]" not in err and "\x07" not in err


COLORING_APP = """
import os
from treaty import App, Ctx, NoArgs

app = App("colors", version="1.0.0")


@app.command("go", description="Write colors", danger_level="safe", exit_codes=())
def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
    os.write(1, b"\\x1b[31mred\\x1b[0m\\n")
    return {}


app.main()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-terminals are POSIX")
def test_main_on_a_terminal_with_piped_stdin_keeps_the_colors_a_print_keeps() -> None:
    # main() sets CI=1 for children off an interactive session; the run still colors
    import pty

    master, slave = pty.openpty()
    off = {"CI", "NO_COLOR", "GITHUB_ACTIONS", "JENKINS_URL"}
    env = {k: v for k, v in os.environ.items() if k not in off}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", COLORING_APP, "go", "--format", "plain"],
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=subprocess.PIPE,
            env={**env, "TERM": "xterm"},
            timeout=30,
            check=True,
        )
    finally:
        os.close(slave)
        os.close(master)
    assert proc.stderr == b"\x1b[31mred\x1b[0m\n"
