import io
import json
from dataclasses import dataclass

import pytest

from treaty import App, Ctx
from treaty._plain import render_event, render_plain


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ("Hello, k!", "Hello, k!\n"),
        (3, "3\n"),
        (1.5, "1.5\n"),
        (True, "true\n"),
        (False, "false\n"),
        (None, ""),
        ({}, ""),
        ([], ""),
    ],
)
def test_top_level_scalars_and_empties(data: object, text: str) -> None:
    assert render_plain(data) == text


def test_object_prints_key_value_lines_in_field_order() -> None:
    assert render_plain({"z": 1, "a": "x"}) == "z: 1\na: x\n"


def test_nested_values_use_dotted_paths() -> None:
    data = {"release": {"tag": "1.3.9"}, "items": [{"name": "api"}, {"name": "web"}]}
    assert render_plain(data) == "release.tag: 1.3.9\nitems.0.name: api\nitems.1.name: web\n"


def test_empty_containers_and_null_under_a_key() -> None:
    assert render_plain({"a": {}, "b": [], "c": None}) == "a: {}\nb: []\nc:\n"


def test_strings_are_raw_with_line_breaks_and_tabs_escaped() -> None:
    assert render_plain({"s": 'say "hi"\nnext\r\tend'}) == 's: say "hi"\\nnext\\r\\tend\n'


def test_top_level_array_of_scalars_prints_one_per_line() -> None:
    assert render_plain(["a", 2, True]) == "a\n2\ntrue\n"


def test_top_level_array_of_flat_objects_prints_a_table() -> None:
    data = [{"n": 1, "text": "a"}, {"n": 2, "text": "b"}]
    assert render_plain(data) == "n  text\n1  a\n2  b\n"


def test_top_level_array_of_nested_objects_prints_blocks_like_a_stream() -> None:
    data = [{"n": 1, "tags": ["a"]}, {"n": 2, "tags": []}]
    assert render_plain(data) == "n: 1\ntags.0: a\n\nn: 2\ntags: []\n\n"
    assert render_plain(data) == "".join(render_event(e) for e in data)


def test_event_blank_line_only_after_containers() -> None:
    assert render_event({"n": 1}) == "n: 1\n\n"
    assert render_event("line") == "line\n"
    assert render_event({}) == ""


def test_non_json_values_fail() -> None:
    with pytest.raises(TypeError, match="JSON values"):
        render_plain({"x": object()})


# Plain mode through the CLI


@dataclass(frozen=True, slots=True)
class NoArgs:
    pass


def plain_app() -> App:
    app = App("showctl", version="1.0.0")

    @app.command("show", description="Show a release", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"service": "api", "release": {"tag": "1.3.9"}}

    return app


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = plain_app().run(argv, stdout=out, stderr=err, env=env or {}, isatty=True)
    return code, out.getvalue(), err.getvalue()


def test_command_without_a_renderer_prints_flat_lines() -> None:
    assert run(["show"]) == (0, "service: api\nrelease.tag: 1.3.9\n", "")


def test_explicit_plain_on_a_pipe_matches_the_terminal_default() -> None:
    out = io.StringIO()
    code = plain_app().run(["show", "--format", "plain"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0 and out.getvalue() == "service: api\nrelease.tag: 1.3.9\n"


@pytest.mark.parametrize(
    ("argv", "env"),
    [(["show", "--format", "human"], {}), (["show"], {"SHOWCTL_FORMAT": "human"})],
)
def test_human_is_an_unknown_format(argv: list[str], env: dict[str, str]) -> None:
    out = io.StringIO()
    code = plain_app().run(argv, stdout=out, stderr=io.StringIO(), env=env, isatty=False)
    error = json.loads(out.getvalue())["error"]
    assert code == 2 and error["code"] == "ARG_ERROR"
    assert error["context"]["allowed"] == ["plain", "json", "jsonl", "ndjson", "tsv"]


def test_manifest_stays_json_in_plain_mode() -> None:
    code, out, _ = run(["manifest"])
    assert code == 0 and "show" in json.loads(out)["commands"]
