"""#68: where a relative --output lands: the cwd, the project root, or a directory a
resource or function of the run resolves (REQ-O-001)"""

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import pytest
from conftest import spec_validator

from treaty import (
    App,
    Binary,
    CliExit,
    Ctx,
    ExitCodeName,
    Flag,
    OutputBase,
    RegistrationError,
)
from treaty._audit import audit

TEXT = "Write the result to this file in the --format representation; stdout gets the envelope"


@dataclass(frozen=True, slots=True)
class ProjectArgs:
    project: Path = Flag(default=Path("."), description="Project directory")
    fail: bool = Flag(default=False, description="Fail after the work")


@dataclass(frozen=True, slots=True)
class Project:
    directory: Path

    @classmethod
    def acquire(cls, args: ProjectArgs, ctx: Ctx) -> Self:
        return cls(args.project)


def inventory_dir(ctx: Ctx, project: Project) -> Path:
    return project.directory / "inventory"


def not_a_path(ctx: Ctx) -> object:
    return "somewhere"


def render(args: ProjectArgs, ctx: Ctx) -> dict[str, int]:
    if args.fail:
        raise CliExit(ExitCodeName("NOT_FOUND"), "nothing to render")
    return {"hosts": 3}


def make_app(output_file: object, project_root: tuple[str, ...] = ()) -> App:
    app = App("cloud", version="1.0.0")
    app.command(
        "render",
        description="Render",
        danger_level="safe",
        exit_codes=("NOT_FOUND",),
        output_file=output_file,
        project_root=project_root,
    )(render)
    return app


def run(app: App, argv: list[str]) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "proj" / "inventory").mkdir(parents=True)
    (tmp_path / "proj" / ".cloud").write_text("")
    (tmp_path / "proj" / "sub").mkdir()
    (tmp_path / "elsewhere").mkdir()
    return tmp_path / "proj"


def test_output_file_true_stays_cwd_relative_with_a_project_root(project: Path) -> None:
    # Declaring project_root= does not move where output_file=True writes
    app = make_app(True, project_root=(".cloud",))
    cwd = project / "sub"
    code, envelope = run(app, ["render", "--cwd", str(cwd), "--output", "r.json"])
    assert code == 0 and envelope["data"]["path"] == str(cwd / "r.json")
    assert json.loads((cwd / "r.json").read_text()) == {"hosts": 3}


def test_project_root_base_puts_a_relative_output_in_the_root(project: Path) -> None:
    app = make_app(OutputBase.PROJECT_ROOT, project_root=(".cloud",))
    code, envelope = run(app, ["render", "--cwd", str(project / "sub"), "--output", "r.json"])
    target = project / "r.json"
    assert code == 0 and envelope["data"] == {"path": str(target), "bytes": target.stat().st_size}


def test_project_root_base_without_a_root_exits_2_before_running(tmp_path: Path) -> None:
    app = make_app(OutputBase.PROJECT_ROOT, project_root=(".cloud",))
    (tmp_path / "bare").mkdir()
    code, envelope = run(app, ["render", "--cwd", str(tmp_path / "bare"), "--output", "r.json"])
    assert code == 2 and "project root" in envelope["error"]["message"]
    assert not list((tmp_path / "bare").iterdir())


@pytest.mark.parametrize("base", [Project, inventory_dir])
def test_a_resource_or_function_base_holds_a_relative_output(
    project: Path, tmp_path: Path, base: object
) -> None:
    app = make_app(base)
    argv = ["render", "--cwd", str(tmp_path / "elsewhere"), "--project", str(project)]
    code, envelope = run(app, [*argv, "--output", "r.json"])
    landed = project / "r.json" if base is Project else project / "inventory" / "r.json"
    assert code == 0 and envelope["data"]["path"] == str(landed)
    assert json.loads(landed.read_text()) == {"hosts": 3}
    assert not list((tmp_path / "elsewhere").iterdir())


def cwd(ctx: Ctx, project: Project) -> Path:
    return project.directory / "inventory"


def project_root(ctx: Ctx, project: Project) -> Path:
    return project.directory / "inventory"


@pytest.mark.parametrize("base", [cwd, project_root])
def test_a_function_named_like_a_builtin_base_still_resolves_the_output(
    project: Path, tmp_path: Path, base: object
) -> None:
    # A function base named cwd or project_root is the function, not the built-in base
    argv = ["render", "--cwd", str(tmp_path / "elsewhere"), "--project", str(project)]
    code, envelope = run(make_app(base, project_root=(".cloud",)), [*argv, "--output", "r"])
    assert code == 0 and envelope["data"]["path"] == str(project / "inventory" / "r")
    assert not list((tmp_path / "elsewhere").iterdir())
    manifest = make_app(base, project_root=(".cloud",)).manifest()
    flag = manifest["commands"]["render"]["flags"]["output"]
    assert flag["description"].endswith(f"lands in the {base.__name__} directory")  # type: ignore[attr-defined]


def test_a_relative_base_is_under_the_cwd(tmp_path: Path, project: Path) -> None:
    argv = ["render", "--cwd", str(tmp_path), "--project", "proj", "--output", "x"]
    code, envelope = run(make_app(Project), argv)
    assert code == 0 and envelope["data"]["path"] == str(project / "x")


