"""Issue #8: ``--format plain`` prints a list of flat objects as an aligned table"""

import io
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from treaty import App, Ctx, NoArgs, Out
from treaty._plain import NO_LAYOUT, Layout, layout_of, render_plain, table_width


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    id: int
    price: Decimal
    ratio: float | None
    active: bool


@dataclass(frozen=True, slots=True)
class Tagged:
    id: int
    name: str
    tags: list[str] = Out(table=False)


@dataclass(frozen=True, slots=True)
class Nested:
    id: int
    tags: list[str]


ITEMS = [
    Item("api", 1, Decimal("9.50"), 0.25, True),
    Item("web-frontend", 12, Decimal("120.00"), None, False),
]


def make_app() -> App:
    app = App("tablectl", version="1.0.0")

    @app.command("items", description="List items", danger_level="safe", exit_codes=(),
                 output_file=True, sort_key="id")  # fmt: skip
    def items(args: NoArgs, ctx: Ctx) -> list[Item]:
        return ITEMS

    @app.command("none", description="List nothing", danger_level="safe", exit_codes=())
    def none(args: NoArgs, ctx: Ctx) -> list[Item]:
        return []

    @app.command("tagged", description="List tagged", danger_level="safe", exit_codes=())
    def tagged(args: NoArgs, ctx: Ctx) -> list[Tagged]:
        return [Tagged(1, "api", ["a", "b"])]

    @app.command("nested", description="List nested", danger_level="safe", exit_codes=())
    def nested(args: NoArgs, ctx: Ctx) -> list[Nested]:
        return [Nested(1, ["a"])]

    @app.command("keys", description="List keys", danger_level="safe", exit_codes=())
    def keys(args: NoArgs, ctx: Ctx) -> list[dict[str, str]]:
        return [{"name": "deploy", "api_key": "ghp_abc123456789abcdef"}]

    return app


def run(argv: list[str], env: dict[str, str] | None = None, *, tty: bool = True) -> str:
    out = io.StringIO()
    code = make_app().run(argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=tty)
    assert code == 0
    return out.getvalue()


def test_a_list_of_flat_objects_is_an_aligned_table() -> None:
    # Field order from the dataclass; int, Decimal, and float right-aligned, the Decimal
    # though it is text in data; null an empty cell; bools as JSON writes them
    assert run(["items"]).splitlines() == [
        "name          id   price  ratio  active",
        "api            1    9.50   0.25  true",
        "web-frontend  12  120.00         false",
    ]


def test_explicit_plain_on_a_pipe_is_the_same_table() -> None:
    assert run(["items", "--format", "plain"], tty=False) == run(["items"])


def test_an_empty_list_of_objects_prints_no_rows() -> None:
    assert run(["none"]) == "(no rows)\n"


def test_an_untyped_empty_list_still_prints_nothing() -> None:
    assert render_plain([]) == ""


def test_column_order_follows_the_dataclass_not_the_keys() -> None:
    layout = layout_of(list[Item])
    data = [{"active": True, "id": 1, "name": "a", "price": "1", "ratio": None}]
    assert render_plain(data, layout).splitlines()[0] == "name  id  price  ratio  active"


def test_fields_selects_the_columns() -> None:
    assert run(["items", "--fields", "id,name"]).splitlines() == [
        "name          id",
        "api            1",
        "web-frontend  12",
    ]


def test_out_table_false_leaves_a_field_out_so_the_rest_is_a_table() -> None:
    assert run(["tagged"]) == "id  name\n 1  api\n"


def test_a_nested_value_keeps_the_key_value_blocks() -> None:
    assert run(["nested"]) == "id: 1\ntags.0: a\n\n"
    data = [{"id": 1, "meta": {"a": 1}}, {"id": 2, "meta": {"a": 2}}]
    assert render_plain(data) == "id: 1\nmeta.a: 1\n\nid: 2\nmeta.a: 2\n\n"


