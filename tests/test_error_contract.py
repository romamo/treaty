"""Error contract: REQ-C-014, C-028, C-030, F-063, F-037, F-033, C-007, and REDIRECTED"""

import io
import json
import shlex
import time
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

from treaty import (
    Affects,
    App,
    Arg,
    Ctx,
    ErrorDetail,
    Exit,
    Flag,
    NetworkContext,
    NoArgs,
    RegistrationError,
    RetryStrategy,
    already_exists,
)
from treaty._audit import audit
from treaty._exit import FrameworkCode, framework_entries


def run(app: App, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env=env or {}, isatty=False)
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


@dataclass(frozen=True, slots=True)
class CountArgs:
    n: int = Flag(description="A number")


def limited_app() -> App:
    app = App("rl", version="1.0.0", default_timeout=0.05)

    @app.command("hint", description="Hint", danger_level="safe", exit_codes=["RATE_LIMITED"])
    def hint(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.RATE_LIMITED("slow down", retry_after_ms=1500)

    @app.command("bare", description="Bare", danger_level="safe", exit_codes=["RATE_LIMITED"])
    def bare(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.RATE_LIMITED("slow down")

    @app.command("count", description="Count", danger_level="safe", exit_codes=())
    def count(args: CountArgs, ctx: Ctx) -> dict[str, int]:
        return {"n": args.n}

    @app.command("read", description="Read", danger_level="safe", exit_codes=())
    def read(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        time.sleep(0.5)
        return {}

    @app.command("write", description="Write", danger_level="mutating", exit_codes=())
    def write(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        time.sleep(0.5)
        return {"effect": "updated"}

    return app


# REQ-C-014


def test_every_error_response_includes_error_retryable_as_a_boolean() -> None:
    app = limited_app()
    for argv in (["hint"], ["count", "--n", "x"], ["nope"], ["read"], ["write"]):
        code, envelope = run(app, argv)
        assert code != 0 and isinstance(envelope["error"]["retryable"], bool), argv


def test_a_rate_limited_error_includes_error_retry_after_ms_above_0() -> None:
    code, envelope = run(limited_app(), ["hint"])
    error = envelope["error"]
    assert code == 11 and error["retryable"] is True and error["retry_after_ms"] == 1500
    assert error["retry_strategy"] == "exponential_backoff"
    # Without a delay the raise is the handler's bug, not a retryable failure
    code, envelope = run(limited_app(), ["bare"])
    assert code == 1 and envelope["error"]["code"] == "INVALID_EXIT"
    assert "retry_after_ms" in envelope["error"]["message"]


def test_a_validation_error_has_retryable_false_and_fix_required_present() -> None:
    code, envelope = run(limited_app(), ["count", "--n", "x"])
    error = envelope["error"]
    assert code == 2 and error["phase"] == "validation"
    assert error["retryable"] is False and error["fix_required"]


def test_a_timeout_with_side_effects_none_is_retryable_and_with_partial_is_not() -> None:
    app = limited_app()
    manifest = app.manifest()["commands"]
    read_entry = manifest["read"]["exit_codes"]["10"]
    assert read_entry["side_effects"] == "none" and read_entry["retryable"] is True
    code, envelope = run(app, ["read"])
    assert code == 10 and envelope["error"]["retryable"] is True
    write_entry = (
        manifest["write"].get("exit_codes", {}).get("10") or app.manifest()["exit_codes"]["10"]
    )
    assert write_entry["side_effects"] == "partial" and write_entry["retryable"] is False
    code, envelope = run(app, ["write"])
    assert code == 10 and envelope["error"]["retryable"] is False


def test_the_framework_error_registry_maps_all_standard_error_codes_to_default_retryable() -> None:
    entries = {e.name.value: e for e in framework_entries()}
    assert {c.name for c in FrameworkCode} <= entries.keys()
    assert all(isinstance(e.retryable, bool) for e in entries.values())
    assert entries["RATE_LIMITED"].retry_strategy is RetryStrategy.EXPONENTIAL_BACKOFF
    assert entries["UNAVAILABLE"].retry_after_ms == 1000


def test_exit_code_retry_defaults_fill_the_error_and_the_raise_wins() -> None:
    app = App("q", version="1.0.0")
    app.exit_code(
        "QUOTA",
        80,
        description="Quota",
        retryable=True,
        side_effects="none",
        retry_after_ms=250,
        retry_strategy="linear_backoff",
    )

    @app.command("q", description="Q", danger_level="safe", exit_codes=["QUOTA"])
    def q(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.QUOTA("over quota")

    @app.command("r", description="R", danger_level="safe", exit_codes=["QUOTA"])
    def r(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.QUOTA("over quota", retry_after_ms=9, retry_strategy="immediate")

    _, envelope = run(app, ["q"])
    assert envelope["error"]["retry_after_ms"] == 250
    assert envelope["error"]["retry_strategy"] == "linear_backoff"
    _, envelope = run(app, ["r"])
    assert envelope["error"]["retry_after_ms"] == 9
    assert envelope["error"]["retry_strategy"] == "immediate"
    with pytest.raises(RegistrationError, match="retryable"):
        app.exit_code(
            "GONE", 81, description="Gone", retryable=False, side_effects="none", retry_after_ms=5
        )
    with pytest.raises(RegistrationError, match="retry_strategy"):
        app.exit_code(
            "SOON", 82, description="Soon", retryable=True, side_effects="none", retry_strategy="x"
        )


def test_audit_flags_a_rate_limited_raise_without_retry_after_ms() -> None:
    report = audit(limited_app(), "rl", limit=3)
    [rule] = [r for r in report.rules if r.id == "retry-hint"]
    assert [f.command for f in rule.findings] == ["bare"]
    assert "retry_after_ms=" in rule.findings[0].fix


def test_error_detail_writes_retry_strategy_only_when_set() -> None:
    plain = ErrorDetail(code="X", message="x", retryable=False).to_json()
    assert "retry_strategy" not in plain
    detail = ErrorDetail(
        code="X", message="x", retryable=True, retry_strategy=RetryStrategy.IMMEDIATE
    )
    assert detail.to_json()["retry_strategy"] == "immediate"


# REQ-F-037: the type; ctx.http fills it in workstream 10


def network_error(proxy: str | None) -> dict[str, Any]:
    context = NetworkContext(
        url="https://api.example.com/v1",
        proxy_used=proxy,
        proxy_source=None if proxy is None else "HTTPS_PROXY",
        no_proxy=None,
        ssl_verify=True,
        suggestion="curl -v https://api.example.com/v1",
    )
    detail = ErrorDetail(
        code="UNAVAILABLE", message="connection refused", retryable=True, network_context=context
    )
    return detail.to_json()


def test_a_connection_failure_error_includes_error_network_context_proxy_used() -> None:
    error = network_error("http://user:secret@proxy.corp:3128")
    assert error["network_context"]["proxy_used"] == "http://proxy.corp:3128"
    assert error["network_context"]["proxy_source"] == "HTTPS_PROXY"


def test_when_no_proxy_is_configured_proxy_used_is_null_not_absent() -> None:
    context = network_error(None)["network_context"]
    assert "proxy_used" in context and context["proxy_used"] is None


def test_network_context_suggestion_contains_an_executable_shell_command() -> None:
    assert network_error(None)["network_context"]["suggestion"].startswith("curl ")
    with pytest.raises(RegistrationError):
        NetworkContext(
            url="https://x",
            proxy_used=None,
            proxy_source=None,
            no_proxy=None,
            ssl_verify=True,
            suggestion="",
        )


def test_the_network_context_block_is_absent_for_non_network_errors() -> None:
    _, envelope = run(limited_app(), ["hint"])
    assert "network_context" not in envelope["error"]
    # A handler cannot attach one: Exit takes no such keyword
    app = App("n", version="1.0.0")

    @app.command("n", description="N", danger_level="safe", exit_codes=["UNAVAILABLE"])
    def n(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.UNAVAILABLE("down", network_context={"proxy_used": None})

    envelope = app.call("n", {}, env={})
    assert envelope.error is not None and envelope.error.network_context is None


# REQ-C-028


@dataclass(frozen=True, slots=True)
class NameArgs:
    name: str = Arg(description="Resource name")


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    size: int


@dataclass(frozen=True, slots=True)
class Created:
    effect: str
    resource: Resource


@dataclass(frozen=True, slots=True)
class DeleteArgs:
    name: str = Arg(description="Resource name")
    dry_run: bool = Flag(default=False, description="Preview")


@dataclass(frozen=True, slots=True)
class Removed:
    effect: str
    status: str
    would_affect: Affects | None = None


def resource_app() -> App:
    store: dict[str, Resource] = {}
    app = App("res", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=["NOT_FOUND"])
    def get(args: NameArgs, ctx: Ctx) -> Resource:
        if args.name not in store:
            raise Exit.NOT_FOUND(f"no {args.name}")
        return store[args.name]

    @app.command("create", description="Create", danger_level="mutating", exit_codes=["CONFLICT"])
    def create(args: NameArgs, ctx: Ctx) -> Created:
        if args.name in store:
            raise already_exists(Created("noop", store[args.name]), conflict_id=args.name)
        store[args.name] = Resource(args.name, 3)
        return Created("created", store[args.name])

    @app.command("delete", description="Delete", danger_level="destructive", exit_codes=())
    def delete(args: DeleteArgs, ctx: Ctx) -> Removed:
        if args.dry_run:
            return Removed("would_delete", "found", Affects("Deletes it", (args.name,), 1))
        if store.pop(args.name, None) is None:
            return Removed("noop", "not_found")
        return Removed("deleted", "deleted")

    return app


def test_create_called_twice_returns_the_resource_on_both_calls() -> None:
    app = resource_app()
    _, first = run(app, ["create", "foo"])
    _, second = run(app, ["create", "foo"])
    assert first["data"]["resource"] == second["data"]["resource"] == {"id": "foo", "size": 3}


def test_second_call_is_ok_false_exit_6_already_exists_with_the_existing_resource() -> None:
    app = resource_app()
    run(app, ["create", "foo"])
    code, envelope = run(app, ["create", "foo"])
    error = envelope["error"]
    assert code == 6 and envelope["ok"] is False and error["code"] == "ALREADY_EXISTS"
    assert error["retryable"] is False and error["conflict_id"] == "foo"
    assert envelope["data"]["resource"] == {"id": "foo", "size": 3}


def test_agent_can_use_data_from_the_second_call_without_a_follow_up_get() -> None:
    app = resource_app()
    run(app, ["create", "foo"])
    _, second = run(app, ["create", "foo"])
    _, got = run(app, ["get", "foo"])
    assert second["data"]["resource"] == got["data"]


def test_delete_of_a_non_existent_resource_exits_0_with_not_found() -> None:
    code, envelope = run(resource_app(), ["delete", "ghost", "--confirm-destructive"])
    assert code == 0 and envelope["ok"] is True
    data = envelope["data"]
    assert data["effect"] == "noop" and data["status"] == "not_found"


def test_conflict_6_is_declared_with_a_description_naming_already_exists() -> None:
    entry = resource_app().manifest()["commands"]["create"]["exit_codes"]["6"]
    assert entry["name"] == "CONFLICT" and "already exists" in entry["description"]


def test_audit_asks_creates_for_conflict_and_deletes_to_drop_not_found() -> None:
    app = App("aud", version="1.0.0")

    @app.command("add", description="Add", danger_level="mutating", exit_codes=())
    def make(args: NameArgs, ctx: Ctx) -> Created:
        return Created("created", Resource(args.name, 1))

    @app.command("drop", description="Drop", danger_level="destructive", exit_codes=["NOT_FOUND"])
    def drop(args: DeleteArgs, ctx: Ctx) -> Removed:
        return Removed("deleted", "deleted")

    rules = {r.id: r for r in audit(app, "aud", limit=3).rules}
    assert [f.command for f in rules["already-exists"].findings] == ["add"]
    assert "treaty.already_exists(" in rules["already-exists"].findings[0].fix
    assert [f.command for f in rules["delete-not-found"].findings] == ["drop"]
    assert '"not_found"' in rules["delete-not-found"].findings[0].fix
    # Declaring CONFLICT on the create answers the first rule
    assert not [f for f in audit(resource_app(), "res", limit=3).rules if f.id == "already-exists"][
        0
    ].findings


# REQ-C-030


@dataclass(frozen=True, slots=True)
class Setup:
    effect: str


def store_app(fixes: dict[str, str] | None = None) -> tuple[App, list[str]]:
    """``list`` needs the store ``init`` makes; ``wipe`` is destructive"""
    made: list[str] = []
    app = App("st", version="1.0.0", companions=("mkdir",))

    @app.command(
        "list",
        description="List",
        danger_level="safe",
        exit_codes=["PRECONDITION"],
        fix_commands=fixes if fixes is not None else {"STORE_MISSING": "st init"},
    )
    def list_(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        if not made:
            raise Exit.PRECONDITION(
                "no store", code="STORE_MISSING", fix_required="the store must exist"
            )
        return {"items": 0}

    @app.command("init", description="Init", danger_level="mutating", exit_codes=())
    def init(args: NoArgs, ctx: Ctx) -> Setup:
        if made:
            return Setup("noop")
        made.append("store")
        return Setup("created")

    @app.command("wipe", description="Wipe", danger_level="destructive", exit_codes=())
    def wipe(args: DeleteArgs, ctx: Ctx) -> Removed:
        return Removed("deleted", "deleted")

    @app.command("locked", description="Locked", danger_level="safe", exit_codes=["PRECONDITION"])
    def locked(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        raise Exit.PRECONDITION("read-only", fix_required="make the store writable")

    return app, made


def run_fix(app: App, fix: str) -> tuple[int, dict[str, Any]]:
    words = shlex.split(fix)
    assert words[0] == app.name
    return run(app, words[1:])


def test_a_caller_correctable_error_with_a_single_safe_fix_includes_error_fix_command() -> None:
    code, envelope = run(store_app()[0], ["list"])
    error = envelope["error"]
    assert code == 4 and error["retryable"] is False and error["fix_required"]
    assert error["fix_command"] == "st init"


def test_error_fix_command_runs_successfully_as_is_with_no_edits() -> None:
    app, _ = store_app()
    _, envelope = run(app, ["list"])
    code, _ = run_fix(app, envelope["error"]["fix_command"])
    assert code == 0


def test_running_error_fix_command_twice_in_a_row_produces_no_additional_side_effects() -> None:
    app, made = store_app()
    _, envelope = run(app, ["list"])
    fix = envelope["error"]["fix_command"]
    _, first = run_fix(app, fix)
    _, second = run_fix(app, fix)
    assert first["data"]["effect"] == "created" and second["data"]["effect"] == "noop"
    assert made == ["store"]


def test_registering_a_fix_command_that_invokes_a_destructive_command_raises() -> None:
    app, _ = store_app({"STORE_MISSING": "st wipe"})
    with pytest.raises(RegistrationError, match="destructive"):
        app.manifest()
    with pytest.raises(RegistrationError, match="destructive"):
        app.run(["list"], stdout=io.StringIO(), stderr=io.StringIO(), env={}, isatty=False)
    app, _ = store_app({"STORE_MISSING": "st nothing"})
    with pytest.raises(RegistrationError, match="names no command"):
        app.manifest()


@pytest.mark.parametrize(
    "fix",
    [
        "st init --token <your-token>",
        "st init > log",
        "st init --project $PROJECT_ID",
        "st init | cat",
        "st init; st list",
        "rm -rf /tmp/x",
        "st 'init",
    ],
)
def test_registering_a_fix_command_with_placeholder_patterns_raises(fix: str) -> None:
    with pytest.raises(RegistrationError):
        store_app({"STORE_MISSING": fix})


def test_after_error_fix_command_exits_0_the_reissue_does_not_fail_with_the_same_code() -> None:
    app, _ = store_app()
    _, envelope = run(app, ["list"])
    code, _ = run_fix(app, envelope["error"]["fix_command"])
    assert code == 0
    code, envelope = run(app, ["list"])
    assert code == 0 and envelope["ok"] is True


def test_when_no_safe_single_command_fix_exists_fix_command_is_absent() -> None:
    _, envelope = run(store_app()[0], ["locked"])
    error = envelope["error"]
    assert "fix_command" not in error and error["fix_required"] == "make the store writable"


def test_a_fix_command_raised_at_run_time_is_checked_too() -> None:
    app = App("rt", version="1.0.0", companions=("mkdir",))
    fixes: list[str] = []

    @app.command("go", description="Go", danger_level="safe", exit_codes=["PRECONDITION"])
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        raise Exit.PRECONDITION("no dir", fix_required="make it", fix_command=fixes[-1])

    for fix, expected in (("mkdir -p tmp/x", "PRECONDITION"), ("curl http://x", "INVALID_EXIT")):
        fixes.append(fix)
        assert run(app, ["go"])[1]["error"]["code"] == expected


def test_audit_asks_to_declare_a_fixed_fix_command() -> None:
    app = App("fx", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=["PRECONDITION"])
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        raise Exit.PRECONDITION("no store", code="STORE_MISSING", fix_command="fx init")

    [rule] = [r for r in audit(app, "fx", limit=3).rules if r.id == "fix-declared"]
    assert [f.fix for f in rule.findings] == [
        "fix_commands={'STORE_MISSING': 'fx init'}, and drop fix_command= from the raise"
    ]
