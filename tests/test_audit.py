import io
import json
import re

import fixture_audit_app

from treaty._audit import ADDITIVE, RULES, audit
from treaty._cli import cli


def test_rules_are_ordered_and_unique() -> None:
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))
    assert ids[0] == "describe" and ids[-1] == "profile"


def test_audit_finds_each_planted_problem(tmp_path) -> None:
    # No conformance/ under tmp_path
    report = audit(fixture_audit_app.app, "fixture_audit_app:app", limit=3, root=tmp_path)
    by_rule = {r.id: r for r in report.rules}
    assert not by_rule["describe"].passed
    assert {f.command for f in by_rule["describe"].findings} == {"delete-item", "create-item"}
    assert [f.command for f in by_rule["danger-level"].findings] == ["delete-item"]
    assert "destructive" in by_rule["danger-level"].findings[0].fix
    assert [f.command for f in by_rule["exit-codes"].findings] == []
    assert [f.command for f in by_rule["retryable"].findings] == ["create-item"]
    assert [f.command for f in by_rule["exit-code-suggestion"].findings] == ["create-item"]
    assert "suggestion=" in by_rule["exit-code-suggestion"].findings[0].fix
    assert {f.command for f in by_rule["typed-output"].findings} == {"delete-item", "create-item"}
    assert [f.command for f in by_rule["network-io"].findings] == ["create-item"]
    assert [f.message[:11] for f in by_rule["path-typed"].findings] == ["report_file"]
    assert [f.command for f in by_rule["raw-payload"].findings] == ["create-item"]
    assert [f.command for f in by_rule["cleanup"].findings] == []
    assert [f.command for f in by_rule["already-exists"].findings] == ["create-item"]
    assert not by_rule["profile"].passed
    # The fixture's findings are warnings and advice: warnings lead, in rule order
    assert [f.rule for f in report.next_steps] == ["danger-level", "retryable", "network-io"]
    assert report.failed == 10


def test_audit_passes_a_clean_app(tmp_path) -> None:
    (tmp_path / "conformance").mkdir()
    (tmp_path / "conformance" / "x.json").write_text("{}")
    from treaty import App, Ctx, NoArgs

    app = App("clean", version="1.0.0")

    @app.command(
        "ping",
        description="Ping",
        examples=[("Ping", "clean ping")],
        danger_level="safe",
        exit_codes=(),
    )
    def ping(args: NoArgs, ctx: Ctx) -> NoArgs:
        return args

    report = audit(app, "clean", limit=3, root=tmp_path)
    assert report.failed == 0 and report.next_steps == ()


def run_cli(argv: list[str], *, isatty: bool) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.run(argv, stdout=out, stderr=io.StringIO(), env={}, isatty=isatty)
    return code, out.getvalue()


def test_cli_audit_json_and_plain(tmp_path) -> None:
    where = ["--cwd", str(tmp_path)]
    code, out = run_cli(["audit", "fixture_audit_app:app", "--limit", "2", *where], isatty=False)
    assert code == 0
    data = json.loads(out)["data"]
    assert data["rules_total"] == len(RULES) and data["failed"] == 10
    assert len(data["next_steps"]) == 2
    code, out = run_cli(["audit", "fixture_audit_app:app", "--all", *where], isatty=True)
    assert code == 0
    assert "Next steps" in out and "1. (warning) danger-level [" in out
    assert "(advice) describe [create-item]" in out
    assert out.count("fix:") == 12


def test_cli_audit_bad_targets() -> None:
    code, out = run_cli(["audit", "nomodule"], isatty=False)
    assert code == 2 and json.loads(out)["error"]["code"] == "ARG_ERROR"
    code, out = run_cli(["audit", "no.such.module:app"], isatty=False)
    assert code == 5 and json.loads(out)["error"]["code"] == "NOT_FOUND"
    code, out = run_cli(["audit", "fixture_audit_app:Name"], isatty=False)
    assert code == 4 and json.loads(out)["error"]["code"] == "PRECONDITION"


def test_cli_rules_and_own_audit(tmp_path) -> None:
    code, out = run_cli(["rules"], isatty=False)
    ids = [r["id"] for r in json.loads(out)["data"]]
    assert code == 0 and ids == [r.id for r in (*RULES, ADDITIVE)]
    code, out = run_cli(["audit", "treaty._cli:cli", "--all", "--cwd", str(tmp_path)], isatty=False)
    assert code == 0
    failing = [r["id"] for r in json.loads(out)["data"]["rules"] if not r["passed"]]
    assert failing == ["profile"]  # no conformance profile in an empty directory


