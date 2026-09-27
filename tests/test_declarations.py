"""Required declarations and the destructive-command contract (REQ-C-001, REQ-C-002,
REQ-C-004, REQ-C-012, REQ-O-021, REQ-O-048)."""

import io
import json
import urllib.request
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import Affects, App, Arg, Ctx, Flag, NoArgs, RegistrationError
from treaty._audit import untimed_network_calls
from treaty._profile import probes_for

APPLIED: list[str] = []


@dataclass(frozen=True, slots=True)
class Purge:
    bucket: str = Arg(description="Bucket name")
    dry_run: bool = Flag(default=False, description="Preview only")


@dataclass(frozen=True, slots=True)
class Purged:
    effect: str
    bucket: str
    would_affect: Affects | None = None


def make_app(*, safe_default: bool = False, affects: bool = True) -> App:
    app = App("store", version="1")

    @app.command(
        "purge",
        description="Delete every object in a bucket",
        danger_level="destructive",
        exit_codes=(),
        safe_default=safe_default,
        examples=[("Preview a purge", "store purge logs")],
    )
    def purge(args: Purge, ctx: Ctx) -> Purged:
        if args.dry_run:
            summary = Affects(f"Deletes 3 objects from {args.bucket}", ("a", "b", "c"), 3)
            return Purged("would_delete", args.bucket, summary if affects else None)
        APPLIED.append(args.bucket)
        return Purged("deleted", args.bucket)

    @app.command("ls", description="List buckets", danger_level="safe", exit_codes=())
    def ls(args: NoArgs, ctx: Ctx) -> list[str]:
        return ["logs"]

    return app


def run(app: App, argv: list[str], stdin: str = "") -> tuple[int, dict[str, object]]:
    APPLIED.clear()
    out = io.StringIO()
    code = app.run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue().splitlines()[-1])


def meta_of(envelope: dict[str, object]) -> dict[str, object]:
    meta = envelope["meta"]
    assert isinstance(meta, dict)
    return meta


# REQ-C-001, REQ-C-002: both declarations are required


def test_command_without_exit_codes_and_danger_level_fails_registration() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError) as info:

        @app.command("x", description="x")
        def x(args: NoArgs, ctx: Ctx) -> None:
            return None

    assert "exit_codes=()" in str(info.value) and 'danger_level="safe"' in str(info.value)
    assert str(info.value).startswith("x:")


def test_each_missing_declaration_is_named() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match=r'add danger_level="safe"'):
        app.command("x", description="x", exit_codes=())
    with pytest.raises(RegistrationError, match=r"add exit_codes=\(\), or"):
        app.command("x", description="x", danger_level="safe")


def test_schema_lists_success_and_danger_level() -> None:
    code, env = run(make_app(), ["ls", "--schema"])
    data = env["data"]
    assert isinstance(data, dict)
    assert code == 0 and "0" in data["exit_codes"] and data["danger_level"] == "safe"


# REQ-C-004: would_affect on destructive dry runs


def test_destructive_output_without_would_affect_fails_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Bare:
        effect: str

    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="would_affect"):

        @app.command("rm", description="rm", danger_level="destructive", exit_codes=())
        def rm(args: Purge, ctx: Ctx) -> Bare:
            return Bare("deleted")


def test_dry_run_carries_would_affect() -> None:
    code, env = run(make_app(), ["purge", "logs", "--dry-run"])
    data = env["data"]
    assert code == 0 and isinstance(data, dict)
    assert data["effect"] == "would_delete"
    assert data["would_affect"] == {
        "summary": "Deletes 3 objects from logs",
        "resources": ["a", "b", "c"],
        "count": 3,
    }


def test_dry_run_without_would_affect_is_invalid_effect() -> None:
    code, env = run(make_app(affects=False), ["purge", "logs", "--dry-run"])
    error = env["error"]
    assert code == 1 and isinstance(error, dict) and error["code"] == "INVALID_EFFECT"
    assert "would_affect" in str(error["message"])


# REQ-O-021: the confirmation gate lists what would be affected


def test_unconfirmed_run_names_what_would_be_affected() -> None:
    code, env = run(make_app(), ["purge", "logs"])
    error = env["error"]
    assert code == 2 and isinstance(error, dict) and error["code"] == "CONFIRMATION_REQUIRED"
    assert error["message"].endswith("it would: Deletes 3 objects from logs.")  # type: ignore[union-attr]
    assert APPLIED == []


def test_schema_of_a_destructive_command_requires_confirmation() -> None:
    _, env = run(make_app(), ["purge", "--schema"])
    assert env["data"]["requires_confirmation"] is True  # type: ignore[index]
    _, env = run(make_app(), ["ls", "--schema"])
    assert "requires_confirmation" not in env["data"]  # type: ignore[operator]


# REQ-O-048: safe_default


def test_safe_default_without_live_is_a_dry_run_that_exits_0() -> None:
    code, env = run(make_app(safe_default=True), ["purge", "logs"])
    assert code == 0 and env["data"]["effect"] == "would_delete"  # type: ignore[index]
    assert meta_of(env)["dry_run"] is True and "confirmed" not in meta_of(env)
    assert APPLIED == []


