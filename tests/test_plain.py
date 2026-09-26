import pytest

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


def test_top_level_array_of_objects_prints_blocks_like_a_stream() -> None:
    data = [{"n": 1, "text": "a"}, {"n": 2, "text": "b"}]
    assert render_plain(data) == "n: 1\ntext: a\n\nn: 2\ntext: b\n\n"
    assert render_plain(data) == "".join(render_event(e) for e in data)


def test_event_blank_line_only_after_containers() -> None:
    assert render_event({"n": 1}) == "n: 1\n\n"
    assert render_event("line") == "line\n"
    assert render_event({}) == ""


def test_non_json_values_fail() -> None:
    with pytest.raises(TypeError, match="JSON values"):
        render_plain({"x": object()})
