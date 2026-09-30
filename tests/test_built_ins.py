"""Built-in commands: REQ-O-026, REQ-O-027, REQ-O-028, REQ-O-029, REQ-O-034, REQ-O-035,
REQ-O-041, and the ``status --show-side-effects`` half of REQ-C-011."""

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import needs_posix_permissions, spec_validator
from test_network_and_fs import Origin, Proxy, serving

from examples.deployctl import app as deployctl
from treaty import App, Check, Ctx, Dependency, NoArgs, RegistrationError, SideEffect, endpoint
from treaty._audit import audit
from treaty._changelog import diff
from treaty._deps import CheckFn
from treaty._tools import tool_list


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": os.environ["PATH"], **(env or {})},
    )
    return code, json.loads(out.getvalue())


def data_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def plain_app() -> App:
    app = App("plain", version="1.2.0", description="A plain tool")

    @app.command("hello", description="Say hello", danger_level="safe", exit_codes=())
    def hello(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"greeting": "hello"}

    return app


# REQ-O-041


def test_tool_manifest_etag_hash_returns_meta_not_modified_true_when_unchanged() -> None:
    app = plain_app()
    _, envelope = run(app, ["manifest"])
    etag = data_of(envelope)["etag"]
    assert isinstance(etag, str)
    code, envelope = run(app, ["manifest", "--etag", etag])
    assert code == 0 and envelope["ok"] is True and envelope["data"] is None
    meta = envelope["meta"]
    assert isinstance(meta, dict) and meta["not_modified"] is True
    spec_validator("response-envelope").validate(envelope)


def test_a_stale_etag_returns_the_full_manifest() -> None:
    app = plain_app()
    code, envelope = run(app, ["manifest", "--etag", "sha256:" + "0" * 32])
    assert code == 0 and "commands" in data_of(envelope)
    meta = envelope["meta"]
    assert isinstance(meta, dict) and "not_modified" not in meta


def test_a_malformed_etag_exits_2() -> None:
    code, envelope = run(plain_app(), ["manifest", "--etag", "abc"])
    assert code == 2
    error = envelope["error"]
    assert isinstance(error, dict) and error["phase"] == "validation"


def test_not_modified_reaches_app_call_and_exec() -> None:
    app = plain_app()
    etag = app.manifest()["etag"]
    called = app.call("manifest", {"etag": etag}).to_json()
    assert called["data"] is None and called["meta"]["not_modified"] is True
    out = io.StringIO()
    line = json.dumps({"_cmd": "manifest", "etag": etag})
    app.run(["exec"], stdin=io.StringIO(line + "\n"), stdout=out, stderr=io.StringIO(), env={})
    answered = json.loads(out.getvalue().splitlines()[0])
    assert answered["data"] is None and answered["meta"]["not_modified"] is True


# REQ-O-026


