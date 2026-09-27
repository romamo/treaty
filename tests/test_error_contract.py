"""Error contract: REQ-C-014, C-028, C-030, F-063, F-037, F-033, C-007, and REDIRECTED"""

import io
import json
import time
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import spec_validator

from treaty import (
    App,
    Ctx,
    ErrorDetail,
    Exit,
    Flag,
    NetworkContext,
    NoArgs,
    RegistrationError,
    RetryStrategy,
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
