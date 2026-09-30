"""Terminal escapes in text for a person (REQ-F-007, #72): plain output, table cells, a
custom renderer's text, stderr error lines, ctx.log lines, and tracebacks"""

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest

from treaty import App, Arg, Ctx, Exit, Flag, Format, NoArgs, table
from treaty._envelope import clean, terminal_text, visible
from treaty._plain import render_plain

TITLE = "\x1b]0;pwned\x07"  # OSC 0: sets the window title
LINK = "\x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\"  # OSC 8: a forged hyperlink
CLIPBOARD = "\x1b]52;c;aGk=\x07"  # OSC 52: writes the clipboard
CURSOR = "\x1b[2A\x1b[2K"  # CSI: up two lines, erase the line
C1 = "\x9b31m\x9d0;pwned\x9c"  # the 8-bit CSI and OSC
HOSTILE = f"{TITLE}{LINK}{CLIPBOARD}{CURSOR}{C1}\x1b[31mRED\x1b[0m"
# Every byte a terminal acts on: ESC, BEL, the C1 range, and a carriage return
ACTIVE = ("\x1b", "\x07", "\x9b", "\x9c", "\x9d", "\r")


def inert(text: str) -> bool:
    return not any(c in text for c in ACTIVE)


def hostile_app() -> App:
    app = App("probe", version="1.0.0")

    @dataclass
    class Said:
        text: str = Arg(description="Text")

    @app.command("echo", description="Echo", danger_level="safe", exit_codes=())
    def echo(args: Said, ctx: Ctx) -> dict[str, str]:
        return {"text": args.text}

    @app.command("data", description="Hostile data", danger_level="safe", exit_codes=())
    def data(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"text": HOSTILE, f"k{CLIPBOARD}": "v", "rows": [{"cell": HOSTILE}]}

    @app.command(
        "styled",
        description="A renderer that colors and embeds data",
        danger_level="safe",
        exit_codes=(),
        renderers={Format.PLAIN: lambda d: f"\x1b[1m{d['text']}\x1b[0m{TITLE}\x07\n"},
    )
    def styled(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"text": HOSTILE}

    @app.command("fail", description="Fail", danger_level="safe", exit_codes=())
    def fail(args: NoArgs, ctx: Ctx) -> None:
        raise Exit.PRECONDITION(
            "no such host",
            context={"host": f"web{CLIPBOARD}", f"k{TITLE}": 1},
            suggestion=f"run probe hosts{CURSOR}",
        )

    @app.command("say", description="Log", danger_level="safe", exit_codes=())
    def say(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.log(f"\x1b[32mok\x1b[0m {CLIPBOARD}{C1}", where=f"here{TITLE}")
        return {}

    @app.command("crash", description="Crash", danger_level="safe", exit_codes=())
    def crash(args: NoArgs, ctx: Ctx) -> None:
        raise ValueError(f"bad input {CLIPBOARD}")

    return app


def run(argv: list[str], *, isatty: bool = False, env: Any = None) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    hostile_app().run(argv, stdout=out, stderr=err, env=env or {}, isatty=isatty)
    return out.getvalue(), err.getvalue()


def test_the_issue_repro_writes_no_escape_in_plain_output() -> None:
    out, _ = run(["echo", "\x1b]0;pwned\x07\x1b[31mRED\x1b[0m", "--format", "plain"])
    assert out == "text: RED\n"


def test_plain_values_lose_osc_csi_and_c1_escapes_like_json() -> None:
    out, _ = run(["data", "--format", "plain"])
    assert inert(out), repr(out)
    assert "text: clickRED\n" in out
    assert "rows.0.cell: clickRED\n" in out
    json_out, _ = run(["data", "--format", "json"])
    # The JSON envelope is ASCII: a C1 control there is a \u escape, and stays
    assert json_out.isascii() and json.loads(json_out)["data"]["text"] == f"click{C1}RED"


def test_a_plain_key_shows_its_escapes_instead_of_dropping_them() -> None:
    out, _ = run(["data", "--format", "plain"])
    assert "k\\x1b]52;c;aGk=\\x07: v\n" in out


def test_plain_keeps_newline_and_tab_escaped_and_shows_other_controls() -> None:
    text = render_plain({"a": "one\ntwo\tthree\rfour\x07\x08"})
    assert text == "a: one\\ntwo\\tthree\\rfour\\x07\\x08\n"


def test_tsv_cells_lose_escapes() -> None:
    out, _ = run(["data", "--format", "tsv"])
    assert inert(out), repr(out)
    assert "clickRED" in out and "k\\x1b]52;c;aGk=\\x07" in out


def test_csv_cells_lose_escapes_and_keep_line_breaks() -> None:
    render = table(",")
    text = render([{"a": f"x{LINK}\r\ny{C1}\x07", f"h{TITLE}": 1}])
    assert "\x1b" not in text and "\x9b" not in text and "\x07" not in text
    assert text == 'a,h\\x1b]0;pwned\\x07\n"xclick\r\ny\\x07",1\n'


def test_a_lone_carriage_return_is_shown_in_csv_and_custom_renderer_text() -> None:
    # A lone CR rewrites the line ("FAILED" reads as "OK"); only a CRLF is a line break
    assert table(",")([{"a": "FAILED\rOK    "}]) == "a\nFAILED\\rOK    \n"
    app = App("probe", version="1.0.0")

    @app.command(
        "show",
        description="Show",
        danger_level="safe",
        exit_codes=(),
        renderers={Format.PLAIN: lambda d: f"status: {d['s']}\r\n"},
    )
    def show(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"s": "FAILED\rOK    "}

    out = io.StringIO()
    app.run(["show", "--format", "plain"], stdout=out, stderr=io.StringIO(), env={})
    assert out.getvalue() == "status: FAILED\\rOK    \r\n"


def test_a_custom_renderer_gets_clean_data_and_keeps_only_color_on_a_terminal() -> None:
    out, _ = run(["styled"], isatty=True)
    assert out == "\x1b[1mclickRED\x1b[0m\\x07\n"


def test_a_custom_renderer_keeps_no_escape_off_a_color_terminal() -> None:
    out, _ = run(["styled", "--format", "plain"])
    assert out == "clickRED\\x07\n"
    out, _ = run(["styled"], isatty=True, env={"NO_COLOR": "1"})
    assert out == "clickRED\\x07\n"


def test_the_stderr_argument_line_shows_osc_52_escaped() -> None:
    _, err = run([f"nosuch{CLIPBOARD}", "--format", "plain"])
    assert inert(err), repr(err)
    assert "\\x1b]52;c;aGk=\\x07" in err


def test_stderr_context_and_hint_lines_show_escapes() -> None:
    _, err = run(["fail", "--format", "plain"])
    assert inert(err), repr(err)
    assert "  host: web\\x1b]52;c;aGk=\\x07\n" in err
    assert "  k\\x1b]0;pwned\\x07: 1\n" in err
    assert "hint: run probe hosts\\x1b[2A\\x1b[2K\n" in err


def test_stderr_error_items_show_escapes() -> None:
    app = App("items", version="1.0.0")

    @dataclass
    class Two:
        a: int = Arg(description="A")
        b: int = Arg(description="B")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: Two, ctx: Ctx) -> None:
        return None

    err = io.StringIO()
    app.run(
        ["go", f"x{TITLE}", f"y{C1}", "--format", "plain"],
        stdout=io.StringIO(),
        stderr=err,
        env={},
    )
    assert inert(err.getvalue()), repr(err.getvalue())
    assert "  - " in err.getvalue()


