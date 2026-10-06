"""A renderer that takes two parameters gets a RenderContext: whether it may color and
the width to fit (#357)"""

import functools
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from treaty import (
    App,
    Ctx,
    Format,
    FormatRenderer,
    NoArgs,
    RegistrationError,
    RenderContext,
    Renderer,
)

RED = "\x1b[31m"


def colored(data: Any, rc: RenderContext) -> str:
    """Red where the run may color, then what it was told"""
    text = f"{data['tag']} color={rc.color} width={rc.width}"
    return f"{RED}{text}\x1b[0m\n" if rc.color else text + "\n"


def one(data: Any) -> str:
    return f"{data['tag']}\n"


def app_with(*, plain: Renderer | None = None, html: Renderer | None = None) -> App:
    app = App("showctl", version="1.0.0")
    if plain is not None:
        app.format(Format.PLAIN, render=plain)
    if html is not None:
        app.format("html", render=html, media_type="text/html")

    @app.command("show", description="Show", danger_level="safe", exit_codes=(), output_file=True)
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"tag": "1.3.9"}

    @app.command("tick", description="Events", streaming=True, danger_level="safe", exit_codes=())
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, str]]:
        yield {"tag": "a"}
        yield {"tag": "b"}

    return app


def run(
    app: App, argv: list[str], *, env: dict[str, str] | None = None, tty: bool = True
) -> tuple[int, str, str]:
    """Off a terminal the run is JSON unless it asks for a text format: these ask for plain"""
    if "--format" not in argv:
        argv = [*argv, "--format", "plain"]
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=tty)
    return code, out.getvalue(), err.getvalue()


def test_a_one_parameter_plain_renderer_is_called_with_data_alone() -> None:
    assert run(app_with(plain=one), ["show"]) == (0, "1.3.9\n", "")


def test_a_two_parameter_plain_renderer_may_color_on_a_terminal() -> None:
    code, out, _ = run(app_with(plain=colored), ["show"])
    assert code == 0 and out == f"{RED}1.3.9 color=True width=None\x1b[0m\n"


@pytest.mark.parametrize(
    ("env", "tty"),
    [({"NO_COLOR": "1"}, True), ({}, False), ({"TERM": "dumb"}, True), ({"CI": "1"}, True)],
)
def test_the_renderer_is_told_not_to_color(env: dict[str, str], tty: bool) -> None:
    code, out, _ = run(app_with(plain=colored), ["show", "--format", "plain"], env=env, tty=tty)
    assert code == 0 and out == "1.3.9 color=False width=None\n"


def test_the_width_is_columns_as_plain_tables_have_it() -> None:
    code, out, _ = run(app_with(plain=colored), ["show"], env={"COLUMNS": "60", "NO_COLOR": "1"})
    assert code == 0 and out == "1.3.9 color=False width=60\n"


@pytest.mark.parametrize("columns", ["0", "wide", ""])
def test_a_columns_plain_tables_ignore_gives_no_width(columns: str) -> None:
    code, out, _ = run(app_with(plain=colored), ["show"], env={"COLUMNS": columns}, tty=False)
    assert code == 0 and out == "1.3.9 color=False width=None\n"


def test_json_mode_never_calls_a_renderer() -> None:
    def refuse(data: Any, rc: RenderContext) -> str:
        raise AssertionError("a renderer ran in JSON mode")

    code, out, _ = run(app_with(plain=refuse), ["show", "--format", "json"])
    assert code == 0 and json.loads(out)["data"] == {"tag": "1.3.9"}


def test_a_custom_format_takes_either_shape() -> None:
    assert run(app_with(html=one), ["show", "--format", "html"])[1] == "1.3.9\n"
    code, out, _ = run(app_with(html=colored), ["show", "--format", "html"], tty=False)
    assert code == 0 and out == "1.3.9 color=False width=None\n"


def test_a_format_renderer_takes_either_shape() -> None:
    app = App("showctl", version="1.0.0")

    @app.command(
        "show",
        description="Show",
        renderers={
            "html": FormatRenderer(colored, media_type="text/html"),
            "md": FormatRenderer(one, media_type="text/markdown"),
        },
        danger_level="safe",
        exit_codes=(),
    )
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"tag": "1.3.9"}

    env = {"COLUMNS": "42"}
    assert run(app, ["show", "--format", "html"], env=env, tty=False)[1] == (
        "1.3.9 color=False width=42\n"
    )
    assert run(app, ["show", "--format", "md"], env=env)[1] == "1.3.9\n"


