import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Exit, Format, FormatName, NoArgs, RegistrationError, Renderer


def csv_rows(data: Any) -> str:
    return "".join(f"{k},{v}\n" for k, v in data.items())


def formats_app(*, plain: Renderer | None = None) -> App:
    app = App("showctl", version="1.0.0")
    app.format(Format.CSV, render=csv_rows)
    if plain is not None:
        app.format(Format.PLAIN, render=plain)

    @app.command("show", description="Show a release", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"service": "api", "tag": "1.3.9"}

    @app.command(
        "tag",
        description="Show only the tag",
        renderers={Format.CSV: lambda d: f"tag\n{d['tag']}\n"},
        danger_level="safe",
        exit_codes=(),
    )
    def tag(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"service": "api", "tag": "1.3.9"}

    @app.command("fail", description="Fail", exit_codes=["NOT_FOUND"], danger_level="safe")
    def fail(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        raise Exit.NOT_FOUND("no such release")

    @app.command("tick", description="Events", streaming=True, danger_level="safe", exit_codes=())
    def tick(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
        yield {"n": 1}
        yield {"n": 2}

    return app


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=True)
    return code, out.getvalue(), err.getvalue()


def test_app_renderer_writes_every_command_in_its_format() -> None:
    assert run(formats_app(), ["show", "--format", "csv"]) == (0, "service,api\ntag,1.3.9\n", "")


def test_app_format_env_selects_a_registered_format() -> None:
    code, out, _ = run(formats_app(), ["show"], env={"SHOWCTL_FORMAT": "csv"})
    assert code == 0 and out == "service,api\ntag,1.3.9\n"


def test_command_renderer_overrides_the_app_renderer() -> None:
    assert run(formats_app(), ["tag", "--format", "csv"]) == (0, "tag\n1.3.9\n", "")


def test_registering_plain_replaces_the_built_in_lines() -> None:
    app = formats_app(plain=lambda d: f"{d['service']} {d['tag']}\n")
    assert run(app, ["show"]) == (0, "api 1.3.9\n", "")


def test_json_ignores_the_renderers() -> None:
    code, out, _ = run(formats_app(), ["tag", "--format", "json"])
    assert code == 0 and json.loads(out)["data"] == {"service": "api", "tag": "1.3.9"}


def test_error_in_a_registered_format_is_prose_on_stderr() -> None:
    code, out, err = run(formats_app(), ["fail", "--format", "csv"])
    assert code == 5 and out == ""
    assert err.startswith("showctl: NOT_FOUND: No such release.\n")


def test_failing_renderer_names_its_format() -> None:
    app = App("showctl", version="1.0.0")
    app.format(Format.YAML, render=lambda d: d["missing"])

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {}

    code, out, err = run(app, ["show", "--format", "yaml"])
    assert code == 1 and out == ""
    assert "HANDLER_CRASHED: the yaml renderer failed" in err


def test_stream_renders_each_event_with_the_app_renderer() -> None:
    assert run(formats_app(), ["tick", "--format", "csv"]) == (0, "n,1\nn,2\n", "")


def test_no_stream_renders_the_buffered_events_one_by_one() -> None:
    code, out, _ = run(formats_app(), ["tick", "--no-stream", "--format", "csv"])
    assert code == 0 and out == "n,1\nn,2\n"


def test_manifest_stays_json_in_a_registered_format() -> None:
    code, out, _ = run(formats_app(), ["manifest", "--format", "csv"])
    assert code == 0 and "show" in json.loads(out)["commands"]


@pytest.mark.parametrize(
    ("argv", "env"),
    [(["show", "--format", "yaml"], {}), (["show"], {"SHOWCTL_FORMAT": "yaml"})],
)
def test_a_known_format_without_a_renderer_is_not_offered(
    argv: list[str], env: dict[str, str]
) -> None:
    out = io.StringIO()
    code = formats_app().run(argv, stdout=out, stderr=io.StringIO(), env=env, isatty=False)
    error = json.loads(out.getvalue())["error"]
    assert code == 2 and error["code"] == "ARG_ERROR"
    assert error["message"] == "--format 'yaml' is not offered by showctl."
    assert error["context"]["allowed"] == ["plain", "json", "jsonl", "ndjson", "csv", "tsv"]


def test_a_value_outside_format_is_unknown() -> None:
    out = io.StringIO()
    code = formats_app().run(["show", "--format", "xml"], stdout=out, stderr=io.StringIO(), env={})
    error = json.loads(out.getvalue())["error"]
    assert code == 2 and error["message"] == "Unknown --format 'xml'"
    assert error["context"]["allowed"] == ["plain", "json", "jsonl", "ndjson", "csv", "tsv"]


def test_manifest_and_help_list_the_offered_formats() -> None:
    app = formats_app()
    # A FormatName equals and hashes like its member, so code comparing members still works
    assert app.formats == (
        Format.PLAIN,
        Format.JSON,
        Format.JSONL,
        Format.NDJSON,
        Format.CSV,
        Format.TSV,
    )
    assert Format.CSV in app.formats and Format.YAML not in app.formats
    assert {Format.CSV: 1}[FormatName("csv")] == 1 and hash(FormatName("csv")) == hash(Format.CSV)
    offered = app.manifest()["flags"]["format"]["enum_values"]  # type: ignore[index]
    assert offered == ["plain", "json", "jsonl", "ndjson", "csv", "tsv"]
    _, out, _ = run(app, ["--help"])
    assert "--format plain|json|jsonl|ndjson|csv|tsv" in out and "$SHOWCTL_FORMAT" in out


# Registration


def test_a_members_name_is_that_member() -> None:
    """``app.format("csv", ...)`` is ``app.format(Format.CSV, ...)``, not a second csv"""
    app = App("t", version="1.0.0")
    app.format("csv", render=csv_rows)
    with pytest.raises(RegistrationError, match="already has a renderer"):
        app.format(Format.CSV, render=csv_rows)
    assert [n.value for n in app.formats].count("csv") == 1


def test_a_command_renderer_key_must_be_a_format() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="not a Format member or a format name"):
        app.command(
            "show",
            description="Show",
            renderers={3: csv_rows},  # type: ignore[dict-item]
            danger_level="safe",
            exit_codes=(),
        )