def test_a_log_line_on_a_color_terminal_keeps_color_and_drops_osc_and_c1() -> None:
    _, err = run(["say"], isatty=True)
    assert err == "\x1b[32mok\x1b[0m  where=here\n"


def test_a_log_line_off_a_color_terminal_has_no_escape() -> None:
    _, err = run(["say", "--format", "plain", "--verbose"])
    assert err == "ok  where=here\n"


def test_a_traceback_on_stderr_shows_escapes() -> None:
    _, err = run(["crash", "--format", "plain"])
    assert "ValueError: bad input \\x1b]52;c;aGk=\\x07" in err
    assert inert(err), repr(err)


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("\x9b2Jgone", "value: gone\n"),  # C1 CSI: erase the screen
        ("\x9d0;pwned\x9ctitle", "value: title\n"),  # C1 OSC: set the title
        ("a\x85b", "value: a\\x85b\n"),  # a lone C1 control is shown
    ],
)
def test_plain_strips_c1_escapes_and_shows_lone_c1_controls(value: str, shown: str) -> None:
    assert render_plain({"value": clean(value)}) == shown


def test_visible_and_terminal_text() -> None:
    assert visible("a\x1bb\r\t\n") == "a\\x1bb\\r\t\n"
    assert visible("a\r\nb", "\r") == "a\r\nb"
    assert visible("a\rb", "\r") == "a\\rb"  # a lone CR rewrites the line
    assert terminal_text(f"\x1b[31mx\x1b[0m{CURSOR}", color=True) == "\x1b[31mx\x1b[0m"
    assert terminal_text(f"\x1b[31mx\x1b[0m{CURSOR}", color=False) == "x"


