"""Defects the 1.0 final review found in the workstreams after the Phase A review"""

import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from conftest import needs_posix_permissions
from fixture_session_app import app as session_app
from jsonschema import Draft7Validator
from test_network_and_fs import Origin, net_app, serving
from test_network_and_fs import run as net_run

from treaty import App, Ctx, Flag, NoArgs, RegistrationError, SideEffect
from treaty._envelope import without_userinfo
from treaty._journal import AuditLog, Journal, read_entries
from treaty._mcp import call_tool
from treaty._tools import tool_entries


@dataclass(frozen=True, slots=True)
class Keyed:
    region: str = "eu"
    api_token: str = ""

    def __post_init__(self) -> None:
        if self.api_token and not self.api_token.startswith("tk-"):
            raise ValueError(f"api_token {self.api_token!r} lacks the tk- prefix")


def keyed_app() -> App:
    app = App("my-tool", version="1.0.0", description="Rows", settings=Keyed)

    @app.command("show", description="Show the region", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Keyed) -> dict[str, str]:
        return {"region": settings.region}

    return app


def run(app: App, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env, isatty=False)
    return code, out.getvalue(), err.getvalue()


def test_a_settings_invalid_message_never_echoes_a_secret_setting_value() -> None:
    code, out, err = run(keyed_app(), ["show"], {"MY_TOOL_API_TOKEN": "hunter2-secret"})
    assert code == 2 and json.loads(out)["error"]["code"] == "CONFIG_INVALID"
    assert "hunter2-secret" not in out + err
    assert "[REDACTED]" in json.loads(out)["error"]["message"]


def test_the_effective_config_hash_does_not_cover_secret_setting_values() -> None:
    def hash_of(env: dict[str, str]) -> str:
        code, out, _ = run(keyed_app(), ["show"], env)
        assert code == 0, out
        return str(json.loads(out)["meta"]["effective_config_hash"])

    assert hash_of({"MY_TOOL_API_TOKEN": "tk-1"}) == hash_of({"MY_TOOL_API_TOKEN": "tk-2"})
    assert hash_of({"MY_TOOL_REGION": "us"}) != hash_of({})


# F-032, F-043: the session temp root and its pruning


SESSIONCTL = Path(__file__).resolve().parent / "fixture_session_app.py"


def session_run(argv: list[str], env: dict[str, str], stdin: str = "") -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = session_app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        env={"PATH": os.environ["PATH"], **env},
        isatty=False,
    )
    return code, out.getvalue()


def aged(path: Path) -> Path:
    old = time.time() - 3 * 86_400
    os.utime(path, (old, old))
    return path


@needs_posix_permissions
def test_pruning_never_follows_a_symlinked_session_root(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "old").mkdir(parents=True)
    aged(victim / "old")
    temp = tmp_path / "tmp"
    temp.mkdir()
    (temp / f"sessionctl-{os.getuid()}").symlink_to(victim)
    session_run(["pwd"], {"TMPDIR": str(temp)})
    assert (victim / "old").is_dir()


@needs_posix_permissions
def test_a_stale_session_that_cannot_be_removed_does_not_crash_the_next_run(
    tmp_path: Path,
) -> None:
    root = tmp_path / f"sessionctl-{os.getuid()}"
    locked = root / "0123abcd" / "locked"
    locked.mkdir(parents=True, mode=0o700)
    (locked / "file").write_text("x")
    locked.chmod(0o500)
    aged(root / "0123abcd")
    try:
        code, out = session_run(["pwd"], {"TMPDIR": str(tmp_path)})
    finally:
        locked.chmod(0o700)
    assert code == 0, out


