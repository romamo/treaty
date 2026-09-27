"""Built-in commands: REQ-O-026, REQ-O-027, REQ-O-028, REQ-O-029, REQ-O-034, REQ-O-035,
REQ-O-041, and the ``status --show-side-effects`` half of REQ-C-011."""

import io
import json
import os
import socket
import subprocess
from pathlib import Path

from conftest import needs_posix_permissions, spec_validator
from test_network_and_fs import Origin, Proxy, serving

from treaty import App, Check, Ctx, NoArgs, endpoint
from treaty._audit import audit
from treaty._deps import CheckFn


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
