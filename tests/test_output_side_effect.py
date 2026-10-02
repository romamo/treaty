"""#184: an ``output`` side effect, a location a command writes as its product. ``status``
lists it, ``cleanup`` never removes it, and it takes no ``ttl_seconds`` or
``clearable_with`` (REQ-C-011, REQ-O-027, REQ-C-002)"""

import io
import json
import os
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Ctx, NoArgs, RegistrationError, SideEffect
from treaty._audit import audit

BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
DASHBOARD = SideEffect("{project_root}/tmp/dashboard/", "output")


def dashboard(*effects: SideEffect) -> App:
    app = App("cf", version="1.0.0")

    @app.command(
        "dashboard",
        description="Build the dashboard under the project",
        danger_level="safe",
        exit_codes=(),
        project_root=(".cfproject",),
        filesystem_side_effects=[DASHBOARD, *effects],
    )
    def build(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        assert ctx.project_root is not None
        out = ctx.project_root / "tmp" / "dashboard"
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("<html></html>")
        return {}

    return app


def run(app: App, argv: list[str], cwd: Path) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--cwd", str(cwd), "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, "TMPDIR": str(cwd)},
    )
    return code, json.loads(out.getvalue())


def built(tmp_path: Path, app: App) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".cfproject").write_text("")
    code, envelope = run(app, ["dashboard"], root)
    assert code == 0, envelope
    return root


def data(envelope: dict[str, object]) -> dict[str, object]:
    found = envelope["data"]
    assert isinstance(found, dict)
    return found


@pytest.mark.parametrize(
    "extra", [{"ttl_seconds": 3600}, {"clearable_with": "cf cleanup"}, {"ttl_seconds": 0}]
)
def test_an_output_side_effect_takes_no_ttl_or_clear_command(extra: dict[str, object]) -> None:
    with pytest.raises(RegistrationError, match="output side effect is the command's product"):
        SideEffect("{project_root}/tmp/dashboard/", "output", **extra)  # type: ignore[arg-type]


def test_the_manifest_emits_the_output_type_and_validates() -> None:
    manifest = dashboard().manifest()
    entry = manifest["commands"]["dashboard"]  # type: ignore[index]
    assert entry["danger_level"] == "safe"  # type: ignore[index]
    assert entry["filesystem_side_effects"] == [  # type: ignore[index]
        {"path": "{project_root}/tmp/dashboard/", "type": "output"}
    ]
    spec_validator("manifest-response").validate(manifest)


def test_a_safe_command_writing_its_product_passes_fs_side_effects() -> None:
    rules = {r.id: r for r in audit(dashboard(), "cf", limit=3).rules}
    assert rules["fs-side-effects"].passed


def test_status_lists_the_output_path_with_its_size(tmp_path: Path) -> None:
    root = built(tmp_path, dashboard())
    code, envelope = run(dashboard(), ["status", "--show-side-effects"], root)
    assert code == 0, envelope
    effects = data(envelope)["side_effects"]
    assert isinstance(effects, list)
    [entry] = [e for e in effects if e["type"] == "output"]
    assert entry["command"] == "dashboard"
    assert entry["bytes"] == len("<html></html>")
    assert [Path(p["path"]) for p in entry["paths"]] == [(root / "tmp" / "dashboard").resolve()]


@pytest.mark.parametrize("scope", ["all", "temp", "cache", "logs"])
def test_cleanup_never_removes_an_output_path_under_any_scope(tmp_path: Path, scope: str) -> None:
    root = built(tmp_path, dashboard())
    code, envelope = run(dashboard(), ["cleanup", "--scope", scope, "--confirm-destructive"], root)
    assert code == 0, envelope
    assert data(envelope)["cleaned"] == []
    assert (root / "tmp" / "dashboard" / "index.html").read_text() == "<html></html>"


@pytest.mark.parametrize("kind", ["cache", "temp", "log"])
def test_cleanup_keeps_an_output_path_a_wider_glob_also_covers(tmp_path: Path, kind: str) -> None:
    app = dashboard(SideEffect("{project_root}/tmp/*", kind))
    root = built(tmp_path, app)
    (root / "tmp" / "scratch").write_text("x")
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert (root / "tmp" / "dashboard" / "index.html").exists()
    assert not (root / "tmp" / "scratch").exists()
    [kept] = [w for w in envelope["warnings"] if w["code"] == "CLEANUP_KEPT"]  # type: ignore[attr-defined]
    assert kept["context"]["paths"] == [str(root / "tmp" / "dashboard")]


def test_cleanup_keeps_a_path_declared_inside_an_output_path(tmp_path: Path) -> None:
    app = dashboard(SideEffect("{project_root}/tmp/dashboard/*", "temp"))
    root = built(tmp_path, app)
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert data(envelope)["cleaned"] == []
    assert (root / "tmp" / "dashboard" / "index.html").exists()


