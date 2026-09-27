"""Multi-step commands: step manifest, --resume-from, --rollback-on-failure
(REQ-C-008, REQ-O-010, REQ-O-011)"""

import io
import json
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import App, CommandPath, Ctx, Exit, Flag, NoArgs, RegistrationError
from treaty._audit import audit
from treaty._manifest import payload_schema
from treaty._profile import probes_for
from treaty._steps import StepName

STEPS = ("backup", "apply_schema", "migrate_data", "verify")


def run(app: App, argv: list[str]) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env={}, isatty=False)
    *_, last = out.getvalue().splitlines()
    envelope = json.loads(last)
    spec_validator("response-envelope").validate(envelope)
    return code, envelope, err.getvalue()


@dataclass(frozen=True, slots=True)
class MigrateArgs:
    fail_at: str = Flag(default="", description="Step that raises")
    how: Literal["exit", "crash", "sleep", "signal"] = Flag(
        default="exit", description="How the failing step fails"
    )


@dataclass(frozen=True, slots=True)
class Migrated:
    effect: str
    rows: int


def migrate_app(done: list[str], *, rollback_fails: bool = False) -> App:
    app = App("mig", version="1.0.0", default_timeout=None)
    app.exit_code("DISK_FULL", 80, description="Disk full", retryable=False, side_effects="partial")

    def undo(args: MigrateArgs, ctx: Ctx, completed: tuple[StepName, ...]) -> None:
        done.append("rollback " + " ".join(s.value for s in completed))
        if rollback_fails:
            raise Exit.DISK_FULL("no room to restore the backup")

    @app.command(
        "migrate",
        description="Migrate the database",
        danger_level="mutating",
        exit_codes=("DISK_FULL",),
        has_network_io=True,
        steps=STEPS,
        resumable=True,
        rollback=undo,
    )
    def migrate(args: MigrateArgs, ctx: Ctx) -> Migrated:
        rows = 0
        for name in STEPS:
            if not ctx.step(name):
                continue
            if name == args.fail_at:
                if args.how == "exit":
                    raise Exit.DISK_FULL("Disk full during data migration")
                if args.how == "crash":
                    raise RuntimeError("boom")
                if args.how == "signal":
                    signal.raise_signal(signal.SIGTERM)
                time.sleep(30)
            done.append(name)
            rows += 1
        return Migrated(effect="updated" if rows else "noop", rows=rows)

    return app


# REQ-C-008


def test_a_multi_step_commands_schema_output_includes_steps_listing_all_step_names() -> None:
    out = io.StringIO()
    migrate_app([]).run(["migrate", "--schema"], stdout=out, env={})
    data = json.loads(out.getvalue())["data"]
    assert data["steps"] == list(STEPS)
    spec_validator("manifest-response").validate(migrate_app([]).manifest())
    assert migrate_app([]).manifest()["commands"]["migrate"]["steps"] == list(STEPS)


def test_a_partial_failure_response_includes_completed_steps_as_an_array_of_completed_step_names() -> (  # noqa: E501
    None
):
    code, envelope, _ = run(migrate_app([]), ["migrate", "--fail-at", "migrate_data"])
    assert code == 3 and envelope["data"]["completed_steps"] == ["backup", "apply_schema"]


def test_a_partial_failure_response_includes_failed_step_as_the_name_of_the_failed_step() -> None:
    code, envelope, _ = run(migrate_app([]), ["migrate", "--fail-at", "migrate_data"])
    data = envelope["data"]
    assert code == 3 and data["failed_step"] == "migrate_data"
    assert data["skipped_steps"] == ["verify"] and data["partial"] is True
    assert data["resume_from"] == "migrate_data"
    # 06-D2: the handler's error code stays, the exit is PARTIAL_FAILURE
    assert envelope["error"]["code"] == "DISK_FULL" and envelope["meta"]["exit_code"] == 3
    assert envelope["error"]["retryable"] is False


@needs_posix_signals
def test_the_sigterm_timeout_response_includes_the_same_step_tracking_fields() -> None:
    code, envelope, _ = run(
        migrate_app([]), ["migrate", "--fail-at", "apply_schema", "--how", "signal"]
    )
    assert code == 143 and envelope["error"]["code"] == "CANCELLED"
    data = envelope["data"]
    assert data["completed_steps"] == ["backup"] and data["failed_step"] == "apply_schema"
    assert data["skipped_steps"] == ["migrate_data", "verify"]
    code, envelope, _ = run(
        migrate_app([]),
        ["migrate", "--fail-at", "migrate_data", "--how", "sleep", "--timeout", "0.3"],
    )
    assert code == 10 and envelope["error"]["code"] == "TIMEOUT"
    assert envelope["data"]["completed_steps"] == ["backup", "apply_schema"]
    assert envelope["data"]["failed_step"] == "migrate_data"


