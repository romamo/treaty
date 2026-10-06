import ast
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fixture_audit_app
import pytest

from treaty import Affects, Flag
from treaty._audit import ADDITIVE, RULES, audit
from treaty._cli import cli
from treaty._scan import reached_functions


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
    assert "Flag(dry_run=True)" in by_rule["danger-level"].findings[0].fix
    assert [f.command for f in by_rule["exit-codes"].findings] == []
    assert [f.command for f in by_rule["retryable"].findings] == ["create-item"]
    assert [f.command for f in by_rule["exit-code-suggestion"].findings] == ["create-item"]
    assert "suggestion=" in by_rule["exit-code-suggestion"].findings[0].fix
    assert {f.command for f in by_rule["typed-output"].findings} == {"delete-item", "create-item"}
    # Their dict[str, object] outputs have every array inside re-sorted (#27)
    assert {f.command for f in by_rule["stable-order"].findings} == {"delete-item", "create-item"}
    assert [f.command for f in by_rule["network-io"].findings] == ["create-item"]
    assert [f.message[:11] for f in by_rule["path-typed"].findings] == ["report_file"]
    assert [f.command for f in by_rule["raw-payload"].findings] == ["create-item"]
    assert [f.command for f in by_rule["cleanup"].findings] == []
    assert [f.command for f in by_rule["already-exists"].findings] == ["create-item"]
    assert [f.command for f in by_rule["explicit-timeout"].findings] == ["create-item"]
    assert not by_rule["profile"].passed
    # The fixture's findings are warnings and advice: warnings lead, in rule order
    assert [f.rule for f in report.next_steps] == ["danger-level", "retryable", "network-io"]
    assert report.failed == 12


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
    assert data["rules_total"] == len(RULES) and data["failed"] == 12
    assert len(data["next_steps"]) == 2
    code, out = run_cli(["audit", "fixture_audit_app:app", "--all", *where], isatty=True)
    assert code == 0
    assert "Next steps" in out and "1. (warning) danger-level [" in out
    assert "(advice) describe [create-item]" in out
    assert out.count("fix:") == 15


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


def test_cli_audit_strict_passes_a_client_configured_from_ctx_network() -> None:
    """The README's ctx.network pattern only advises, so --strict passes (#356)"""
    code, out = run_cli(["audit", "fixture_net_client_app:app", "--strict"], isatty=False)
    assert code == 0, out
    rules = json.loads(out)["data"]["rules"]
    [http] = [r for r in rules if r["id"] == "http-client"]
    assert [(f["command"], f["severity"]) for f in http["findings"]] == [
        ("helper", "advice"),
        ("session", "advice"),
    ]


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

UPSTREAM_BUSY = {
    "code": 80,
    "description": "The upstream is busy",
    "retryable": True,
    "side_effects": "none",
}


def _fixctl(declared: dict[str, dict[str, object]]) -> object:
    """An app whose findings print app.exit_code(...) fixes, with ``declared`` registered"""
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("fixctl", version="1.0.0")
    for name, declaration in declared.items():
        app.exit_code(name, **declaration)  # type: ignore[arg-type]

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=("UPSTREAM_BUSY", "RATE_LIMITED", "UNAVAILABLE"),
    )
    def fetch(args: NoArgs, ctx: Ctx) -> Done:
        return Done("noop")

    @app.command("sync", description="Sync", danger_level="mutating", exit_codes=())
    def sync(args: NoArgs, ctx: Ctx) -> Done:
        return Done("updated")

    return app


def _registration(fix: str) -> ast.Call | None:
    """The app.exit_code(...) call a fix prints, parsed; None when it prints none"""
    start = fix.find("app.exit_code(")
    if start < 0:
        return None
    for end in (i + 1 for i, ch in enumerate(fix) if ch == ")" and i > start):
        try:
            node = ast.parse(fix[start:end], mode="eval").body
        except SyntaxError:
            continue  # the call ends at a later parenthesis
        assert isinstance(node, ast.Call)
        return node
    raise AssertionError(f"the fix prints an app.exit_code( call that does not parse: {fix}")


def test_every_printed_exit_code_registration_registers() -> None:
    """A fix printed as app.exit_code(...) registers when applied, with ``...`` keeping the
    rest of the declaration; a framework code such as RATE_LIMITED gets none (#30)"""
    declared = {"UPSTREAM_BUSY": UPSTREAM_BUSY}
    report = audit(_fixctl(declared), "x:app", limit=100)  # type: ignore[arg-type]
    calls = [
        (r.id, call) for r in report.rules for f in r.findings if (call := _registration(f.fix))
    ]
    assert sorted(rule for rule, _ in calls) == ["exit-code-suggestion", "exit-codes"]
    for rule, call in calls:
        name, code = (ast.literal_eval(a) for a in call.args[:2])
        keeps = any(isinstance(a, ast.Constant) and a.value is Ellipsis for a in call.args)
        declaration = {
            **(declared.get(name, {}) if keeps else {}),
            "code": code,
            **{k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg},
        }
        fixed = _fixctl({**declared, name: declaration})  # a RegistrationError fails here
        assert name not in " ".join(
            f.message for f in _findings_of(fixed, "exit-code-suggestion")
        ), rule


def _findings_of(app: object, rule: str) -> list:
    report = audit(app, "x:app", limit=3)  # type: ignore[arg-type]
    return [f for r in report.rules if r.id == rule for f in r.findings]


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

    from treaty import App, Arg, Ctx, Flag

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


def test_the_network_and_subprocess_rules_follow_helpers_in_the_same_module() -> None:
    """A fetch() beside the handler runs as part of it"""
    import subprocess
    import urllib.request
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    def fetch(url: str) -> bytes:
        with urllib.request.urlopen(url) as response:
            return bytes(response.read())

    def pack() -> None:
        subprocess.run(["tar", "-cf", "x.tar", "."], check=True)

    def refresh_all() -> int:
        return len(fetch("https://example.com"))

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("subscribe", description="Subscribe", danger_level="safe", exit_codes=())
    def subscribe(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": refresh_all()}

    @app.command(
        "refresh", description="Refresh", danger_level="safe", exit_codes=(), has_network_io=True
    )
    def refresh(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(fetch("https://example.com"))}

    @app.command("backup", description="Back up", danger_level="mutating", exit_codes=())
    def backup(args: NoArgs, ctx: Ctx) -> Done:
        pack()
        return Done("created")

    [network] = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "network-io" for f in r.findings
    ]
    assert network.command == "subscribe" and "function" in network.message
    assert _findings(app, "network-timeout") == ["refresh"]
    assert _findings(app, "http-client") == ["refresh"]
    assert _findings(app, "subprocess-declared") == ["backup"]


def _lambda_fetch() -> object:
    from urllib.request import urlopen

    return (
        lambda url: urlopen(url, timeout=5).read()  # noqa: S310
    )