def test_cli_audit_strict() -> None:
    code, out = run_cli(["audit", "fixture_audit_app:app", "--strict"], isatty=False)
    envelope = json.loads(out)
    assert code == 79 and envelope["error"]["code"] == "AUDIT_FAILED"
    assert envelope["error"]["context"]["rules"] == [
        "danger-level",
        "network-io",
        "path-typed",
        "retryable",
    ]
    assert envelope["data"]["rules_total"] == len(RULES)
    code, out = run_cli(["audit", "treaty._cli:cli", "--strict"], isatty=False)
    assert code == 0 and json.loads(out)["ok"]


def test_cli_audit_strict_renders_plain_report() -> None:
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


def test_heuristics_skip_words_that_only_start_like_a_verb_or_end_like_a_path() -> None:
    from dataclasses import dataclass

    from treaty import App, Ctx, Flag
    from treaty._audit import audit

    @dataclass(frozen=True, slots=True)
    class Prefs:
        profile: str = Flag(default="default", description="Profile name")
        outfile: str = Flag(default="-", description="Where to write")

    app = App("prefs", version="1.0.0")

    @app.command(
        "settings",
        description="Show settings",
        examples=[("x", "prefs settings")],
        danger_level="safe",
        exit_codes=(),
    )
    def settings(args: Prefs, ctx: Ctx) -> dict[str, str]:
        return {}

    by_rule = {r.id: r for r in audit(app, "prefs:app", limit=10).rules}
    assert by_rule["danger-level"].findings == ()
    assert [f.message.split()[0] for f in by_rule["path-typed"].findings] == ["outfile"]


def test_next_steps_put_what_fails_strict_before_advice() -> None:
    """An error from the last rule leads, though earlier rules found advice"""
    old = fixture_audit_app.app.manifest()
    commands = old["commands"]
    assert isinstance(commands, dict)
    baseline = {**old, "commands": {**commands, "gone": next(iter(commands.values()))}}
    report = audit(fixture_audit_app.app, "fixture_audit_app", limit=100, baseline=baseline)
    ranks = {"error": 0, "warning": 1, "advice": 2}
    order = [ranks[f.severity.value] for f in report.next_steps]
    assert order == sorted(order) and report.next_steps[0].rule == "additive"


def test_every_exit_codes_fix_registers_when_applied_as_written() -> None:
    """Each finding suggests its own free code, and a description registration accepts"""
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")
    app.exit_code("TAKEN", 79, description="Already here", retryable=False, side_effects="none")
    for name in ("add", "remove"):

        @app.command(name, description=f"{name} it", exit_codes=(), danger_level="mutating")
        def handler(args: NoArgs, ctx: Ctx) -> Done:
            return Done("updated")

    rule = next(r for r in audit(app, "x:app", limit=3).rules if r.id == "exit-codes")
    suggested = [re.match(FIX, f.fix) for f in rule.findings]
    assert [m.group(1, 2) for m in suggested if m] == [
        ("ADD_FAILED", "80"),
        ("REMOVE_FAILED", "81"),
    ]
    for m in suggested:
        assert m is not None
        app.exit_code(
            m.group(1),
            int(m.group(2)),
            description=m.group(3),
            retryable=False,
            side_effects="none",
        )


FIX = r'app\.exit_code\("(\w+)", (\d+), description="([^"]+)"'


def _findings(app: object, rule: str) -> list[str]:
    report = audit(app, "x:app", limit=3)  # type: ignore[arg-type]
    return [f.command for r in report.rules if r.id == rule for f in r.findings]


def test_a_raw_subprocess_call_in_a_handler_is_reported() -> None:
    """Outside ctx.run nothing declares the child, so the audit asks for ctx.run first"""
    import subprocess
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("pack", description="Pack it", danger_level="mutating", exit_codes=())
    def pack(args: NoArgs, ctx: Ctx) -> Done:
        subprocess.run(["tar", "-cf", "x.tar", "."], check=True)
        return Done("created")

    assert _findings(app, "subprocess-declared") == ["pack"]


