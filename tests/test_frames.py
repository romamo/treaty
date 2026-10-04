"""A streaming command's frame renderer redraws in place at a terminal (#350)"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import pytest

from treaty import App, Ctx, Format, FormatRenderer, NoArgs, RegistrationError
from treaty._plain import frame_rows

CLEAR_ONE = "\r\x1b[1A\x1b[J"
TERMINAL = {"AP_AUDIT_LOG": "0", "TERM": "xterm"}


@dataclass(frozen=True, slots=True)
class Frame:
    n: int


def frame_app(*, warn_at: int | None = None, fail_at: int | None = None) -> App:
    app = App("ap", version="0.1.0")

    @app.command(
        "frames",
        description="Draw frames",
        danger_level="safe",
        exit_codes=(),
        streaming=True,
        timeout=None,
        renderers={
            Format.PLAIN: FormatRenderer(lambda e: f"\x1b[2J\x1b[Hframe {e['n']}\n", frame=True)
        },
    )
    def frames(args: NoArgs, ctx: Ctx) -> Iterator[Frame]:
        for n in (1, 2, 3):
            if n == fail_at:
                raise RuntimeError("feed lost")
            if n == warn_at:
                ctx.log("reconnecting")
            yield Frame(n)

    return app


def run(
    app: App, env: Mapping[str, str] = TERMINAL, *, tty: bool = True, fmt: str = "plain"
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(["frames", "--format", fmt], stdout=out, stderr=err, isatty=tty, env=env)
    return code, out.getvalue(), err.getvalue()


def test_a_terminal_clears_the_last_frame_with_treatys_own_escapes() -> None:
    code, out, _ = run(frame_app())
    # The renderer's clear-screen and home are stripped as before; treaty's own move up
    # and clear replace each frame
    assert (code, out) == (0, f"frame 1\n{CLEAR_ONE}frame 2\n{CLEAR_ONE}frame 3\n")


@pytest.mark.parametrize(
    ("env", "tty", "fmt"),
    [
        (TERMINAL, False, "plain"),  # a pipe or a file
        ({**TERMINAL, "NO_COLOR": ""}, True, "plain"),
        ({**TERMINAL, "TERM": "dumb"}, True, "plain"),
        ({**TERMINAL, "CI": "1"}, True, "plain"),
    ],
)
def test_frames_append_where_no_terminal_acts_on_escapes(
    env: Mapping[str, str], tty: bool, fmt: str
) -> None:
    assert run(frame_app(), env, tty=tty, fmt=fmt) == (0, "frame 1\nframe 2\nframe 3\n", "")


def test_other_formats_are_unchanged() -> None:
    code, out, _ = run(frame_app(), fmt="ndjson")
    assert (code, out) == (0, '{"n":1}\n{"n":2}\n{"n":3}\n')


def test_a_stderr_line_between_frames_is_not_cleared() -> None:
    code, out, err = run(frame_app(warn_at=2), {**TERMINAL, "AP_VERBOSE": "1"})
    assert code == 0
    assert "reconnecting" in err
    # Frame 2 goes below the log line; frame 3 replaces frame 2 alone
    assert out == f"frame 1\nframe 2\n{CLEAR_ONE}frame 3\n"


def test_the_last_frame_stays_when_the_stream_fails() -> None:
    code, out, err = run(frame_app(fail_at=3))
    assert code == 1
    assert out == f"frame 1\n{CLEAR_ONE}frame 2\n"
    assert "HANDLER_CRASHED" in err


def test_frame_rows_count_wrapped_lines_and_an_open_last_line() -> None:
    assert frame_rows("ab\ncd\n", None) == 2
    assert frame_rows("ab\ncd", None) == 1
    assert frame_rows("\x1b[31mabcdef\x1b[0m\n", 4) == 2  # colors take no cells
    assert frame_rows("abcd\n", 4) == 1
    assert frame_rows("", 4) == 0


def test_a_wrapped_frame_moves_up_every_row_it_took() -> None:
    app = App("ap", version="0.1.0")

    @app.command(
        "frames",
        description="Draw frames",
        danger_level="safe",
        exit_codes=(),
        streaming=True,
        timeout=None,
        renderers={Format.PLAIN: FormatRenderer(lambda e: "x" * 10 + "\n" * 3, frame=True)},
    )
    def frames(args: NoArgs, ctx: Ctx) -> Iterator[Frame]:
        yield Frame(1)
        yield Frame(2)

    _, out, _ = run(app, {**TERMINAL, "COLUMNS": "4"})
    # 10 cells over 4 columns wrap to 3 rows, then two blank lines
    assert out == "xxxxxxxxxx\n\n\n\r\x1b[5A\x1b[Jxxxxxxxxxx\n\n\n"


def test_frame_is_refused_off_a_stream_or_off_plain() -> None:
    app = App("ap", version="0.1.0")
    with pytest.raises(RegistrationError, match="streaming=True"):

        @app.command(
            "one",
            description="One",
            danger_level="safe",
            exit_codes=(),
            renderers={Format.PLAIN: FormatRenderer(lambda d: "x\n", frame=True)},
        )
        def one(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            return {}

    with pytest.raises(RegistrationError, match="plain renderer"):

        @app.command(
            "tab",
            description="Tab",
            danger_level="safe",
            exit_codes=(),
            streaming=True,
            renderers={Format.TSV: FormatRenderer(lambda d: "x\n", frame=True)},
        )
        def tab(args: NoArgs, ctx: Ctx) -> Iterator[Frame]:
            yield Frame(1)

    with pytest.raises(RegistrationError, match="not a bool"):
        FormatRenderer(lambda d: "x\n", frame=1)  # type: ignore[arg-type]