def test_helper_following_survives_lambdas_and_ignores_network_words() -> None:
    """A lambda's source is not a statement; a docstring or urllib.parse is no network call"""
    import functools
    import urllib.parse
    import urllib.request
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    fetch = _lambda_fetch()

    def slug(text: str) -> str:
        """Built without any requests to a server"""
        return urllib.parse.quote(text)

    @functools.cache
    def cached_fetch(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            return bytes(response.read())

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("note", description="Note it", danger_level="safe", exit_codes=())
    def note(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"slug": slug("a b")}

    @app.command("peek", description="Peek", danger_level="safe", exit_codes=())
    def peek(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(fetch("https://example.com"))}  # type: ignore[operator]

    @app.command("pull", description="Pull", danger_level="safe", exit_codes=())
    def pull(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(cached_fetch("https://example.com"))}

    assert _findings(app, "network-io") == ["pull"]


class _Lazy:
    """Answers every attribute, as sh, plumbum, a lazy loader, or a mock does"""

    def __getattr__(self, name: str) -> object:
        return _Lazy()

    def __call__(self, *args: object) -> str:
        return ""


_run = _Lazy()


def test_helper_following_survives_proxies_unhashable_checks_and_local_aliases() -> None:
    from dataclasses import dataclass
    from pathlib import Path

    from treaty import App, Ctx, NoArgs

    @dataclass
    class DirCheck:
        """An ordinary, unhashable doctor check"""

        path: Path

        def __call__(self, ctx: Ctx) -> str | None:
            return None

    def fetch(url: str) -> bytes:
        import requests as r

        return bytes(r.get(url, timeout=5).content)

    app = App("x", version="1.0.0", checks=[DirCheck(Path("."))])

    @app.command("proxy", description="Proxy", danger_level="safe", exit_codes=())
    def proxy(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"out": _run("x")}

    @app.command("pull", description="Pull", danger_level="safe", exit_codes=())
    def pull(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(fetch("https://example.com"))}

    assert _findings(app, "network-io") == ["pull"]


def test_strict_keeps_next_steps_in_severity_order(tmp_path) -> None:
    """Exit data keeps its dataclass's declared order, as a result does"""
    where = ["--cwd", str(tmp_path)]
    _, ordered = run_cli(["audit", "fixture_audit_app:app", *where], isatty=False)
    code, strict = run_cli(["audit", "fixture_audit_app:app", "--strict", *where], isatty=False)
    steps = [[s["severity"], s["rule"]] for s in json.loads(ordered)["data"]["next_steps"]]
    assert code != 0 and steps
    assert [[s["severity"], s["rule"]] for s in json.loads(strict)["data"]["next_steps"]] == steps


def test_an_example_that_does_not_parse_is_an_error() -> None:
    from dataclasses import dataclass

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Show:
        verbose_level: int = Flag(default=0, description="How much")

    app = App("x", version="1.0.0")

    @app.command(
        "show",
        description="Show it",
        danger_level="safe",
        exit_codes=(),
        examples=[("Show more", "x show --level 2"), ("Show", "x show --verbose-level 1")],
    )
    def show(args: Show, ctx: Ctx) -> dict[str, int]:
        return {"n": args.verbose_level}

    report = audit(app, "x:app", limit=3)
    errors = [f for r in report.rules if r.id == "describe" for f in r.findings]
    assert [f.severity.value for f in errors] == ["error"]
    assert "x show --level 2" in errors[0].message


def test_a_loop_that_sleeps_to_throttle_is_not_a_retry() -> None:
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("apply", description="Apply", danger_level="mutating", exit_codes=())
    def apply(args: NoArgs, ctx: Ctx) -> Done:
        for item in ["1", "x", "2"]:
            try:
                int(item)
            except ValueError:
                continue
            time.sleep(0)
        return Done("updated")

    @app.command("fetch", description="Fetch", danger_level="mutating", exit_codes=())
    def fetch(args: NoArgs, ctx: Ctx) -> Done:
        for _ in range(3):
            try:
                return Done("updated")
            except ConnectionError:
                time.sleep(1)
        return Done("noop")

    @app.command("push", description="Push", danger_level="mutating", exit_codes=())
    def push(args: NoArgs, ctx: Ctx) -> Done:
        for attempt in range(3):
            try:
                return Done("updated")
            except ConnectionError:
                if attempt == 2:
                    raise
            time.sleep(2**attempt)
        return Done("noop")

    assert _findings(app, "retry-declared") == ["fetch", "push"]


def test_the_example_check_judges_spelling_not_the_callers_world() -> None:
    """A variable, a directory, a pipeline, or plain output is the caller's, not a typo"""
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class T:
        title: str = Arg(description="Title")
        api_token: str = Flag(default="", description="Token")

    app = App("adv", version="1.0.0")

    @app.command(
        "t",
        description="T",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Plain", "adv t x --format plain"),
            ("Dash title", "adv t -- -title"),
            ("From a variable", "adv t x --api-token-from-env ADV_TOKEN_NOT_SET"),
            ("Elsewhere", "adv t x --cwd ./nowhere"),
            ("With a variable", "ADV_X=1 adv t x"),
            ("Piped", "echo y | adv t x"),
        ],
    )
    def t(args: T, ctx: Ctx) -> dict[str, int]:
        return {}

    assert [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ] == []


def test_network_io_follows_from_imports_and_keeps_package_names() -> None:
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    def fetch(url: str) -> bytes:
        from requests import get as g

        return bytes(g(url, timeout=5).content)

    def cookies() -> int:
        import http.cookies  # noqa: F401

        connection = http.client.HTTPSConnection("example.com", timeout=5)
        return len(str(connection))

    app = App("x", version="1.0.0")

    @app.command("pull", description="Pull", danger_level="safe", exit_codes=())
    def pull(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(fetch("https://example.com"))}

    @app.command("jar", description="Jar", danger_level="safe", exit_codes=())
    def jar(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": cookies()}

    @dataclass(frozen=True, slots=True)
    class Unused:
        effect: str

    assert _findings(app, "network-io") == ["jar", "pull"]


def test_the_example_check_skips_redirects_and_keeps_words_that_look_like_sources() -> None:
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class B:
        name: str = Arg(description="Name")
        load_from_file: bool = Flag(default=False, description="Load it")

    app = App("adv", version="1.0.0")

    @app.command(
        "b",
        description="B",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Quiet", "adv b x 2>/dev/null"),
            ("To a file", "adv b x >out.json"),
            ("Then", "adv b x; echo done"),
            ("Commented", "adv b x  # the name"),
            ("A boolean named like a source", "adv b x --load-from-file"),
            ("A value named like one", "adv b copy-from-file"),
            ("Through uv", "uv run adv b x"),
        ],
    )
    def b(args: B, ctx: Ctx) -> dict[str, int]:
        return {}

    assert _findings(app, "describe") == []


