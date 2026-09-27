"""Argument grammar: REQ-C-020, C-026, C-027, F-049, F-059, F-067, F-075, O-006, O-009"""

import functools
import io
import json
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

import pytest
from conftest import needs_posix_signals, spec_validator

from treaty import (
    App,
    Arg,
    Ctx,
    DefaultWhenAbsent,
    Excludes,
    Flag,
    NoArgs,
    Out,
    ParseError,
    RegistrationError,
    RequiredWhen,
)
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


# REQ-F-059: treaty's JSON inputs are --raw-payload and exec lines; --config is a file path


@dataclass(frozen=True, slots=True)
class PayloadArgs:
    env: str = Flag(description="Environment")
    tags: tuple[str, ...] = Flag(default=(), description="Tags")
    replicas: int = Flag(default=1, description="Replicas")


def payload_app() -> App:
    app = App("pl", version="1.0.0")

    @app.command(
        "deploy",
        description="Deploy",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def deploy(args: PayloadArgs, ctx: Ctx) -> dict[str, object]:
        return {"env": args.env, "tags": list(args.tags), "replicas": args.replicas}

    return app


def payload(text: str) -> tuple[int, dict[str, Any]]:
    return run(payload_app(), ["deploy", "--raw-payload", text, "--stable-output"])


def test_a_trailing_comma_is_accepted_and_parsed_correctly() -> None:
    code, envelope = payload('{"env": "prod", "tags": ["a", "b",],}')
    assert code == 0 and envelope["data"] == {"env": "prod", "tags": ["a", "b"], "replicas": 1}


def test_a_block_comment_is_accepted_and_parsed_correctly() -> None:
    code, envelope = payload('{"env": /* staging */ "prod" /* comment */}')
    assert code == 0 and envelope["data"]["env"] == "prod"
    code, envelope = payload("{env: 'it\\'s', // line comment\n replicas: 3}")
    assert code == 0 and envelope["data"]["env"] == "it's" and envelope["data"]["replicas"] == 3


def test_a_malformed_input_that_cannot_be_normalized_has_corrected_input() -> None:
    code, envelope = payload("{env prod}")
    error = envelope["error"]
    assert code == 2 and error["code"] == "INVALID_JSON" and error["phase"] == "validation"
    assert json.loads(error["corrected_input"]) == {"env": "prod"}
    code, envelope = payload(error["corrected_input"])
    assert code == 0
    code, envelope = payload("{not json at all")
    assert code == 2 and envelope["error"]["code"] == "INVALID_JSON"


def test_normalized_inputs_pass_schema_validation_identically_to_strict_json() -> None:
    for strict, loose in (
        ('{"env": "prod", "replicas": 2}', "{env: 'prod', replicas: 2,}"),
        ('{"env": "prod", "replicas": "two"}', "{env: 'prod', replicas: 'two'}"),
        ('{"env": "prod", "bogus": 1}', "{env: 'prod', /* x */ bogus: 1}"),
    ):
        assert payload(strict) == payload(loose)


def test_an_exec_line_is_normalized_too() -> None:
    app = payload_app()
    out = io.StringIO()
    plan = "{_cmd: 'deploy', env: 'prod',}\n{_cmd: 'deploy' env: 'prod'}\n"
    code = app.run(
        ["exec", "--ignore-errors"],
        stdin=io.StringIO(plan),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert code == 1 and first["ok"] is True and second["error"]["code"] == "INVALID_JSON"
    assert json.loads(second["error"]["corrected_input"])["_cmd"] == "deploy"


# REQ-C-026


@dataclass(frozen=True, slots=True)
class ExportArgs:
    layout: Literal["csv", "json", "parquet"] = Arg(description="Output format")
    separator: str | None = Flag(default=None, description="Field separator for CSV")
    output: str | None = Flag(default=None, description="Output file")
    stdout: bool = Flag(default=False, description="Write to stdout instead")
    compress: bool = Flag(default=False, description="Compress output")
    level: int = Flag(default=0, description="Compression level")


EXPORT_RULES = [
    RequiredWhen("layout", "csv", then=("separator",)),
    Excludes("output", prohibited=("stdout",)),
    DefaultWhenAbsent("output", target="level", default=9),
]


def export_app(touched: list[str] | None = None) -> App:
    app = App("ex", version="1.0.0")

    @app.command(
        "export", description="Export", danger_level="safe", exit_codes=(), requires=EXPORT_RULES
    )
    def export(args: ExportArgs, ctx: Ctx) -> dict[str, object]:
        if touched is not None:
            touched.append("ran")
        return {"level": args.level, "separator": args.separator}

    return app


def test_csv_layout_without_separator_exits_2_with_a_structured_error() -> None:
    code, envelope = run(export_app(), ["export", "csv"])
    error = envelope["error"]
    assert code == 2 and error["context"]["flag"] == "separator"
    assert error["context"]["rule"] == {
        "if_flag": "layout",
        "if_value": "csv",
        "then_required": ["separator"],
    }
    assert run(export_app(), ["export", "csv", "--separator", ","])[0] == 0
    assert run(export_app(), ["export", "json"])[0] == 0


def test_the_schema_output_includes_the_full_conditional_dependency_graph() -> None:
    code, envelope = run(export_app(), ["export", "--schema"])
    assert code == 0 and envelope["data"]["requires"] == [
        {"if_flag": "layout", "if_value": "csv", "then_required": ["separator"]},
        {"if_flag": "output", "prohibited": ["stdout"]},
        {"if_flag": "output", "target_flag": "level", "default": 9},
    ]
    spec_validator("manifest-response").validate(export_app().manifest())


def test_mutually_exclusive_flags_exit_2_in_phase_1_before_any_io() -> None:
    touched: list[str] = []
    code, envelope = run(export_app(touched), ["export", "json", "--output", "r.json", "--stdout"])
    assert code == 2 and envelope["error"]["phase"] == "validation" and touched == []
    assert "mutually exclusive" in envelope["error"]["message"]
    envelope_obj = export_app(touched).call(
        "export", {"layout": "csv", "output": "r", "stdout": True}, env={}
    )
    assert envelope_obj.exit_code == 2 and touched == []
    assert envelope_obj.error is not None and len(envelope_obj.error.errors or ()) == 2


def test_an_agent_can_determine_the_required_flags_from_the_schema_alone() -> None:
    """The rules in --schema decide every combination the handler would refuse"""
    rules = export_app().manifest()["commands"]["export"]["requires"]

    def required(given: dict[str, object]) -> set[str]:
        return {
            flag
            for r in rules
            if "then_required" in r and given.get(r["if_flag"]) == r["if_value"]
            for flag in r["then_required"]
        }

    for fmt in ("csv", "json", "parquet"):
        argv = ["export", fmt, *(f"--{f}=," for f in sorted(required({"layout": fmt})))]
        assert run(export_app(), argv)[0] == 0, argv


def test_default_when_absent_changes_the_default_post_init_sees() -> None:
    code, envelope = run(export_app(), ["export", "json"])
    assert code == 0 and envelope["data"]["level"] == 9
    code, envelope = run(export_app(), ["export", "json", "--output", "r"])
    assert envelope["data"]["level"] == 0
    code, envelope = run(export_app(), ["export", "json", "--level", "3"])
    assert envelope["data"]["level"] == 3


def export_handler(args: ExportArgs, ctx: Ctx) -> dict[str, object]:
    return {}


def test_a_rule_naming_an_unknown_flag_or_a_wrong_value_fails_registration() -> None:
    app = App("bad", version="1.0.0")
    for rules, match in (
        ([RequiredWhen("fmt", "csv", then=("separator",))], "not a flag"),
        ([RequiredWhen("layout", "xml", then=("separator",))], "not a value"),
        ([Excludes("output", prohibited=("layout",))], "always required"),
        ([DefaultWhenAbsent("output", target="level", default="high")], "not a value"),
    ):
        with pytest.raises(RegistrationError, match=match):
            app.command("x", description="X", danger_level="safe", exit_codes=(), requires=rules)(
                export_handler
            )


@dataclass(frozen=True, slots=True)
class HandChecked:
    layout: str = Flag(default="json", description="Format")
    separator: str | None = Flag(default=None, description="Separator")

    def __post_init__(self) -> None:
        if self.layout == "csv" and self.separator is None:
            raise ParseError("csv needs a separator", context={"flag": "separator"})


def test_audit_suggests_requires_for_a_cross_field_post_init_check() -> None:
    app = App("hc", version="1.0.0")

    @app.command("x", description="X", danger_level="safe", exit_codes=())
    def x(args: HandChecked, ctx: Ctx) -> dict[str, str]:
        return {}

    [finding] = findings(app, "conditional-rules")
    assert 'RequiredWhen("layout", \'csv\', then=("separator",))' in finding.fix


# REQ-C-027 and REQ-F-067


@dataclass(frozen=True, slots=True)
class RunArgs:
    target: str = Arg(description="Script to run")
    child_args: tuple[str, ...] | None = Arg(description="Passed to the script verbatim")
    loud: bool = Flag(default=False, description="Say more")
    env: str = Flag(default="dev", description="Environment")


@dataclass(frozen=True, slots=True)
class ListArgs:
    kind: str = Arg(description="What to list")
    limit_to: int = Flag(default=10, description="Most items")


@dataclass(frozen=True, slots=True)
class RunOut:
    target: str
    loud: bool
    env: str
    child: tuple[str, ...] = Out(default=(), ordered=True)


def placement_app() -> App:
    app = App("tool", version="1.0.0")

    @app.command(
        "run",
        description="Run a script, forwarding the rest",
        danger_level="safe",
        exit_codes=(),
        option_placement="strict",
    )
    def run_(args: RunArgs, ctx: Ctx) -> RunOut:
        return RunOut(args.target, args.loud, args.env, args.child_args or ())

    @app.command("list", description="List things", danger_level="safe", exit_codes=())
    def list_(args: ListArgs, ctx: Ctx) -> dict[str, object]:
        return {"kind": args.kind, "limit_to": args.limit_to}

    return app


def test_commands_forwarding_args_to_a_subprocess_declare_strict_at_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Forward:
        rest: tuple[str, ...] = Arg(description="Arguments for git")

    app = App("fw", version="1.0.0")

    @app.command("git", description="Git", danger_level="safe", exit_codes=())
    def git(args: Forward, ctx: Ctx) -> dict[str, str]:
        ctx.run(["git", *args.rest])
        return {}

    [finding] = findings(app, "option-placement")
    assert 'option_placement="strict"' in finding.fix
    assert findings(placement_app(), "option-placement") == []
    with pytest.raises(RegistrationError, match="variadic"):
        app.command(
            "x", description="X", danger_level="safe", exit_codes=(), option_placement="strict"
        )(list_handler)
    with pytest.raises(RegistrationError, match="not one of"):
        app.command(
            "y", description="Y", danger_level="safe", exit_codes=(), option_placement="loose"
        )(list_handler)


def list_handler(args: ListArgs, ctx: Ctx) -> dict[str, object]:
    return {}


def test_tool_manifest_includes_an_option_placement_field_for_every_command() -> None:
    manifest = placement_app().manifest()
    assert all("option_placement" in entry for entry in manifest["commands"].values())
    assert manifest["commands"]["run"]["option_placement"] == "strict"
    spec_validator("manifest-response").validate(manifest)


def test_commands_without_the_declaration_default_to_any() -> None:
    assert placement_app().manifest()["commands"]["list"]["option_placement"] == "any"
    code, envelope = run(placement_app(), ["list", "things", "--limit-to", "3"])
    assert code == 0 and envelope["data"] == {"kind": "things", "limit_to": 3}


def test_under_strict_format_is_parsed_and_child_flags_are_forwarded() -> None:
    code, envelope = run(placement_app(), ["run", "--format", "json", "./script", "--child-flag"])
    assert code == 0 and envelope["data"]["child"] == ["--child-flag"]
    code, envelope = run(placement_app(), ["run", "--format", "json", "--", "./script"])
    assert code == 0 and envelope["data"]["target"] == "./script"
    code, envelope = run(placement_app(), ["--env", "prod", "run"])
    assert code == 2  # a local option still goes after the path


def test_under_strict_an_option_after_the_first_positional_reaches_the_child_verbatim() -> None:
    argv = ["--format", "json", "run", "--env", "prod", "./s", "--format", "plain", "--loud"]
    code, envelope = run(placement_app(), argv)
    assert code == 0 and envelope["data"] == {
        "target": "./s",
        "child": ["--format", "plain", "--loud"],
        "loud": False,
        "env": "prod",
    }
    code, envelope = run(placement_app(), ["run", "./s", "--", "-x", "--help"])
    assert code == 0 and envelope["data"]["child"] == ["--", "-x", "--help"]


def test_positional_then_flag_and_flag_then_positional_produce_identical_output() -> None:
    first = run(placement_app(), ["list", "things", "--limit-to", "5", "--stable-output"])
    second = run(placement_app(), ["list", "--limit-to", "5", "things", "--stable-output"])
    assert first == second and first[0] == 0


def test_options_after_positionals_are_never_silently_treated_as_positional_values() -> None:
    code, envelope = run(placement_app(), ["list", "things", "--bogus", "x"])
    assert code == 2 and envelope["error"]["context"]["flag"] == "bogus"


def test_global_options_are_accepted_after_the_command_path_and_after_positionals() -> None:
    code, envelope = run(placement_app(), ["list", "things", "--format", "json"])
    assert code == 0 and envelope["data"]["kind"] == "things"


def test_a_double_dash_passes_a_dash_value_as_a_positional() -> None:
    code, envelope = run(placement_app(), ["list", "--", "-value"])
    assert code == 0 and envelope["data"]["kind"] == "-value"


def test_a_conflicting_global_repeat_exits_2_naming_format_and_a_same_repeat_succeeds() -> None:
    code, envelope = run(placement_app(), ["--format", "json", "list", "x", "--format", "plain"])
    assert code == 2 and envelope["error"]["context"]["flag"] == "format"
    code, envelope = run(placement_app(), ["--format", "json", "list", "x", "--format", "json"])
    assert code == 0


def test_a_command_that_cannot_intersperse_declares_strict_in_its_manifest() -> None:
    code, envelope = run(placement_app(), ["run", "--schema"])
    assert code == 0 and envelope["data"]["option_placement"] == "strict"