def test_a_raw_subprocess_call_is_found_however_it_was_imported() -> None:
    """A migrated CLI often imports run by name, or subprocess under another name"""
    import subprocess as sp
    from dataclasses import dataclass
    from subprocess import run

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("pack", description="Pack it", danger_level="mutating", exit_codes=())
    def pack(args: NoArgs, ctx: Ctx) -> Done:
        run(["tar", "-cf", "x.tar", "."], check=True)
        return Done("created")

    @app.command("unpack", description="Unpack it", danger_level="mutating", exit_codes=())
    def unpack(args: NoArgs, ctx: Ctx) -> Done:
        sp.check_output(["tar", "-xf", "x.tar"])
        return Done("updated")

    @app.command("count", description="Count it", danger_level="safe", exit_codes=())
    def count(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        # A local name that happens to be called run is not subprocess.run
        def run(items: list[str]) -> int:
            return len(items)

        return {"n": run(["a"])}

    assert _findings(app, "subprocess-declared") == ["pack", "unpack"]


def test_delete_not_found_is_only_for_commands_that_delete() -> None:
    """A restore from a missing snapshot is a real failure, not a delete of something gone"""
    from dataclasses import dataclass

    from treaty import Affects, App, Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class ById:
        id: int = Arg(description="Id")
        dry_run: bool = Flag(default=False, description="Preview")

    @dataclass(frozen=True, slots=True)
    class Result:
        effect: str
        would_affect: Affects | None = None

    app = App("x", version="1.0.0")
    for name in ("restore", "delete", "wipe", "prune"):

        @app.command(name, description=name, danger_level="destructive", exit_codes=["NOT_FOUND"])
        def handler(args: ById, ctx: Ctx) -> Result:
            return Result("noop")

    assert _findings(app, "delete-not-found") == ["delete", "prune", "wipe"]


def test_external_false_acknowledges_a_network_command_that_returns_computed_values() -> None:
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Count:
        pages: int

    app = App("x", version="1.0.0")
    for name, external in (("count", None), ("tally", False)):

        @app.command(
            name,
            description=name,
            danger_level="safe",
            exit_codes=(),
            has_network_io=True,
            external=external,
        )
        def handler(args: NoArgs, ctx: Ctx) -> Count:
            return Count(1)

    assert _findings(app, "external-data") == ["count"]


def test_network_io_scans_the_resources_a_handler_uses() -> None:
    """A migrated CLI keeps its HTTP client in a resource, where ctx.obj used to be"""
    import urllib.request
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Client:
        base: str

        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Client:
            return cls("https://example.com")

        def get(self, path: str) -> bytes:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return bytes(response.read())

    app = App("x", version="1.0.0")

    @app.command("show", description="Show it", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, client: Client) -> dict[str, int]:
        return {"size": len(client.get("/"))}

    report = audit(app, "x:app", limit=3)
    [finding] = [f for r in report.rules if r.id == "network-io" for f in r.findings]
    assert finding.command == "show" and "resource Client" in finding.message


def test_network_io_scans_a_resource_that_another_resource_acquires() -> None:
    import urllib.request
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Session:
        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx) -> Session:
            return cls()

        def get(self, url: str) -> bytes:
            with urllib.request.urlopen(url, timeout=5) as response:
                return bytes(response.read())

    @dataclass(frozen=True, slots=True)
    class Api:
        session: Session

        @classmethod
        def acquire(cls, args: NoArgs, ctx: Ctx, session: Session) -> Api:
            return cls(session)

    app = App("x", version="1.0.0")

    @app.command("show", description="Show it", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, api: Api) -> dict[str, int]:
        return {"size": len(api.session.get("https://example.com"))}

    report = audit(app, "x:app", limit=3)
    assert [f.command for r in report.rules if r.id == "network-io" for f in r.findings] == ["show"]


def test_a_list_that_keeps_its_own_page_flag_is_warned() -> None:
    """meta.pagination would say has_more: false while the API has more pages"""
    from dataclasses import dataclass

    from treaty import App, Ctx, Flag, NoArgs

    @dataclass(frozen=True, slots=True)
    class Paged:
        page: int = Flag(default=1, description="API page")

    app = App("x", version="1.0.0")

    @app.command("issues", description="List issues", danger_level="safe", exit_codes=())
    def issues(args: Paged, ctx: Ctx) -> list[dict[str, int]]:
        return [{"id": args.page}]

    @app.command("tags", description="List tags", danger_level="safe", exit_codes=())
    def tags(args: NoArgs, ctx: Ctx) -> list[dict[str, int]]:
        return [{"id": 1}]

    assert _findings(app, "paginated-list") == ["issues"]