def test_every_suggested_example_passes_the_example_check() -> None:
    """The no-example fix is meant to be pasted: each one must parse as written"""
    import re as regex
    from dataclasses import dataclass
    from enum import StrEnum

    from treaty import App, Arg, Ctx, Flag

    class Color(StrEnum):
        RED = "red"

    @dataclass(frozen=True, slots=True)
    class Wide:
        id: int = Arg(description="Id")
        release: str = Flag(description="Release", pattern_type="semver")
        home: str = Flag(description="Home", pattern_type="url")
        uid: str = Flag(description="Uid", pattern_type="uuid")
        slug: str = Flag(description="Slug", pattern_type="alphanumeric_id")
        api_token: str = Flag(description="Token")
        colors: tuple[Color, ...] = Flag(description="Colors")
        counts: tuple[int, ...] = Flag(description="Counts")
        where: Path = Flag(description="Where")
        ratio: float = Flag(description="Ratio")

    app = App("adv", version="1.0.0")

    @app.command("w", description="W", danger_level="safe", exit_codes=())
    def w(args: Wide, ctx: Ctx) -> dict[str, int]:
        return {}

    [finding] = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    suggested = regex.search(r'"(adv [^"]+)"', finding.fix)
    assert suggested is not None
    from treaty._audit import _example_problem
    from treaty._values import CommandPath

    command = app.commands[CommandPath("w")]
    assert "--api-token-from-env" in suggested.group(1)
    assert _example_problem(app, command, suggested.group(1)) is None, suggested.group(1)


def test_retry_needs_an_exit_on_success_from_inside_the_try() -> None:
    import itertools
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    def fetch() -> Done:
        return Done("updated")

    app = App("x", version="1.0.0")

    @app.command("forever", description="Forever", danger_level="mutating", exit_codes=())
    def forever(args: NoArgs, ctx: Ctx) -> Done:
        for _ in itertools.count():
            try:
                return fetch()
            except OSError:
                time.sleep(1)
        return Done("noop")

    @app.command("backoff", description="Backoff", danger_level="mutating", exit_codes=())
    def backoff(args: NoArgs, ctx: Ctx) -> Done:
        for delay in (1, 2, 4):
            try:
                result = fetch()
                break
            except OSError:
                time.sleep(delay)
        else:
            return Done("noop")
        return result

    @app.command("poll", description="Poll", danger_level="mutating", exit_codes=())
    def poll(args: NoArgs, ctx: Ctx) -> Done:
        count = 0
        while count < 3:
            try:
                count += int("1")
            except ValueError:
                count = 3
            time.sleep(0)
        return Done("updated")

    @app.command("events", description="Events", danger_level="mutating", exit_codes=())
    def events(args: NoArgs, ctx: Ctx) -> Done:
        while True:
            try:
                fetch()
            except KeyboardInterrupt:
                return Done("updated")
            time.sleep(0)

    assert _findings(app, "retry-declared") == ["backoff", "forever"]


def test_a_placeholder_in_an_example_is_an_error_not_a_redirect() -> None:
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx

    @dataclass(frozen=True, slots=True)
    class Done:
        id: int = Arg(description="Id")

    @dataclass(frozen=True, slots=True)
    class Add:
        text: str = Arg(description="Text")

    app = App("todo", version="1.0.0")

    @app.command(
        "done",
        description="Done",
        danger_level="safe",
        exit_codes=(),
        examples=[("x", "todo done <id>")],
    )
    def done(args: Done, ctx: Ctx) -> dict[str, int]:
        return {}

    @app.command(
        "add",
        description="Add",
        danger_level="safe",
        exit_codes=(),
        examples=[("x", 'todo add "<text>"')],
    )
    def add(args: Add, ctx: Ctx) -> dict[str, int]:
        return {}

    report = audit(app, "x:app", limit=3)
    errors = [f for r in report.rules if r.id == "describe" for f in r.findings]
    assert sorted(f.command for f in errors) == ["add", "done"]
    assert all("placeholder" in f.message for f in errors)


def test_the_example_check_keeps_hash_words_and_judges_secrets_and_own_version_flags() -> None:
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class View:
        ref: str = Arg(description="Issue", pattern=r"[\w-]+/[\w-]+#\d+")

    @dataclass(frozen=True, slots=True)
    class Login:
        api_token: str = Flag(description="Token")

    @dataclass(frozen=True, slots=True)
    class Release:
        version: str = Flag(description="Version", pattern_type="semver")

    app = App("adv", version="1.0.0")

    @app.command(
        "view",
        description="View",
        danger_level="safe",
        exit_codes=(),
        examples=[("x", "adv view octo/repo#12")],
    )
    def view(args: View, ctx: Ctx) -> dict[str, int]:
        return {}

    @app.command(
        "login",
        description="Login",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("x", "adv login --api-token-from-env MY_TOKEN"),
            ("y", "adv login --api-token-from-file /run/t"),
        ],
    )
    def login(args: Login, ctx: Ctx) -> dict[str, int]:
        return {}

    @app.command(
        "release",
        description="Release",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("x", "adv release --version 1.2.3"),
            ("bad", "adv release --version nope --bogus"),
        ],
    )
    def release(args: Release, ctx: Ctx) -> dict[str, int]:
        return {}

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert [f.command for f in errors] == ["release"]
    assert "nope" in errors[0].message or "--bogus" in errors[0].message


def test_pollers_consumers_and_searches_are_not_retries() -> None:
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    state = {"n": 0}

    def get() -> str:
        return "done"

    app = App("x", version="1.0.0")

    @app.command("poll", description="Poll", danger_level="mutating", exit_codes=())
    def poll(args: NoArgs, ctx: Ctx) -> Done:
        while True:
            try:
                status = get()
                if status == "done":
                    return Done("updated")
            except KeyError:
                pass
            time.sleep(0)

    @app.command("find", description="Find", danger_level="mutating", exit_codes=())
    def find(args: NoArgs, ctx: Ctx) -> Done:
        for item in ["a", "b"]:
            try:
                for other in ["b"]:
                    if other == item:
                        break
            except ValueError:
                pass
            time.sleep(0)
        state["n"] += 1
        return Done("updated")

    assert _findings(app, "retry-declared") == []


def test_the_example_check_never_runs_a_handler_and_keeps_quoted_values() -> None:
    """A value flag missing its value must not swallow --validate-only and run the handler"""
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx, Flag

    ran: list[str] = []

    @dataclass(frozen=True, slots=True)
    class Label:
        name: str = Arg(description="Name")
        color: str = Flag(default="", description="Color")
        body: str = Flag(default="", description="Body")

    app = App("labels", version="1.0.0")

    @app.command(
        "create",
        description="Create a label",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Missing value", "labels create bug --color"),
            ("Quoted hash", "labels create bug --color '#d73a4a'"),
            ("HTML", "labels create bug --body '<p>Hello</p>'"),
            ("Commented", "labels create bug  # the name"),
        ],
    )
    def create(args: Label, ctx: Ctx) -> dict[str, str]:
        ran.append(args.color)
        return {}

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert ran == []
    assert [f.message for f in errors] == [
        "the example 'labels create bug --color' does not parse: '--color' needs a value"
    ]


