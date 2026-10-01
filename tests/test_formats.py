import io
import json
from collections.abc import Iterator
from typing import Any

import pytest

from treaty import App, Ctx, Exit, Format, NoArgs, RegistrationError, Renderer


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
    assert app.formats == (
        Format.PLAIN,
        Format.JSON,
        Format.JSONL,
        Format.NDJSON,
        Format.CSV,
        Format.TSV,
    )
    offered = app.manifest()["flags"]["format"]["enum_values"]  # type: ignore[index]
    assert offered == ["plain", "json", "jsonl", "ndjson", "csv", "tsv"]
    _, out, _ = run(app, ["--help"])
    assert "--format plain|json|jsonl|ndjson|csv|tsv" in out and "$SHOWCTL_FORMAT" in out


# Registration


def test_a_string_is_not_a_format() -> None:
    with pytest.raises(RegistrationError, match="not a Format member"):
        App("t", version="1.0.0").format("csv", render=csv_rows)  # type: ignore[arg-type]


def test_a_command_renderer_key_must_be_a_format() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="not a Format member"):
        app.command(
            "show",
            description="Show",
            renderers={"plain": csv_rows},
            danger_level="safe",
            exit_codes=(),
        )  # type: ignore[dict-item]


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
