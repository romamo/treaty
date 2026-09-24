import io
import json

import fixture_audit_app

from treaty._audit import RULES, audit
from treaty._cli import cli


def test_rules_are_ordered_and_unique() -> None:
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))
    assert ids[0] == "describe" and ids[-1] == "profile"


def test_audit_finds_each_planted_problem(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # no ./conformance here
    report = audit(fixture_audit_app.app, "fixture_audit_app:app", limit=3)
    by_rule = {r.id: r for r in report.rules}
    assert not by_rule["describe"].passed
    assert {f.command for f in by_rule["describe"].findings} == {"delete-item", "create-item"}
    assert [f.command for f in by_rule["danger-level"].findings] == ["delete-item"]
    assert "destructive" in by_rule["danger-level"].findings[0].fix
    assert [f.command for f in by_rule["exit-codes"].findings] == []
    assert [f.command for f in by_rule["retryable"].findings] == ["create-item"]
    assert {f.command for f in by_rule["typed-output"].findings} == {"delete-item", "create-item"}
    assert [f.command for f in by_rule["network-io"].findings] == ["create-item"]
    assert [f.command for f in by_rule["raw-payload"].findings] == ["create-item"]
    assert [f.command for f in by_rule["cleanup"].findings] == []
    assert not by_rule["profile"].passed
    assert len(report.next_steps) == 3 and report.next_steps[0].rule == "describe"
    assert report.failed == 7


def test_audit_passes_a_clean_app(monkeypatch, tmp_path) -> None:
    (tmp_path / "conformance").mkdir()
    (tmp_path / "conformance" / "x.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    from treaty import App, Ctx, NoArgs

    app = App("clean", version="1")

    @app.command("ping", description="Ping", examples=[("Ping", "clean ping")])
    def ping(args: NoArgs, ctx: Ctx) -> NoArgs:
        return args

    report = audit(app, "clean", limit=3)
    assert report.failed == 0 and report.next_steps == ()


def run_cli(argv: list[str], *, isatty: bool) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=isatty)
    return code, out.getvalue()


def test_cli_audit_json_and_human(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = run_cli(["audit", "fixture_audit_app:app", "--limit", "2"], isatty=False)
    assert code == 0
    data = json.loads(out)["data"]
    assert data["rules_total"] == len(RULES) and data["failed"] == 7
    assert len(data["next_steps"]) == 2
    code, out = run_cli(["audit", "fixture_audit_app:app", "--all"], isatty=True)
    assert code == 0
    assert "Next steps" in out and "1. (advice) describe [create-item]" in out
    assert out.count("fix:") == 9


def test_cli_audit_bad_targets() -> None:
    code, out = run_cli(["audit", "nomodule"], isatty=False)
    assert code == 2 and json.loads(out)["error"]["code"] == "ARG_ERROR"
    code, out = run_cli(["audit", "no.such.module:app"], isatty=False)
    assert code == 5 and json.loads(out)["error"]["code"] == "NOT_FOUND"
    code, out = run_cli(["audit", "fixture_audit_app:Name"], isatty=False)
    assert code == 4 and json.loads(out)["error"]["code"] == "PRECONDITION"


def test_cli_rules_and_own_audit(monkeypatch, tmp_path) -> None:
    code, out = run_cli(["rules"], isatty=False)
    assert code == 0 and [r["id"] for r in json.loads(out)["data"]] == [r.id for r in RULES]
    monkeypatch.chdir(tmp_path)
    code, out = run_cli(["audit", "treaty._cli:cli", "--all"], isatty=False)
    assert code == 0
    failing = [r["id"] for r in json.loads(out)["data"]["rules"] if not r["passed"]]
    assert failing == ["describe", "profile"]


def test_cli_audit_strict() -> None:
    code, out = run_cli(["audit", "fixture_audit_app:app", "--strict"], isatty=False)
    envelope = json.loads(out)
    assert code == 79 and envelope["error"]["code"] == "AUDIT_FAILED"
    assert envelope["error"]["context"]["rules"] == ["danger-level", "network-io", "retryable"]
    assert envelope["data"]["rules_total"] == len(RULES)
    code, out = run_cli(["audit", "treaty._cli:cli", "--strict"], isatty=False)
    assert code == 0 and json.loads(out)["ok"]


def test_cli_audit_strict_renders_human_report() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(
        ["audit", "fixture_audit_app:app", "--strict"],
        stdout=out,
        stderr=err,
        env={},
        isatty=True,
    )
    assert code == 79
    assert "fixture_audit_app:app:" in out.getvalue() and "Next steps" in out.getvalue()
    assert "AUDIT_FAILED" in err.getvalue()