def test_out_table_false_does_not_change_the_schema() -> None:
    @dataclass(frozen=True, slots=True)
    class Shown:
        id: int
        name: str
        tags: list[str]

    def schema(tp: type) -> dict[str, object]:
        app = App("schemactl", version="1.0.0")

        @app.command("ls", description="List", danger_level="safe", exit_codes=())
        def ls(args: NoArgs, ctx: Ctx) -> list[tp]:  # type: ignore[valid-type]
            return []

        out = io.StringIO()
        app.run(["ls", "--schema"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
        output = json.loads(out.getvalue())["data"]["output_schema"]
        output["items"]["title"] = "-"
        return output  # type: ignore[no-any-return]

    assert schema(Tagged) == schema(Shown)


def test_out_table_must_be_a_bool() -> None:
    from treaty import RegistrationError

    with pytest.raises(RegistrationError, match="table is True or False"):
        Out(table="no")


def test_json_jsonl_and_tsv_do_not_change() -> None:
    envelope = json.loads(run(["items"], tty=False))
    assert envelope["data"][0] == {
        "name": "api",
        "id": 1,
        "price": "9.50",
        "ratio": 0.25,
        "active": True,
    }
    assert run(["items", "--format", "tsv"]) == (
        "name\tid\tprice\tratio\tactive\napi\t1\t9.50\t0.25\ttrue\n"
        "web-frontend\t12\t120.00\t\tfalse\n"
    )
    lines = run(["items", "--format", "jsonl"]).splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["data"] == envelope["data"]


def test_redacted_secrets_stay_redacted() -> None:
    text = run(["keys"])
    assert "ghp_abc123456789abcdef" not in text
    assert text.splitlines()[1].startswith("deploy  [KEY: ghp_abc1")


def test_color_settings_do_not_change_the_table() -> None:
    plain = run(["items"])
    for env in ({"NO_COLOR": "1"}, {"TERM": "dumb"}, {"TERM": "xterm-256color"}):
        assert run(["items"], env) == plain


# Width


def test_newlines_and_tabs_are_escaped_so_columns_stay_aligned() -> None:
    data = [{"a": "x\ny", "b": 1}, {"a": "t\tab", "b": 22}]
    assert render_plain(data) == "a       b\nx\\ny    1\nt\\tab  22\n"


def test_wide_characters_take_two_cells() -> None:
    data = [{"name": "日本", "n": 1}, {"name": "abcde", "n": 2}, {"name": "é", "n": 3}]
    assert render_plain(data).splitlines() == [
        "name   n",
        "日本   1",
        "abcde  2",
        "é      3",
    ]


@pytest.mark.parametrize(
    ("env", "width"),
    [
        ({}, None),
        ({"COLUMNS": ""}, None),
        ({"COLUMNS": "wide"}, None),
        ({"COLUMNS": "-5"}, None),
        ({"COLUMNS": "0"}, None),
        ({"COLUMNS": "8.5"}, None),
        ({"COLUMNS": "٣٠"}, None),  # Arabic-Indic digits are not a width
        ({"COLUMNS": "30"}, 30),
    ],
)
def test_columns_is_the_width_only_when_a_positive_integer(
    env: dict[str, str], width: int | None
) -> None:
    assert table_width(env) == width


def test_unset_or_invalid_columns_never_cuts() -> None:
    wide = run(["items"])
    assert run(["items"], {"COLUMNS": "abc"}) == wide
    assert run(["items"], {"COLUMNS": "200"}) == wide


def test_columns_cuts_the_widest_text_column_with_an_ellipsis() -> None:
    # 12 + 2 + 2 + 2 + 6 + 2 + 5 + 2 + 6 = 39 cells; 35 cuts four from "name"
    assert run(["items"], {"COLUMNS": "35"}).splitlines() == [
        "name      id   price  ratio  active",
        "api        1    9.50   0.25  true",
        "web-fro…  12  120.00         false",
    ]


def test_the_rightmost_of_equally_wide_text_columns_is_cut_first() -> None:
    data = [{"a": "aaaaaa", "b": "bbbbbb"}]
    assert render_plain(data, NO_LAYOUT, 13) == "a       b\naaaaaa  bbbb…\n"


def test_numbers_are_never_cut_and_text_stops_at_four_cells() -> None:
    data = [{"text": "abcdefghij", "n": 1234567890}]
    assert render_plain(data, NO_LAYOUT, 5) == "text           n\nabc…  1234567890\n"


def test_a_cut_wide_character_is_padded_not_split() -> None:
    data = [{"t": "日本語です", "u": "x"}]
    # Ten cells cut to seven: three ideographs and the ellipsis
    assert render_plain(data, NO_LAYOUT, 10) == "t        u\n日本語…  x\n"
    # Six cells: a second ideograph and the ellipsis take five, and a space pads the sixth
    assert render_plain(data, NO_LAYOUT, 9) == "t       u\n日本…   x\n"


def test_a_file_written_with_output_is_never_cut(tmp_path: Path) -> None:
    target = tmp_path / "items.txt"
    out = io.StringIO()
    code = make_app().run(
        ["items", "--format", "plain", "--output", str(target)],
        stdout=out,
        stderr=io.StringIO(),
        env={"COLUMNS": "20"},
        isatty=False,
    )
    assert code == 0
    assert target.read_text(encoding="utf-8") == run(["items"])


# Shapes


def test_mixed_object_shapes_share_one_header_with_empty_cells() -> None:
    data = [{"id": 1, "name": "a"}, {"id": 2, "size": 10}]
    assert render_plain(data) == "id  name  size\n 1  a\n 2          10\n"


def test_a_column_mixing_numbers_and_text_is_left_aligned() -> None:
    data = [{"v": 5}, {"v": "five"}]
    assert render_plain(data) == "v\n5\nfive\n"


def test_a_list_of_scalars_keeps_one_per_line() -> None:
    assert render_plain(["a", 2, True], layout_of(list[str])) == "a\n2\ntrue\n"


def test_non_list_outputs_declare_no_layout() -> None:
    assert layout_of(Item) is NO_LAYOUT and layout_of(list[str]) is NO_LAYOUT
    assert layout_of(list[Item] | None) == Layout(
        ("name", "id", "price", "ratio", "active"),
        frozenset({"id", "price", "ratio"}),
        frozenset(),
        rows=True,
    )