def test_a_retry_that_continues_and_returns_after_the_try_is_found() -> None:
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    def call() -> Done:
        return Done("updated")

    app = App("x", version="1.0.0")

    @app.command("send", description="Send", danger_level="mutating", exit_codes=())
    def send(args: NoArgs, ctx: Ctx) -> Done:
        for attempt in range(5):
            try:
                result = call()
            except ConnectionError:
                time.sleep(2**attempt)
                continue
            return result
        return Done("noop")

    assert _findings(app, "retry-declared") == ["send"]


def test_the_example_check_reads_quotes_as_a_shell_does() -> None:
    from dataclasses import dataclass

    from treaty import App, Arg, Ctx, Flag

    ran: list[str] = []

    @dataclass(frozen=True, slots=True)
    class Issue:
        title: str = Arg(description="Title")
        body: str = Flag(default="", description="Body")
        tag: str = Flag(default="", description="Tag")

    @dataclass(frozen=True, slots=True)
    class Echo:
        words: tuple[str, ...] = Arg(description="Words")

    app = App("issues", version="1.0.0")

    @app.command(
        "create",
        description="Create an issue",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Fixes", 'issues create Crash --body="Fixes #12"'),
            ("Apostrophe", "issues create 'Don'\\''t crash'"),
            ("Version", "issues create x --tag v<version>"),
        ],
    )
    def create(args: Issue, ctx: Ctx) -> dict[str, int]:
        ran.append(args.title)
        return {}

    @app.command(
        "echo",
        description="Echo words",
        danger_level="safe",
        exit_codes=(),
        examples=[("Another command's words", "issues create -- echo")],
    )
    def echo(args: Echo, ctx: Ctx) -> dict[str, int]:
        ran.append(" ".join(args.words))
        return {}

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert ran == []
    assert [f.message.rsplit(": ", 1)[1] for f in errors] == [
        "<version> is a placeholder; write a real value"
    ]


def test_a_throttled_search_is_not_a_retry() -> None:
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    app = App("x", version="1.0.0")

    @app.command("scan", description="Scan", danger_level="mutating", exit_codes=())
    def scan(args: NoArgs, ctx: Ctx) -> Done:
        for item in ["a", "1"]:
            time.sleep(0)
            try:
                found = int(item)
            except ValueError:
                continue
            return Done(str(found))
        return Done("noop")

    assert _findings(app, "retry-declared") == []


def test_a_field_read_through_a_local_is_named_in_the_derived_declaration() -> None:
    """The field's values reach git through `extra`; the manifest names the field, and a
    local from anything else, such as a path or a constant, stays hard-coded"""
    from dataclasses import dataclass

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Log:
        extra: tuple[str, ...] = Flag(default=(), description="More git log arguments")

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    app = App("x", version="1.0.0")

    @app.command("direct", description="Direct", danger_level="safe", exit_codes=())
    def direct(args: Log, ctx: Ctx) -> Shown:
        return Shown(ctx.run(["git", "log", *args.extra]).stdout)

    @app.command("local", description="Local", danger_level="safe", exit_codes=())
    def local(args: Log, ctx: Ctx) -> Shown:
        extra = list(args.extra)
        return Shown(ctx.run(["git", "log", *extra]).stdout)

    @app.command("where", description="Where", danger_level="safe", exit_codes=())
    def where(args: Log, ctx: Ctx) -> Shown:
        repo = ctx.cwd / "repo"
        return Shown(ctx.run(["git", "-C", str(repo), "status"]).stdout)

    assert _findings(app, "subprocess-declared") == []
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["local"]["subprocess"]["user_controlled_args"] == ["extra"]
    assert commands["where"]["subprocess"]["user_controlled_args"] == []


def test_backoffs_with_a_gated_sleep_or_a_reraising_handler_are_retries() -> None:
    import time
    from dataclasses import dataclass

    from treaty import App, Ctx, NoArgs

    @dataclass(frozen=True, slots=True)
    class Done:
        effect: str

    def call() -> Done:
        return Done("updated")

    app = App("x", version="1.0.0")

    @app.command("gated", description="Gated", danger_level="mutating", exit_codes=())
    def gated(args: NoArgs, ctx: Ctx) -> Done:
        for attempt in range(4):
            if attempt:
                time.sleep(2**attempt)
            try:
                result = call()
            except ConnectionError:
                continue
            return result
        return Done("noop")

    @app.command("reraise", description="Reraise", danger_level="mutating", exit_codes=())
    def reraise(args: NoArgs, ctx: Ctx) -> Done:
        for _ in range(4):
            try:
                result = call()
            except TimeoutError:
                time.sleep(1)
                continue
            except ValueError:
                raise
            return result
        return Done("noop")

    assert _findings(app, "retry-declared") == ["gated", "reraise"]


def test_the_example_check_judges_after_a_global_value_flag_and_keeps_generics() -> None:
    from dataclasses import dataclass
    from typing import Literal

    from treaty import App, Arg, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Add:
        text: str = Arg(description="Text")
        priority: Literal["low", "high"] = Flag(default="low", description="Priority")
        kind: str = Flag(default="", description="Type name")

    app = App("todo", version="1.0.0")

    @app.command(
        "add",
        description="Add",
        danger_level="safe",
        exit_codes=(),
        examples=[
            ("Bad value after a global flag", "todo --format json add x --priority two"),
            ("A generic type", "todo add x --kind List<String>"),
        ],
    )
    def add(args: Add, ctx: Ctx) -> dict[str, int]:
        return {}

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert [f.message.split(" does not parse")[0] for f in errors] == [
        "the example 'todo --format json add x --priority two'"
    ]


def test_a_field_put_into_a_list_by_mutation_walrus_or_with_is_named() -> None:
    """cmd.append(args.x) and cmd.extend(args.extra) are the common way to build argv"""
    from dataclasses import dataclass
    from pathlib import Path

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Log:
        stat: bool = Flag(default=False, description="Show stats")
        extra: tuple[str, ...] = Flag(default=(), description="More arguments")
        repo: Path = Flag(default=Path("."), description="Repository")

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    app = App("x", version="1.0.0")

    @app.command("built", description="Built", danger_level="safe", exit_codes=())
    def built(args: Log, ctx: Ctx) -> Shown:
        cmd: list[str] = []
        if args.stat:
            cmd.append("--stat")
        cmd.extend(args.extra)
        return Shown(ctx.run(["git", "log", *cmd]).stdout)

    @app.command("walrus", description="Walrus", danger_level="safe", exit_codes=())
    def walrus(args: Log, ctx: Ctx) -> Shown:
        if extra := list(args.extra):
            return Shown(ctx.run(["git", "log", *extra]).stdout)
        return Shown("")

    @app.command("opened", description="Opened", danger_level="safe", exit_codes=())
    def opened(args: Log, ctx: Ctx) -> Shown:
        with open(args.repo) as f:
            return Shown(ctx.run(["cat", f.name]).stdout)

    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["built"]["subprocess"]["user_controlled_args"] == ["extra"]
    assert commands["walrus"]["subprocess"]["user_controlled_args"] == ["extra"]
    assert commands["opened"]["subprocess"]["user_controlled_args"] == ["repo"]


