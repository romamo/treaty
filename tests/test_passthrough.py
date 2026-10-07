"""Passthrough commands (#35): a command whose arguments belong to another tool's parser"""

import io
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import SPEC_DIR, needs_posix_signals, spec_validator
from fixture_passthrough_app import app as ledger
from fixture_passthrough_read_app import app as wrapper

from treaty import App, Ctx, Flag, NoArgs, RegistrationError
from treaty._agents_md import render_block
from treaty._audit import audit
from treaty._completion import tree
from treaty._mcp import call_tool, tool_entries
from treaty._profile import argument_order_for, build_profile, has_kit, probes_for, run_kit
from treaty._skills import render

LEDGER = Path(__file__).resolve().parent / "fixture_passthrough_app.py"
WRAPPER = LEDGER.with_name("fixture_passthrough_read_app.py")
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


@dataclass(frozen=True)
class Ran:
    code: int
    stdout: str
    stderr: str

    @property
    def envelope(self) -> dict[str, Any]:
        """The last line on stderr: the run's envelope, valid against the spec's schema"""
        envelope: dict[str, Any] = json.loads(self.stderr.splitlines()[-1])
        spec_validator("response-envelope").validate(envelope)
        return envelope


def run(argv: list[str], *, app: App = ledger, env: dict[str, str] | None = None) -> Ran:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env or {}, isatty=False)
    return Ran(code, out.getvalue(), err.getvalue())


