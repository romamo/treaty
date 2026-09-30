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
from jsonschema import Draft7Validator

from treaty import (
    App,
    Arg,
    Ctx,
    DefaultWhenAbsent,
    Deprecated,
    Excludes,
    Flag,
    NoArgs,
    Out,
    ParseError,
    RegistrationError,
    RequiredWhen,
    RequiresAny,
    RequiresOne,
)
from treaty._audit import audit
from treaty._cli import cli, read_baseline
from treaty._mcp import call_tool, tool_entries


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


# REQ-F-049: handlers and resources may be async def (tests/test_async_handlers.py);
# every other callable treaty invokes must be plain, or its body would never run


def test_an_async_callable_treaty_never_awaits_produces_a_registration_error() -> None:
    app = App("aio", version="1.0.0")

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

    with pytest.raises(RegistrationError, match="make the handler async def"):

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


# REQ-C-026: RequiresAny and RequiresOne (#99)


@dataclass(frozen=True, slots=True)
class LookupArgs:
    isin: str | None = Flag(default=None, description="Fetch using an ISIN")
    figi: str | None = Flag(default=None, description="Fetch using a FIGI")
    symbol: str | None = Flag(default=None, description="Fetch using a provider symbol")
    exact: bool = Flag(default=False, description="Match exactly")
    fuzzy: bool = Flag(default=False, description="Match loosely")
    limit: int = Flag(default=5, description="Most matches")


LOOKUP_RULES = [RequiresAny(("isin", "figi", "symbol")), RequiresOne(("exact", "fuzzy"))]


def lookup_app(touched: list[str] | None = None, *, rules: list[object] | None = None) -> App:
    app = App("lk", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch an instrument",
        danger_level="safe",
        exit_codes=(),
        requires=LOOKUP_RULES if rules is None else rules,  # type: ignore[arg-type]
        supports_raw_payload=True,
    )
    def fetch(args: LookupArgs, ctx: Ctx) -> dict[str, object]:
        if touched is not None:
            touched.append("ran")
        return {"isin": args.isin, "figi": args.figi}

    return app


def test_requires_any_with_none_given_exits_2_listing_the_flags_before_the_handler() -> None:
    touched: list[str] = []
    code, envelope = run(lookup_app(touched), ["fetch", "--exact"])
    error = envelope["error"]
    assert code == 2 and error["code"] == "ARG_ERROR" and error["phase"] == "validation"
    assert error["message"] == "Pass at least one of --isin, --figi, or --symbol"
    assert error["context"] == {
        "flags": ["isin", "figi", "symbol"],
        "rule": {"any_of": ["isin", "figi", "symbol"]},
    }
    assert touched == []


@pytest.mark.parametrize(
    "given", [["--isin", "X"], ["--symbol", "S"], ["--isin", "X", "--figi", "Y"]]
)
def test_requires_any_passes_with_one_or_more_given(given: list[str]) -> None:
    assert run(lookup_app(), ["fetch", "--fuzzy", *given])[0] == 0


def test_requires_one_refuses_none_and_two_and_passes_one() -> None:
    code, envelope = run(lookup_app(), ["fetch", "--isin", "X"])
    assert code == 2 and envelope["error"]["message"] == "Pass exactly one of --exact or --fuzzy"
    code, envelope = run(lookup_app(), ["fetch", "--isin", "X", "--exact", "--fuzzy"])
    assert code == 2 and envelope["error"]["context"]["rule"] == {"one_of": ["exact", "fuzzy"]}
    assert "mutually exclusive" in envelope["error"]["message"]
    assert run(lookup_app(), ["fetch", "--isin", "X", "--exact"])[0] == 0


def test_a_group_counts_presence_as_the_other_rules_do() -> None:
    """A value equal to the default is given; null and a false boolean are not"""
    app = lookup_app(rules=[RequiresAny(("isin", "limit", "exact"))])
    assert run(app, ["fetch", "--limit", "5"])[0] == 0
    assert app.call("fetch", {"limit": 5}, env={}).exit_code == 0
    assert app.call("fetch", {"isin": None, "exact": False}, env={}).exit_code == 2
    assert app.call("fetch", {"exact": True}, env={}).exit_code == 0