def test_argv_built_from_the_whole_arguments_object_asks_for_a_declaration() -> None:
    """flags(args) may pass any field: a worked-out declaration naming none would be wrong"""
    from dataclasses import dataclass

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Log:
        ref: str = Flag(default="HEAD", description="Ref")
        pos: int = Flag(default=0, description="Where to insert")

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    def flags(args: Log) -> list[str]:
        return [args.ref]

    class Builder:
        def __init__(self) -> None:
            self.cmd: list[str] = ["git", "log"]

    app = App("x", version="1.0.0")

    @app.command("whole", description="Whole", danger_level="safe", exit_codes=())
    def whole(args: Log, ctx: Ctx) -> Shown:
        return Shown(ctx.run(["git", "log", *flags(args)]).stdout)

    @app.command("member", description="Member", danger_level="safe", exit_codes=())
    def member(args: Log, ctx: Ctx) -> Shown:
        builder = Builder()
        builder.cmd.append(args.ref)
        builder.cmd.insert(args.pos, "--oneline")
        return Shown(ctx.run(["git", "log", *builder.cmd]).stdout)

    assert _findings(app, "subprocess-declared") == ["whole"]
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["member"]["subprocess"]["user_controlled_args"] == ["ref"]


def test_a_method_of_the_arguments_is_unknown_and_a_replaced_copy_reads_by_field() -> None:
    from dataclasses import dataclass, replace

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Co:
        ref: str = Flag(default="main", description="Ref")

        def argv(self) -> list[str]:
            return [self.ref]

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    app = App("x", version="1.0.0")

    @app.command("method", description="Method", danger_level="safe", exit_codes=())
    def method(args: Co, ctx: Ctx) -> Shown:
        return Shown(ctx.run(["git", "checkout", *args.argv()]).stdout)

    @app.command("copy", description="Copy", danger_level="safe", exit_codes=())
    def copy(args: Co, ctx: Ctx) -> Shown:
        clean = replace(args, ref=args.ref.strip())
        return Shown(ctx.run(["git", "checkout", clean.ref]).stdout)

    assert _findings(app, "subprocess-declared") == ["method"]
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["copy"]["subprocess"]["user_controlled_args"] == ["ref"]


def test_a_copy_names_the_fields_replaced_into_it_and_a_derived_object_is_unknown() -> None:
    from dataclasses import dataclass, replace

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Log:
        ref: str = Flag(default="", description="Ref")
        base: str = Flag(default="main", description="Base")

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    @dataclass(frozen=True, slots=True)
    class Settings:
        ref: str

    def load(args: Log) -> Settings:
        return Settings(args.ref or "HEAD")

    app = App("x", version="1.0.0")

    @app.command("either", description="Either", danger_level="safe", exit_codes=())
    def either(args: Log, ctx: Ctx) -> Shown:
        clean = replace(args, ref=args.ref or args.base)
        return Shown(ctx.run(["git", "log", "-1", clean.ref]).stdout)

    @app.command("through", description="Through", danger_level="safe", exit_codes=())
    def through(args: Log, ctx: Ctx) -> Shown:
        base = args.base
        clean = replace(args, ref=base)
        return Shown(ctx.run(["git", "log", "-1", clean.ref]).stdout)

    @app.command("loaded", description="Loaded", danger_level="safe", exit_codes=())
    def loaded(args: Log, ctx: Ctx) -> Shown:
        cfg = load(args)
        return Shown(ctx.run(["git", "log", "-1", cfg.ref]).stdout)

    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["either"]["subprocess"]["user_controlled_args"] == ["ref", "base"]
    assert "base" in commands["through"]["subprocess"]["user_controlled_args"]
    assert _findings(app, "subprocess-declared") == ["loaded"]


def test_a_name_is_a_copy_only_when_every_binding_is_one() -> None:
    import copy
    from dataclasses import dataclass, replace

    from treaty import App, Ctx, Flag

    @dataclass(frozen=True, slots=True)
    class Log:
        ref: str = Flag(default="", description="Ref")
        base: str = Flag(default="main", description="Base")

    @dataclass(frozen=True, slots=True)
    class Shown:
        text: str

    def load(args: Log) -> Log:
        return Log(ref=args.base)

    app = App("x", version="1.0.0")

    @app.command("branch", description="Branch", danger_level="safe", exit_codes=())
    def branch(args: Log, ctx: Ctx) -> Shown:
        opts = args
        if args.base:
            opts = load(args)
        return Shown(ctx.run(["git", "log", opts.ref]).stdout)

    @app.command("rebound", description="Rebound", danger_level="safe", exit_codes=())
    def rebound(args: Log, ctx: Ctx) -> Shown:
        args = replace(args, ref=args.ref or args.base)
        return Shown(ctx.run(["git", "log", args.ref]).stdout)

    @app.command("stdlib", description="Stdlib", danger_level="safe", exit_codes=())
    def stdlib(args: Log, ctx: Ctx) -> Shown:
        clean = copy.replace(args, ref=args.base)
        again = replace(clean, base="x")
        return Shown(ctx.run(["git", "log", again.ref]).stdout)

    assert _findings(app, "subprocess-declared") == ["branch"]
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    assert commands["rebound"]["subprocess"]["user_controlled_args"] == ["ref", "base"]
    assert "base" in commands["stdlib"]["subprocess"]["user_controlled_args"]


BROKEN_TARGETS = {
    "bad_version": 'from treaty import App\n\napp = App("spike", version="one")\n',
    "bad_annotation": """from dataclasses import dataclass

from treaty import App, Arg, Ctx

app = App("spike", version="1.0.0")


@dataclass(frozen=True)
class Point:
    x: int


@dataclass(frozen=True)
class Args:
    points: tuple[Point, ...] = Arg(description="Points")


@app.command("plot", description="Plot", danger_level="safe", exit_codes=())
def plot(args: Args, ctx: Ctx) -> dict[str, str]:
    return {}
""",
    "undefined_annotation": """from dataclasses import dataclass
from typing import TYPE_CHECKING

from treaty import App, Arg, Ctx

if TYPE_CHECKING:
    from decimal import Decimal

app = App("spike", version="1.0.0")


@dataclass(frozen=True)
class Args:
    amount: Decimal = Arg(description="Amount")


@app.command("pay", description="Pay", danger_level="safe", exit_codes=())
def pay(args: Args, ctx: Ctx) -> dict[str, str]:
    return {}
""",
}