def test_an_absolute_output_is_used_as_given(project: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "abs.json"
    argv = ["render", "--project", str(project), "--output", str(target)]
    code, envelope = run(make_app(Project), argv)
    assert code == 0 and envelope["data"]["path"] == str(target) and target.exists()
    assert not (project / "abs.json").exists()


def test_a_failed_run_writes_no_file_in_the_base(project: Path) -> None:
    argv = ["render", "--project", str(project), "--fail", "--output", "r.json"]
    code, envelope = run(make_app(Project), argv)
    assert code != 0 and envelope["error"]["code"] == "NOT_FOUND"
    assert not (project / "r.json").exists()


def test_a_missing_parent_in_the_base_fails_and_keeps_the_data(project: Path) -> None:
    argv = ["render", "--project", str(project), "--output", "missing/r.json"]
    code, envelope = run(make_app(Project), argv)
    assert code == 1 and envelope["error"]["code"] == "OUTPUT_UNWRITABLE"
    assert envelope["data"] == {"hosts": 3} and not (project / "missing").exists()


def test_dot_dot_out_of_the_base_is_refused_as_for_the_cwd(project: Path) -> None:
    argv = ["render", "--project", str(project), "--output", "../r.json"]
    code, envelope = run(make_app(Project), argv)
    [error] = envelope["error"]["errors"]
    assert code == 2 and error["context"]["rejected_pattern"] == "path_traversal"
    assert not (project.parent / "r.json").exists()


def test_a_base_function_returning_no_path_fails_without_a_file(project: Path) -> None:
    code, envelope = run(make_app(not_a_path), ["render", "--output", "r.json"])
    assert code != 0 and not envelope["ok"] and not Path("r.json").exists()


def test_help_and_manifest_name_the_base() -> None:
    def description(app: App) -> str:
        manifest = app.manifest()
        spec_validator("manifest-response").validate(manifest)
        flag = manifest["commands"]["render"]["flags"]["output"]
        assert flag["pattern_type"] == "filepath"
        return str(flag["description"])

    assert description(make_app(True, project_root=(".cloud",))) == TEXT
    assert description(make_app(OutputBase.CWD)) == TEXT
    assert description(make_app(OutputBase.PROJECT_ROOT, project_root=(".cloud",))).endswith(
        "a relative path lands in the project root"
    )
    assert description(make_app(Project)).endswith("lands in the Project directory")
    assert description(make_app(inventory_dir)).endswith("lands in the inventory_dir directory")


class NotAResource:
    directory = Path(".")


@dataclass(frozen=True, slots=True)
class NoDirectory:
    @classmethod
    def acquire(cls, args: object, ctx: Ctx) -> Self:
        return cls()


def no_ctx(project: Project) -> Path:
    return project.directory


def bad_dep(ctx: Ctx, other: NotAResource) -> Path:
    return Path(".")


@pytest.mark.parametrize(
    ("value", "roots", "match"),
    [
        (OutputBase.PROJECT_ROOT, (), "declares no project_root"),
        (NotAResource, (), "NotAResource is not a resource"),
        (NoDirectory, (), "declares none; add directory"),
        (no_ctx, (), r"must take \(ctx: Ctx, \*resources\)"),
        (bad_dep, (), "NotAResource is not a resource"),
        ("project_root", (".cloud",), "output_file takes True"),
        (lambda ctx: Path("."), (), r"must take \(ctx: Ctx"),
    ],
)
def test_a_bad_base_is_a_registration_error_naming_the_command(
    value: object, roots: tuple[str, ...], match: str
) -> None:
    with pytest.raises(RegistrationError, match=match) as raised:
        make_app(value, project_root=roots)
    assert str(raised.value).startswith("render: ")


# fs-side-effects: a write to a path from a Path flag is the command's output


@dataclass(frozen=True, slots=True)
class ExportArgs:
    output: Path | None = Flag(default=None, description="Where the inventory goes")


def export_by_hand(args: ExportArgs, ctx: Ctx) -> dict[str, str]:
    if args.output is not None:
        target = ctx.cwd / args.output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    return {}


def audit_app(handler: Callable[..., object]) -> App:
    app = App("writer", version="1.0.0")
    app.command("x", description="x", danger_level="safe", exit_codes=())(handler)
    return app


def test_fs_side_effects_suggests_output_file_for_a_path_flag_write() -> None:
    [rule] = [r for r in audit(audit_app(export_by_hand), "w", limit=3).rules
              if r.id == "fs-side-effects"]  # fmt: skip
    [finding] = rule.findings
    assert "--output" in finding.message and finding.fix.startswith("output_file=True")
    assert "SideEffect" not in finding.fix


def test_fs_side_effects_keeps_the_cache_advice_for_an_untraced_write(tmp_path: Path) -> None:
    def cache(args: ExportArgs, ctx: Ctx) -> dict[str, str]:
        (tmp_path / "cache.json").write_text(str(args.output))
        return {}

    [rule] = [r for r in audit(audit_app(cache), "w", limit=3).rules
              if r.id == "fs-side-effects"]  # fmt: skip
    [finding] = rule.findings
    assert "treaty.SideEffect" in finding.fix and "output_file" not in finding.fix


def test_a_replayed_result_with_an_unresolved_base_writes_no_file(
    project: Path, tmp_path: Path
) -> None:
    app = App("cloud", version="1.0.0", state_dir=tmp_path / "state")

    @app.command("make", description="Make", danger_level="mutating", exit_codes=(),
                 output_file=Project)  # fmt: skip
    def make(args: ProjectArgs, ctx: Ctx) -> dict[str, str]:
        return {"effect": "created"}

    argv = ["make", "--project", str(project), "--idempotency-key", "k1", "--output", "m.json"]
    code, first = run(app, argv)
    assert code == 0 and first["data"]["path"] == str(project / "m.json")
    (project / "m.json").unlink()
    # The replay runs no handler, so no resource says where a relative --output lands
    code, again = run(app, argv)
    assert code == 1 and again["error"]["code"] == "OUTPUT_UNWRITABLE"
    assert "absolute --output" in again["error"]["message"]
    assert not (project / "m.json").exists()
    code, again = run(app, [*argv[:-1], str(project / "m.json")])
    assert code == 0 and (project / "m.json").exists()


# #10 with #68: raw bytes from a treaty.Binary land in the declared base too

PNG = b"\x89PNG\r\n\x1a\n\x00\x01binary\xff"


def snapshot(args: ProjectArgs, ctx: Ctx) -> Binary:
    if args.fail:
        raise CliExit(ExitCodeName("NOT_FOUND"), "nothing to snapshot")
    return Binary(PNG, content_type="image/png")


def binary_app(output_file: object, project_root: tuple[str, ...] = ()) -> App:
    app = App("cloud", version="1.0.0")
    app.command(
        "snapshot",
        description="Snapshot",
        danger_level="safe",
        exit_codes=("NOT_FOUND",),
        output_file=output_file,
        project_root=project_root,
    )(snapshot)
    return app


@pytest.mark.parametrize(
    ("base", "landed"),
    [
        (OutputBase.PROJECT_ROOT, "snap.png"),
        (Project, "snap.png"),
        (inventory_dir, "inventory/snap.png"),
    ],
)
def test_binary_raw_bytes_land_in_the_declared_base(
    project: Path, base: object, landed: str
) -> None:
    app = binary_app(base, project_root=(".cloud",))
    argv = ["snapshot", "--cwd", str(project / "sub"), "--project", str(project)]
    code, envelope = run(app, [*argv, "--format", "plain", "--output", "snap.png"])
    target = project / landed
    assert code == 0 and target.read_bytes() == PNG
    assert envelope["data"] == {
        "path": str(target),
        "bytes": len(PNG),
        "content_type": "image/png",
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    assert not (project / "sub" / "snap.png").exists()


def test_binary_raw_bytes_stay_cwd_relative_with_output_file_true(project: Path) -> None:
    cwd = project / "sub"
    code, envelope = run(binary_app(True), ["snapshot", "--cwd", str(cwd), "--output", "s.png"])
    assert code == 0 and (cwd / "s.png").read_bytes() == PNG
    assert envelope["data"]["path"] == str(cwd / "s.png")


def test_binary_in_a_base_writes_no_file_on_failure_or_a_missing_parent(project: Path) -> None:
    argv = ["snapshot", "--project", str(project)]
    code, envelope = run(binary_app(Project), [*argv, "--fail", "--output", "s.png"])
    assert code != 0 and envelope["error"]["code"] == "NOT_FOUND"
    assert not (project / "s.png").exists()
    code, envelope = run(binary_app(Project), [*argv, "--output", "missing/s.png"])
    assert code == 1 and envelope["error"]["code"] == "OUTPUT_UNWRITABLE"
    assert envelope["data"]["value"] and not (project / "missing").exists()


def test_binary_with_a_base_names_both_in_the_description_and_marks_the_manifest() -> None:
    def entry(app: App, path: str) -> dict[str, Any]:
        manifest = app.manifest()
        spec_validator("manifest-response").validate(manifest)
        return dict(manifest["commands"][path])

    plain = entry(binary_app(True), "snapshot")
    assert plain["output_file"] == "binary"
    assert "sha256" in plain["flags"]["output"]["description"]
    assert "lands in" not in plain["flags"]["output"]["description"]
    for base, where in [
        (OutputBase.PROJECT_ROOT, "the project root"),
        (Project, "the Project directory"),
        (inventory_dir, "the inventory_dir directory"),
    ]:
        binary = entry(binary_app(base, project_root=(".cloud",)), "snapshot")
        description = binary["flags"]["output"]["description"]
        assert binary["output_file"] == "binary"
        assert description.startswith("Write the returned bytes to this file as they are")
        assert "sha256" in description and description.endswith(f"lands in {where}")
        formatted = entry(make_app(base, project_root=(".cloud",)), "render")
        assert formatted["output_file"] == "formatted"
        assert formatted["flags"]["output"]["description"].startswith(TEXT)