def test_the_schema_shows_a_group_outside_the_manifest_requires() -> None:
    code, envelope = run(lookup_app(), ["fetch", "--schema"])
    data = envelope["data"]
    assert code == 0 and "requires" not in data
    assert data["requires_groups"] == [
        {"any_of": ["isin", "figi", "symbol"]},
        {"one_of": ["exact", "fuzzy"]},
    ]
    assert data["raw_payload_schema"]["allOf"] == [
        {
            "anyOf": [
                {"required": [k], "properties": {k: {"not": {"type": "null"}}}}
                for k in ("isin", "figi", "symbol")
            ]
        },
        {
            "oneOf": [
                {"required": [k], "properties": {k: {"const": True}}} for k in ("exact", "fuzzy")
            ]
        },
    ]
    manifest = lookup_app().manifest()
    assert "requires" not in manifest["commands"]["fetch"]
    spec_validator("manifest-response").validate(manifest)


def test_the_raw_payload_schema_decides_what_validation_decides() -> None:
    raw = run(lookup_app(), ["fetch", "--schema"])[1]["data"]["raw_payload_schema"]
    validator = Draft7Validator(raw)
    for payload in (
        {"exact": True},
        {"isin": None, "exact": True},
        {"isin": "X"},
        {"isin": "X", "exact": True, "fuzzy": True},
        {"isin": "X", "exact": True, "fuzzy": False},
        {"figi": "Y", "symbol": "S", "fuzzy": True},
    ):
        valid = validator.is_valid(payload)
        code = lookup_app().call("fetch", payload, env={}).exit_code
        assert valid is (code == 0), payload
        code, _ = run(lookup_app(), ["fetch", "--raw-payload", json.dumps(payload)])
        assert valid is (code == 0), payload


