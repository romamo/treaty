"""``tsv`` and ``table(...)`` formats write no trust tags as columns: an external
command's untrusted status reaches the caller as the ``UNTRUSTED_CONTENT`` warning on
stderr, while an app's own renderer still gets the tags (#336)"""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from treaty import App, Ctx, Format, NoArgs, table

TAGS = {"_source": "external", "_trusted": False}
UNTRUSTED = "warning: UNTRUSTED_CONTENT:"


@dataclass(frozen=True, slots=True)
class Row:
    title: str
    price: float


def app(seen: list[object]) -> App:
    app = App("shop", version="1.0.0")
    app.format(Format.CSV, render=table(","))

    def own_render(data: object) -> str:
        seen.append(data)
        return "own\n"

    app.format(Format.YAML, render=own_render)

    @app.command(
        "rows",
        description="List",
        danger_level="safe",
        exit_codes=(),
        external=True,
        ordered=True,
        output_file=True,
    )
    def rows(args: NoArgs, ctx: Ctx) -> list[Row]:
        return [Row("a", 1.5), Row("b", 2.0)]

    @app.command("one", description="One", danger_level="safe", exit_codes=(), external=True)
    def one(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"title": "a", "meta": {"_source": "cache", "_trusted": False}}

    @app.command("own", description="Own data", danger_level="safe", exit_codes=())
    def own(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"_source": "external", "_trusted": False, "name": "mine"}

    @app.command(
        "feed",
        description="Stream",
        danger_level="safe",
        exit_codes=(),
        external=True,
        streaming=True,
    )
    def feed(args: NoArgs, ctx: Ctx) -> Iterator[Row]:
        yield Row("a", 1.5)
        yield Row("b", 2.0)

    return app


def run(argv: list[str], seen: list[object] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app([] if seen is None else seen).run(
        argv, stdin=io.StringIO(), stdout=out, stderr=err, env={}, isatty=False
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("csv", "title,price\na,1.5\nb,2.0\n"),
        ("tsv", "title\tprice\na\t1.5\nb\t2.0\n"),
    ],
)
def test_a_table_of_external_rows_has_no_tag_columns_and_warns(fmt: str, expected: str) -> None:
    code, out, err = run(["rows", "--format", fmt])
    assert code == 0
    assert out == expected
    assert UNTRUSTED in err


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("csv", "title,price\na,1.5\nb,2.0\n"),
        ("tsv", "title\tprice\na\t1.5\nb\t2.0\n"),
    ],
)
def test_an_output_file_in_a_table_format_has_no_tag_columns(
    tmp_path: Path, fmt: str, expected: str
) -> None:
    target = tmp_path / f"rows.{fmt}"
    code, out, _ = run(["rows", "--format", fmt, "--output", str(target)])
    assert code == 0
    assert target.read_text() == expected
    # stdout is the envelope describing the write, with the warning
    assert "UNTRUSTED_CONTENT" in [w["code"] for w in json.loads(out)["warnings"]]


@pytest.mark.parametrize(
    ("fmt", "first", "second"),
    [
        ("csv", "title,price\na,1.5\n", "title,price\nb,2.0\n"),
        ("tsv", "title\tprice\na\t1.5\n", "title\tprice\nb\t2.0\n"),
    ],
)
def test_each_page_of_a_paginated_table_has_no_tag_columns_and_warns(
    tmp_path: Path, fmt: str, first: str, second: str
) -> None:
    target = tmp_path / f"page1.{fmt}"
    code, out, _ = run(["rows", "--format", fmt, "--limit", "1", "--output", str(target)])
    assert code == 0
    assert target.read_text() == first
    envelope = json.loads(out)
    assert "UNTRUSTED_CONTENT" in [w["code"] for w in envelope["warnings"]]
    cursor = envelope["meta"]["pagination"]["next_cursor"]
    code, out, err = run(["rows", "--format", fmt, "--limit", "1", "--cursor", cursor])
    assert code == 0
    assert out == second
    assert UNTRUSTED in err


def test_a_single_object_loses_only_its_top_level_tags() -> None:
    _, out, _ = run(["one", "--format", "csv"])
    assert out == 'title,meta\na,"{""_source"":""cache"",""_trusted"":false}"\n'


def test_fields_selection_keeps_no_tag_columns() -> None:
    _, out, _ = run(["rows", "--format", "tsv", "--fields", "title"])
    assert out == "title\na\nb\n"


def test_a_stream_and_its_buffered_form_have_no_tag_columns() -> None:
    _, out, err = run(["feed", "--format", "tsv"])
    assert out == "title\tprice\na\t1.5\ntitle\tprice\nb\t2.0\n"
    assert UNTRUSTED in err
    _, out, err = run(["feed", "--format", "csv", "--no-stream"])
    assert out == "title,price\na,1.5\ntitle,price\nb,2.0\n"
    assert UNTRUSTED in err


def test_fields_named_like_the_tags_on_a_command_that_is_not_external_still_print() -> None:
    _, out, _ = run(["own", "--format", "tsv"])
    assert out.splitlines()[0] == "_source\t_trusted\tname"


def test_an_apps_own_renderer_gets_the_tags() -> None:
    seen: list[object] = []
    _, out, err = run(["rows", "--format", "yaml"], seen)
    assert out == "own\n"
    assert seen == [[{**TAGS, "title": "a", "price": 1.5}, {**TAGS, "title": "b", "price": 2.0}]]
    assert UNTRUSTED in err


def test_json_and_ndjson_keep_the_tags() -> None:
    _, out, _ = run(["rows", "--format", "json"])
    assert json.loads(out)["data"][0] == {**TAGS, "title": "a", "price": 1.5}
    _, out, _ = run(["rows", "--format", "ndjson"])
    assert json.loads(out.splitlines()[0]) == {**TAGS, "title": "a", "price": 1.5}


def test_a_buffered_external_stream_in_plain_prints_the_marker_not_the_tags() -> None:
    _, out, _ = run(["feed", "--format", "plain", "--no-stream"])
    assert out.startswith("(external content, untrusted)\n")
    assert "_source" not in out and "_trusted" not in out


def test_without_injection_protection_nothing_is_tagged() -> None:
    _, out, err = run(["rows", "--format", "csv", "--no-injection-protection"])
    assert out == "title,price\na,1.5\nb,2.0\n"
    assert UNTRUSTED not in err