def test_json_takes_no_renderer() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="json is the response envelope"):
        app.format(Format.JSON, render=csv_rows)
    with pytest.raises(RegistrationError, match="json is the response envelope"):
        app.command(
            "show",
            description="Show",
            renderers={Format.JSON: csv_rows},
            danger_level="safe",
            exit_codes=(),
        )


def test_a_command_renderer_needs_the_format_registered_first() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"app\.format\(Format\.CSV"):
        app.command(
            "show",
            description="Show",
            renderers={Format.CSV: csv_rows},
            danger_level="safe",
            exit_codes=(),
        )


def test_a_format_is_registered_once() -> None:
    app = App("t", version="1.0.0")
    app.format(Format.CSV, render=csv_rows)
    with pytest.raises(RegistrationError, match="already has a renderer"):
        app.format(Format.CSV, render=csv_rows)


def test_a_renderer_must_be_callable() -> None:
    with pytest.raises(RegistrationError, match="not callable"):
        App("t", version="1.0.0").format(Format.CSV, render="csv")  # type: ignore[arg-type]


# Custom format names (#179)


def html_page(data: Any) -> str:
    return (
        "<html><body>" + "".join(f"<p>{k}: {v}</p>" for k, v in data.items()) + "</body></html>\n"
    )


def html_app() -> App:
    app = formats_app()
    app.format("html", render=html_page, media_type="text/html")
    app.format("rst", render=lambda d: f"{d}\n")

    @app.command(
        "why",
        description="Why it happened",
        renderers={"html": lambda d: f"<h1>{d['tag']}</h1>\n"},
        output_file=True,
        supports_raw_payload=True,
        danger_level="safe",
        exit_codes=(),
    )
    def why(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"service": "api", "tag": "1.3.9"}

    return app


SHOW_HTML = "<html><body><p>service: api</p><p>tag: 1.3.9</p></body></html>\n"


def test_a_custom_name_renders_every_command_and_a_command_may_override_it() -> None:
    app = html_app()
    assert run(app, ["show", "--format", "html"]) == (0, SHOW_HTML, "")
    assert run(app, ["why", "--format", "html"]) == (0, "<h1>1.3.9</h1>\n", "")
    assert run(app, ["tick", "--format", "rst"]) == (0, "{'n': 1}\n{'n': 2}\n", "")


def test_the_format_variable_selects_a_custom_name() -> None:
    code, out, _ = run(html_app(), ["show"], env={"SHOWCTL_FORMAT": "html"})
    assert code == 0 and out == SHOW_HTML


def test_an_error_under_a_custom_name_is_prose_on_stderr() -> None:
    code, out, err = run(html_app(), ["fail", "--format", "html"])
    assert code == 5 and out == ""
    assert err.startswith("showctl: NOT_FOUND: No such release.\n")


def test_manifest_help_and_completion_list_a_custom_name() -> None:
    app = html_app()
    manifest = app.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["flags"]["format"]  # type: ignore[index]
    assert entry["enum_values"] == ["plain", "json", "jsonl", "ndjson", "csv", "tsv", "html", "rst"]
    # FlagEntry has no media type key, so the description states it
    assert entry["description"].endswith("; html writes text/html")
    _, out, _ = run(app, ["--help"])
    assert "--format plain|json|jsonl|ndjson|csv|tsv|html|rst" in out
    assert "html writes text/html" in out
    code, script, _ = run(app, ["completion", "bash", "--format", "plain"])
    assert code == 0 and "html" in script and "rst" in script