def run_main(argv: list[str]) -> Ran:
    """The ledger under its own __main__, in an environment with no FORCE_COLOR or
    PYTHON_COLORS: the delegated argparse runs in-process and follows its own colour
    rules, so a test reading its text must not inherit a colour request (#170)"""
    proc = subprocess.run(
        [sys.executable, str(LEDGER), *argv],
        env=BASE_ENV,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return Ran(proc.returncode, proc.stdout, proc.stderr)


def echoed(ran: Ran) -> tuple[str, ...]:
    value: tuple[str, ...] = eval(ran.stdout)  # noqa: S307 - the fixture prints a repr
    return value


# Registration


def test_a_passthrough_command_takes_no_args_and_returns_an_exit_code() -> None:
    app = App("tool", version="1.0.0")

    @dataclass(frozen=True)
    class Some:
        name: str = Flag(default="x", description="A name")

    def wrong_args(args: Some, ctx: Ctx) -> int:
        return 0

    def wrong_return(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    def handler(args: NoArgs, ctx: Ctx) -> int:
        return 0

    meta: dict[str, Any] = {"description": "Wrap", "danger_level": "safe", "exit_codes": ()}
    with pytest.raises(RegistrationError, match="treaty.NoArgs"):
        app.command("a", passthrough=True, **meta)(wrong_args)
    with pytest.raises(RegistrationError, match="exit code"):
        app.command("b", passthrough=True, **meta)(wrong_return)
    with pytest.raises(RegistrationError, match="streaming=True"):
        app.command("c", passthrough=True, streaming=True, **meta)(handler)
    for stdin_input in (True, "lines"):
        with pytest.raises(RegistrationError, match="stdin_input="):
            app.command("c2", passthrough=True, stdin_input=stdin_input, **meta)(handler)
    with pytest.raises(RegistrationError, match="stdin_records="):
        app.command("c3", passthrough=True, stdin_records=Some, **meta)(handler)
    with pytest.raises(RegistrationError, match="passthrough=True"):
        app.command("d", help_command=("--help",), **meta)(handler)
    with pytest.raises(RegistrationError, match="non-empty argv tokens"):
        app.command("e", passthrough=True, help_command="--help", **meta)(handler)
    with pytest.raises(RegistrationError, match="cannot be destructive"):
        app.command(
            "f", description="Wrap", danger_level="destructive", exit_codes=(), passthrough=True
        )(handler)
    with pytest.raises(RegistrationError, match="danger level"):
        app.command("g", description="Wrap", exit_codes=(), passthrough=True)(handler)


# Argv


def test_every_token_after_the_path_reaches_the_tool_verbatim() -> None:
    argv = ["echo", "--format", "plain", "--", "-x", "--schema", "--validate-only", "-v"]
    ran = run(["--format", "json", "ingest", *argv])
    assert ran.code == 0
    assert echoed(ran) == tuple(argv)
    assert ran.envelope["data"] == {"exit_code": 0}
    assert ran.envelope["meta"]["command"] == "ingest"


def test_the_tool_owns_stdout_and_the_envelope_is_the_last_line_on_stderr() -> None:
    ran = run(["ingest", "extract", "statement.csv", "--output", "out.beancount"])
    assert ran.code == 0
    assert ran.stdout == "entries of statement.csv\nwritten to out.beancount\n"
    assert ran.envelope["ok"] is True
    assert ran.envelope["warnings"] == []  # the tool's stdout is no THIRD_PARTY_STDOUT


def test_the_tool_exit_code_is_the_process_exit_code() -> None:
    ran = run(["ingest", "fail", "3"])
    assert ran.code == 3 and ran.stdout == ""
    envelope = ran.envelope
    assert envelope["meta"]["exit_code"] == 3 and envelope["data"] == {"exit_code": 3}
    assert envelope["error"]["code"] == "DELEGATED_EXIT"


def test_a_parser_exiting_on_its_own_is_its_exit_code_not_a_crash() -> None:
    ran = run_main(["ingest", "bogus"])
    assert ran.code == 2
    assert "invalid choice: 'bogus'" in ran.stderr  # argparse's, before the envelope
    assert ran.envelope["error"]["code"] == "DELEGATED_EXIT"
    ran = run_main(["ingest", "extract", "--help"])
    assert ran.code == 0 and "usage: ledger ingest extract" in ran.stdout
    ran = run(["ingest", "bogus"])  # in-process too: the code, not argparse's coloured text
    assert ran.code == 2 and ran.envelope["error"]["code"] == "DELEGATED_EXIT"


def test_help_command_stands_in_for_a_lone_help_after_the_path() -> None:
    for token in ("--help", "-h"):
        ran = run_main(["ingest", token])
        assert ran.code == 0 and "usage: ledger ingest extract" in ran.stdout


def test_treaty_flags_go_before_the_path() -> None:
    ran = run(["--help", "--format", "plain", "ingest"])
    assert ran.code == 0
    assert ran.stdout.startswith("ledger [flags] ingest [tool arguments...]")
    assert "A lone --help or -h after the path runs ledger ingest extract --help" in ran.stdout
    ran = run(["--validate-only", "ingest", "fail", "3"])
    assert ran.code == 0 and ran.stdout == ""
    assert ran.envelope["meta"]["validation_only"] is True


def test_a_flag_the_command_lacks_before_the_path_names_where_the_tools_go() -> None:
    ran = run(["--timeout", "5", "ingest", "extract", "x"])
    assert ran.code == 2
    error = json.loads(ran.stdout)["error"]  # no tool ran: stdout is still treaty's
    assert error["context"]["command"] == "ledger ingest"
    assert "every token after its path goes to the tool" in error["message"]


def test_output_before_the_path_gets_the_envelope_too(tmp_path: Path) -> None:
    target = tmp_path / "envelope.json"
    ran = run(["--output", str(target), "ingest", "extract", "s.csv", "--output", "x"])
    assert ran.code == 0 and "written to x" in ran.stdout
    assert json.loads(target.read_text()) == ran.envelope


def test_a_timeout_ends_the_run_with_an_envelope_on_stderr() -> None:
    app = App("slow", version="1.0.0")

    @app.command(
        "wait",
        description="Wait",
        danger_level="safe",
        exit_codes=(),
        passthrough=True,
        timeout=0.2,
    )
    def wait(args: NoArgs, ctx: Ctx) -> None:
        release.wait(10)

    release = threading.Event()
    try:
        ran = run(["wait", "--anything"], app=app)
    finally:
        release.set()
    assert ran.code == 10 and ran.envelope["error"]["code"] == "TIMEOUT"


def test_a_session_repeat_of_the_same_argv_is_not_run_again(tmp_path: Path) -> None:
    env = {"LEDGER_SESSION": "s1", "LEDGER_STATE_DIR": str(tmp_path)}
    first = run(["ingest", "extract", "a.csv"], env=env)
    again = run(["ingest", "extract", "a.csv"], env=env)
    other = run(["ingest", "extract", "b.csv"], env=env)
    assert first.stdout == "entries of a.csv\n" and first.code == 0
    assert again.code == 0 and again.stdout == ""  # replayed: the tool did not run
    assert again.envelope["meta"]["idempotency_hit"] is True
    assert again.envelope["data"] == {"exit_code": 0, "effect": "noop"}
    assert other.stdout == "entries of b.csv\n"


def test_the_audit_log_records_the_run_without_its_raw_argv(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    ran = run(["ingest", "extract", "secret-token"], env={"LEDGER_AUDIT_LOG": str(log)})
    assert ran.code == 0
    [line] = log.read_text().splitlines()
    entry = json.loads(line)
    spec_validator("audit-log-entry").validate(entry)
    assert entry["command"] == "ingest" and entry["exit_code"] == 0
    assert entry["args"] == {"argv": "[OMITTED]"}
    assert "secret-token" not in line


def test_the_unprotected_record_on_stderr_omits_the_tools_argv() -> None:
    ran = run(["--no-injection-protection", "ingest", "extract", "secret-token"])
    assert ran.code == 0 and ran.stdout == "entries of secret-token\n"
    record = json.loads(ran.stderr.splitlines()[0])
    assert record["code"] == "INJECTION_PROTECTION_DISABLED"
    assert record["argv"] == ["ledger", "--no-injection-protection", "ingest", "[OMITTED]"]
    assert "secret-token" not in ran.stderr


def test_descriptor_1_is_the_tools_under_main() -> None:
    proc = subprocess.run(
        [sys.executable, str(LEDGER), "ingest", "native"],
        env=BASE_ENV,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "from C code\nfrom a child\n"
    envelope = json.loads(proc.stderr.splitlines()[-1])
    assert envelope["ok"] is True and envelope["warnings"] == []


SWITCHING = """
import sys, threading
from treaty._stdout import intercept_stdout
sys.setswitchinterval(1e-6)
interceptor = intercept_stdout()
stop = threading.Event()
def take():
    while not stop.is_set():
        interceptor.take()
taker = threading.Thread(target=take)
taker.start()
for _ in range(50_000):
    interceptor.pause()
    interceptor.resume()
stop.set()
taker.join()
interceptor.close()
"""


def test_a_concurrent_envelope_sends_no_marker_to_the_tools_stdout() -> None:
    """Another thread's envelope syncing descriptor 1 while a passthrough run pauses or
    resumes it writes its marker to the pipe, never to stdout"""
    proc = subprocess.run(
        [sys.executable, "-c", SWITCHING],
        env=BASE_ENV,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""


@needs_posix_signals
def test_a_signal_ends_the_run_with_an_envelope_on_stderr() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(LEDGER), "ingest", "sleep"],
        env=BASE_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == "ready\n"
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 143
    envelope = json.loads(err.splitlines()[-1])
    assert envelope["error"]["code"] == "CANCELLED" and envelope["meta"]["exit_code"] == 143


# Exec, App.call, and MCP


def test_app_call_and_exec_take_the_tools_argv_as_a_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope = ledger.call("ingest", {"argv": ["echo", "--help"]}, env={})
    assert envelope.ok and envelope.data == {"exit_code": 0}
    assert capsys.readouterr().out == "('echo', '--help')\n"  # the host's stdout
    refused = ledger.call("ingest", {"argv": "echo"}, env={})
    assert refused.exit_code == 2 and refused.error is not None
    assert refused.error.context["field"] == "argv"

    out, err = io.StringIO(), io.StringIO()
    line = json.dumps({"_cmd": "ingest", "argv": ["fail", "4"]})
    code = ledger.run(["exec"], stdin=io.StringIO(line + "\n"), stdout=out, stderr=err, env={})
    [answer] = [json.loads(x) for x in out.getvalue().splitlines()]
    assert answer["meta"]["exit_code"] == 4 and answer["error"]["code"] == "DELEGATED_EXIT"
    assert code != 0


def test_mcp_lists_no_passthrough_command() -> None:
    entries = {e.name: e for e in tool_entries(ledger)}
    assert "ingest" not in entries
    envelope = call_tool(ledger, entries, "ingest", {"argv": []}, env={})
    assert envelope.error is not None and envelope.error.code == "UNKNOWN_TOOL"


# Schema, manifest, completion, audit, and agent docs


def test_the_manifest_marks_it_within_the_spec_schema() -> None:
    manifest = ledger.manifest()
    spec_validator("manifest-response").validate(manifest)
    entry = manifest["commands"]["ingest"]
    # REQ-C-031 (ManifestResponse 3.9): the keys, not a sentence in the description; the
    # flags it takes go before its path, as global options do, so the entry lists none
    assert entry["arguments"] == "passthrough"
    assert entry["help_argv"] == ["extract", "--help"]
    assert entry["option_placement"] == "strict" and "positionals" not in entry
    assert entry["description"] == "Import statements with the ingest tool's own arguments"
    assert entry["flags"] == {}
    # Its --output gets the final envelope whatever --format says (ManifestResponse 3.6)
    assert entry["output_file"] == "envelope"
    assert "stdin" not in entry and "stderr" not in entry and "confirm_flag" not in entry
    schema = ledger.run(["--schema", "ingest"], stdout=(got := io.StringIO()), env={})
    assert schema == 0
    data = json.loads(got.getvalue())["data"]
    assert {"output", "validate-only", "idempotency-key"} <= set(data["flags"])
    assert data["parameters"] == data["flags"]
    out = io.StringIO()
    assert ledger.run(["--schema"], stdout=out, stderr=io.StringIO(), env={}) == 0
    spec_validator("manifest-response").validate(json.loads(out.getvalue())["data"])


def test_schema_goes_before_the_path() -> None:
    ran = run(["ingest", "--schema"])
    assert ran.code == 2  # --schema after the path is the tool's: argparse refuses it
    assert echoed(run(["ingest", "echo", "--schema"])) == ("echo", "--schema")
    out = io.StringIO()
    assert ledger.run(["--schema", "ingest"], stdout=out, stderr=io.StringIO(), env={}) == 0
    schema = json.loads(out.getvalue())["data"]
    assert schema["option_placement"] == "strict"
    assert schema["output_schema"]["properties"]["exit_code"]["type"] == "integer"


def test_completion_offers_file_paths_after_the_path() -> None:
    nodes = {n.key: n for n in tree(ledger.manifest(), {}).nodes}
    node = nodes["ingest"]
    assert node.options == () and node.variadic and node.slots[0].files


def test_audit_asks_no_exit_codes_or_ctx_log_of_a_passthrough_command() -> None:
    report = audit(ledger, "fixture_passthrough_app:app", limit=10)
    found = {(r.id, f.command) for r in report.rules for f in r.findings}
    assert ("exit-codes", "ingest") not in found
    assert ("log-not-print", "ingest") not in found


def test_agents_md_says_where_the_flags_go() -> None:
    block = render_block(ledger, "fixture_passthrough_app:app", "ledger")
    assert "`ledger ingest` hands every token after the command path" in block


def test_the_skill_file_puts_treaty_flags_before_the_path() -> None:
    skill = render(ledger)["SKILL-ingest.md"]
    assert "ledger --schema ingest" in skill and "ledger ingest --schema" not in skill
    assert "`ledger --validate-only ingest ...`" in skill


# Conformance probes


def wrapper_app(ran: list[tuple[str, ...]]) -> App:
    """A network passthrough command whose example has options after the path that
    argument_order could pick"""
    app = App("gitw", version="1.0.0")

    @app.command(
        "git",
        description="Run git",
        danger_level="safe",
        exit_codes=(),
        passthrough=True,
        has_network_io=True,
        examples=[("Log", "gitw --format json git log --format oneline --max-count 3")],
    )
    def git(args: NoArgs, ctx: Ctx) -> int:
        ran.append(ctx.argv_rest)
        return 0

    return app


def test_no_probe_runs_a_passthrough_command() -> None:
    """Its tool owns stdout and its envelope is on stderr, where the kit's json_envelope
    check does not read it: the read and --proxy probes of one failed that check (#386).
    The unknown-flag probe's base is then the version built-in"""
    probes = {p.name: p for p in probes_for(wrapper_app([]))}
    assert [p for p in probes.values() if "git" in p.argv] == []
    assert probes["unknown flag"].argv == ("version", "--no-such-flag")


def test_a_passthrough_only_app_passes_the_conformance_kit(tmp_path: Path) -> None:
    """The kit on an app whose one command is a safe network passthrough (#386)"""
    if not has_kit(SPEC_DIR):
        pytest.skip(f"conformance kit not found at {SPEC_DIR}; set TREATY_SPEC_DIR")
    profile = build_profile(wrapper, [sys.executable, str(WRAPPER)], probes_for(wrapper))
    path = tmp_path / "gitw.json"
    path.write_text(json.dumps(profile))
    kit = run_kit(SPEC_DIR, path, 300, os.environ)
    assert kit.exit_code == 0, (kit.envelope, kit.stderr)


def test_argument_order_skips_a_passthrough_command() -> None:
    """The kit moves --format after the path, where the tool would get it (#367)"""
    order = argument_order_for(wrapper_app([]))
    assert order is not None and order["command_path"] == ["manifest"]