def test_a_complete_run_lists_every_step_completed_and_none_failed() -> None:
    code, envelope, err = run(migrate_app([]), ["migrate"])
    assert code == 0
    assert envelope["data"] == {
        "effect": "updated",
        "rows": 4,
        "completed_steps": list(STEPS),
        "failed_step": None,
        "skipped_steps": [],
    }
    events = [json.loads(line) for line in err.splitlines()]
    assert [(e["message"], e["fields"]["step"]) for e in events[:3]] == [
        ("step started", "backup"),
        ("step completed", "backup"),
        ("step started", "apply_schema"),
    ]
    assert events[-1]["fields"] == {"step": "verify", "index": 4, "total": 4}


def test_a_failure_before_any_step_completed_keeps_its_exit() -> None:
    code, envelope, _ = run(migrate_app([]), ["migrate", "--fail-at", "backup", "--how", "crash"])
    assert code == 1 and envelope["error"]["code"] == "HANDLER_CRASHED"
    assert envelope["data"]["partial"] is False and envelope["data"]["failed_step"] == "backup"


def test_heartbeat_lines_name_the_step_in_progress() -> None:
    app = App("hb", version="1.0.0")

    @app.command(
        "go", description="Go", danger_level="safe", exit_codes=(), heartbeat=True, steps=["wait"]
    )
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        ctx.step("wait")
        time.sleep(0.3)
        return {}

    out = io.StringIO()
    assert app.run(["go", "--heartbeat-ms", "50"], stdout=out, stderr=io.StringIO(), env={}) == 0
    *beats, _ = [json.loads(line) for line in out.getvalue().splitlines()]
    assert beats and all(b["step"] == "wait" for b in beats)


def test_ctx_step_out_of_order_is_invalid_step() -> None:
    app = App("ord", version="1.0.0")

    @app.command("go", description="Go", danger_level="safe", exit_codes=(), steps=["a", "b"])
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        ctx.step("b")
        ctx.step("a")
        return {}

    code, envelope, _ = run(app, ["go"])
    assert code == 1 and envelope["error"]["code"] == "INVALID_STEP"
    assert envelope["data"]["completed_steps"] == [] and envelope["data"]["failed_step"] == "b"


@pytest.mark.parametrize(
    ("meta", "match"),
    [
        ({"steps": ["Backup"]}, "step name"),
        ({"steps": ["a", "a"]}, "twice"),
        ({"resumable": True}, "declare steps"),
        ({"rollback": lambda a, c, s: None}, "declare steps"),
    ],
)
def test_step_declarations_are_checked_at_registration(meta: dict[str, Any], match: str) -> None:
    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("go", description="Go", danger_level="safe", exit_codes=(), **meta)
        def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            return {}


def test_a_stream_cannot_declare_steps() -> None:
    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="drop steps"):

        @app.command(
            "go", description="Go", danger_level="safe", exit_codes=(), steps=["a"], streaming=True
        )
        def go(args: NoArgs, ctx: Ctx) -> Iterator[dict[str, int]]:
            yield {}


def test_a_ctx_step_the_command_does_not_declare_fails_registration() -> None:
    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="'verify', which is not one of steps"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=(), steps=["a"])
        def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            ctx.step("verify")
            return {}

    with pytest.raises(RegistrationError, match="declare steps"):

        @app.command("other", description="Other", danger_level="safe", exit_codes=())
        def other(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            ctx.step("a")
            return {}


def test_output_fields_named_like_step_fields_fail_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Clash:
        completed_steps: int

    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="completed_steps"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=(), steps=["a"])
        def go(args: NoArgs, ctx: Ctx) -> Clash:
            return Clash(1)


# REQ-O-010


def test_resume_from_step_3_begins_execution_at_step_3_having_skipped_steps_1_and_2() -> None:
    done: list[str] = []
    code, envelope, _ = run(migrate_app(done), ["migrate", "--resume-from", "migrate_data"])
    assert code == 0 and done == ["migrate_data", "verify"]
    assert envelope["data"]["completed_steps"] == ["migrate_data", "verify"]


def test_skipped_steps_appear_in_the_responses_skipped_steps_array() -> None:
    _, envelope, _ = run(migrate_app([]), ["migrate", "--resume-from", "migrate_data"])
    assert envelope["data"]["skipped_steps"] == ["backup", "apply_schema"]
    assert envelope["data"]["failed_step"] is None