def test_a_command_renderer_takes_either_shape() -> None:
    app = App("showctl", version="1.0.0")

    @app.command(
        "show",
        description="Show",
        renderers={Format.PLAIN: colored, Format.CSV: one},
        danger_level="safe",
        exit_codes=(),
    )
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"tag": "1.3.9"}

    assert run(app, ["show"], tty=False)[1] == "1.3.9 color=False width=None\n"
    assert run(app, ["show", "--format", "csv"])[1] == "1.3.9\n"


def test_a_file_is_written_without_color_or_width(tmp_path: Path) -> None:
    target = tmp_path / "show.txt"
    code, _, _ = run(
        app_with(plain=colored), ["show", "--output", str(target)], env={"COLUMNS": "60"}
    )
    assert code == 0 and target.read_text() == "1.3.9 color=False width=None\n"


def test_a_stream_renders_each_event_with_the_context() -> None:
    code, out, _ = run(app_with(plain=colored), ["tick"], env={"COLUMNS": "50"}, tty=False)
    assert code == 0 and out == "a color=False width=50\nb color=False width=50\n"


def test_no_stream_renders_the_buffered_events_with_the_context() -> None:
    code, out, _ = run(app_with(plain=colored), ["tick", "--no-stream"], tty=False)
    assert code == 0 and out == "a color=False width=None\nb color=False width=None\n"


def test_a_one_parameter_stream_renderer_still_gets_data_alone() -> None:
    assert run(app_with(plain=one), ["tick"])[1] == "a\nb\n"


class Page:
    def __call__(self, data: Any, rc: RenderContext) -> str:
        return f"page {data['tag']} {rc.color}\n"


class Lines:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def render(self, data: Any) -> str:
        return f"{self.prefix}{data['tag']}\n"


def framed(frame: str, data: Any, rc: RenderContext) -> str:
    return f"{frame}{data['tag']}{frame} {rc.width}\n"


def with_separator(data: Any, separator: str = ",") -> str:
    return f"{data['tag']}{separator}\n"


@pytest.mark.parametrize(
    ("render", "expected"),
    [
        (Page(), "page 1.3.9 False\n"),
        (Lines("v").render, "v1.3.9\n"),
        (functools.partial(framed, "*"), "*1.3.9* None\n"),
        (with_separator, "1.3.9,\n"),
        (lambda data, rc: f"{data['tag']} {rc.width}\n", "1.3.9 None\n"),
    ],
)
def test_any_callable_is_read_by_its_parameters(render: Renderer, expected: str) -> None:
    assert run(app_with(plain=render), ["show"], tty=False)[1] == expected


def test_a_callable_without_a_signature_is_called_with_data_alone() -> None:
    # str has no signature: it is called str(data), as before (#357)
    out = run(app_with(plain=str), ["show"])[1]
    assert out == "{'tag': '1.3.9'}"


def zero() -> str:
    return ""


def three(data: Any, rc: RenderContext, extra: object) -> str:
    return ""


def keyword(data: Any, *, style: str) -> str:
    return ""


def only_args(*items: Any) -> str:
    return ""


@pytest.mark.parametrize(
    ("render", "named"),
    [
        (zero, "zero takes 0 required positional"),
        (three, "three takes 3 required positional"),
        (only_args, "only_args takes 0 required positional"),
        (keyword, "keyword requires the keyword-only parameter(s) style"),
    ],
)
def test_any_other_shape_is_refused_at_registration(render: Any, named: str) -> None:
    with pytest.raises(RegistrationError, match=r"app\.format: html: the renderer ") as exc:
        App("showctl", version="1.0.0").format("html", render=render)
    assert named in str(exc.value)
    with pytest.raises(RegistrationError, match=re.escape(named)):
        FormatRenderer(render, media_type="text/html")
    app = App("showctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"show: renderers: plain: the renderer "):

        @app.command(
            "show",
            description="Show",
            renderers={Format.PLAIN: render},
            danger_level="safe",
            exit_codes=(),
        )
        def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            return {}


def test_the_manifest_does_not_depend_on_the_renderer_shape() -> None:
    assert app_with(plain=one).manifest() == app_with(plain=colored).manifest()
    assert app_with(html=one).manifest() == app_with(html=colored).manifest()


@pytest.mark.parametrize(
    ("color", "width", "error"),
    [
        ("yes", None, TypeError),
        (True, "80", TypeError),
        (True, True, TypeError),
        (True, 0, ValueError),
    ],
)
def test_a_render_context_is_strict(color: Any, width: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        RenderContext(color=color, width=width)
