"""Argument grammar: REQ-C-020, C-026, C-027, F-049, F-059, F-067, F-075, O-006, O-009"""

import functools
import io
import json
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError
from treaty._audit import audit


def run(
    app: App, argv: list[str], *, stdin: str | None = None, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = app.run(
        argv,
        stdin=None if stdin is None else io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)
    return code, envelope


def findings(app: App, rule: str) -> list[Any]:
    [result] = [r for r in audit(app, "t", limit=3).rules if r.id == rule]
    return list(result.findings)


# REQ-C-020


@dataclass(frozen=True, slots=True)
class DeployArgs:
    cluster_id: str = Arg(description="Target cluster", pattern_type="alphanumeric_id")
    version: str = Flag(default="1.0.0", description="Version", pattern_type="semver")
    ticket: str | None = Flag(default=None, description="Change ticket", pattern="[a-z0-9-]{3,64}")


def id_app() -> App:
    app = App("ids", version="1.0.0")

    @app.command("deploy", description="Deploy", danger_level="safe", exit_codes=())
    def deploy(args: DeployArgs, ctx: Ctx) -> dict[str, str]:
        return {"cluster": args.cluster_id}

    return app


@pytest.mark.parametrize("bad", ["prod/east", "prod.east", "prod?x", "prod#x", "prod%2F"])
def test_an_alphanumeric_id_argument_rejects_slash_dot_question_hash_and_percent(bad: str) -> None:
    code, envelope = run(id_app(), ["deploy", bad])
    assert code == 2 and envelope["error"]["context"]["pattern_type"] == "alphanumeric_id"
    assert run(id_app(), ["deploy", "prod-east_1"])[0] == 0


def test_a_custom_regex_argument_rejects_inputs_that_do_not_match() -> None:
    code, envelope = run(id_app(), ["deploy", "prod", "--ticket", "AB"])
    assert code == 2 and envelope["error"]["context"]["pattern"] == "[a-z0-9-]{3,64}"
    assert run(id_app(), ["deploy", "prod", "--ticket", "chg-42"])[0] == 0


def test_a_resource_id_field_with_no_pattern_triggers_a_registration_warning() -> None:
    @dataclass(frozen=True, slots=True)
    class Loose:
        user_id: str = Flag(description="User")
        name: str = Flag(default="x", description="Name")

    app = App("loose", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: Loose, ctx: Ctx) -> dict[str, str]:
        return {}

    [finding] = findings(app, "id-pattern")
    assert finding.severity == "warning" and 'pattern_type="alphanumeric_id"' in finding.fix
    assert findings(id_app(), "id-pattern") == []


def test_pattern_validation_failures_exit_2_naming_the_argument_and_the_pattern() -> None:
    code, envelope = run(id_app(), ["deploy", "prod", "--version", "not-semver"])
    context = envelope["error"]["context"]
    assert code == 2 and context["flag"] == "version" and context["pattern_type"] == "semver"
    assert context["pattern"].startswith("^") and "version" in envelope["error"]["message"]


def test_pattern_type_is_listed_in_the_manifest_and_checked_on_json_input() -> None:
    app = id_app()
    entry = app.manifest()["commands"]["deploy"]
    assert entry["flags"]["cluster-id"]["pattern_type"] == "alphanumeric_id"
    assert entry["flags"]["version"]["pattern_type"] == "semver"
    envelope = app.call("deploy", {"cluster_id": "a/b"}, env={})
    assert envelope.exit_code == 2


def test_pattern_type_is_refused_where_it_cannot_apply() -> None:
    with pytest.raises(RegistrationError, match="mutually exclusive"):
        Flag(description="x", pattern="a", pattern_type="uuid")
    with pytest.raises(RegistrationError, match="not one of"):
        Flag(description="x", pattern_type="filepath")

    @dataclass(frozen=True, slots=True)
    class Counted:
        n: int = Flag(description="N", pattern_type="uuid")

    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="pattern_type is for str fields"):

        @app.command("n", description="N", danger_level="safe", exit_codes=())
        def n(args: Counted, ctx: Ctx) -> dict[str, str]:
            return {}


# REQ-F-049: treaty's handlers are sync by design (04-D4), so async is what it refuses


def test_an_async_handler_produces_a_framework_registration_error() -> None:
    app = App("aio", version="1.0.0")
    with pytest.raises(RegistrationError, match="async def"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}

    async def later() -> None:
        return None

    with pytest.raises(RegistrationError, match="cleanup: is async def"):

        @app.command("c", description="C", danger_level="safe", exit_codes=(), cleanup=later)
        def c(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}

    class Conn:
        @classmethod
        async def acquire(cls, args: object, ctx: Ctx) -> Conn:
            return cls()

    with pytest.raises(RegistrationError, match="acquire: is async def"):

        @app.command("r", description="R", danger_level="safe", exit_codes=())
        def r(args: NoArgs, ctx: Ctx, conn: Conn) -> dict[str, str]:
            return {}


def sync_signature(
    fn: Callable[[NoArgs, Ctx], Coroutine[None, None, dict[str, str]]],
) -> Callable[[NoArgs, Ctx], dict[str, str]]:
    """A decorator that hides a coroutine function behind a plain one"""

    @functools.wraps(fn)
    def wrapper(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return fn(args, ctx)  # type: ignore[return-value]

    return wrapper


def test_an_async_operation_not_awaited_by_a_handler_is_detected() -> None:
    ran: list[bool] = []
    app = App("aio", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    @sync_signature
    async def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ran.append(True)
        return {}

    code, envelope = run(app, ["go"])
    assert code == 1 and envelope["error"]["code"] == "HANDLER_CRASHED"
    assert "awaitable" in envelope["error"]["message"] and ran == []


@needs_posix_signals
def test_the_process_exits_only_after_its_teardown_hooks_have_resolved() -> None:
    out = io.StringIO()
    seen: list[str] = []
    app = App("td", version="1.0.0", default_timeout=None)

    @app.command(
        "go",
        description="Go",
        danger_level="safe",
        exit_codes=(),
        cleanup=lambda: seen.append(out.getvalue()),
    )
    def go(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        signal.raise_signal(signal.SIGTERM)
        return {}

    code = app.run(["go"], stdout=out, stderr=io.StringIO(), env={}, isatty=False)
    assert code == 143 and seen == [""] and json.loads(out.getvalue())["meta"]["exit_code"] == 143


def test_a_command_that_performs_io_completes_all_writes_before_exiting(tmp_path: Any) -> None:
    target = tmp_path / "out.txt"
    app = App("w", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        with target.open("w") as fh:
            for n in range(1000):
                fh.write(f"{n}\n")
        return {"lines": 1000}

    code, envelope = run(app, ["go"])
    assert code == 0 and len(target.read_text().splitlines()) == envelope["data"]["lines"]
