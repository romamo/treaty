"""ManifestResponse 3.6 to 3.15 (#231): one app declaring every contract those versions
gave a key, each emitted as the spec defines it and none as a sentence in a description"""

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import pytest
from conftest import spec_validator

from treaty import (
    App,
    Binary,
    Ctx,
    Flag,
    FormatRenderer,
    NoArgs,
    OutputBase,
    RegistrationError,
    SideEffect,
)
from treaty._audit import removals


@dataclass(frozen=True, slots=True)
class Filter:
    status: str
    min_total: int = 0


@dataclass(frozen=True, slots=True)
class ReportArgs:
    filter: Filter | None = Flag(default=None, description="Order filter")
    lines: tuple[Filter, ...] = Flag(default=(), description="Extra filters, one each")


@dataclass(frozen=True, slots=True)
class ExportArgs:
    output: Path = Flag(description="Path of the compiled export")


@dataclass(frozen=True, slots=True)
class Sec:
    id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class MigrateArgs:
    yes: bool = Flag(confirm=True, description="Apply the migrations")


@dataclass(frozen=True, slots=True)
class Settings:
    ledger: str = Flag(default="main.ledger", description="Ledger", env=("LEDGER_FILE",))
    api_key: str = Flag(default="", description="API key", secret=True, env=("LEDGER_KEY",))


@dataclass(frozen=True, slots=True)
class ProjectArgs:
    project: Path = Flag(default=Path("."), description="Project directory")


@dataclass(frozen=True, slots=True)
class Project:
    directory: Path

    @classmethod
    def acquire(cls, args: ProjectArgs, ctx: Ctx) -> Self:
        return cls(args.project)