@pytest.mark.parametrize(
    ("module", "said"),
    [
        ("bad_version", "App spike"),
        ("bad_annotation", "plot: "),
        ("undefined_annotation", "Args.amount names Decimal"),
    ],
)
@pytest.mark.parametrize(
    "command", ["audit", "schema-lock", "changelog-add", "agents-md", "check-docs", "conformance"]
)
def test_a_target_that_fails_to_register_is_app_import_failed_without_a_traceback(
    tmp_path: Path, command: str, module: str, said: str
) -> None:
    """The mistake is the target's, not treaty's, on every command that loads a target"""
    name = f"target_{module}_{command.replace('-', '_')}"
    (tmp_path / f"{name}.py").write_text(BROKEN_TARGETS[module])
    out, err = io.StringIO(), io.StringIO()
    target = f"{name}:app"
    argv = [command, target, *(["AGENTS.md"] if command == "check-docs" else [])]
    code = cli.run([*argv, "--cwd", str(tmp_path)], stdout=out, stderr=err, env={}, isatty=False)
    error = json.loads(out.getvalue())["error"]
    assert code == 4 and error["code"] == "APP_IMPORT_FAILED"
    assert error["context"]["target"] == target
    assert error["context"]["exception"] == "RegistrationError"
    assert said in error["message"] and said in error["context"]["message"]
    assert "Traceback" not in out.getvalue() + err.getvalue()


def _messages(app: object, target: str, rule: str) -> dict[str, str]:
    report = audit(app, target, limit=3)  # type: ignore[arg-type]
    return {f.command: f.message for r in report.rules if r.id == rule for f in r.findings}


def _via(fn: object) -> str:
    code = fn.__code__  # type: ignore[attr-defined]
    name = f"{fn.__module__}.{fn.__qualname__}"  # type: ignore[attr-defined]
    return f"{name} ({os.path.basename(code.co_filename)}:{code.co_firstlineno})"


def test_rules_follow_a_handler_into_a_helper_module_and_name_where() -> None:
    """Issue 14: a helper in another module of the app is read as part of the handler"""
    import fixture_follow_app
    import fixture_follow_helpers

    via = f"; found via {_via(fixture_follow_helpers.enter)}"
    for rule in ("no-chdir", "env-prefix", "network-io"):
        messages = _messages(fixture_follow_app.app, "fixture_follow_app:app", rule)
        assert messages["go"].endswith(via), rule
    env = _messages(fixture_follow_app.app, "fixture_follow_app:app", "env-prefix")
    assert list(env) == ["go"] and "OTHER_TOOL_TOKEN" in env["go"]
    timeout = _messages(fixture_follow_app.app, "fixture_follow_app:app", "network-timeout")
    assert timeout == {
        "refresh": "urllib.request.urlopen(...) has no timeout=, so it can outlive --timeout"
        f"; found via {_via(fixture_follow_helpers.untimed_fetch)}"
    }
    assert list(_messages(fixture_follow_app.app, "x:app", "http-client")) == ["refresh"]


def test_helper_modules_keep_the_same_module_hardening() -> None:
    """Across modules too: a proxy, a lazy module, and a lambda are not followed, a
    docstring or urllib.parse is no network call, and a decorated or aliased one is"""
    import fixture_follow_app
    import fixture_follow_helpers

    network = _messages(fixture_follow_app.app, "fixture_follow_app:app", "network-io")
    assert sorted(network) == ["alias", "go", "pull"]
    assert network["pull"].endswith(
        f"; found via {_via(fixture_follow_helpers.cached_fetch.__wrapped__)}"
    )
    assert fixture_follow_helpers.asked == []
    assert [u.fn for u in reached_functions(fixture_follow_app.proxy)] == [fixture_follow_app.proxy]


def test_other_modules_are_followed_three_calls_deep_and_never_into_the_stdlib() -> None:
    import fixture_follow_app
    import fixture_follow_helpers as h

    units = reached_functions(fixture_follow_app.deep)
    assert [u.fn for u in units] == [fixture_follow_app.deep, h.first, h.second, h.third]
    assert units[-1].via == (_via(h.first), _via(h.second), _via(h.third))
    assert [u.fn for u in reached_functions(fixture_follow_app.encode)] == [
        fixture_follow_app.encode,
        h.encode,
    ]
    assert _messages(fixture_follow_app.app, "x:app", "no-chdir").keys() == {"go"}


def test_the_handlers_own_module_is_still_followed_however_deep() -> None:
    import urllib.request

    from treaty import App, Ctx, NoArgs

    def five() -> bytes:
        return bytes(urllib.request.urlopen("https://example.com").read())  # noqa: S310

    def four() -> bytes:
        return five()

    def three() -> bytes:
        return four()

    def two() -> bytes:
        return three()

    def one() -> bytes:
        return two()

    app = App("x", version="1.0.0")

    @app.command("far", description="Far", danger_level="safe", exit_codes=())
    def far(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": len(one())}

    assert _findings(app, "network-io") == ["far"]


def test_a_package_follows_its_own_modules_only() -> None:
    from fixture_follow_pkg import cli as pkg
    from fixture_follow_pkg import net, ops

    env = _messages(pkg.app, "fixture_follow_pkg.cli:app", "env-prefix")
    assert env == {
        "token": "reads the unprefixed variable OTHER_TOOL_TOKEN, which an agent may set for "
        "another tool in the same session (REQ-F-073); found via "
        "fixture_follow_pkg.ops.read_token (fixture_follow_pkg/ops.py:10) -> "
        "fixture_follow_pkg.ops.Store.load (fixture_follow_pkg/ops.py:5)"
    }
    assert _messages(pkg.app, "x:app", "no-chdir") == {}  # another package's helper
    # A module a helper imports itself, relative to its package, is followed; one past the
    # top-level package is an import that never runs, and the audit keeps going
    network = _messages(pkg.app, "x:app", "network-io")
    assert list(network) == ["pull"]
    assert network["pull"].endswith(
        "; found via fixture_follow_pkg.ops.pull (fixture_follow_pkg/ops.py:14) -> "
        "fixture_follow_pkg.net.fetch (fixture_follow_pkg/net.py:4)"
    )
    assert [u.fn for u in reached_functions(pkg.beyond)] == [pkg.beyond, ops.beyond]
    assert net.fetch in [u.fn for u in reached_functions(pkg.pull)]


def test_a_namespace_package_follows_its_own_modules_too() -> None:
    from fixture_follow_ns import cli as ns

    assert _messages(ns.app, "fixture_follow_ns.cli:app", "no-chdir")["enter"].endswith(
        "; found via fixture_follow_ns.helpers.enter (fixture_follow_ns/helpers.py:4)"
    )


def test_treaty_code_is_never_followed_out_of_its_module() -> None:
    from treaty import _cli

    assert {u.fn.__module__ for u in reached_functions(_cli.audit_command)} == {"treaty._cli"}


def test_an_unhashable_callable_is_followed_without_the_cache() -> None:
    from dataclasses import dataclass

    @dataclass
    class Acquire:
        path: str

        def __call__(self) -> str:
            return self.path

    acquire = Acquire(".")
    assert [u.fn for u in reached_functions(acquire)] == [acquire]


def test_the_report_says_what_the_source_rules_read() -> None:
    code, out = run_cli(["audit", "fixture_follow_app:app"], isatty=False)
    scope = json.loads(out)["data"]["scope"]
    here = Path(__file__).resolve().parent
    assert code == 0 and f"first-party modules (files under {here}) 3 calls deep" in scope
    code, out = run_cli(["audit", "fixture_follow_app:app"], isatty=True)
    assert out.splitlines()[1] == f"Scope: {scope}"
    code, out = run_cli(["audit", "fixture_follow_pkg.cli:app"], isatty=False)
    assert (
        "first-party modules (fixture_follow_pkg) 3 calls deep" in json.loads(out)["data"]["scope"]
    )


_TWO_PACKAGES = {
    "apppkg/__init__.py": "",
    "apppkg/cli.py": """\
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from libpkg.workdir import enter
from otherlib import hop

from treaty import App, Arg, Ctx

app = App("apppkg", version="1.0.0")


@dataclass(frozen=True, slots=True)
class Args:
    directory: Path = Arg(description="Where to go")


@app.command("go", description="Go", danger_level="safe", exit_codes=(),
             examples=[("Typical call", "apppkg go .")])
def go(args: Args, _ctx: Ctx) -> dict[str, str]:
    enter(args.directory)
    return {"cwd": str(args.directory)}


@app.command("hop", description="Hop", danger_level="safe", exit_codes=(),
             examples=[("Typical call", "apppkg hop .")])
def hop_command(args: Args, _ctx: Ctx) -> dict[str, str]:
    hop(args.directory)
    return {"cwd": str(args.directory)}
""",
    "libpkg/__init__.py": "",
    "libpkg/workdir.py": "import os\n\n\ndef enter(directory):\n    os.chdir(directory)\n",
}
_OTHERLIB = {
    "otherlib/__init__.py": "import os\n\n\ndef hop(directory):\n    os.chdir(directory)\n"
}


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def _dist_info(site: Path, name: str, record: list[str], **extra: str) -> None:
    """``<name>-0.1.dist-info`` in ``site``: what an installer leaves, which
    ``importlib.metadata`` reads from any ``sys.path`` entry"""
    info = site / f"{name}-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: 0.1\n")
    (info / "RECORD").write_text("".join(f"{r},,\n" for r in [*record, f"{info.name}/RECORD"]))
    for file, text in extra.items():
        (info / file).write_text(text)