def test_a_relative_tmpdir_gives_an_absolute_session_directory(tmp_path: Path) -> None:
    # Relative to the process's directory, on tmp_path's drive: Windows has no path from D:
    # to C:. A process of its own, so this one's working directory never changes
    (tmp_path / "tmp").mkdir()
    done = subprocess.run(
        [sys.executable, str(SESSIONCTL), "scratch", "--format", "json"],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": "tmp"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    made = Path(json.loads(done.stdout)["meta"]["session_tmp_dir"])
    assert made.is_absolute() and made.is_relative_to(tmp_path.resolve() / "tmp")


def test_two_exec_lines_each_get_their_own_output_file(tmp_path: Path) -> None:
    line = json.dumps({"_cmd": "report"}) + "\n"
    code, out = session_run(["exec"], {"TMPDIR": str(tmp_path)}, stdin=line * 2)
    envelopes = [json.loads(x) for x in out.splitlines()]
    assert code == 0 and all(e["ok"] for e in envelopes), out
    assert len({e["data"]["output_file"] for e in envelopes}) == 2


# C-011, O-027: cleanup removes only what the declarations cover


def temp_app(pattern: str) -> App:
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=[SideEffect(pattern, "temp")],
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def cleanup(app: App, tmp_path: Path) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        ["cleanup", "--confirm-destructive", "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path / "t")},
    )
    return code, json.loads(out.getvalue()), err.getvalue()


@needs_posix_permissions
def test_cleanup_never_removes_a_match_reached_through_a_symlinked_segment(
    tmp_path: Path,
) -> None:
    base, victim = tmp_path / "base", tmp_path / "victim"
    (victim / "cache").mkdir(parents=True)
    (base / "tool-real" / "cache").mkdir(parents=True)
    (base / "tool-evil").symlink_to(victim)
    code, envelope, _ = cleanup(temp_app(f"{base}/tool-{{s}}/cache"), tmp_path)
    assert code == 0, envelope
    assert (victim / "cache").is_dir()
    assert not (base / "tool-real" / "cache").exists()
    assert [c["path"] for c in envelope["data"]["cleaned"]] == [str(base / "tool-real" / "cache")]


@needs_posix_permissions
def test_cleanup_reports_a_path_it_cannot_remove_and_removes_the_rest(tmp_path: Path) -> None:
    base = tmp_path / "base"
    locked = base / "tool-a" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    (base / "tool-b").mkdir()
    locked.chmod(0o500)
    try:
        code, envelope, err = cleanup(temp_app(f"{base}/tool-{{s}}"), tmp_path)
    finally:
        locked.chmod(0o700)
    assert code == 0 and "Traceback" not in err, envelope
    assert not (base / "tool-b").exists()
    assert envelope["data"]["failed"] == [str(base / "tool-a")]
    assert [w["code"] for w in envelope["warnings"]] == ["CLEANUP_INCOMPLETE"]


@pytest.mark.parametrize("pattern", ["/", "~/", "~/{x}", "/*", "/tmp/../etc", "~/./x"])
def test_a_side_effect_that_would_cover_a_whole_root_or_escape_is_refused(pattern: str) -> None:
    with pytest.raises(RegistrationError):
        SideEffect(pattern, "temp")


# F-037: ctx.http proxies


def redirecting(target: str) -> type[BaseHTTPRequestHandler]:
    """A proxy that sends every request on to ``target``"""

    class Redirecting(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches to
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Redirecting


def test_proxy_credentials_never_follow_a_redirect_to_a_host_that_bypasses_the_proxy() -> None:
    with serving(Origin) as origin, serving(redirecting(f"{origin.url}/landed")) as proxy:
        env = {
            "HTTP_PROXY": f"http://alice:s3cret@127.0.0.1:{proxy.server_port}",
            "NO_PROXY": "127.0.0.1",
        }
        code, envelope = net_run(net_app(), ["get", "--url", "http://example.test/"], env)
    assert code == 0, envelope
    assert [path for _, path, _ in origin.seen] == ["/landed"]
    assert all("Proxy-Authorization" not in headers for _, _, headers in origin.seen)


def test_a_proxy_flag_with_a_bad_port_exits_2() -> None:
    code, envelope = net_run(
        net_app(), ["get", "--url", "http://x.test/", "--proxy", "http://p.test:99999"]
    )
    assert code == 2 and envelope["error"]["code"] == "ARG_ERROR", envelope


def test_a_proxy_variable_with_a_bad_port_names_the_variable_instead_of_crashing() -> None:
    code, envelope = net_run(
        net_app(), ["get", "--url", "http://x.test/"], {"HTTP_PROXY": "http://u:pw@p.test:abc"}
    )
    error = envelope["error"]
    assert code == 4 and error["code"] == "PROXY_INVALID", envelope
    assert "HTTP_PROXY" in error["message"] and "pw" not in json.dumps(envelope)


def test_a_proxy_named_in_an_error_keeps_an_ipv6_host_in_brackets() -> None:
    assert without_userinfo("http://u:p@[::1]:8080/x") == "http://[::1]:8080/x"


# F-034, F-006: a secret a handler prints


@dataclass(frozen=True, slots=True)
class Login:
    api_token: str = Flag(description="Token")


def printing_app() -> App:
    app = App("printer", version="1.0.0")

    @app.command("login", description="Log in", danger_level="safe", exit_codes=())
    def login(args: Login, ctx: Ctx) -> dict[str, bool]:
        print(f"using {args.api_token}")
        return {"ok": True}

    return app


@pytest.mark.parametrize("verbosity", [[], ["--verbose"]])
def test_a_printed_secret_is_redacted_in_the_warning_and_on_stderr(verbosity: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = printing_app().run(
        ["login", "--api-token-from-env", "TOKEN", *verbosity, "--format", "json"],
        stdout=out,
        stderr=err,
        env={"TOKEN": "SUPERSECRET123"},
        isatty=False,
    )
    envelope = json.loads(out.getvalue())
    assert code == 0 and envelope["warnings"][0]["code"] == "THIRD_PARTY_STDOUT"
    assert "SUPERSECRET123" not in out.getvalue() + err.getvalue()
    assert "[REDACTED]" in envelope["warnings"][0]["context"]["text"]


# The built-ins on edge input


def builtin(app: App, argv: list[str], cwd: Path) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(
        [*argv, "--cwd", str(cwd), "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        env={"PATH": os.environ["PATH"]},
        isatty=False,
    )
    return code, json.loads(out.getvalue()), err.getvalue()


def no_args_app() -> App:
    app = App("plain", version="1.0.0")

    @app.command("ping", description="Ping", danger_level="safe", exit_codes=())
    def ping(args: NoArgs, ctx: Ctx) -> dict[str, bool]:
        return {"ok": True}

    return app


def test_generate_skills_covers_a_command_without_positionals(tmp_path: Path) -> None:
    code, envelope, _ = builtin(no_args_app(), ["generate-skills"], tmp_path)
    assert code == 0, envelope
    assert (tmp_path / "skills" / "SKILL-ping.md").is_file()


def test_generate_skills_into_a_file_exits_4_without_a_traceback(tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("")
    code, envelope, err = builtin(
        no_args_app(), ["generate-skills", "--output-dir", "afile"], tmp_path
    )
    assert code == 4 and envelope["error"]["code"] == "OUTPUT_DIR_UNWRITABLE", envelope
    assert "Traceback" not in err


def test_mcp_validate_with_a_missing_file_exits_with_its_declared_not_found(
    tmp_path: Path,
) -> None:
    code, envelope, _ = builtin(
        no_args_app(), ["mcp-validate", "--mcp-schema-file", "missing.json"], tmp_path
    )
    assert envelope["error"]["code"] == "NOT_FOUND", envelope
    assert code != 1


# F-026: the audit log read while other runs rotate it


def test_reading_the_audit_log_during_rotation_never_fails(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    stop = threading.Event()

    def write() -> None:
        journal = Journal(path, AuditLog(path=path, max_bytes=200, keep=2))
        while not stop.is_set():
            journal.append({"x": "y" * 100})

    writers = [threading.Thread(target=write) for _ in range(3)]
    for writer in writers:
        writer.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            for _ in read_entries(path):
                pass
    finally:
        stop.set()
        for writer in writers:
            writer.join()


# O-025, O-032: --warnings-as-errors and the token budget


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    text: str


def items_app() -> App:
    app = App("items", version="1.0.0")

    @app.command("ls", description="List items", danger_level="safe", exit_codes=())
    def ls(args: NoArgs, ctx: Ctx) -> list[Item]:
        return [Item(str(i), "word " * (40 if i == 0 else 3)) for i in range(4)]

    @app.command("big", description="One big field", danger_level="safe", exit_codes=())
    def big(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"text": "a" * 20_000}

    @app.command("wide", description="Numbers no cut shrinks", danger_level="safe", exit_codes=())
    def wide(args: NoArgs, ctx: Ctx) -> list[int]:
        return [10**99, 1, 2]

    app.tokenizer("broken", count=lambda text: 1 // 0)
    return app


def items_run(argv: list[str], fmt: str = "json") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = items_app().run([*argv, "--format", fmt], stdout=out, stderr=err, env={}, isatty=False)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("argv", [["big", "--max-output", "4096"], ["ls", "--token-limit", "20"]])
def test_warnings_as_errors_counts_the_truncation_warnings_of_the_cap_and_budget(
    argv: list[str],
) -> None:
    code, out, _ = items_run([*argv, "--warnings-as-errors"])
    envelope = json.loads(out)
    assert code == 1 and envelope["error"]["code"] == "WARNINGS_AS_ERRORS", envelope


def test_next_token_offset_always_moves_past_an_item_too_big_for_the_limit() -> None:
    code, out, _ = items_run(["wide", "--token-limit", "20"])
    meta = json.loads(out)["meta"]
    assert code == 0 and meta["next_token_offset"] > meta.get("token_offset", 0), meta


def test_text_output_cut_by_the_token_limit_says_so_on_stderr() -> None:
    code, out, err = items_run(["ls", "--token-offset", "1", "--token-limit", "20"], "tsv")
    assert code == 0 and "--token-offset" in err, err


def test_a_tokenizer_that_raises_answers_handler_crashed() -> None:
    code, out, _ = items_run(["ls", "--tokenizer", "broken", "--token-count"])
    assert code == 1 and json.loads(out)["error"]["code"] == "HANDLER_CRASHED", out


# O-002: --fields over MCP and with --format id


def test_an_mcp_result_projected_with_fields_passes_the_tools_output_schema() -> None:
    app = items_app()
    entries = {e.name: e for e in tool_entries(app)}
    envelope = call_tool(app, entries, "ls", {"fields": "text"}).to_json()
    assert envelope["ok"] and envelope["meta"]["fields"] == ["text"]
    assert list(Draft7Validator(entries["ls"].output_schema).iter_errors(envelope)) == []


def test_format_id_keeps_the_id_field_whatever_fields_names() -> None:
    app = App("ids", version="1.0.0")

    @app.command("ls", description="List", danger_level="safe", exit_codes=(), id_field="id")
    def ls(args: NoArgs, ctx: Ctx) -> list[Item]:
        return [Item("a", "x"), Item("b", "y")]

    out = io.StringIO()
    code = app.run(["ls", "--format", "id", "--fields", "text"], stdout=out, env={})
    assert code == 0 and out.getvalue() == "a\nb\n"