def make_app(**kw: Any) -> App:
    app = App("ledger", version="1.0.0", settings=Settings, **kw)
    app.format("html", render=lambda data: "<p></p>\n", media_type="text/html")
    app.format("rst", render=lambda data: "x\n")

    @app.command(
        "report",
        description="Render a report into the project's reports directory",
        danger_level="mutating",
        exit_codes=(),
        output_file=OutputBase.PROJECT_ROOT,
        project_root=(".ledger",),
        renderers={
            "pdf": FormatRenderer(lambda data: "%PDF\n", media_type="application/pdf"),
            "yaml": lambda data: "a: 1\n",
        },
    )
    def report(args: ReportArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    @app.command(
        "download",
        description="Download the statement",
        danger_level="safe",
        exit_codes=(),
        output_file=Project,
    )
    def download(args: ProjectArgs, ctx: Ctx, project: Project) -> Binary:
        return Binary(b"%PDF", "application/pdf")

    @app.command("export", description="Compile the export", danger_level="safe", exit_codes=())
    def export(args: ExportArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    @app.command(
        "import", description="Import a payload", danger_level="mutating", exit_codes=(),
        stdin_input=True,
    )  # fmt: skip
    def import_(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    @app.command(
        "count", description="Count lines", danger_level="safe", exit_codes=(), stdin_input="lines"
    )
    def count(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"lines": sum(1 for _ in ctx.stdin_lines)}

    @app.command(
        "tag", description="Tag securities", danger_level="mutating", exit_codes=(),
        stdin_records=Sec,
    )  # fmt: skip
    def tag(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "updated"}

    @app.command(
        "ingest",
        description="Run the ingest tool",
        danger_level="mutating",
        exit_codes=(),
        passthrough=True,
        help_command=("extract", "--help"),
    )
    def ingest(args: NoArgs, ctx: Ctx) -> int:
        return 0

    @app.command(
        "dashboard",
        description="Render the dashboard",
        danger_level="safe",
        exit_codes=(),
        project_root=(".ledger",),
        filesystem_side_effects=[SideEffect("{project_root}/tmp/dashboard/", "output")],
    )
    def dashboard(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    @app.command(
        "provision", description="Run the playbook", danger_level="mutating", exit_codes=(),
        child_log=True,
    )  # fmt: skip
    def provision(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "updated"}

    @app.command("migrate", description="Apply migrations", danger_level="mutating", exit_codes=())
    def migrate(args: MigrateArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "would_update" if ctx.dry_run else "updated"}

    @app.command(
        "observe", description="Rewrite the snapshots", danger_level="mutating", exit_codes=(),
        idempotent=True,
    )  # fmt: skip
    def observe(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "updated"}

    return app


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    built = make_app().manifest()
    spec_validator("manifest-response").validate(built)
    return built


def test_the_manifest_declares_3_15(manifest: dict[str, Any]) -> None:
    assert manifest["schema_version"] == "3.15"


def test_3_6_output_written_by_the_handler_or_the_envelope(manifest: dict[str, Any]) -> None:
    commands = manifest["commands"]
    assert commands["export"]["output_file"] == "handler"
    assert commands["ingest"]["output_file"] == "envelope"
    assert commands["report"]["output_file"] == "formatted"
    assert commands["download"]["output_file"] == "binary"


def test_3_6_a_passthrough_command_with_output_file_writes_the_envelope(tmp_path: Path) -> None:
    """``output_file=`` on a passthrough command sets only where a relative --output lands:
    the file still gets the final envelope as JSON, whatever --format says"""
    app = App("pt", version="1.0.0")

    @app.command(
        "ingest",
        description="Run the ingest tool",
        danger_level="safe",
        exit_codes=(),
        passthrough=True,
        output_file=True,
    )
    def ingest(args: NoArgs, ctx: Ctx) -> int:
        return 0

    target = tmp_path / "envelope.json"
    argv = ["--output", str(target), "--format", "plain", "ingest"]
    assert app.run(argv, stdout=io.StringIO(), stderr=io.StringIO(), env={}) == 0
    assert json.loads(target.read_text())["data"] == {"exit_code": 0}
    built = app.manifest()
    spec_validator("manifest-response").validate(built)
    assert built["commands"]["ingest"]["output_file"] == "envelope"


def test_3_7_object_flags_and_the_output_base(manifest: dict[str, Any]) -> None:
    report = manifest["commands"]["report"]
    single, many = report["flags"]["filter"], report["flags"]["lines"]
    assert single["type"] == "object" and many["type"] == "array"
    assert single["schema"] == many["schema"]
    assert single["schema"]["required"] == ["status"]
    assert single["description"] == "Order filter"
    assert report["output_file_base"] == "project_root"
    assert manifest["commands"]["download"]["output_file_base"] == "resource"
    assert "output_file_base" not in manifest["commands"]["export"]


def test_3_8_stdin_modes(manifest: dict[str, Any]) -> None:
    commands = manifest["commands"]
    assert commands["import"]["stdin"] == {"mode": "buffered"}
    assert commands["count"]["stdin"] == {"mode": "lines"}
    tag = commands["tag"]["stdin"]
    assert tag["mode"] == "records" and set(tag["record_schema"]["properties"]) == {"id", "symbol"}
    # exec reads its plan from stdin as a payload, under the same cap
    assert commands["exec"]["stdin"] == {"mode": "buffered"}
    assert "stdin" not in commands["observe"]


def test_3_8_caps_other_than_the_spec_defaults_are_listed() -> None:
    built = make_app(max_stdin_bytes=4096, max_line_bytes=512).manifest()
    spec_validator("manifest-response").validate(built)
    commands = built["commands"]
    assert commands["import"]["stdin"] == {"mode": "buffered", "max_bytes": 4096}
    assert commands["count"]["stdin"] == {"mode": "lines", "max_line_bytes": 512}
    assert commands["tag"]["stdin"]["max_line_bytes"] == 512


def test_3_9_passthrough(manifest: dict[str, Any]) -> None:
    ingest = manifest["commands"]["ingest"]
    assert ingest["arguments"] == "passthrough" and ingest["option_placement"] == "strict"
    assert ingest["help_argv"] == ["extract", "--help"] and ingest["flags"] == {}
    assert ingest["description"] == "Run the ingest tool"
    assert all("arguments" not in c for p, c in manifest["commands"].items() if p != "ingest")


def test_3_10_output_side_effect_on_a_safe_command(manifest: dict[str, Any]) -> None:
    dashboard = manifest["commands"]["dashboard"]
    assert dashboard["danger_level"] == "safe"
    assert dashboard["filesystem_side_effects"] == [
        {"path": "{project_root}/tmp/dashboard/", "type": "output"}
    ]


def test_3_11_child_log(manifest: dict[str, Any]) -> None:
    provision = manifest["commands"]["provision"]
    assert provision["stderr"] == "child_log" and provision["description"] == "Run the playbook"


def test_3_12_media_types(manifest: dict[str, Any]) -> None:
    output_format = manifest["flags"]["format"]
    assert output_format["media_types"] == {"ndjson": "application/x-ndjson", "html": "text/html"}
    assert "writes" not in output_format["description"]
    report = manifest["commands"]["report"]
    assert report["output_formats"] == ["html", "rst", "pdf", "yaml"]
    assert report["output_media_types"] == {"pdf": "application/pdf", "yaml": "application/yaml"}
    assert report["description"] == "Render a report into the project's reports directory"
    # A command that inherits the app's formats only needs no map of its own
    assert "output_media_types" not in manifest["commands"]["export"]


def test_3_12_help_still_names_a_registered_formats_media_type() -> None:
    out = io.StringIO()
    make_app().run(["--help", "--format", "plain"], stdout=out, stderr=io.StringIO(), env={})
    assert "; html writes text/html" in out.getvalue()
    assert "ndjson writes" not in out.getvalue()


def test_3_13_tool_wide_secrets_and_ecosystem_names(manifest: dict[str, Any]) -> None:
    assert manifest["secret_env_vars"] == ["LEDGER_API_KEY", "LEDGER_KEY"]
    names = [e["name"] for e in manifest["env_vars"]]
    # The ecosystem's name follows the prefixed one, which the tool reads first
    assert names[names.index("LEDGER_LEDGER") + 1] == "LEDGER_FILE"
    assert not {"LEDGER_API_KEY", "LEDGER_KEY"} & set(names)
    secrets = {v for c in manifest["commands"].values() for v in c.get("secret_env_vars", [])}
    assert not secrets & set(manifest["secret_env_vars"])


@dataclass(frozen=True, slots=True)
class KeyArgs:
    api_key: str = Flag(default="", description="API key for this call", secret=True)
    token: str = Flag(default="", description="Upload token", secret=True)


def test_3_13_a_root_secret_is_in_no_commands_secret_env_vars() -> None:
    """A secret field named as a secret setting reads the same <APP>_<NAME>, which the root
    already lists for every command"""
    app = App("ledger", version="1.0.0", settings=Settings)

    @app.command("push", description="Push", danger_level="safe", exit_codes=())
    def push(args: KeyArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    built = app.manifest()
    spec_validator("manifest-response").validate(built)
    assert built["secret_env_vars"] == ["LEDGER_API_KEY", "LEDGER_KEY"]
    assert built["commands"]["push"]["secret_env_vars"] == ["LEDGER_TOKEN"]


def test_3_13_an_app_without_a_secret_setting_has_no_root_secrets() -> None:
    app = App("plain", version="1.0.0")
    assert "secret_env_vars" not in app.manifest()


def test_3_14_confirm_flag(manifest: dict[str, Any]) -> None:
    migrate = manifest["commands"]["migrate"]
    assert migrate["confirm_flag"] == "yes"
    assert migrate["flags"]["yes"]["description"] == "Apply the migrations"
    assert all("confirm_flag" not in c for p, c in manifest["commands"].items() if p != "migrate")


def test_3_15_idempotent(manifest: dict[str, Any]) -> None:
    observe = manifest["commands"]["observe"]
    assert observe["idempotent"] is True and observe["description"] == "Rewrite the snapshots"
    assert "idempotent" not in manifest["commands"]["provision"]


def test_schema_of_one_command_carries_the_new_keys() -> None:
    out = io.StringIO()
    code = make_app().run(["--schema", "observe"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0 and json.loads(out.getvalue())["data"]["idempotent"] is True


def test_a_spec_format_keeps_its_media_type() -> None:
    """REQ-O-001: plain is always text/plain; a variant is a format name of its own"""
    app = App("plain", version="1.0.0")
    with pytest.raises(RegistrationError, match="text/plain"):
        app.format("plain", render=lambda data: "x\n", media_type="text/html")
    with pytest.raises(RegistrationError, match="text/tab-separated-values"):

        @app.command(
            "x",
            description="X",
            danger_level="safe",
            exit_codes=(),
            renderers={"tsv": FormatRenderer(lambda data: "x\n", media_type="text/csv")},
        )
        def x(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}

    app.format("plain", render=lambda data: "x\n", media_type="text/plain")


def test_a_baseline_listing_a_passthrough_commands_flags_is_no_removal(
    manifest: dict[str, Any],
) -> None:
    """Before 3.9 a passthrough entry listed the framework flags it takes before its path;
    the baseline audit does not report them gone (REQ-F-075)"""
    baseline = json.loads(json.dumps(manifest))
    baseline["commands"]["ingest"]["flags"] = {
        "output": {"type": "string", "required": False, "description": "Envelope file"}
    }
    baseline["commands"]["observe"]["flags"]["gone"] = {
        "type": "string",
        "required": False,
        "description": "A flag the app dropped",
    }
    found = [(f.command, f.message) for f in removals(make_app(), baseline)]
    assert found == [("observe", "--gone is gone without a release that deprecated it")]
