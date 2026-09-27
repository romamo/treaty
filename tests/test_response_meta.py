"""Response metadata: REQ-F-021, F-022, F-023, F-024, F-025, F-027, F-078, O-013, O-014"""

import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import WINDOWS, spec_validator
from jsonschema import Draft7Validator

from treaty import App, Ctx, Exit, Flag, NoArgs, RegistrationError, Retry
from treaty._audit import LOCK_FILE, audit, schema_change
from treaty._manifest import payload_schema

METACTL = Path(__file__).resolve().parent / "fixture_meta_app.py"


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ItemV1:
    ident: str


@dataclass(frozen=True, slots=True)
class Stamped:
    id: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class Get:
    id: str = Flag(default="a", description="Item id")


def to_v1(out: Item) -> ItemV1:
    return ItemV1(ident=out.id)


def make_app() -> App:
    app = App("itemctl", version="2.4.1", description="Items")

    @app.command(
        "get",
        description="Get an item",
        danger_level="safe",
        exit_codes=(),
        schema_version="2.1",
        compat={"1.3": to_v1},
    )
    def get(args: Get, ctx: Ctx) -> Item:
        return Item(args.id, "Alice")

    @app.command("fail", description="Always fails", danger_level="safe", exit_codes=())
    def fail(args: NoArgs, ctx: Ctx) -> None:
        raise Exit.PRECONDITION("not ready")

    @app.command("say", description="Log a line", danger_level="safe", exit_codes=())
    def say(args: NoArgs, ctx: Ctx) -> None:
        ctx.log("hello", step=1)

    return app


def run(
    argv: list[str], *, env: dict[str, str] | None = None, app: App | None = None, **kw: object
) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    code = (app or make_app()).run(argv, stdout=out, stderr=err, env=env or {}, **kw)  # type: ignore[arg-type]
    return code, json.loads(out.getvalue()), err.getvalue()