def test_the_response_effect_correctly_reflects_only_the_work_done_during_the_resumed_execution() -> (  # noqa: E501
    None
):
    _, envelope, _ = run(migrate_app([]), ["migrate", "--resume-from", "verify"])
    assert envelope["data"]["rows"] == 1 and envelope["data"]["effect"] == "updated"


def test_passing_an_invalid_step_name_to_resume_from_exits_2_with_a_validation_error() -> None:
    done: list[str] = []
    code, envelope, _ = run(migrate_app(done), ["migrate", "--resume-from", "step-9"])
    assert code == 2 and done == []
    assert envelope["error"]["context"]["available"] == list(STEPS)
    assert envelope["error"]["phase"] == "validation"


def test_resume_from_is_an_enum_of_the_steps_in_the_manifest_and_schema() -> None:
    out = io.StringIO()
    migrate_app([]).run(["migrate", "--schema"], stdout=out, env={})
    data = json.loads(out.getvalue())["data"]
    assert data["resumable"] is True and data["rollback_available"] is True
    flag = data["flags"]["resume-from"]
    assert flag["type"] == "enum" and flag["enum_values"] == list(STEPS)
    prop = payload_schema(migrate_app([]).commands[CommandPath("migrate")])["properties"]
    assert prop["resume_from"] == {
        "type": "string",
        "enum": list(STEPS),
        "description": flag["description"],
    }


def test_resume_from_in_an_exec_line() -> None:
    done: list[str] = []
    plan = io.StringIO('{"_cmd": "migrate", "resume_from": "verify"}\n')
    out = io.StringIO()
    code = migrate_app(done).run(["exec"], stdin=plan, stdout=out, stderr=io.StringIO(), env={})
    assert code == 0 and done == ["verify"]


def test_audit_flags_a_resumable_handler_that_ignores_ctx_step() -> None:
    app = App("rg", version="1.0.0")

    @app.command(
        "go", description="Go", danger_level="safe", exit_codes=(), steps=["a"], resumable=True
    )
    def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        ctx.step("a")
        return {}

    [rule] = [r for r in audit(migrate_app([]), "mig", limit=3).rules if r.id == "resume-guard"]
    assert rule.passed
    [rule] = [r for r in audit(app, "rg", limit=3).rules if r.id == "resume-guard"]
    assert [f.command for f in rule.findings] == ["go"]
    assert rule.findings[0].fix.startswith("if ctx.step('a'):")


# REQ-O-011


def test_rollback_on_failure_with_a_mid_step_failure_triggers_the_rollback_hook() -> None:
    done: list[str] = []
    run(migrate_app(done), ["migrate", "--fail-at", "migrate_data", "--rollback-on-failure"])
    assert done == ["backup", "apply_schema", "rollback apply_schema backup"]


def test_the_response_includes_rollback_status() -> None:
    _, envelope, _ = run(
        migrate_app([]), ["migrate", "--fail-at", "migrate_data", "--rollback-on-failure"]
    )
    assert envelope["data"]["rollback_status"] == "completed"
    _, envelope, _ = run(migrate_app([]), ["migrate", "--fail-at", "migrate_data"])
    assert envelope["data"]["rollback_status"] == "not_attempted"


def test_a_rollback_failure_is_reported_with_rollback_status_failed_and_details_in_the_response() -> (  # noqa: E501
    None
):
    code, envelope, err = run(
        migrate_app([], rollback_fails=True),
        ["migrate", "--fail-at", "migrate_data", "--rollback-on-failure"],
    )
    data = envelope["data"]
    assert code == 3 and data["rollback_status"] == "failed"
    assert data["rollback_error"] == {
        "code": "DISK_FULL",
        "message": "no room to restore the backup",
    }
    assert "no room to restore the backup" in err


def test_the_exit_code_after_a_failed_step_with_successful_rollback_is_3_not_0() -> None:
    code, envelope, _ = run(
        migrate_app([]), ["migrate", "--fail-at", "migrate_data", "--rollback-on-failure"]
    )
    assert code == 3 and envelope["ok"] is False


def test_rollback_is_not_attempted_when_no_step_completed() -> None:
    done: list[str] = []
    _, envelope, _ = run(
        migrate_app(done), ["migrate", "--fail-at", "backup", "--rollback-on-failure"]
    )
    assert done == [] and envelope["data"]["rollback_status"] == "not_attempted"


def test_the_conformance_profile_probes_resume_from_with_an_unknown_step() -> None:
    app = migrate_app([])
    [probe] = [p for p in probes_for(app) if "--resume-from" in p.argv]
    assert probe.kind == "invalid"
    code, _, _ = run(app, list(probe.argv))
    assert code == 2