def test_output_writes_a_custom_format_and_refuses_its_name_as_a_path(tmp_path: Any) -> None:
    target = tmp_path / "why.html"
    code, out, _ = run(html_app(), ["why", "--format", "html", "--output", str(target)])
    assert code == 0 and target.read_text() == "<h1>1.3.9</h1>\n"
    assert json.loads(out)["data"]["path"] == str(target)
    out_io = io.StringIO()
    code = html_app().run(
        ["--cwd", str(tmp_path), "why", "--output", "html"],
        stdout=out_io,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    error = json.loads(out_io.getvalue())["error"]
    assert code == 2 and error["suggestion"] == "use --format html to choose the representation"
    assert not (tmp_path / "html").exists()


def test_an_unregistered_name_is_unknown_listing_the_custom_ones() -> None:
    out = io.StringIO()
    code = html_app().run(
        ["show", "--format", "xml"], stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    error = json.loads(out.getvalue())["error"]
    assert code == 2 and error["message"] == "Unknown --format 'xml'"
    assert error["context"]["allowed"][-2:] == ["html", "rst"]


@pytest.mark.parametrize("name", ["json", "jsonl", "ndjson", "id"])
def test_treatys_own_formats_stay_reserved(name: str) -> None:
    with pytest.raises(RegistrationError, match="takes no renderer"):
        App("t", version="1.0.0").format(name, render=csv_rows)


@pytest.mark.parametrize("name", ["HTML", "html page", "1html", "-html", "html-", "", "x" * 33])
def test_a_custom_name_is_validated(name: str) -> None:
    with pytest.raises(RegistrationError, match="format name"):
        App("t", version="1.0.0").format(name, render=csv_rows)


@pytest.mark.parametrize("media_type", ["html", "text/HTML", "text/html; charset=utf-8", ""])
def test_a_media_type_is_validated(media_type: str) -> None:
    with pytest.raises(RegistrationError, match="media type"):
        App("t", version="1.0.0").format("html", render=csv_rows, media_type=media_type)


def test_a_command_renderer_for_a_custom_name_needs_it_registered_first() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"app\.format\('html'"):
        app.command(
            "show",
            description="Show",
            renderers={"html": csv_rows},
            danger_level="safe",
            exit_codes=(),
        )


def test_a_handler_tells_a_custom_name_from_plain() -> None:
    """``ctx.mode`` is plain's for a custom name; ``ctx.format_name`` is what was asked"""
    app = html_app()
    seen: list[tuple[Format, FormatName]] = []

    @app.command("peek", description="Peek", danger_level="safe", exit_codes=())
    def peek(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        seen.append((ctx.mode, ctx.format_name))
        return {}

    run(app, ["peek", "--format", "html"])
    run(app, ["peek", "--format", "plain"])
    run(app, ["peek", "--format", "csv"])
    # jsonl runs as json, but the handler still sees what the caller asked for
    run(app, ["peek", "--format", "jsonl"])
    # An exec line and App.call run and answer in JSON, whatever --format the run had
    app.run(
        ["--format", "html", "exec"],
        stdin=io.StringIO('{"_cmd": "peek"}\n'),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    app.call("peek", {})
    assert seen == [
        (Format.PLAIN, FormatName("html")),
        (Format.PLAIN, FormatName("plain")),
        (Format.CSV, FormatName("csv")),
        (Format.JSON, FormatName("jsonl")),
        (Format.JSON, FormatName("json")),
        (Format.JSON, FormatName("json")),
    ]


def test_output_is_argv_only_so_no_other_path_can_name_a_format_file() -> None:
    """The ``--output html`` refusal runs on argv, ``--raw-payload`` included; an ``exec``
    line and ``App.call`` take no ``output`` key at all, so they cannot write a file"""
    app = html_app()
    called = app.call("why", {"output": "html"})
    assert called.error is not None and called.error.code == "ARG_ERROR"
    assert "Unknown field 'output'" in called.error.message
    out = io.StringIO()
    code = app.run(
        ["exec"],
        stdin=io.StringIO('{"_cmd": "why", "output": "html"}\n'),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    line = json.loads(out.getvalue())
    assert code != 0 and line["error"]["code"] == "ARG_ERROR"
    assert "Unknown field 'output'" in line["error"]["message"]
    out = io.StringIO()
    code = app.run(
        ["why", "--raw-payload", "{}", "--output", "html"],
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    assert code == 2 and json.loads(out.getvalue())["error"]["context"]["value"] == "html"