def doctor_app(*checks: CheckFn, tools: dict[str, str] | None = None) -> App:
    app = App("doc", version="1.0.0", checks=checks)

    @app.command(
        "build",
        description="Build",
        danger_level="safe",
        exit_codes=(),
        required_tools=tools or {},
    )
    def build(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def disk_ok(ctx: Ctx) -> Check:
    return Check("disk", True, version="42")


def api_key_set(ctx: Ctx) -> Check:
    ok = "DOC_API_KEY" in ctx.env
    return Check("api-key", ok, fix="export DOC_API_KEY=<key>", error=None if ok else "unset")


def test_tool_doctor_format_json_returns_a_structured_json_object_with_a_checks_array(
    tmp_path: Path,
) -> None:
    code, envelope = run(doctor_app(disk_ok), ["doctor"], env={"HOME": str(tmp_path)})
    assert code == 0
    checks = data_of(envelope)["checks"]
    assert isinstance(checks, list)
    assert [c["name"] for c in checks] == ["config-dir", "disk", "state-dir"]
    for check in checks:
        assert {"name", "ok", "version", "required"} <= set(check)
    assert checks[1] == {"name": "disk", "ok": True, "version": "42", "required": None}


def test_each_failed_check_includes_a_fix_field_with_an_executable_shell_command(
    tmp_path: Path,
) -> None:
    code, envelope = run(doctor_app(api_key_set), ["doctor"], env={"HOME": str(tmp_path)})
    assert code == 4
    failed = [c for c in data_of(envelope)["checks"] if not c["ok"]]  # type: ignore[union-attr]
    assert failed == [
        {
            "name": "api-key",
            "ok": False,
            "version": None,
            "required": None,
            "error": "unset",
            "fix": "export DOC_API_KEY=<key>",
        }
    ]


@needs_posix_permissions
def test_a_framework_check_failure_is_resolved_by_running_its_fix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    try:
        code, envelope = run(doctor_app(), ["doctor"], env={"HOME": str(home)})
        assert code == 4
        failed = [c for c in data_of(envelope)["checks"] if not c["ok"]]  # type: ignore[union-attr]
        assert {c["name"] for c in failed} == {"config-dir", "state-dir"}
        for check in failed:
            subprocess.run(check["fix"], shell=True, check=True)  # noqa: S602 - the point
        code, _ = run(doctor_app(), ["doctor"], env={"HOME": str(home)})
        assert code == 0
    finally:
        home.chmod(0o700)


def test_a_missing_required_dependency_appears_as_a_failed_check_with_ok_false() -> None:
    app = doctor_app(tools={"no-such-tool-xyz": "1.0"})
    code, envelope = run(app, ["doctor"])
    assert code == 4
    (check,) = data_of(envelope)["checks"]  # type: ignore[misc]
    assert check["name"] == "no-such-tool-xyz" and check["ok"] is False
    assert check["version"] is None and check["fix"]


def test_a_missing_declared_dependency_is_a_failed_check_too() -> None:
    gone = Dependency("gone", ("treaty-no-such-tool", "--version"), "1.0", "brew install gone")
    app = App("deps", version="1.0.0", dependencies=[gone])
    code, envelope = run(app, ["doctor"])
    assert code == 4
    (check,) = data_of(envelope)["checks"]  # type: ignore[misc]
    assert check["name"] == "gone" and check["ok"] is False
    assert check["fix"] == "brew install gone" and check["version"] is None


def test_tool_doctor_exit_code_is_0_iff_all_checks_pass_otherwise_4_with_the_report(
    tmp_path: Path,
) -> None:
    env = {"HOME": str(tmp_path)}
    assert run(doctor_app(api_key_set), ["doctor"], env={**env, "DOC_API_KEY": "k"})[0] == 0
    code, envelope = run(doctor_app(api_key_set), ["doctor"], env=env)
    assert code == 4
    error = envelope["error"]
    assert isinstance(error, dict) and error["code"] == "DOCTOR_CHECKS_FAILED"
    assert "checks" in data_of(envelope)


def test_a_failing_check_without_a_fix_is_invalid_output() -> None:
    def unfixed(ctx: Ctx) -> Check:
        return Check("thing", False)

    code, envelope = run(doctor_app(unfixed), ["doctor"])
    error = envelope["error"]
    assert code == 1 and isinstance(error, dict) and error["code"] == "INVALID_OUTPUT"


def test_an_endpoint_check_passes_when_the_url_answers() -> None:
    with serving(Origin) as server:
        code, envelope = run(doctor_app(endpoint(server.url, fix="true")), ["doctor"])
    assert code == 0
    assert data_of(envelope)["checks"] == [
        {"name": "127.0.0.1", "ok": True, "version": None, "required": None}
    ]


def test_doctor_tests_network_connectivity_through_the_proxy_settings() -> None:
    """REQ-F-036 applies: the check goes through HTTP_PROXY, and a failure says so (F-037)"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    with serving(Proxy) as proxy:
        app = doctor_app(endpoint("http://api.invalid/health", fix="check the proxy"))
        code, _ = run(app, ["doctor"], env={"HTTP_PROXY": proxy.url})
        assert code == 0 and proxy.seen[0][1] == "http://api.invalid/health"
    code, envelope = run(app, ["doctor"], env={"HTTP_PROXY": dead})
    assert code == 4
    (check,) = data_of(envelope)["checks"]  # type: ignore[misc]
    assert check["fix"] == "check the proxy" and "failed" in check["error"]
    assert check["network_context"]["proxy_used"] == dead
    assert check["network_context"]["proxy_source"] == "HTTP_PROXY"


def test_doctor_fix_rule_flags_a_check_that_can_fail_without_a_fix() -> None:
    def unfixed(ctx: Ctx) -> Check:
        return Check("thing", "X" in ctx.env)

    rule = {r.id: r for r in audit(doctor_app(unfixed, api_key_set), "m:app", limit=9).rules}
    (finding,) = rule["doctor-fix"].findings
    assert "unfixed" in finding.message and "fix=" in finding.fix


# REQ-O-027, REQ-O-028, REQ-C-011


def effects_app(root: Path) -> App:
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[
            SideEffect(f"{root}/cache/", "cache", clearable_with="fx cleanup --scope cache"),
            SideEffect(f"{root}/tmp/fetch-{{session}}/", "temp"),
            SideEffect(f"{root}/logs/{{date}}.log", "log"),
            SideEffect(f"{root}/token.json", "credential"),
        ],
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def populate(root: Path) -> None:
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "a.json").write_text("x" * 100)
    for session in ("1", "2"):
        (root / "tmp" / f"fetch-{session}").mkdir(parents=True)
        (root / "tmp" / f"fetch-{session}" / "part").write_text("y" * 10)
    (root / "logs").mkdir()
    (root / "logs" / "today.log").write_text("z" * 7)
    (root / "token.json").write_text("{}")


def age(path: Path, seconds: float) -> None:
    """Set the mtime of ``path`` and everything under it ``seconds`` in the past"""
    when = time.time() - seconds
    for item in [path, *path.rglob("*")]:
        os.utime(item, (when, when))


def cleaned_paths(envelope: dict[str, object]) -> list[str]:
    cleaned = data_of(envelope)["cleaned"]
    assert isinstance(cleaned, list)
    return [c["path"] for c in cleaned]


def test_tool_cleanup_scope_temp_removes_all_paths_declared_as_type_temp(tmp_path: Path) -> None:
    populate(tmp_path)
    code, envelope = run(
        effects_app(tmp_path), ["cleanup", "--scope", "temp", "--confirm-destructive"]
    )
    assert code == 0
    assert cleaned_paths(envelope) == [
        str(tmp_path / "tmp" / "fetch-1"),
        str(tmp_path / "tmp" / "fetch-2"),
    ]
    assert not (tmp_path / "tmp" / "fetch-1").exists()
    assert (tmp_path / "cache").exists() and (tmp_path / "logs" / "today.log").exists()


def test_tool_cleanup_format_json_returns_a_list_of_removed_paths_and_total_bytes_freed(
    tmp_path: Path,
) -> None:
    populate(tmp_path)
    code, envelope = run(effects_app(tmp_path), ["cleanup", "--confirm-destructive"])
    assert code == 0
    data = data_of(envelope)
    assert data["cleaned"] == [
        {"path": str(tmp_path / "cache"), "type": "cache", "bytes_freed": 100},
        {"path": str(tmp_path / "logs" / "today.log"), "type": "log", "bytes_freed": 7},
        {"path": str(tmp_path / "tmp" / "fetch-1"), "type": "temp", "bytes_freed": 10},
        {"path": str(tmp_path / "tmp" / "fetch-2"), "type": "temp", "bytes_freed": 10},
    ]
    assert data["total_bytes_freed"] == 127
    assert (tmp_path / "token.json").exists()  # a credential is never cleaned


def test_tool_cleanup_min_age_3600_does_not_remove_any_file_or_directory_created_in_the_last_hour(
    tmp_path: Path,
) -> None:
    populate(tmp_path)
    for path in (tmp_path / "cache", tmp_path / "logs", tmp_path / "tmp"):
        age(path, 7200)
    (tmp_path / "tmp" / "fetch-2" / "fresh").write_text("new")  # a new file in an old directory
    code, envelope = run(
        effects_app(tmp_path), ["cleanup", "--min-age", "3600", "--confirm-destructive"]
    )
    assert code == 0
    assert str(tmp_path / "tmp" / "fetch-2") not in cleaned_paths(envelope)
    assert data_of(envelope)["skipped"] == [str(tmp_path / "tmp" / "fetch-2")]
    assert (tmp_path / "tmp" / "fetch-2" / "fresh").exists()
    assert not (tmp_path / "tmp" / "fetch-1").exists()


def test_tool_cleanup_scope_cache_does_not_affect_logs_or_temp_files(tmp_path: Path) -> None:
    populate(tmp_path)
    code, envelope = run(
        effects_app(tmp_path), ["cleanup", "--scope", "cache", "--confirm-destructive"]
    )
    assert code == 0 and cleaned_paths(envelope) == [str(tmp_path / "cache")]
    assert (tmp_path / "logs" / "today.log").exists()
    assert (tmp_path / "tmp" / "fetch-1" / "part").exists()


def test_cleanup_refuses_a_negative_min_age_and_an_unknown_scope(tmp_path: Path) -> None:
    assert run(effects_app(tmp_path), ["cleanup", "--min-age", "-1"])[0] == 2
    assert run(effects_app(tmp_path), ["cleanup", "--scope", "config"])[0] == 2


def test_tool_status_show_side_effects_returns_paths_types_and_sizes_for_all_declared_side_effects(
    tmp_path: Path,
) -> None:
    populate(tmp_path)
    code, envelope = run(effects_app(tmp_path), ["status", "--show-side-effects"])
    assert code == 0
    data = data_of(envelope)
    assert set(data) == {"side_effects"}
    effects = data["side_effects"]
    assert isinstance(effects, list)
    assert [(e["type"], e["bytes"]) for e in effects] == [
        ("cache", 100),
        ("temp", 20),
        ("log", 7),
        ("credential", 2),
    ]
    assert effects[1]["paths"] == [
        {"path": str((tmp_path / "tmp" / "fetch-1").resolve()), "bytes": 10},
        {"path": str((tmp_path / "tmp" / "fetch-2").resolve()), "bytes": 10},
    ]
    assert effects[0]["clearable_with"] == "fx cleanup --scope cache"


def test_tool_status_show_side_effects_lists_all_paths_declared_by_registered_commands(
    tmp_path: Path,
) -> None:
    code, envelope = run(effects_app(tmp_path), ["status", "--show-side-effects"])
    effects = data_of(envelope)["side_effects"]
    assert code == 0 and isinstance(effects, list)
    declared = {
        e["path"]
        for e in effects_app(tmp_path).manifest()["commands"]["fetch"][  # type: ignore[index]
            "filesystem_side_effects"
        ]
    }
    listed = {e["pattern"] for e in effects}
    assert len(listed) == len(declared) == 4
    assert all(e["command"] == "fetch" for e in effects)


def test_tool_status_show_state_files_returns_paths_and_summaries_of_all_global_state_files(
    tmp_path: Path,
) -> None:
    populate(tmp_path)
    home = tmp_path / "home"
    (home / ".config" / "fx").mkdir(parents=True)
    (home / ".config" / "fx" / "config.toml").write_bytes(b"region = 'eu'\n")  # 14 bytes
    code, envelope = run(
        effects_app(tmp_path),
        ["status", "--show-state-files"],
        {"HOME": str(home), "FX_AUDIT_LOG": "1"},  # the audit log is listed while it is on
    )
    assert code == 0
    files = {f["purpose"]: f for f in data_of(envelope)["state_files"]}  # type: ignore[union-attr]
    assert set(files) == {
        "project config",
        "user config",
        "idempotency records",
        "audit log",
        "credential of fetch",
    }
    assert files["user config"] == {
        "path": str((home / ".config" / "fx" / "config.toml").resolve()),
        "purpose": "user config",
        "exists": True,
        "bytes": 14,
    }
    assert files["credential of fetch"]["exists"] is True
    assert "eu" not in json.dumps(envelope)  # values are never echoed


def test_the_command_exits_0_and_produces_valid_json_regardless_of_what_state_exists(
    tmp_path: Path,
) -> None:
    app = effects_app(tmp_path / "nothing-here")
    for env in ({}, {"HOME": str(tmp_path / "missing-home")}):
        code, envelope = run(app, ["status"], env)
        assert code == 0
        spec_validator("response-envelope").validate(envelope)
        assert set(data_of(envelope)) == {"side_effects", "state_files"}
    populate(tmp_path / "nothing-here")
    assert run(app, ["status"], {"HOME": str(tmp_path)})[0] == 0


def test_all_path_values_in_the_output_are_absolute(tmp_path: Path) -> None:
    populate(tmp_path)
    _, envelope = run(effects_app(tmp_path), ["status"], {"HOME": str(tmp_path)})
    data = data_of(envelope)
    paths = [f["path"] for f in data["state_files"]]  # type: ignore[union-attr]
    for effect in data["side_effects"]:  # type: ignore[union-attr]
        paths += [effect["pattern"], *(p["path"] for p in effect["paths"])]
    assert paths and all(Path(p).is_absolute() for p in paths)


def test_status_show_config_is_the_show_config_flag(tmp_path: Path) -> None:
    code, envelope = run(effects_app(tmp_path), ["status", "--show-config"])
    assert code == 0 and set(data_of(envelope)) >= {"effective_config", "sources"}


def test_status_is_safe_and_reports_logged_in_with_credentials(tmp_path: Path) -> None:
    class Held:
        def active_scopes(self, ctx: Ctx) -> list[str] | None:
            return ["read"] if "TOKEN" in ctx.env else None

    app = App("auth", version="1.0.0", credentials=Held())
    assert app.manifest()["commands"]["status"]["danger_level"] == "safe"  # type: ignore[index]
    assert data_of(run(app, ["status"], {"TOKEN": "t"})[1])["logged_in"] is True
    assert data_of(run(app, ["status"])[1])["logged_in"] is False


# REQ-O-029

CHANGELOG = [
    {
        "version": "1.0.0",
        "date": "2026-01-10",
        "breaking": False,
        "added": ["deploy"],
        "removed": [],
        "changed": [],
        "etag": "sha256:" + "1" * 32,
    },
    {
        "version": "2.0.0",
        "date": "2026-03-01",
        "breaking": True,
        "added": ["deploy.output.deployed_url"],
        "removed": ["deploy.output.url"],
        "changed": [],
        "etag": "sha256:" + "2" * 32,
    },
    {
        "version": "1.1.0",
        "date": "2026-02-01",
        "breaking": False,
        "added": ["deploy.flags.region"],
        "removed": [],
        "changed": [],
        "etag": "sha256:" + "3" * 32,
    },
]


def changelog_app(path: Path) -> App:
    return App("chg", version="2.0.0", schema_changelog=path)


def test_tool_changelog_format_json_returns_a_valid_json_array_of_version_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-changelog.json"
    path.write_text(json.dumps(CHANGELOG))
    code, envelope = run(changelog_app(path), ["changelog"])
    assert code == 0
    entries = data_of(envelope)["entries"]
    assert isinstance(entries, list)
    assert [e["version"] for e in entries] == ["2.0.0", "1.1.0", "1.0.0"]  # newest first


def test_each_entry_includes_version_date_breaking_added_removed_changed(tmp_path: Path) -> None:
    path = tmp_path / "schema-changelog.json"
    path.write_text(json.dumps(CHANGELOG))
    _, envelope = run(changelog_app(path), ["changelog"])
    for entry in data_of(envelope)["entries"]:  # type: ignore[union-attr]
        assert {"version", "date", "breaking", "added", "removed", "changed"} <= set(entry)


def test_since_1_0_0_returns_only_entries_for_versions_after_1_0_0(tmp_path: Path) -> None:
    path = tmp_path / "schema-changelog.json"
    path.write_text(json.dumps(CHANGELOG))
    _, envelope = run(changelog_app(path), ["changelog", "--since", "1.0.0"])
    assert [e["version"] for e in data_of(envelope)["entries"]] == ["2.0.0", "1.1.0"]  # type: ignore[union-attr]
    assert run(changelog_app(path), ["changelog", "--since", "one"])[0] == 2


def test_a_malformed_schema_changelog_is_a_registration_error(tmp_path: Path) -> None:
    path = tmp_path / "schema-changelog.json"
    for bad in ({"entries": []}, [{"version": "1.0"}], [{**CHANGELOG[0], "date": "soon"}]):
        path.write_text(json.dumps(bad))
        with pytest.raises(RegistrationError, match="schema_changelog"):
            changelog_app(path)
    assert "changelog" not in App("none", version="1.0.0").manifest()["commands"]  # type: ignore[operator]


def deploy_manifest(**flags: dict[str, object]) -> dict[str, object]:
    return {
        "commands": {
            "deploy": {
                "flags": flags,
                "exit_codes": {"10": {"name": "TIMEOUT"}},
                "output_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
            }
        }
    }


def test_breaking_changes_are_correctly_flagged_as_breaking_true() -> None:
    base = deploy_manifest(target={"type": "string", "required": False})
    optional = deploy_manifest(
        target={"type": "string", "required": False}, region={"type": "string"}
    )
    change = diff(base, optional)
    assert change.added == ("deploy.flags.region",) and change.breaking is False
    required = deploy_manifest(
        target={"type": "string", "required": False},
        region={"type": "string", "required": True},
    )
    assert diff(base, required).breaking is True
    retyped = deploy_manifest(target={"type": "integer", "required": False})
    change = diff(base, retyped)
    assert change.changed == ("deploy.flags.target",) and change.breaking is True
    removed = deploy_manifest()
    change = diff(base, removed)
    assert change.removed == ("deploy.flags.target",) and change.breaking is True
    assert diff(None, base).added == (
        "deploy",
        "deploy.exit_codes.10",
        "deploy.flags.target",
        "deploy.output.url",
    )


CHG_MODULE = """
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Ctx, Flag

app = App("chg", version="{version}", schema_changelog=Path(__file__).parent / "chg.json")


@dataclass(frozen=True, slots=True)
class Args:
{fields}


@app.command("deploy", description="Deploy", danger_level="safe", exit_codes=())
def deploy(args: Args, ctx: Ctx) -> dict[str, str]:
    return {{}}
"""


def treaty_cli(cwd: Path, *argv: str) -> tuple[int, dict[str, object]]:
    done = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()", *argv, "--format", "json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, json.loads(done.stdout)


def test_changelog_add_records_the_manifest_diff_and_the_changelog_serves_it(
    tmp_path: Path,
) -> None:
    module = tmp_path / "chgmod.py"
    target = '    target: str | None = Flag(default=None, description="Where")'
    module.write_text(CHG_MODULE.format(version="1.0.0", fields=target))
    code, envelope = treaty_cli(tmp_path, "audit", "chgmod:app", "--all")
    findings = [f["rule"] for f in data_of(envelope)["next_steps"]]  # type: ignore[union-attr]
    assert "schema-changelog" in findings
    code, envelope = treaty_cli(tmp_path, "changelog-add", "chgmod:app")
    assert code == 0 and data_of(envelope)["effect"] == "created"
    assert (tmp_path / "chg.manifest.json").is_file()
    code, envelope = treaty_cli(tmp_path, "changelog-add", "chgmod:app")
    assert code == 0 and data_of(envelope)["effect"] == "noop"
    code, envelope = treaty_cli(tmp_path, "audit", "chgmod:app", "--all")
    findings = [f["rule"] for f in data_of(envelope)["next_steps"]]  # type: ignore[union-attr]
    assert "schema-changelog" not in findings

    module.write_text(CHG_MODULE.format(version="2.0.0", fields="    pass"))
    code, envelope = treaty_cli(tmp_path, "changelog-add", "chgmod:app")
    entry = data_of(envelope)["entry"]
    assert code == 0 and isinstance(entry, dict)
    assert entry["version"] == "2.0.0" and entry["breaking"] is True
    assert entry["removed"] == ["deploy.flags.target"]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "from chgmod import app; app.main()",
            "changelog",
            "--since",
            "1.0.0",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    entries = json.loads(done.stdout)["data"]["entries"]
    assert [e["version"] for e in entries] == ["2.0.0"]


# REQ-O-034

SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def frontmatter(text: str) -> dict[str, object]:
    """Every value is JSON, which is valid YAML: parse it without a YAML library"""
    assert text.startswith("---\n")
    block, sep, _ = text[4:].partition("\n---\n")
    assert sep
    fields: dict[str, object] = {}
    for line in block.splitlines():
        key, colon, value = line.partition(": ")
        assert colon and re.fullmatch(r"[a-z_]+", key)
        fields[key] = json.loads(value)
    return fields


def usage(text: str) -> list[str]:
    block = text.partition("## Usage\n\n```bash\n")[2].partition("```")[0]
    return block.splitlines()


def generated(tmp_path: Path, app: App) -> dict[str, str]:
    code, envelope = run(app, ["generate-skills", "--output-dir", str(tmp_path / "skills")])
    assert code == 0 and data_of(envelope)["effect"] == "created"
    return {p.name: p.read_text() for p in (tmp_path / "skills").iterdir()}


def test_tool_generate_skills_output_dir_skills_creates_context_md_and_one_skill_md_per_command(
    tmp_path: Path,
) -> None:
    app = deployctl
    files = generated(tmp_path, app)
    user = sorted(p.value for p in app.commands if p not in app.builtins)
    assert sorted(files) == sorted(
        ["CONTEXT.md", *(f"SKILL-{p.replace('.', '-')}.md" for p in user)]
    )
    _, envelope = run(app, ["generate-skills", "--output-dir", str(tmp_path / "skills")])
    skills = data_of(envelope)["skills"]
    assert data_of(envelope)["effect"] == "noop" and isinstance(skills, list)
    assert {s["type"] for s in skills} == {"context", "skill"}
    assert all(Path(s["path"]).is_absolute() for s in skills)


def test_each_skill_file_includes_a_yaml_frontmatter_block_with_name_description_and_args(
    tmp_path: Path,
) -> None:
    app = deployctl
    for name, text in generated(tmp_path, app).items():
        if name == "CONTEXT.md":
            continue
        front = frontmatter(text)
        assert set(front) == {"name", "description", "version", "command", "args"}
        assert front["version"] == app.version
        args = front["args"]
        assert isinstance(args, dict) and args["type"] == "object"


def test_each_skill_file_includes_at_least_three_example_invocations(tmp_path: Path) -> None:
    app = deployctl
    for name, text in generated(tmp_path, app).items():
        if name == "CONTEXT.md":
            continue
        command = str(frontmatter(text)["command"])
        lines = usage(text)
        assert len(lines) >= 3 and all(line.startswith(command) for line in lines)
        assert f"{command} --schema" in lines


def test_the_generated_files_pass_validation_by_an_openclaw_compatible_skill_loader(
    tmp_path: Path,
) -> None:
    for name, text in generated(tmp_path, deployctl).items():
        if name == "CONTEXT.md":
            version, _, rest = text.partition("\n")
            assert version == f"<!-- cli-version: {deployctl.version} -->"
            assert rest.startswith("# ") and "## Exit codes" in rest
            continue
        front = frontmatter(text)
        assert isinstance(front["name"], str) and len(front["name"]) <= 64
        assert SKILL_NAME.fullmatch(front["name"])
        assert isinstance(front["description"], str) and 0 < len(front["description"]) <= 1024
        body = text.partition("\n---\n")[2]
        assert body.lstrip().startswith("# ") and "## Guardrails" in body


# REQ-O-035

ROOT = Path(__file__).resolve().parents[1]


def listed_tools(path: Path) -> dict[str, object]:
    """``treaty-mcp examples.deployctl:app --list-tools``, as a CI step runs it"""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from treaty._mcp import main; sys.exit(main())",
            "examples.deployctl:app",
            "--list-tools",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    path.write_text(done.stdout)
    listing = json.loads(done.stdout)
    assert isinstance(listing, dict)
    return listing


def drift_of(envelope: dict[str, object]) -> dict[str, object]:
    drift = data_of(envelope)["drift"]
    assert isinstance(drift, dict)
    return drift


def test_tool_mcp_validate_mcp_schema_file_exits_0_if_schemas_match_non_zero_if_drift_is_detected(
    tmp_path: Path,
) -> None:
    listing = listed_tools(tmp_path / "mcp.json")
    assert listing["cli_version"] == deployctl.version
    code, envelope = run(
        deployctl, ["mcp-validate", "--mcp-schema-file", str(tmp_path / "mcp.json")]
    )
    assert code == 0
    assert drift_of(envelope) == {"added": [], "removed": [], "changed": [], "missing_from_mcp": []}
    tools = listing["tools"]
    assert isinstance(tools, list)
    (tmp_path / "stale.json").write_text(json.dumps({"tools": tools[1:]}))
    code, envelope = run(
        deployctl, ["mcp-validate", "--mcp-schema-file", str(tmp_path / "stale.json")]
    )
    error = envelope["error"]
    assert code == 1 and isinstance(error, dict) and error["code"] == "SCHEMA_DRIFT_DETECTED"


def test_drift_is_reported_as_a_structured_json_diff_with_added_removed_and_changed_fields(
    tmp_path: Path,
) -> None:
    listing = listed_tools(tmp_path / "mcp.json")
    tools = {t["name"]: t for t in listing["tools"]}  # type: ignore[union-attr]
    props = tools["deploy_rollback"]["inputSchema"]["properties"]
    del props["strategy"]
    props["region"] = {"type": "string"}
    props["to"] = {"type": "null"}
    (tmp_path / "mcp.json").write_text(json.dumps({"tools": list(tools.values())}))
    code, envelope = run(
        deployctl, ["mcp-validate", "--mcp-schema-file", str(tmp_path / "mcp.json")]
    )
    assert code == 1
    assert drift_of(envelope) == {
        "added": [{"command": "deploy.rollback", "field": "input.strategy"}],
        "removed": [{"command": "deploy.rollback", "field": "input.region"}],
        "changed": [
            {
                "command": "deploy.rollback",
                "field": "input.to",
                "cli_type": "string|null",
                "mcp_type": "null",
            }
        ],
        "missing_from_mcp": [],
    }


def test_a_new_cli_command_not_present_in_the_mcp_schema_is_reported_as_missing_from_mcp(
    tmp_path: Path,
) -> None:
    app = plain_app()
    (tmp_path / "mcp.json").write_text(json.dumps(tool_list(app)))

    @app.command("bye", description="Say bye", danger_level="safe", exit_codes=())
    def bye(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    code, envelope = run(app, ["mcp-validate", "--mcp-schema-file", str(tmp_path / "mcp.json")])
    assert code == 1 and drift_of(envelope)["missing_from_mcp"] == ["bye"]


def test_the_command_can_be_run_in_ci_to_detect_schema_staleness_before_deployment(
    tmp_path: Path,
) -> None:
    listed_tools(tmp_path / "mcp.json")
    check = [
        sys.executable,
        "-c",
        "from examples.deployctl import app; app.main()",
        "mcp-validate",
        "--mcp-schema-file",
        str(tmp_path / "mcp.json"),
    ]
    assert subprocess.run(check, cwd=ROOT, capture_output=True, check=False).returncode == 0
    (tmp_path / "mcp.json").write_text('{"tools": []}')
    assert subprocess.run(check, cwd=ROOT, capture_output=True, check=False).returncode == 1
    (tmp_path / "mcp.json").write_text("[]")
    assert subprocess.run(check, cwd=ROOT, capture_output=True, check=False).returncode == 4
