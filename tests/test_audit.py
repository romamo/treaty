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