def test_cleanup_keeps_a_handed_out_output_file_declared_as_output(tmp_path: Path) -> None:
    # The output files commands hand out are cleanup's own temp paths, unless a command
    # declares their directory as its product
    def report_app(*effects: SideEffect) -> App:
        app = App("cf", version="1.0.0")

        @app.command(
            "report",
            description="Write a report file",
            danger_level="safe",
            exit_codes=(),
            filesystem_side_effects=list(effects),
        )
        def report(args: NoArgs, ctx: Ctx) -> dict[str, object]:
            path = ctx.output_file("report.json", keep_seconds=3600)
            path.write_text("{}")
            return {"path": str(path)}

        return app

    # The session directory is cf-<uid> on POSIX and cf on Windows, which has no getuid
    declared = SideEffect(f"{tmp_path.as_posix()}/cf*/out/*", "output")
    code, envelope = run(report_app(declared), ["report"], tmp_path)
    assert code == 0, envelope
    handed = Path(str(data(envelope)["path"]))
    code, envelope = run(report_app(declared), ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert handed.exists()
    code, envelope = run(report_app(), ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert not handed.exists()


@pytest.mark.parametrize("product", ["alias", "real"])
def test_cleanup_keeps_an_output_path_spelled_through_a_symlink(
    tmp_path: Path, product: str
) -> None:
    # One directory under two spellings, as /tmp and /private/tmp on macOS: a cache glob
    # in one must not remove the output path declared in the other
    real = tmp_path / "real"
    (real / "dash").mkdir(parents=True)
    (real / "dash" / "index.html").write_text("<html></html>")
    (real / "scratch").write_text("x")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges here")
    cache = alias if product == "real" else real
    out = real if product == "real" else alias
    app = App("cf", version="1.0.0")

    @app.command(
        "dash",
        description="Build the dashboard",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(f"{out.as_posix()}/dash/", "output"),
            SideEffect(f"{cache.as_posix()}/*", "cache"),
        ],
    )
    def dash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert (real / "dash" / "index.html").exists()
    assert not (real / "scratch").exists()


def test_cleanup_keeps_the_target_of_an_output_path_that_is_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "scratch" / "dash"
    target.mkdir(parents=True)
    (target / "index.html").write_text("<html></html>")
    (tmp_path / "proj").mkdir()
    try:
        (tmp_path / "proj" / "dash").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges here")
    app = App("cf", version="1.0.0")

    @app.command(
        "dash",
        description="Build the dashboard",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(f"{tmp_path.as_posix()}/proj/dash/", "output"),
            SideEffect(f"{tmp_path.as_posix()}/scratch/*", "temp"),
        ],
    )
    def dash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert (target / "index.html").exists()


def test_cleanup_removes_a_temp_path_sharing_only_a_name_prefix_with_an_output_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "dash").mkdir()
    (tmp_path / "dash-tmp").mkdir()
    app = App("cf", version="1.0.0")

    @app.command(
        "dash",
        description="Build the dashboard",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(f"{tmp_path.as_posix()}/dash/", "output"),
            SideEffect(f"{tmp_path.as_posix()}/dash-tmp/", "temp"),
        ],
    )
    def dash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert (tmp_path / "dash").exists()
    assert not (tmp_path / "dash-tmp").exists()


def linked_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose ``tmp`` is a symlink to a scratch directory outside it (#227)"""
    scratch = tmp_path / "scratch" / "tmp"
    scratch.mkdir(parents=True)
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".cfproject").write_text("")
    try:
        (root / "tmp").symlink_to(scratch, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges here")
    (scratch / "dashboard").mkdir()
    (scratch / "dashboard" / "index.html").write_text("<html></html>")
    (scratch / "stale").write_text("x")
    return root, scratch


@pytest.mark.parametrize(
    "output", [DASHBOARD, SideEffect("{project_root}/tm*/dashboard/", "output")]
)
def test_cleanup_keeps_an_output_path_whose_project_directory_links_outside(
    tmp_path: Path, output: SideEffect
) -> None:
    # The output's match resolves outside the project, so cleanup never removes through
    # it; it still keeps what another command's temp glob reaches by the resolved path
    root, scratch = linked_project(tmp_path)
    app = App("cf", version="1.0.0")

    @app.command(
        "dashboard",
        description="Build the dashboard under the project",
        danger_level="safe",
        exit_codes=(),
        project_root=(".cfproject",),
        filesystem_side_effects=[output],
    )
    def build(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    @app.command(
        "scratch",
        description="Use the scratch directory",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[SideEffect(f"{scratch.as_posix()}/*", "temp")],
    )
    def use(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["cleanup", "--scope", "all", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert (scratch / "dashboard" / "index.html").read_text() == "<html></html>"
    assert not (scratch / "stale").exists()
    assert (root / "tmp").is_symlink()


def test_cleanup_keeps_an_output_path_a_glob_spells_in_another_case(tmp_path: Path) -> None:
    # On a case-insensitive filesystem /x/Scratch and /x/scratch are one directory
    real = tmp_path / "scratch"
    (real / "dash").mkdir(parents=True)
    (real / "dash" / "index.html").write_text("<html></html>")
    (real / "stale").write_text("x")
    other = tmp_path / "SCRATCH"
    if not other.exists():
        pytest.skip("the filesystem here is case-sensitive")
    app = App("cf", version="1.0.0")

    @app.command(
        "dash",
        description="Build the dashboard",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(f"{real.as_posix()}/dash/", "output"),
            SideEffect(f"{other.as_posix()}/*", "temp"),
        ],
    )
    def dash(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], tmp_path)
    assert code == 0, envelope
    assert (real / "dash" / "index.html").exists()
    assert not (real / "stale").exists()