def test_a_binary_wrapper_content_type_loses_its_escapes_in_plain_output() -> None:
    # The <binary ...> line takes content_type and size_bytes from the data, as is
    app = App("probe", version="1.0.0")

    @app.command("blob", description="Blob", danger_level="safe", exit_codes=())
    def blob(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        wrapper = {"type": "binary", "encoding": "base64", "value": ""}
        return {"file": {**wrapper, "size_bytes": f"0{TITLE}", "content_type": f"x/y{C1}\r"}}

    out, err = io.StringIO(), io.StringIO()
    app.run(["blob", "--format", "plain"], stdout=out, stderr=err, env={}, isatty=False)
    assert inert(out.getvalue()) and "<binary 0 bytes x/y" in out.getvalue()


# #105: text a handler or a library prints reaches stderr as a ctx.log line has it

PRINTED = f"\x1b[32mok\x1b[0m {CLIPBOARD}copied{C1}"


def printing_app() -> App:
    app = App("probe", version="1.0.0")

    @dataclass
    class Login:
        token: str = Flag(description="Token", secret=True, default="")

    @app.command("print", description="Print", danger_level="safe", exit_codes=())
    def printed(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        print(PRINTED)
        return {}

    @app.command("split", description="Print in parts", danger_level="safe", exit_codes=())
    def split(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        print("\x1b]52;c;", end="", flush=True)  # OSC 52, its payload in the next write
        print("aGk=\x07after \x1b[", end="")
        print("2Jcursor \x9d0;", end="")
        print("pwned\x9c title \x1b]0;never ended")  # released when the run ends
        return {}

    @app.command("leak", description="Print a secret", danger_level="safe", exit_codes=())
    def leak(args: Login, ctx: Ctx) -> dict[str, str]:
        print(f"token {args.token[:3]}\x1b[0m{args.token[3:]}")
        return {}

    return app


def run_printing(argv: list[str], *, isatty: bool = False, env: Any = None) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    printing_app().run(argv, stdout=out, stderr=err, env=env or {}, isatty=isatty)
    return out.getvalue(), err.getvalue()


def test_printed_osc_52_on_a_color_terminal_keeps_only_the_color() -> None:
    _, err = run_printing(["print"], isatty=True)
    assert err == "\x1b[32mok\x1b[0m copied\n"


def test_printed_osc_52_off_a_color_terminal_has_no_escape() -> None:
    _, err = run_printing(["print"], isatty=True, env={"NO_COLOR": "1"})
    assert err == "ok copied\n"
    _, err = run_printing(["print", "--format", "plain", "--verbose"])
    assert err == "ok copied\n"


def test_an_escape_printed_in_two_writes_is_cleaned_whole() -> None:
    _, err = run_printing(["split"], isatty=True)
    # The unended OSC runs to the end of the text, its newline too, as it would in one write
    assert err == "after cursor  title "


def test_printed_text_under_debug_carries_no_escape() -> None:
    for fmt in ("plain", "json"):
        _, err = run_printing(["print", "--debug", "--format", fmt])
        assert inert(err) and "\\u001b" not in err and "\\u009b" not in err, err
        assert "ok copied" in err


def test_the_third_party_stdout_warning_text_is_clean_and_counts_the_printed_bytes() -> None:
    out, _ = run_printing(["print", "--format", "json"])
    [warning] = json.loads(out)["warnings"]
    assert warning["code"] == "THIRD_PARTY_STDOUT"
    # As the envelope has any text: the 7-bit escapes go, the C1 ones are \u escapes
    assert warning["context"]["text"] == f"ok copied{C1}"
    assert warning["context"]["bytes"] == len(f"{PRINTED}\n".encode())


def test_a_secret_an_escape_splits_is_redacted_once_the_escape_is_gone() -> None:
    env = {"PROBE_TOKEN": "hunter22"}
    for isatty in (True, False):
        out, err = run_printing(["leak", "--verbose", "--format", "json"], isatty=isatty, env=env)
        shown = err.replace("\x1b[0m", "")
        assert "hunter22" not in shown and "token [REDACTED]" in shown, err
        [warning] = json.loads(out)["warnings"]
        assert warning["context"]["text"] == "token [REDACTED]"