def test_safe_default_live_and_confirmed_applies() -> None:
    code, env = run(
        make_app(safe_default=True), ["purge", "logs", "--live", "--confirm-destructive"]
    )
    assert code == 0 and env["data"]["effect"] == "deleted"  # type: ignore[index]
    assert meta_of(env)["dry_run"] is False and meta_of(env)["confirmed"] is True
    assert APPLIED == ["logs"]


def test_safe_default_live_alone_is_the_confirmation() -> None:
    code, env = run(make_app(safe_default=True), ["purge", "logs", "--live"])
    assert code == 0 and env["data"]["effect"] == "deleted"  # type: ignore[index]
    assert meta_of(env)["dry_run"] is False and meta_of(env)["confirmed"] is True
    assert APPLIED == ["logs"]
    _, env = run(make_app(safe_default=True), ["purge", "--schema"])
    assert env["data"]["requires_confirmation"] is True  # type: ignore[index]


def test_safe_default_dry_run_flag_wins_over_live() -> None:
    code, env = run(make_app(safe_default=True), ["purge", "logs", "--live", "--dry-run"])
    assert code == 0 and meta_of(env)["dry_run"] is True and APPLIED == []


def test_safe_default_errors_carry_dry_run_too() -> None:
    app = make_app(safe_default=True)
    code, env = run(
        app, ["purge", "logs", "--live", "--confirm-destructive", "--idempotency-key", "k"]
    )
    assert code == 4 and env["error"]["code"] == "STATE_DIR_UNKNOWN"  # type: ignore[index]
    assert meta_of(env)["dry_run"] is False and APPLIED == []


def test_safe_default_through_exec_and_app_call() -> None:
    line = json.dumps({"_cmd": "purge", "bucket": "logs"})
    code, env = run(make_app(safe_default=True), ["exec"], stdin=line + "\n")
    assert code == 0 and meta_of(env)["dry_run"] is True
    envelope = make_app(safe_default=True).call(
        "purge", {"bucket": "logs", "live": True, "confirm_destructive": True}, env={}
    )
    assert envelope.exit_code == 0 and envelope.extra_meta["confirmed"] is True


def test_safe_default_argument_errors_carry_dry_run() -> None:
    code, env = run(make_app(safe_default=True), ["purge", "--bogus"])
    assert code == 2 and meta_of(env)["dry_run"] is True
    envelope = make_app(safe_default=True).call("purge", {"bogus": 1}, env={})
    assert envelope.exit_code == 2 and envelope.extra_meta["dry_run"] is True


def test_live_is_unknown_on_other_commands() -> None:
    code, env = run(make_app(), ["purge", "logs", "--live"])
    assert code == 2 and env["error"]["context"]["flag"] == "live"  # type: ignore[index]


def test_safe_default_on_a_non_destructive_command_fails_registration() -> None:
    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="safe_default"):

        @app.command(
            "x", description="x", danger_level="mutating", exit_codes=(), safe_default=True
        )
        def x(args: Purge, ctx: Ctx) -> Purged:
            return Purged("updated", args.bucket)


def test_manifest_shows_safe_default_and_live_and_validates() -> None:
    manifest = make_app(safe_default=True).manifest()
    entry = manifest["commands"]["purge"]  # type: ignore[index]
    assert entry["safe_default"] is True and entry["flags"]["live"]["type"] == "boolean"
    validator = spec_validator("manifest-response")
    assert not list(validator.iter_errors(manifest))


def test_conformance_probe_for_safe_default_uses_live() -> None:
    probes = {p.name: p for p in probes_for(make_app(safe_default=True))}
    assert probes["purge"].argv == ("purge", "logs", "--live")
    assert probes["purge"].kind == "destructive"


# REQ-C-012: the network-timeout audit rule


def test_audit_flags_network_calls_without_timeout() -> None:
    def fetch(url: str) -> None:
        urllib.request.urlopen(url)
        urllib.request.urlopen(url, timeout=5)

    assert untimed_network_calls(fetch) == ["urllib.request.urlopen"]


def test_audit_network_timeout_matches_request_verbs_only() -> None:
    """Clients, sessions, and exception classes of requests and httpx take no request"""

    def fetch(url: str) -> None:
        requests.Session()  # noqa: F821
        httpx.Client(base_url=url)  # noqa: F821
        requests.exceptions.HTTPError(url)  # noqa: F821
        loop.create_connection(url)  # noqa: F821
        httpx.post(url)  # noqa: F821
        socket.create_connection((url, 80))  # noqa: F821
        http.client.HTTPSConnection(url)  # noqa: F821

    assert untimed_network_calls(fetch) == [
        "httpx.post",
        "socket.create_connection",
        "http.client.HTTPSConnection",
    ]


def test_audit_rule_only_applies_to_network_commands() -> None:
    from treaty._audit import audit

    app = App("x", version="1")

    @app.command("get", description="Get", danger_level="safe", exit_codes=(), has_network_io=True)
    def get(args: NoArgs, ctx: Ctx) -> None:
        urllib.request.urlopen("https://example.com")

    @app.command("ok", description="Ok", danger_level="safe", exit_codes=(), has_network_io=True)
    def ok(args: NoArgs, ctx: Ctx) -> None:
        urllib.request.urlopen("https://example.com", timeout=ctx.timeout.seconds)

    report = audit(app, "x:app", limit=3)
    rule = next(r for r in report.rules if r.id == "network-timeout")
    assert [f.command for f in rule.findings] == ["get"]
    assert "timeout=ctx.timeout.seconds" in rule.findings[0].fix