def _audit_in_child(*path: Path) -> dict[str, Any]:
    """``treaty audit apppkg.cli:app`` in a fresh interpreter with ``path`` as its
    PYTHONPATH, so the distributions there are the only ones it adds"""
    proc = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()"]
        + ["audit", "apppkg.cli:app", "--all", "--format", "json"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, path))},
        cwd=path[0],
        check=False,
    )
    data: dict[str, Any] = json.loads(proc.stdout)["data"]
    return data


def _chdir_findings(data: dict[str, Any]) -> dict[str, str]:
    rule = next(r for r in data["rules"] if r["id"] == "no-chdir")
    return {f["command"]: f["message"] for f in rule["findings"]}


def _expect_both_packages_followed(data: dict[str, Any]) -> None:
    found = _chdir_findings(data)
    assert list(found) == ["go"]  # otherlib, another distribution's, is not followed
    assert found["go"].endswith("; found via libpkg.workdir.enter (libpkg/workdir.py:4)")
    assert "first-party modules (apppkg, libpkg) 3 calls deep" in data["scope"]


def test_first_party_is_every_package_of_the_handlers_distribution(tmp_path: Path) -> None:
    """Issue 67: a wheel that ships apppkg and libpkg is one first-party codebase, while
    otherlib, installed beside it by another distribution, stays out"""
    site = tmp_path / "site-packages"
    _write(site, {**_TWO_PACKAGES, **_OTHERLIB})
    _dist_info(site, "two", [n for n in _TWO_PACKAGES])
    _dist_info(site, "otherlib", list(_OTHERLIB))
    _expect_both_packages_followed(_audit_in_child(site))


def test_first_party_covers_an_editable_install_of_the_distribution(tmp_path: Path) -> None:
    """An editable install records only its .pth file, which names the checkout"""
    project, site = tmp_path / "two", tmp_path / "site-packages"
    _write(project, {**_TWO_PACKAGES, "tests/__init__.py": ""})
    _write(site, {**_OTHERLIB, "two.pth": f"{project}\n"})
    direct_url = json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
    _dist_info(site, "two", ["two.pth"], **{"direct_url.json": direct_url})
    _dist_info(site, "otherlib", list(_OTHERLIB))
    data = _audit_in_child(project, site)
    assert list(_chdir_findings(data)) == ["go"]
    assert "first-party modules (apppkg, libpkg, tests) 3 calls deep" in data["scope"]


def test_without_a_distribution_only_the_handlers_package_is_first_party(
    tmp_path: Path,
) -> None:
    """A checkout on sys.path that nothing installed keeps the handler's package only"""
    _write(tmp_path, {**_TWO_PACKAGES, **_OTHERLIB})
    data = _audit_in_child(tmp_path)
    assert _chdir_findings(data) == {}
    assert "first-party modules (apppkg) 3 calls deep" in data["scope"]


def test_a_local_package_shadowing_an_installed_one_keeps_only_its_own_package(
    tmp_path: Path,
) -> None:
    """Issue 106: a local apppkg ahead of an unrelated installed distribution that also
    ships apppkg is not that distribution's code, so its otherlib is not followed"""
    local, site = tmp_path / "local", tmp_path / "site-packages"
    _write(local, _TWO_PACKAGES)
    shadowed = {"apppkg/__init__.py": "", **_OTHERLIB}
    _write(site, shadowed)
    _dist_info(site, "shadowed", list(shadowed))
    data = _audit_in_child(local, site)
    assert _chdir_findings(data) == {}  # no otherlib.hop through the other distribution
    assert "first-party modules (apppkg) 3 calls deep" in data["scope"]


def test_first_party_covers_an_editable_install_that_hooks_the_import_system(
    tmp_path: Path,
) -> None:
    """An editable install through an import hook names no directory in its .pth; its
    direct_url.json names the project, which holds the handler"""
    project, site = tmp_path / "two", tmp_path / "site-packages"
    _write(project, _TWO_PACKAGES)
    _write(site, {**_OTHERLIB, "__editable__.two.pth": "import __editable___two_finder\n"})
    direct_url = json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
    record = ["__editable__.two.pth", "__editable___two_finder.py"]
    extra = {"direct_url.json": direct_url, "top_level.txt": "apppkg\nlibpkg\n"}
    _dist_info(site, "two", record, **extra)
    _dist_info(site, "otherlib", list(_OTHERLIB))
    _expect_both_packages_followed(_audit_in_child(project, site))