def test_exec_and_mcp_apply_a_group_in_phase_1() -> None:
    app = lookup_app()
    out = io.StringIO()
    plan = '{"_cmd": "fetch", "exact": true}\n{"_cmd": "fetch", "isin": "X", "exact": true}\n'
    code = app.run(
        ["exec", "--ignore-errors"],
        stdin=io.StringIO(plan),
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert code == 1 and first["error"]["context"]["rule"] == {"any_of": ["isin", "figi", "symbol"]}
    assert second["ok"] is True
    entries = {e.name: e for e in tool_entries(app)}
    entry = entries["fetch"]
    assert "anyOf" not in entry.input_schema and "allOf" not in entry.input_schema
    assert entry.description.endswith(
        " Rules: pass at least one of --isin, --figi, or --symbol; "
        "pass exactly one of --exact or --fuzzy."
    )
    refused = call_tool(app, entries, "fetch", {"exact": True})
    assert refused.exit_code == 2 and refused.error is not None
    assert refused.error.message == "Pass at least one of --isin, --figi, or --symbol"
    assert call_tool(app, entries, "fetch", {"figi": "Y", "fuzzy": True}).exit_code == 0


def test_help_lists_every_rule() -> None:
    out = io.StringIO()
    lookup_app().run(["fetch", "--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert (
        "Rules\n  pass at least one of --isin, --figi, or --symbol\n"
        "  pass exactly one of --exact or --fuzzy\n"
    ) in out.getvalue()


@pytest.mark.parametrize(
    ("rule", "match"),
    [
        (RequiresAny(("isin",)), "needs two or more"),
        (RequiresAny(()), "non-empty tuple"),
        (RequiresAny("isin"), "non-empty tuple"),  # type: ignore[arg-type]
        (RequiresAny(("isin", "isin")), "names a flag twice"),
        (RequiresAny(("isin", "cusip")), "not a flag"),
        (RequiresOne(("layout", "isin")), "not a flag"),
    ],
)
def test_a_group_naming_too_few_unknown_or_repeated_flags_fails_registration(
    rule: object, match: str
) -> None:
    with pytest.raises(RegistrationError, match=match):
        lookup_app(rules=[rule])


def test_a_group_naming_a_required_flag_fails_registration() -> None:
    app = App("bad", version="1.0.0")
    with pytest.raises(RegistrationError, match="always required"):
        app.command(
            "x",
            description="X",
            danger_level="safe",
            exit_codes=(),
            requires=[RequiresAny(("layout", "output"))],
        )(export_handler)


@dataclass(frozen=True, slots=True)
class FetchArgs:
    isin: str | None = Flag(default=None, description="Fetch using an ISIN")
    figi: str | None = Flag(default=None, description="Fetch using a FIGI (FT Markets only)")
    symbol: str | None = Flag(default=None, description="Fetch using a provider symbol")

    def __post_init__(self) -> None:
        if not (self.isin or self.figi or self.symbol):
            raise ParseError("pass --isin, --figi, or --symbol")


@dataclass(frozen=True, slots=True)
class AnyNoneArgs:
    isin: str | None = Flag(default=None, description="ISIN")
    figi: str | None = Flag(default=None, description="FIGI")

    def __post_init__(self) -> None:
        if self.isin is None and self.figi is None:
            raise ParseError("pass --isin or --figi")
        if not any((self.isin is not None, self.figi)):
            raise ParseError("pass --isin or --figi")


@dataclass(frozen=True, slots=True)
class BothSetArgs:
    output: str | None = Flag(default=None, description="Output file")
    stdout: bool = Flag(default=False, description="Write to stdout")

    def __post_init__(self) -> None:
        if self.output and self.stdout:
            raise ParseError("pick one")


def fetch_handler(args: FetchArgs, ctx: Ctx) -> dict[str, str]:
    return {}


def any_none_handler(args: AnyNoneArgs, ctx: Ctx) -> dict[str, str]:
    return {}


def both_set_handler(args: BothSetArgs, ctx: Ctx) -> dict[str, str]:
    return {}


def audited(handler: Callable[..., dict[str, str]], rules: list[Any] | None = None) -> list[Any]:
    app = App("hc", version="1.0.0")
    app.command("x", description="X", danger_level="safe", exit_codes=(), requires=rules or ())(
        handler
    )
    return findings(app, "conditional-rules")


def test_audit_suggests_requires_any_for_an_or_of_presence_checks() -> None:
    [finding] = audited(fetch_handler)
    assert 'requires=[treaty.RequiresAny(("isin", "figi", "symbol"))]' in finding.fix
    assert "Excludes" not in finding.fix
    assert audited(fetch_handler, [RequiresAny(("isin", "figi", "symbol"))]) == []


def test_audit_reads_is_none_and_any_forms_and_keeps_excludes_for_both_set() -> None:
    assert [f.fix.split("]")[0] for f in audited(any_none_handler)] == [
        'requires=[treaty.RequiresAny(("isin", "figi"))',
        'requires=[treaty.RequiresAny(("isin", "figi"))',
    ]
    [finding] = audited(both_set_handler)
    assert 'Excludes("output", prohibited=("stdout",))' in finding.fix


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


# REQ-O-006


@dataclass(frozen=True, slots=True)
class DeleteUserArgs:
    id: str = Flag(description="User id", pattern_type="alphanumeric_id", from_stdin=True)


@dataclass(frozen=True, slots=True)
class DeleteManyArgs:
    ids: tuple[str, ...] = Arg(description="User ids", from_stdin=True)


@dataclass(frozen=True, slots=True)
class Deleted:
    effect: str
    ids: tuple[str, ...] = Out(ordered=True)


def users_app() -> App:
    app = App("tool", version="1.0.0")

    @app.command("delete-user", description="Delete", danger_level="mutating", exit_codes=())
    def delete_user(args: DeleteUserArgs, ctx: Ctx) -> Deleted:
        return Deleted("deleted", (args.id,))

    @app.command("delete-users", description="Delete", danger_level="mutating", exit_codes=())
    def delete_users(args: DeleteManyArgs, ctx: Ctx) -> Deleted:
        return Deleted("deleted", args.ids)

    return app


def test_echo_42_piped_to_id_dash_is_equivalent_to_id_42() -> None:
    piped = run(users_app(), ["delete-user", "--id", "-", "--stable-output"], stdin="42\n")
    direct = run(users_app(), ["delete-user", "--id", "42", "--stable-output"], stdin="")
    assert piped == direct and piped[1]["data"]["ids"] == ["42"]
    code, envelope = run(users_app(), ["delete-users", "-"], stdin="a\n\nb\n")
    assert code == 0 and envelope["data"]["ids"] == ["a", "b"]
    code, envelope = run(users_app(), ["delete-user", "--id", "-"], stdin="a\nb\n")
    assert code == 2 and envelope["error"]["context"]["lines"] == 2
    code, envelope = run(users_app(), ["delete-user", "--id", "-"], stdin="a/b\n")
    assert code == 2 and envelope["error"]["context"]["pattern_type"] == "alphanumeric_id"


def test_reading_dash_works_from_a_pipe_and_from_a_file_redirect(tmp_path: Any) -> None:
    source = tmp_path / "ids.txt"
    source.write_text("7\n")
    out = io.StringIO()
    with source.open() as redirected:
        code = users_app().run(
            ["delete-user", "--id", "-"], stdin=redirected, stdout=out, stderr=io.StringIO(), env={}
        )
    assert code == 0 and json.loads(out.getvalue())["data"]["ids"] == ["7"]
    assert run(users_app(), ["delete-user", "--id", "-"], stdin="7\n")[0] == 0


def test_an_error_is_raised_if_dash_is_passed_but_stdin_is_empty() -> None:
    code, envelope = run(users_app(), ["delete-user", "--id", "-"], stdin="")
    error = envelope["error"]
    assert code == 2 and error["code"] == "EMPTY_STDIN" and error["phase"] == "validation"


def test_from_stdin_is_described_in_the_manifest_and_limited_to_one_field() -> None:
    entry = users_app().manifest()["commands"]["delete-user"]["flags"]["id"]
    assert "(- reads it from stdin)" in entry["description"]

    @dataclass(frozen=True, slots=True)
    class Two:
        a: str = Flag(description="A", from_stdin=True)
        b: str = Flag(description="B", from_stdin=True)

    app = App("two", version="1.0.0")
    with pytest.raises(RegistrationError, match="at most one field"):

        @app.command("x", description="X", danger_level="safe", exit_codes=())
        def x(args: Two, ctx: Ctx) -> dict[str, str]:
            return {}


# REQ-O-009


@dataclass(frozen=True, slots=True)
class TargetArgs:
    target: Literal["staging", "prod"] = Flag(description="Environment")
    replicas: int = Flag(default=1, description="Replicas")


def validating_app(effects: list[str]) -> App:
    app = App("tool", version="1.0.0")

    @app.command("deploy", description="Deploy", danger_level="mutating", exit_codes=())
    def deploy(args: TargetArgs, ctx: Ctx) -> dict[str, str]:
        effects.append(args.target)
        return {"effect": "created"}

    return app


def test_validate_only_with_valid_args_exits_0_saying_validation_passed() -> None:
    effects: list[str] = []
    code, envelope = run(
        validating_app(effects), ["deploy", "--target", "staging", "--validate-only"]
    )
    assert code == 0 and envelope["ok"] is True and envelope["data"] is None
    assert envelope["meta"]["validation_only"] is True and effects == []


def test_validate_only_with_invalid_args_exits_2_listing_every_error() -> None:
    effects: list[str] = []
    argv = ["deploy", "--target", "qa", "--replicas", "x", "--validate-only"]
    code, envelope = run(validating_app(effects), argv)
    fields = {e["field"] for e in envelope["error"]["errors"]}
    assert code == 2 and fields == {"target", "replicas"} and effects == []


def test_validate_only_never_causes_side_effects_even_with_valid_args(tmp_path: Any) -> None:
    effects: list[str] = []
    app = validating_app(effects)
    env = {"TOOL_STATE_DIR": str(tmp_path), "TOOL_SESSION": "s1"}
    argv = ["deploy", "--target", "prod", "--idempotency-key", "k1", "--validate-only"]
    assert run(app, argv, env=env)[0] == 0
    assert run(app, argv, env=env)[0] == 0
    envelope = app.call("deploy", {"target": "prod", "validate_only": True}, env={})
    assert envelope.exit_code == 0 and envelope.meta.command == "deploy"
    assert effects == [] and list(tmp_path.iterdir()) == []
    code, envelope = run(app, ["deploy", "--target", "prod"], env=env)
    assert code == 0 and effects == ["prod"]


def test_the_validate_only_flag_is_present_in_every_commands_help_output() -> None:
    app = placement_app()
    for path in ("run", "list", "manifest", "version"):
        out = io.StringIO()
        app.run([*path.split("."), "--help"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
        assert "--validate-only" in out.getvalue(), path


# REQ-F-075


@dataclass(frozen=True, slots=True)
class SubArgs:
    name: str = Flag(default="x", description="Name", pattern_type="alphanumeric_id")
    label: str | None = Flag(
        default=None,
        description="Old spelling of --name",
        deprecated=Deprecated("1.1.0", replacement="name", removed_in="2.0.0"),
    )


def versioned_app(version: str, *, stage: str) -> App:
    """``stage``: ``both`` commands, ``old`` ``deprecated``, or ``old`` ``redirected``"""
    app = App("tool", version=version)

    @app.command(
        "new-sub",
        description="The current way",
        danger_level="safe",
        exit_codes=(),
        introduced_in="1.1.0",
    )
    def new_sub(args: SubArgs, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    if stage in ("both", "deprecated"):
        old = (
            Deprecated("1.1.0", replacement="new-sub", removed_in="2.0.0")
            if stage == "deprecated"
            else None
        )

        @app.command(
            "old-sub",
            description="The first way",
            danger_level="safe",
            exit_codes=(),
            introduced_in="1.0.0",
            deprecated=old,
        )
        def old_sub(args: SubArgs, ctx: Ctx) -> dict[str, str]:
            return {"name": args.name}

    if stage == "redirected":
        app.redirect("old-sub", to="new-sub")
    return app


def run_err(app: App, argv: list[str]) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env={}, isatty=False)
    return code, json.loads(out.getvalue()), err.getvalue()


def test_removing_a_subcommand_without_a_prior_deprecation_release_fails_the_audit(
    tmp_path: Any,
) -> None:
    released = versioned_app("1.0.0", stage="both").manifest()
    removed = versioned_app("1.1.0", stage="gone")
    [finding] = [
        f
        for r in audit(removed, "t", limit=3, baseline=released).rules
        for f in r.findings
        if r.id == "additive"
    ]
    assert finding.command == "old-sub" and "redirect" in finding.fix
    assert all(
        r.passed
        for r in audit(
            versioned_app("1.1.0", stage="redirected"), "t", limit=3, baseline=released
        ).rules
        if r.id == "additive"
    )
    deprecated = versioned_app("1.1.0", stage="deprecated").manifest()
    assert all(
        r.passed
        for r in audit(removed, "t", limit=3, baseline=deprecated).rules
        if r.id == "additive"
    )
    assert all(
        r.passed
        for r in audit(
            versioned_app("2.0.0", stage="gone"), "t", limit=3, baseline=released, released="1.0.0"
        ).rules
        if r.id == "additive"
    )
    baseline = tmp_path / "manifest.json"
    manifest = cli.manifest()
    manifest["commands"]["retired"] = {"description": "Gone", "flags": {}}  # type: ignore[index]
    baseline.write_text(json.dumps({"data": manifest}))
    out = io.StringIO()
    code = cli.run(
        ["audit", "treaty._cli:cli", "--baseline", str(baseline), "--strict"],
        stdout=out,
        stderr=io.StringIO(),
        env={},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    assert code == 79 and "additive" in envelope["error"]["context"]["rules"]


def test_a_deprecated_subcommand_executes_normally_and_emits_deprecated_to_stderr() -> None:
    code, envelope, err = run_err(versioned_app("1.1.0", stage="deprecated"), ["old-sub"])
    assert code == 0 and envelope["data"] == {"name": "x"}
    assert json.loads(err.splitlines()[-1]) == {
        "level": "warn",
        "code": "DEPRECATED",
        "message": "tool old-sub is deprecated since 1.1.0; use tool new-sub instead",
        "replacement": "tool new-sub",
        "removed_in": "2.0.0",
    }
    [warning] = envelope["warnings"]
    assert warning["code"] == "DEPRECATED"
    assert warning["context"]["replacement"] == "tool new-sub"


def test_a_deprecated_flag_warns_only_when_it_is_passed() -> None:
    app = versioned_app("1.1.0", stage="both")
    code, envelope, err = run_err(app, ["new-sub", "--label", "y"])
    assert code == 0 and envelope["warnings"][0]["code"] == "DEPRECATED_FLAG"
    assert json.loads(err.splitlines()[-1])["replacement"] == "--name"
    code, envelope, err = run_err(app, ["new-sub", "--name", "y"])
    assert envelope["warnings"] == [] and err == ""
    flag = app.manifest()["commands"]["new-sub"]["flags"]["label"]
    assert flag["description"].endswith("(deprecated since 1.1.0; use --name)")


def test_the_schema_lists_introduced_in_and_deprecated_in_for_each_subcommand() -> None:
    app = versioned_app("1.1.0", stage="deprecated")
    code, envelope, _ = run_err(app, ["old-sub", "--schema"])
    data = envelope["data"]
    assert data["introduced_in"] == "1.0.0" and data["deprecated_in"] == "1.1.0"
    assert data["replacement"] == "new-sub" and data["removed_in"] == "2.0.0"
    code, envelope, _ = run_err(app, ["new-sub", "--schema"])
    assert envelope["data"]["introduced_in"] == "1.1.0" and "deprecated_in" not in envelope["data"]
    spec_validator("manifest-response").validate(app.manifest())


def test_upgrading_never_turns_a_working_invocation_into_unknown_command_without_warning() -> None:
    releases = [
        versioned_app("1.0.0", stage="both"),
        versioned_app("1.1.0", stage="deprecated"),
        versioned_app("1.2.0", stage="redirected"),
    ]
    answers = [run_err(app, ["old-sub", "--name", "a"]) for app in releases]
    assert [code for code, _, _ in answers] == [0, 0, 13]
    assert answers[1][1]["warnings"][0]["code"] == "DEPRECATED"
    assert answers[2][1]["error"]["redirect"]["command"] == "tool new-sub --name a"


def test_a_deprecated_replacement_must_be_a_registered_command() -> None:
    app = App("tool", version="1.0.0")

    @app.command(
        "a",
        description="A",
        danger_level="safe",
        exit_codes=(),
        deprecated=Deprecated("1.0.0", replacement="missing"),
    )
    def a(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    with pytest.raises(RegistrationError, match="not a registered command"):
        app.run(["a"], stdout=io.StringIO(), stderr=io.StringIO(), env={})
    with pytest.raises(RegistrationError, match="tool version"):
        Deprecated("soon")


def test_pep_440_versions_in_introduced_in_and_deprecated_are_kept_in_semver_spelling() -> None:
    """The same versions App(version=) takes, reported as meta.tool_version is"""
    old = Deprecated("2.0.0rc1", replacement="new", removed_in="3.0.0b2")
    assert (old.since, old.removed_in) == ("2.0.0-rc.1", "3.0.0-beta.2")
    app = App("tool", version="2.0.0rc1")

    @app.command(
        "new", description="New", danger_level="safe", exit_codes=(), introduced_in="2.0.0a1"
    )
    def new(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    @app.command("old", description="Old", danger_level="safe", exit_codes=(), deprecated=old)
    def old_command(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    _, envelope, _ = run_err(app, ["new", "--schema"])
    assert envelope["data"]["introduced_in"] == "2.0.0-alpha.1"
    _, envelope, _ = run_err(app, ["old", "--schema"])
    assert envelope["data"]["deprecated_in"] == "2.0.0-rc.1"
    assert envelope["data"]["removed_in"] == "3.0.0-beta.2"
    _, envelope, _ = run_err(app, ["old"])
    assert "deprecated since 2.0.0-rc.1" in envelope["warnings"][0]["message"]


def test_dev_and_post_releases_in_deprecated_are_kept_in_semver_spelling() -> None:
    old = Deprecated("2.0.0.dev1", removed_in="3.0.0.post1")
    assert (old.since, old.removed_in) == ("2.0.0-dev.1", "3.0.0+post.1")


@pytest.mark.parametrize("version", ["2.0.0.dev", "1!2.0.0", "2.0"])
def test_introduced_in_and_deprecated_refuse_versions_app_version_refuses(version: str) -> None:
    with pytest.raises(RegistrationError, match="tool version"):
        Deprecated(version)
    app = App("tool", version="1.0.0")
    with pytest.raises(RegistrationError, match="introduced_in"):

        @app.command(
            "a", description="A", danger_level="safe", exit_codes=(), introduced_in=version
        )
        def a(args: NoArgs, ctx: Ctx) -> dict[str, str]:
            return {}


def test_a_baseline_audit_reads_the_released_app_version_from_meta_tool_version(
    tmp_path: Any,
) -> None:
    saved = tmp_path / "manifest.json"
    manifest = versioned_app("1.0.0", stage="both").manifest()
    saved.write_text(json.dumps({"data": manifest, "meta": {"tool_version": "1.0.0"}}))
    assert read_baseline(saved) == (manifest, "1.0.0")
    saved.write_text(json.dumps(manifest))
    assert read_baseline(saved) == (manifest, None)