def metactl(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    return subprocess.run(
        [sys.executable, str(METACTL), *argv],
        cwd=cwd,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# REQ-F-021


def test_two_responses_for_the_same_operation_have_byte_identical_data() -> None:
    first, second = run(["get"])[1], run(["get"])[1]
    assert json.dumps(first["data"], sort_keys=True) == json.dumps(second["data"], sort_keys=True)
    assert first["meta"]["request_id"] != second["meta"]["request_id"]


def test_meta_fields_are_volatile_and_excluded_from_data() -> None:
    _, env, _ = run(["get"])
    volatile = {"request_id", "duration_ms", "timestamp", "tool_version", "schema_version"}
    assert volatile <= set(env["meta"]) and volatile.isdisjoint(env["data"])


def test_a_timestamp_in_data_triggers_a_volatile_data_warning() -> None:
    app = App("stampctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Stamped:
        return Stamped("a", "now")

    report = _audit(app)
    findings = report["volatile-data"]
    assert [(f.severity.value, f.command) for f in findings] == [("warning", "get")]
    assert "fetched_at" in findings[0].fix and "meta" in findings[0].fix


def test_a_datetime_output_field_gets_volatile_data_advice() -> None:
    @dataclass(frozen=True, slots=True)
    class Record:
        created: dt.datetime

    app = App("recctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Record:
        return Record(dt.datetime.now(dt.UTC))

    assert [f.severity.value for f in _audit(app)["volatile-data"]] == ["advice"]


def _audit(app: App) -> dict[str, list]:
    report = audit(app, "x:app", limit=100)
    return {r.id: list(r.findings) for r in report.rules}


# REQ-F-022


@pytest.mark.parametrize(
    "argv",
    [["get"], ["fail"], ["nope"], ["get", "--bogus"], ["get", "--schema"], ["--schema"]],
)
def test_every_response_includes_meta_schema_version_as_major_minor(argv: list[str]) -> None:
    _, env, _ = run(argv)
    assert spec_validator("response-envelope").is_valid(env)
    assert isinstance(env["meta"]["schema_version"], str)
    major, _, minor = env["meta"]["schema_version"].partition(".")
    assert major.isdigit() and minor.isdigit()


def test_meta_schema_version_is_the_command_contract_not_the_tool_version() -> None:
    assert run(["get"])[1]["meta"]["schema_version"] == "2.1"
    assert run(["fail"])[1]["meta"]["schema_version"] == "1.0"
    assert run(["nope"])[1]["meta"]["schema_version"] == "1.0"


def test_meta_schema_version_is_stable_across_invocations() -> None:
    assert {run(["get"])[1]["meta"]["schema_version"] for _ in range(3)} == {"2.1"}


def test_schema_version_must_be_major_minor() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="MAJOR.MINOR"):
        app.command("go", description="Go", danger_level="safe", exit_codes=(), schema_version="1")


LOCK_APP = """
from dataclasses import dataclass
from treaty import App, Ctx, Exit, NoArgs

app = App("lockctl", version="1.0.0")

@dataclass(frozen=True, slots=True)
class Out:
{fields}

@app.command(
    "get", description="Get", danger_level="safe", exit_codes=(), schema_version="{version}"
)
def get(args: NoArgs, ctx: Ctx) -> Out:
    raise Exit.PRECONDITION("unused")
"""


def treaty_cli(argv: list[str], cwd: Path) -> tuple[int, dict]:
    """The treaty console script as a real process, in ``cwd``, where the lock lives"""
    proc = subprocess.run(
        [sys.executable, "-c", "from treaty._cli import main; main()", *argv],
        cwd=cwd,
        # No bytecode: the module is rewritten within the same second
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parent),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout)


def lock_then_change(tmp_path: Path, after: str, version: str) -> list[dict]:
    """Record ``id`` and ``name``, change the output to ``after``, and audit the change"""
    directory = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    directory.mkdir()
    module = directory / "lockapp.py"
    module.write_text(LOCK_APP.format(fields="    id: str\n    name: str", version="1.0"))
    code, env = treaty_cli(["schema-lock", "lockapp:app"], directory)
    assert code == 0 and env["data"]["effect"] == "created", env
    module.write_text(LOCK_APP.format(fields=after, version=version))
    code, env = treaty_cli(["audit", "lockapp:app", "--all"], directory)
    rules = {r["id"]: r for r in env["data"]["rules"]}
    return rules["schema-version"]["findings"]


def test_a_breaking_output_change_increments_the_major_component(tmp_path: Path) -> None:
    findings = lock_then_change(tmp_path, "    ident: str", "1.0")
    assert [(f["severity"], f["fix"].split(",")[0]) for f in findings] == [
        ("error", 'schema_version="2.0"')
    ]
    assert lock_then_change(tmp_path, "    ident: str", "2.0") == []


def test_an_additive_output_change_increments_the_minor_component(tmp_path: Path) -> None:
    wider = "    id: str\n    name: str\n    role: str"
    findings = lock_then_change(tmp_path, wider, "1.0")
    assert [(f["severity"], f["fix"].split(",")[0]) for f in findings] == [
        ("warning", 'schema_version="1.1"')
    ]
    assert lock_then_change(tmp_path, wider, "1.1") == []


def test_schema_change_classifies_readers_breakage() -> None:
    old = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert schema_change(old, old).value == "none"
    retyped = {**old, "properties": {"a": {"type": "integer"}}}
    assert schema_change(old, retyped).value == "breaking"
    optional = {**old, "required": []}
    assert schema_change(old, optional).value == "breaking"
    added = {**old, "properties": {**old["properties"], "b": {"type": "string"}}}
    assert schema_change(old, added).value == "additive"
    described = {**old, "properties": {"a": {"type": "string", "description": "A"}}}
    assert schema_change(old, described).value == "none"


def test_schema_lock_twice_is_a_noop(tmp_path: Path) -> None:
    code, env = treaty_cli(["schema-lock", "fixture_meta_app:app"], tmp_path)
    assert code == 0 and env["data"] == {"effect": "created", "lock": str(LOCK_FILE), "commands": 1}
    lock = json.loads((tmp_path / LOCK_FILE).read_text())
    assert lock["commands"]["where"]["schema_version"] == "1.0"
    code, env = treaty_cli(["schema-lock", "fixture_meta_app:app"], tmp_path)
    assert code == 0 and env["data"]["effect"] == "noop"


# REQ-F-023


@pytest.mark.parametrize("argv", [["get"], ["fail"], ["nope"], ["--schema"], ["get", "--help"]])
def test_every_response_includes_meta_tool_version(argv: list[str]) -> None:
    assert run(argv)[1]["meta"]["tool_version"] == "2.4.1"


def test_meta_tool_version_matches_the_output_of_version() -> None:
    _, env, _ = run(["--version"])
    assert env["data"]["version"] == env["meta"]["tool_version"]


def test_meta_update_available_is_absent_when_no_update_is_available() -> None:
    assert "update_available" not in run(["get"])[1]["meta"]


def test_app_version_must_be_semver() -> None:
    with pytest.raises(RegistrationError, match="semver"):
        App("t", version="1.0")


# REQ-F-024


def test_every_response_includes_a_unique_meta_request_id() -> None:
    ids = {run(argv)[1]["meta"]["request_id"] for argv in (["get"], ["get"], ["fail"], ["nope"])}
    assert len(ids) == 4


def test_tool_trace_id_is_meta_trace_id_on_every_response() -> None:
    for argv in (["get"], ["fail"], ["nope"], ["get", "--bogus"]):
        assert run(argv, env={"TOOL_TRACE_ID": "abc123"})[1]["meta"]["trace_id"] == "abc123"
    assert "trace_id" not in run(["get"])[1]["meta"]


def test_meta_command_matches_the_name_of_the_command_invoked() -> None:
    assert run(["get"])[1]["meta"]["command"] == "get"
    assert run(["get", "--bogus"])[1]["meta"]["command"] == "get"
    assert run(["nope"])[1]["meta"]["command"] == "itemctl"  # no command resolved


def test_meta_timestamp_is_a_valid_iso_8601_datetime() -> None:
    stamp = run(["get"])[1]["meta"]["timestamp"]
    parsed = dt.datetime.fromisoformat(stamp)
    assert stamp.endswith("Z") and parsed.tzinfo is not None
    assert abs((dt.datetime.now(dt.UTC) - parsed).total_seconds()) < 60


@pytest.mark.parametrize("value", ["x" * 257, "a\nb", "a\x1bb"])
def test_an_unusable_tool_trace_id_exits_2(value: str) -> None:
    code, env, _ = run(["get"], env={"TOOL_TRACE_ID": value})
    assert code == 2 and env["error"]["code"] == "TRACE_ID_INVALID"
    assert "trace_id" not in env["meta"]


@pytest.mark.parametrize("argv", [["--help"], ["--version"], [], ["--schema"], ["manifest"]])
def test_help_and_version_answer_over_an_unusable_tool_trace_id(argv: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = make_app().run(argv, stdout=out, stderr=err, env={"TOOL_TRACE_ID": "a\nb"})
    assert code == 0


def test_app_call_answers_version_over_an_unusable_tool_trace_id() -> None:
    app = make_app()
    assert app.call("version", {}, env={"TOOL_TRACE_ID": "a\nb"}).exit_code == 0
    refused = app.call("get", {}, env={"TOOL_TRACE_ID": "a\nb"})
    assert refused.exit_code == 2 and refused.error is not None
    assert refused.error.code == "TRACE_ID_INVALID"


def test_exec_lines_share_the_run_trace_and_name_their_own_command() -> None:
    plan = '{"_cmd": "get"}\n{"_cmd": "fail"}\n'
    out = io.StringIO()
    make_app().run(
        ["exec", "--ignore-errors"],
        stdin=io.StringIO(plan),
        stdout=out,
        stderr=io.StringIO(),
        env={"TOOL_TRACE_ID": "t-1"},
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["meta"]["command"] for line in lines] == ["get", "fail"]
    assert {line["meta"]["trace_id"] for line in lines} == {"t-1"}
    assert [line["meta"]["schema_version"] for line in lines] == ["2.1", "1.0"]


# REQ-F-025


def test_a_child_process_spawned_by_the_framework_inherits_tool_trace_id(tmp_path: Path) -> None:
    proc = metactl(["where"], tmp_path, {"TOOL_TRACE_ID": "span-42"})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["data"]["child_trace"] == "span-42"


def test_framework_log_lines_include_the_trace_id_when_set() -> None:
    _, _, err = run(["say"], env={"TOOL_TRACE_ID": "span-42"})
    assert json.loads(err)["trace_id"] == "span-42"
    out, errs = io.StringIO(), io.StringIO()
    make_app().run(
        ["say", "--format", "plain"], stdout=out, stderr=errs, env={"TOOL_TRACE_ID": "s"}
    )
    assert errs.getvalue() == "hello step=1 trace=s\n"
    errs = io.StringIO()
    make_app().run(
        ["fail", "--format", "plain"], stdout=out, stderr=errs, env={"TOOL_TRACE_ID": "s"}
    )
    assert errs.getvalue().splitlines()[0].endswith("trace=s")


def test_log_lines_carry_no_trace_id_without_one() -> None:
    _, _, err = run(["say"])
    assert "trace_id" not in json.loads(err)


# REQ-F-027


@pytest.mark.skipif(WINDOWS, reason="pwd and symlinked directories are POSIX")
def test_meta_cwd_matches_pwd_in_the_invoking_shell(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    shell = f"cd {tmp_path / 'link'} && pwd && exec {sys.executable} {METACTL} where"
    proc = subprocess.run(
        ["/bin/sh", "-c", shell],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    pwd, envelope = proc.stdout.split("\n", 1)
    meta = json.loads(envelope)["meta"]
    assert meta["cwd"] == pwd == str(tmp_path / "link")


def test_every_response_includes_meta_cwd_as_an_absolute_path() -> None:
    for argv in (["get"], ["fail"], ["nope"]):
        assert Path(run(argv)[1]["meta"]["cwd"]).is_absolute()


def test_a_command_that_resolves_a_project_root_includes_meta_project_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "marker.toml").write_text("")
    nested = tmp_path / "packages" / "core"
    nested.mkdir(parents=True)
    proc = metactl(["where"], nested, {})
    env = json.loads(proc.stdout)
    root = Path(env["meta"]["project_root"])
    assert root.is_absolute() and root.resolve() == tmp_path.resolve()
    assert env["data"]["root"] == env["meta"]["project_root"]


def test_no_marker_leaves_project_root_absent(tmp_path: Path) -> None:
    proc = metactl(["where"], tmp_path, {})
    env = json.loads(proc.stdout)
    assert proc.returncode == 0 and "project_root" not in env["meta"]
    assert env["data"]["root"] is None


def test_project_root_names_marker_files() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="marker"):

        @app.command(
            "go", description="Go", danger_level="safe", exit_codes=(), project_root=".git"
        )
        def go(args: NoArgs, ctx: Ctx) -> None:
            return None


def test_a_handler_walking_up_from_the_cwd_gets_project_root_advice() -> None:
    app = App("t", version="1.0.0")

    @app.command("root", description="Find the root", danger_level="safe", exit_codes=())
    def root(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        here = Path.cwd()
        while not (here / "pyproject.toml").exists():
            here = here.parent
        return {"root": str(here)}

    findings = _audit(app)["project-root"]
    assert [f.fix for f in findings] == [
        'project_root=("pyproject.toml",), then read ctx.project_root'
    ]


# REQ-F-078


def retry_app(fails: int, retry: Retry | None = None) -> tuple[App, list[int]]:
    """``fetch`` fails its first ``fails`` attempts with ConnectionError"""
    calls: list[int] = []
    app = App("netctl", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=("UNAVAILABLE",),
        has_network_io=True,
        retry=retry or Retry(delay_ms=1),
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        def attempt() -> int:
            calls.append(1)
            if len(calls) <= fails:
                raise ConnectionError("connection refused")
            return len(calls)

        return {"attempts": ctx.retry(attempt)}

    return app, calls


def test_a_response_after_internal_retries_includes_meta_retries() -> None:
    app, _ = retry_app(2)
    code, env, _ = run(["fetch"], app=app)
    assert code == 0 and env["data"] == {"attempts": 3} and env["meta"]["retries"] == 2


def test_meta_retries_is_omitted_when_no_retries_occurred() -> None:
    app, _ = retry_app(0)
    code, env, _ = run(["fetch"], app=app)
    assert code == 0 and "retries" not in env["meta"]


def test_retries_0_disables_all_internal_retries() -> None:
    app, calls = retry_app(1)
    code, env, _ = run(["fetch", "--retries", "0"], app=app)
    assert code == 12 and len(calls) == 1
    assert "retries" not in env["meta"] and "retries_exhausted" not in env["error"]


def test_retries_and_retry_delay_set_the_retry_budget() -> None:
    app, calls = retry_app(10)
    code, env, _ = run(["fetch", "--retries", "2", "--retry-delay", "50ms"], app=app)
    assert code == 12 and len(calls) == 3
    assert env["meta"]["retries"] == 2 and env["meta"]["duration_ms"] >= 100


def test_a_command_that_exhausted_all_retries_exits_non_zero_with_retryable_false() -> None:
    app, calls = retry_app(10)
    code, env, _ = run(["fetch"], app=app)
    assert code == 12 and len(calls) == 4
    error = env["error"]
    assert error["code"] == "UNAVAILABLE" and error["retryable"] is False
    assert error["retries_exhausted"] == 3 and env["meta"]["retries"] == 3
    assert spec_validator("response-envelope").is_valid(env)


def test_retries_never_sleep_past_the_timeout() -> None:
    app, calls = retry_app(10, Retry(delay_ms=60_000))
    code, env, _ = run(["fetch", "--timeout", "2"], app=app)
    assert code == 12 and len(calls) == 1 and env["meta"]["duration_ms"] < 2000


def test_retry_flags_come_from_json_callers_too() -> None:
    app, calls = retry_app(10)
    env = app.call("fetch", {"retries": 1, "retry_delay": "1ms"}, env={}).to_json()
    assert env["meta"]["retries"] == 1 and len(calls) == 2


@pytest.mark.parametrize("delay", ["5x", "-1ms", "2h", "99999999999"])
def test_a_malformed_retry_delay_exits_2(delay: str) -> None:
    app, _ = retry_app(0)
    code, env, _ = run(["fetch", "--retry-delay", delay], app=app)
    assert code == 2 and env["error"]["errors"][0]["field"] == "retry-delay"


def test_the_manifest_lists_the_retry_flags_with_their_defaults() -> None:
    app, _ = retry_app(0, Retry(retries=5, delay_ms=250))
    flags = app.manifest()["commands"]["fetch"]["flags"]
    assert flags["retries"]["default"] == 5 and flags["retry-delay"]["default"] == "250ms"


def test_ctx_retry_without_retry_fails_registration() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="retry=treaty.Retry"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: NoArgs, ctx: Ctx) -> dict[str, int]:
            return {"n": ctx.retry(lambda: 1)}


def test_the_exhausted_code_must_be_declared() -> None:
    app = App("t", version="1.0.0")
    with pytest.raises(RegistrationError, match="UNAVAILABLE"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=(), retry=Retry())
        def go(args: NoArgs, ctx: Ctx) -> None:
            return None


def test_retry_values_are_checked() -> None:
    with pytest.raises(RegistrationError):
        Retry(retries=-1)
    with pytest.raises(RegistrationError):
        Retry(on=())


def test_a_hand_rolled_retry_loop_gets_retry_declared_advice() -> None:
    app = App("t", version="1.0.0")

    @app.command("poll", description="Poll", danger_level="safe", exit_codes=())
    def poll(args: NoArgs, ctx: Ctx) -> None:
        for _ in range(3):
            try:
                return None
            except ConnectionError:
                time.sleep(1)

    findings = _audit(app)["retry-declared"]
    assert len(findings) == 1 and "ctx.retry" in findings[0].fix


# REQ-O-013


def test_schema_is_machine_parseable_json(tmp_path: Path) -> None:
    proc = metactl(["--schema"], tmp_path, {})
    assert proc.returncode == 0 and json.loads(proc.stdout)["ok"] is True


def test_print_schema_returns_the_same_payload_as_schema() -> None:
    assert run(["--print-schema"])[1]["data"] == run(["--schema"])[1]["data"]
    assert run(["get", "--print-schema"])[1]["data"] == run(["get", "--schema"])[1]["data"]


def test_the_schema_includes_parameters_output_schema_exit_codes_and_danger_level() -> None:
    for path in make_app().commands:
        data = run([*path.parts, "--schema"])[1]["data"]
        assert {"parameters", "output_schema", "exit_codes", "danger_level"} <= set(data)


def test_output_schema_is_a_json_schema_the_command_data_conforms_to() -> None:
    code, env, _ = run(["get", "--output-schema"])
    assert code == 0 and env["meta"]["schema_version"] == "2.1"
    Draft7Validator.check_schema(env["data"])
    Draft7Validator(env["data"]).validate(run(["get"])[1]["data"])


def test_the_schema_output_is_stable_between_invocations() -> None:
    assert run(["--schema"])[1]["data"] == run(["--schema"])[1]["data"]
    assert run(["get", "--output-schema"])[1]["data"] == run(["get", "--output-schema"])[1]["data"]


def test_output_schema_needs_a_command() -> None:
    code, env, _ = run(["--output-schema"])
    assert code == 2 and env["error"]["context"]["flag"] == "output-schema"


# REQ-O-014


def test_schema_version_1_on_a_v2_command_produces_output_conforming_to_v1() -> None:
    code, env, _ = run(["get", "--id", "x", "--schema-version", "1"])
    assert code == 0 and env["data"] == {"ident": "x"}
    assert env["meta"]["schema_version"] == "1.3"
    old = run(["get", "--output-schema", "--schema-version", "1"])[1]["data"]
    Draft7Validator(old).validate(env["data"])
    assert old != run(["get", "--output-schema"])[1]["data"]


def test_a_schema_deprecated_warning_appears_when_using_an_old_schema_version() -> None:
    _, env, _ = run(["get", "--schema-version", "1"])
    assert env["warnings"] == [
        {
            "code": "SCHEMA_DEPRECATED",
            "message": "Schema version 1.3 is deprecated; current is 2.1",
            "context": {"current_version": "2.1", "requested_version": "1.3"},
        }
    ]
    assert run(["get", "--schema-version", "2"])[1]["warnings"] == []


@pytest.mark.parametrize("requested", ["0", "3", "x"])
def test_a_schema_version_the_command_does_not_serve_raises_a_structured_error(
    requested: str,
) -> None:
    code, env, _ = run(["get", "--schema-version", requested])
    assert code == 2
    context = env["error"]["context"]
    assert context["min_schema_version"] == "1.3" and context["schema_version"] == "2.1"
    if requested.isdigit():
        assert env["error"]["code"] == "SCHEMA_VERSION_UNSUPPORTED"


def test_the_current_and_minimum_schema_versions_are_in_the_schema_output() -> None:
    data = run(["get", "--schema"])[1]["data"]
    assert data["schema_version"] == "2.1" and data["min_schema_version"] == "1.3"
    data = run(["fail", "--schema"])[1]["data"]
    assert data["schema_version"] == data["min_schema_version"] == "1.0"


def test_json_callers_pin_a_schema_version_with_the_schema_version_key() -> None:
    env = make_app().call("get", {"id": "y", "schema_version": 1}, env={}).to_json()
    assert env["data"] == {"ident": "y"} and env["meta"]["schema_version"] == "1.3"


def test_compat_keys_are_older_majors_with_typed_shims() -> None:
    def wrong(out: ItemV1) -> ItemV1:
        return out

    for compat, match in (({"2.0": to_v1}, "older major"), ({"1.0": wrong}, "annotated")):
        app = App("t", version="1.0.0")
        with pytest.raises(RegistrationError, match=match):

            @app.command(
                "get",
                description="Get",
                danger_level="safe",
                exit_codes=(),
                schema_version="2.0",
                compat=compat,
            )
            def get(args: Get, ctx: Ctx) -> Item:
                return Item(args.id, "a")


def test_the_manifest_lists_the_schema_flags_once_at_the_root() -> None:
    manifest = make_app().manifest()
    assert {"output-schema", "print-schema", "schema-version"} <= set(manifest["flags"])
    assert "schema-version" not in manifest["commands"]["get"]["flags"]
    assert spec_validator("manifest-response").is_valid(manifest)


def test_json_payload_schemas_offer_schema_version_on_compat_commands() -> None:
    commands = {p.value: c for p, c in make_app().commands.items()}
    assert "schema_version" in payload_schema(commands["get"])["properties"]
    assert "schema_version" not in payload_schema(commands["fail"])["properties"]


@dataclass(frozen=True, slots=True)
class Applied:
    effect: str


@dataclass(frozen=True, slots=True)
class AppliedV1:
    done: bool


def applied_to_v1(out: Applied) -> AppliedV1:
    return AppliedV1(done=True)


def applied_to_list(out: Applied) -> list[str]:
    return [out.effect]


def test_a_compat_shape_of_a_mutating_command_needs_an_effect_field() -> None:
    app = App("itemctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"compat\['1.0'\] returns no 'effect'"):

        @app.command(
            "apply",
            description="Apply",
            danger_level="mutating",
            exit_codes=(),
            schema_version="2.0",
            compat={"1.0": applied_to_v1},
        )
        def apply(args: NoArgs, ctx: Ctx) -> Applied:
            return Applied("updated")


def test_a_compat_shape_of_a_steps_command_is_an_object() -> None:
    app = App("itemctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"compat\['1.0'\]: a steps= command"):

        @app.command(
            "apply",
            description="Apply",
            danger_level="safe",
            exit_codes=(),
            schema_version="2.0",
            compat={"1.0": applied_to_list},
            steps=["check"],
        )
        def apply(args: NoArgs, ctx: Ctx) -> Applied:
            ctx.step("check")
            return Applied("noop")


@pytest.mark.skipif(WINDOWS, reason="Windows cannot remove a process's working directory")
def test_help_and_version_answer_in_a_removed_working_directory(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    script = (
        "import os, runpy, sys; os.rmdir(os.getcwd()); sys.argv[:] = sys.argv[1:]; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    for argv in (["--help"], ["--version"], ["where"]):
        gone.mkdir()
        proc = subprocess.run(
            [sys.executable, "-c", script, str(METACTL), *argv],
            cwd=gone,
            env={"PATH": os.environ["PATH"], "PWD": str(gone)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert "Traceback" not in proc.stderr, argv
        assert proc.returncode == 0 or json.loads(proc.stdout)["meta"]["cwd"] == str(gone), argv
