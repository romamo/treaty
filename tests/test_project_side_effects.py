"""#184: a ``SideEffect`` path under the project, ``{project_root}/...``, resolved against
the command's ``project_root=`` markers when ``cleanup`` and ``status`` run"""

import io
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Ctx, NoArgs, RegistrationError, SideEffect
from treaty._audit import audit

WINDOWS = sys.platform == "win32"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def dashboard(*effects: SideEffect) -> App:
    app = App("cf", version="1.0.0")

    @app.command(
        "dashboard",
        description="Build the dashboard under the project",
        danger_level="safe",
        exit_codes=(),
        project_root=(".cfproject",),
        filesystem_side_effects=list(effects)
        or [SideEffect("{project_root}/tmp/dashboard/", "cache")],
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


def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".cfproject").write_text("")
    return root


def data(envelope: dict[str, object]) -> dict[str, object]:
    found = envelope["data"]
    assert isinstance(found, dict)
    return found


def warnings(envelope: dict[str, object]) -> dict[str, dict[str, object]]:
    listed = envelope["warnings"]
    assert isinstance(listed, list)
    return {w["code"]: w for w in listed}


def test_a_project_path_needs_the_commands_project_root_markers() -> None:
    app = App("cf", version="1.0.0")
    with pytest.raises(RegistrationError, match="no project_root= markers"):

        @app.command(
            "dashboard",
            description="d",
            danger_level="safe",
            exit_codes=(),
            filesystem_side_effects=[SideEffect("{project_root}/tmp/dashboard/", "cache")],
        )
        def build(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


@pytest.mark.parametrize(
    "path",
    [
        "{project_root}/../outside/",
        "{project_root}/tmp/../../x",
        "{project_root}/",
        "{project_root}/*",
    ],
)
def test_a_project_path_stays_under_a_named_directory_of_the_project(path: str) -> None:
    with pytest.raises(RegistrationError, match=r"\.\. segments|not every entry"):
        SideEffect(path, "cache")


def test_the_manifest_carries_the_project_template(tmp_path: Path) -> None:
    manifest = dashboard().manifest()
    entry = manifest["commands"]["dashboard"]  # type: ignore[index]
    assert entry["filesystem_side_effects"] == [  # type: ignore[index]
        {"path": "{project_root}/tmp/dashboard/", "type": "cache"}
    ]
    spec_validator("manifest-response").validate(manifest)


def test_a_safe_command_declaring_its_project_writes_passes_fs_side_effects() -> None:
    rules = {r.id: r for r in audit(dashboard(), "cf", limit=3).rules}
    assert rules["fs-side-effects"].passed


def test_status_and_cleanup_resolve_the_project_from_a_subdirectory(tmp_path: Path) -> None:
    root = project(tmp_path)
    app = dashboard()
    code, envelope = run(app, ["dashboard"], root / "src")
    assert code == 0, envelope
    built = root / "tmp" / "dashboard"
    assert (built / "index.html").exists()

    code, envelope = run(app, ["status", "--show-side-effects"], root / "src")
    assert code == 0, envelope
    [entry] = data(envelope)["side_effects"]  # type: ignore[misc]
    assert entry["pattern"] == str((root / "tmp" / "dashboard").resolve())
    assert [p["path"] for p in entry["paths"]] == [str(built.resolve())]
    assert entry["bytes"] == len("<html></html>")

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], root / "src")
    assert code == 0, envelope
    assert data(envelope)["effect"] == "deleted"
    assert not built.exists()
    assert (root / ".cfproject").exists() and (root / "src").is_dir()


def test_outside_any_project_cleanup_leaves_the_paths_and_says_so(tmp_path: Path) -> None:
    root = project(tmp_path)
    built = root / "tmp" / "dashboard"
    built.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    # A cwd holding the same layout is never taken for the project
    (elsewhere / "tmp" / "dashboard").mkdir(parents=True)
    app = dashboard()

    code, envelope = run(app, ["cleanup", "--confirm-destructive"], elsewhere)
    assert code == 0, envelope
    assert data(envelope)["effect"] == "noop"
    assert built.is_dir() and (elsewhere / "tmp" / "dashboard").is_dir()
    warning = warnings(envelope)["PROJECT_ROOT_NOT_FOUND"]
    assert warning["context"]["paths"] == [  # type: ignore[index]
        {
            "command": "dashboard",
            "path": "{project_root}/tmp/dashboard/",
            "markers": [".cfproject"],
        }
    ]

    code, envelope = run(app, ["status", "--show-side-effects"], elsewhere)
    assert code == 0, envelope
    [entry] = data(envelope)["side_effects"]  # type: ignore[misc]
    assert "pattern" not in entry and entry["paths"] == []
    assert "PROJECT_ROOT_NOT_FOUND" in warnings(envelope)


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_cleanup_never_reaches_outside_the_project_through_a_symlink(tmp_path: Path) -> None:
    root = project(tmp_path)
    victim = tmp_path / "victim"
    (victim / "dashboard").mkdir(parents=True)
    (victim / "dashboard" / "precious").write_text("keep")
    (root / "tmp").symlink_to(victim)
    code, envelope = run(dashboard(), ["cleanup", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert data(envelope)["effect"] == "noop"
    assert (victim / "dashboard" / "precious").read_text() == "keep"


def test_cleanup_keeps_a_project_config_path(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "tmp").mkdir()
    (root / "tmp" / "settings.toml").write_text("x = 1")
    (root / "tmp" / "scratch").write_text("x")
    app = dashboard(
        SideEffect("{project_root}/tmp/*", "temp"),
        SideEffect("{project_root}/tmp/settings.toml", "config"),
    )
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert (root / "tmp" / "settings.toml").exists()
    assert not (root / "tmp" / "scratch").exists()


def test_a_brace_in_the_project_directory_is_literal(tmp_path: Path) -> None:
    root = tmp_path / "a{b}"
    root.mkdir()
    (root / ".cfproject").write_text("")
    (root / "tmp" / "dashboard").mkdir(parents=True)
    sibling = tmp_path / "aXb"
    (sibling / "tmp" / "dashboard").mkdir(parents=True)
    code, envelope = run(dashboard(), ["cleanup", "--confirm-destructive"], root)
    assert code == 0, envelope
    assert not (root / "tmp" / "dashboard").exists()
    assert (sibling / "tmp" / "dashboard").is_dir()
