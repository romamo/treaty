"""Defects the 1.0 final review found in the workstreams after the Phase A review"""

import io
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_permissions
from fixture_session_app import app as session_app

from treaty import App, Ctx, NoArgs, RegistrationError, SideEffect


@dataclass(frozen=True, slots=True)
class Keyed:
    region: str = "eu"
    api_token: str = ""

    def __post_init__(self) -> None:
        if self.api_token and not self.api_token.startswith("tk-"):
            raise ValueError(f"api_token {self.api_token!r} lacks the tk- prefix")


def keyed_app() -> App:
    app = App("my-tool", version="1.0.0", description="Rows", settings=Keyed)

    @app.command("show", description="Show the region", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Keyed) -> dict[str, str]:
        return {"region": settings.region}

    return app


def run(app: App, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env, isatty=False)
    return code, out.getvalue(), err.getvalue()


def test_a_settings_invalid_message_never_echoes_a_secret_setting_value() -> None:
    code, out, err = run(keyed_app(), ["show"], {"MY_TOOL_API_TOKEN": "hunter2-secret"})
    assert code == 2 and json.loads(out)["error"]["code"] == "CONFIG_INVALID"
    assert "hunter2-secret" not in out + err
    assert "[REDACTED]" in json.loads(out)["error"]["message"]


def test_the_effective_config_hash_does_not_cover_secret_setting_values() -> None:
    def hash_of(env: dict[str, str]) -> str:
        code, out, _ = run(keyed_app(), ["show"], env)
        assert code == 0, out
        return str(json.loads(out)["meta"]["effective_config_hash"])

    assert hash_of({"MY_TOOL_API_TOKEN": "tk-1"}) == hash_of({"MY_TOOL_API_TOKEN": "tk-2"})
    assert hash_of({"MY_TOOL_REGION": "us"}) != hash_of({})


# F-032, F-043: the session temp root and its pruning


def session_run(argv: list[str], env: dict[str, str], stdin: str = "") -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = session_app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        env={"PATH": os.environ["PATH"], **env},
        isatty=False,
    )
    return code, out.getvalue()


def aged(path: Path) -> Path:
    old = time.time() - 3 * 86_400
    os.utime(path, (old, old))
    return path


@needs_posix_permissions
def test_pruning_never_follows_a_symlinked_session_root(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "old").mkdir(parents=True)
    aged(victim / "old")
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / f"sessionctl-{os.getuid()}").symlink_to(victim)
    session_run(["pwd"], {"TMPDIR": str(temp)})
    assert (victim / "old").is_dir()


@needs_posix_permissions
def test_a_stale_session_that_cannot_be_removed_does_not_crash_the_next_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / f"sessionctl-{os.getuid()}"
    locked = root / "0123abcd" / "locked"
    locked.mkdir(parents=True, mode=0o700)
    (locked / "file").write_text("x")
    locked.chmod(0o500)
    aged(root / "0123abcd")
    try:
        code, out = session_run(["pwd"], {"TMPDIR": str(tmp_path)})
    finally:
        locked.chmod(0o700)
    assert code == 0, out


def test_a_relative_tmpdir_gives_an_absolute_session_directory(tmp_path: Path) -> None:
    code, out = session_run(["scratch"], {"TMPDIR": os.path.relpath(tmp_path)})
    assert code == 0, out
    assert Path(json.loads(out)["meta"]["session_tmp_dir"]).is_absolute()


def test_two_exec_lines_each_get_their_own_output_file(tmp_path: Path) -> None:
    line = json.dumps({"_cmd": "report"}) + "\n"
    code, out = session_run(["exec"], {"TMPDIR": str(tmp_path)}, stdin=line * 2)
    envelopes = [json.loads(x) for x in out.splitlines()]
    assert code == 0 and all(e["ok"] for e in envelopes), out
    assert len({e["data"]["output_file"] for e in envelopes}) == 2


# C-011, O-027: cleanup removes only what the declarations cover


def temp_app(pattern: str) -> App:
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[SideEffect(pattern, "temp")],
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def cleanup(app: App, tmp_path: Path) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        ["cleanup", "--confirm-destructive", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path / "t")},
    )
    return code, json.loads(out.getvalue()), err.getvalue()


@needs_posix_permissions
def test_cleanup_never_removes_a_match_reached_through_a_symlinked_segment(
    tmp_path: Path,
) -> None:
    base, victim = tmp_path / "base", tmp_path / "victim"
    (victim / "cache").mkdir(parents=True)
    (base / "tool-real" / "cache").mkdir(parents=True)
    (base / "tool-evil").symlink_to(victim)
    code, envelope, _ = cleanup(temp_app(f"{base}/tool-{{s}}/cache"), tmp_path)
    assert code == 0, envelope
    assert (victim / "cache").is_dir()
    assert not (base / "tool-real" / "cache").exists()
    assert [c["path"] for c in envelope["data"]["cleaned"]] == [str(base / "tool-real" / "cache")]


@needs_posix_permissions
def test_cleanup_reports_a_path_it_cannot_remove_and_removes_the_rest(tmp_path: Path) -> None:
    base = tmp_path / "base"
    locked = base / "tool-a" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    (base / "tool-b").mkdir()
    locked.chmod(0o500)
    try:
        code, envelope, err = cleanup(temp_app(f"{base}/tool-{{s}}"), tmp_path)
    finally:
        locked.chmod(0o700)
    assert code == 0 and "Traceback" not in err, envelope
    assert not (base / "tool-b").exists()
    assert envelope["data"]["failed"] == [str(base / "tool-a")]
    assert [w["code"] for w in envelope["warnings"]] == ["CLEANUP_INCOMPLETE"]


@pytest.mark.parametrize("pattern", ["/", "~/", "~/{x}", "/*", "/tmp/../etc", "~/./x"])
def test_a_side_effect_that_would_cover_a_whole_root_or_escape_is_refused(pattern: str) -> None:
    with pytest.raises(RegistrationError):
        SideEffect(pattern, "temp")