def test_a_malformed_direct_url_record_of_the_owner_stops_the_audit(tmp_path: Path) -> None:
    """The record of the distribution that ships the handler's package decides whether
    the handler is that distribution's: one the audit cannot parse is an error, not a
    silent non-editable"""
    project, site = tmp_path / "two", tmp_path / "site-packages"
    _write(project, _TWO_PACKAGES)
    _write(site, {**_OTHERLIB, "__editable__.two.pth": "import __editable___two_finder\n"})
    record = ["__editable__.two.pth", "__editable___two_finder.py"]
    extra = {"direct_url.json": "{not json", "top_level.txt": "apppkg\nlibpkg\n"}
    _dist_info(site, "two", record, **extra)
    proc = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()"]
        + ["audit", "apppkg.cli:app", "--format", "json"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (project, site)))},
        cwd=project,
        check=False,
    )
    error = json.loads(proc.stdout)["error"]
    assert proc.returncode == 4 and error["code"] == "PRECONDITION"
    assert "direct_url.json of the installed distribution two" in error["message"]
    assert error["context"]["distribution"] == "two"


def test_a_malformed_direct_url_record_of_another_distribution_is_skipped_and_named(
    tmp_path: Path,
) -> None:
    """Looking for an editable install of a local checkout reads every distribution's
    record: a broken, unrelated one is skipped, and the scope names it"""
    local, site = tmp_path / "local", tmp_path / "site-packages"
    _write(local, {**_TWO_PACKAGES, **_OTHERLIB})
    _dist_info(site, "broken", [], **{"direct_url.json": "[]"})
    data = _audit_in_child(local, site)
    assert _chdir_findings(data) == {}
    assert "first-party modules (apppkg) 3 calls deep" in data["scope"]
    assert "direct_url.json is not a PEP 610 record (broken)" in data["scope"]


@dataclass(frozen=True, slots=True)
class Restarted:
    effect: str
    would_affect: Affects | None = None


@dataclass(frozen=True, slots=True)
class RestartArgs:
    dry_run: bool = Flag(default=False, description="Preview the restart")


def _timeout_app(**declared: object) -> dict[str, list[tuple[str | None, str, str]]]:
    """``restart``, destructive, registered with ``declared``; the findings of the two
    timeout rules as (command, message, fix)"""
    from treaty import App, Ctx

    app = App("ops", version="1.0.0")

    @app.command(
        "restart",
        description="Restart the service",
        examples=[("Restart", "ops restart")],
        danger_level="destructive",
        exit_codes=["UNAVAILABLE"],
        **declared,  # type: ignore[arg-type]
    )
    def restart(args: RestartArgs, ctx: Ctx) -> Restarted:
        return Restarted("restarted")

    rules = {r.id: r for r in audit(app, "ops", limit=10).rules}
    return {
        rule: [(f.command, f.message, f.fix) for f in rules[rule].findings]
        for rule in ("explicit-timeout", "timeout-budget")
    }


def test_explicit_timeout_advises_a_destructive_command_on_the_default() -> None:
    [(command, message, fix)] = _timeout_app()["explicit-timeout"]
    assert command == "restart"
    assert message.startswith("a destructive command inherits the app's 60 s default")
    assert "timeout=None" in fix and "timeout=60 keeps the default" in fix


def test_explicit_timeout_is_silent_once_timeout_is_declared_even_at_the_default() -> None:
    assert _timeout_app(timeout=60)["explicit-timeout"] == []
    assert _timeout_app(timeout=None)["explicit-timeout"] == []


def test_explicit_timeout_is_advice_and_skips_safe_commands() -> None:
    from treaty import App, Ctx, NoArgs

    rule = next(r for r in RULES if r.id == "explicit-timeout")
    assert rule.severity.value == "advice"
    app = App("ops", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> NoArgs:
        return args

    assert [r for r in audit(app, "ops", limit=5).rules if r.id == rule.id][0].passed


def test_timeout_budget_flags_retry_waits_over_the_timeout() -> None:
    from treaty import Retry

    # 60 s, then 120 s capped, 19 more times: 2340 s against the inherited 60 s
    retry = Retry(retries=20, delay_ms=60_000, backoff=2.0, max_delay_ms=120_000)
    [(command, message, fix)] = _timeout_app(retry=retry)["timeout-budget"]
    assert command == "restart"
    assert "may wait 2340 s in all, over the app's default 60 s timeout" in message
    assert "gives up before the last retries with UNAVAILABLE" in message
    assert fix.startswith("timeout=2820 on restart")


def test_timeout_budget_counts_jitter_and_an_explicit_timeout() -> None:
    from treaty import Retry

    retry = Retry(retries=3, delay_ms=10_000, jitter=0.5)  # 3 waits of at most 15 s
    [(_, message, fix)] = _timeout_app(retry=retry, timeout=30)["timeout-budget"]
    assert "may wait 45 s in all, over its 30 s timeout" in message
    assert fix.startswith("timeout=60 on restart")


def test_timeout_budget_rounds_the_scaled_waits() -> None:
    from treaty._audit import _headroom

    # 59 s and 60 s of waits scale to 70.8 s and 72 s: both round to a whole minute
    assert (_headroom(45), _headroom(59), _headroom(60)) == (60, 120, 120)


def test_timeout_budget_passes_a_retry_that_fits() -> None:
    from treaty import Retry

    retry = Retry(retries=20, delay_ms=60_000, backoff=2.0, max_delay_ms=120_000)
    assert _timeout_app(retry=retry, timeout=2820)["timeout-budget"] == []
    assert _timeout_app(retry=Retry(retries=3, delay_ms=500))["timeout-budget"] == []
    assert _timeout_app(retry=retry, timeout=None)["timeout-budget"] == []


def test_timeout_budget_flags_a_heartbeat_on_the_default_timeout() -> None:
    [(command, message, fix)] = _timeout_app(heartbeat=True)["timeout-budget"]
    assert command == "restart" and "heartbeat=True" in message and "60 s" in message
    assert "timeout=None" in fix
    assert _timeout_app(heartbeat=True, timeout=3600)["timeout-budget"] == []


def test_the_example_check_of_a_passthrough_command_judges_treatys_part_only() -> None:
    """#306: the words after the path are the tool's; --validate-only goes before the path,
    so the tool never sees it and the handler never runs"""
    import argparse

    from treaty import App, Ctx, NoArgs

    app = App("probe", version="0.1.0")
    ran: list[tuple[str, ...]] = []

    @app.command(
        "ingest",
        description="Hand argv to argparse",
        danger_level="mutating",
        exit_codes=(),
        examples=[
            ("Extract a statement", "probe ingest extract statement.csv"),
            ("A bad timeout", "probe --timeout soon ingest extract statement.csv"),
        ],
        passthrough=True,
    )
    def ingest(args: NoArgs, ctx: Ctx) -> int:
        ran.append(ctx.argv_rest)
        parser = argparse.ArgumentParser(prog="probe ingest")
        parser.add_argument("action", choices=["extract"])
        parser.add_argument("src")
        parser.parse_args(list(ctx.argv_rest))
        return 0

    errors = [
        f for r in audit(app, "x:app", limit=3).rules if r.id == "describe" for f in r.findings
    ]
    assert [f.message.split(" does not parse")[0] for f in errors] == [
        "the example 'probe --timeout soon ingest extract statement.csv'"
    ]
    assert ran == []
