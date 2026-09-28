"""The tutorial's code matches its example files, and the finished examples behave as told"""

import ast
import asyncio
import functools
import http.server
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import SPEC_DIR, needs_posix_permissions, needs_sh_launcher

from examples.tutorial import todo_exit_codes, todo_network, todo_payload
from examples.tutorial.todo_treaty import app
from treaty import App, Ctx, Envelope, Exit, NoArgs, RegistrationError
from treaty._cli import cli
from treaty._profile import build_profile, probes_for

ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = ROOT / "docs" / "tutorial"
# A fenced block taken from a file is preceded by <!-- file: path -->
_SOURCED = re.compile(r"<!-- file: (?P<path>\S+) -->\n```\w*\n(?P<code>.*?)```", re.S)


def _sourced_blocks() -> list[tuple[str, str, str]]:
    return [
        (page.relative_to(ROOT).as_posix(), m["path"], m["code"])
        for page in sorted(TUTORIAL.rglob("*.md"))
        for m in _SOURCED.finditer(page.read_text())
    ]


def test_the_tutorial_has_sourced_blocks() -> None:
    assert _sourced_blocks()


# Links to files and directories, not to web pages; an anchor is checked by its page only
_LOCAL_LINK = re.compile(r"\]\((?!https?://)(?P<target>[^)#]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("page", sorted(TUTORIAL.rglob("*.md")), ids=lambda p: p.name)
def test_every_local_link_resolves(page: Path) -> None:
    targets = [m["target"] for m in _LOCAL_LINK.finditer(page.read_text())]
    assert [t for t in targets if not (page.parent / t).exists()] == []


@pytest.mark.parametrize(("page", "path", "code"), _sourced_blocks())
def test_a_sourced_block_is_in_its_file(page: str, path: str, code: str) -> None:
    """Matched line for line, ignoring the block's common indentation"""
    want = textwrap.dedent(code).rstrip("\n")
    lines = (ROOT / path).read_text().splitlines()
    n = len(want.splitlines())
    windows = ("\n".join(lines[i : i + n]) for i in range(len(lines) - n + 1))
    assert any(textwrap.dedent(w) == want for w in windows), f"{page}: block not in {path}"


# A Check the reader runs is a bash block preceded by <!-- check -->: every line exits 0 when
# it passes, and a chapter's blocks run in order, each on the state the one before it left
_CHECK = re.compile(r"<!-- check -->\n```bash\n(?P<code>.*?)```", re.S)


def _checked_pages() -> list[Path]:
    return [p for p in sorted(TUTORIAL.rglob("*.md")) if _CHECK.search(p.read_text())]


def test_every_page_but_the_index_has_checks_that_run() -> None:
    pages = {p.relative_to(TUTORIAL) for p in TUTORIAL.rglob("*.md")} - {Path("index.md")}
    assert {p.relative_to(TUTORIAL) for p in _checked_pages()} == pages


@needs_sh_launcher
@pytest.mark.parametrize("page", _checked_pages(), ids=lambda p: p.name)
def test_a_chapters_checks_pass_in_order(page: Path) -> None:
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("the checks need bash and jq")
    if page.name == "conformance.md" and not (SPEC_DIR / "conformance" / "run.py").is_file():
        pytest.skip(f"conformance kit not found at {SPEC_DIR}; set TREATY_SPEC_DIR")
    if page.name in ("mcp.md", "agent-docs.md") and importlib.util.find_spec("mcp") is None:
        pytest.skip("the mcp extra is not installed")
    if page.name == "exit-codes.md" and os.geteuid() == 0:
        pytest.skip("root ignores the read-only directory the checks rely on")
    # A run that failed between chmod a-w and chmod u+w leaves a directory rm -rf cannot empty
    leftover = ROOT / "tmp" / "tutorial" / "ro"
    if leftover.is_dir():
        leftover.chmod(0o755)
    script = "\n".join(m["code"] for m in _CHECK.finditer(page.read_text()))
    env = {
        **os.environ,
        "TODO_AUDIT_LOG": "off",
        "TREATY_AUDIT_LOG": "off",
        "TREATY_SPEC_DIR": str(SPEC_DIR),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = app.run(list(argv), stdout=out, stderr=io.StringIO(), env={})
    return code, out.getvalue()


def _effect(env: Envelope) -> object:
    assert isinstance(env.data, dict)
    return env.data["effect"]


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "todo.json")


def test_add_reports_the_created_item(db: str) -> None:
    code, out = _run("add", "Buy milk", "--priority", "high", "--db", db)
    assert code == 0
    assert json.loads(out)["data"] == {
        "effect": "created",
        "item": {"id": 1, "text": "Buy milk", "priority": "high", "done": False},
    }


def test_add_rejects_an_unknown_priority(db: str) -> None:
    env = app.call("add", {"text": "x", "priority": "urgent", "db": db})
    assert env.exit_code == 2 and env.error is not None
    assert env.error.message == "'priority' must be one of low, normal, high."


def test_a_root_option_before_the_command_is_corrected() -> None:
    code, out = _run("--db", "x.json", "list")
    error = json.loads(out)["error"]
    assert code == 2
    assert error["suggestion"] == "flags go after the command: todo list [arguments] --db"


def test_done_updates_once_then_reports_noop(db: str) -> None:
    app.call("add", {"text": "Walk dog", "db": db})
    first = app.call("done", {"id": 1, "db": db})
    second = app.call("done", {"id": 1, "db": db})
    assert _effect(first) == "updated"
    assert _effect(second) == "noop"


def test_done_on_a_missing_item_is_not_found(db: str) -> None:
    env = app.call("done", {"id": 9, "db": db})
    assert env.exit_code == 5 and env.error is not None
    assert env.error.code == "NOT_FOUND"
    assert env.error.suggestion == "todo list --all shows every item number"


def test_purge_previews_until_confirmed(db: str) -> None:
    app.call("add", {"text": "Walk dog", "db": db})
    app.call("done", {"id": 1, "db": db})
    preview = app.call("purge", {"db": db})
    applied = app.call("purge", {"db": db, "confirm_destructive": True})
    assert preview.exit_code == 2 and preview.error is not None
    assert preview.error.code == "CONFIRMATION_REQUIRED"
    assert _effect(preview) == "would_delete"
    assert _effect(applied) == "deleted"
    assert app.call("list", {"all": True, "db": db}).data == []


def test_list_keeps_the_old_plain_output(db: str) -> None:
    app.call("add", {"text": "Buy milk", "priority": "high", "db": db})
    app.call("add", {"text": "Walk dog", "db": db})
    app.call("done", {"id": 1, "db": db})
    code, out = _run("list", "-a", "--db", db, "--format", "plain")
    assert code == 0
    assert out == "[x] #1 Buy milk (high)\n[ ] #2 Walk dog (normal)\n"


def _audit(*argv: str) -> tuple[int, list[dict[str, str]]]:
    out = io.StringIO()
    code = cli.run(["audit", *argv], stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())["data"]["next_steps"]


def test_only_exit_codes_findings_remain() -> None:
    """The argparse chapter's Done when: the next chapter's rule is the only one left"""
    _, steps = _audit("examples.tutorial.todo_treaty:app")
    assert {s["rule"] for s in steps if s["severity"] == "warning"} == {"exit-codes"}


# Describe every command


def app_examples(app: App) -> list[str]:
    """Every example of the app's own commands, in manifest order; built-ins have their own"""
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    builtins = {path.value for path in app.builtins}
    return [
        example["command"]
        for name, command in commands.items()
        if name not in builtins
        for example in command.get("examples", [])
    ]


EXAMPLE_APPS = [app, todo_exit_codes.app, todo_network.app, todo_payload.app]
"""todo as the chapters leave it: todo_treaty.py, todo_exit_codes.py, and its two branches"""


@pytest.mark.parametrize(
    ("cli_app", "example"), [(a, e) for a in EXAMPLE_APPS for e in app_examples(a)]
)
def test_an_example_parses(cli_app: App, example: str) -> None:
    """Registration checks only the quoting: a renamed flag or a <placeholder> fails here"""
    argv = shlex.split(example)[1:]  # without the program name
    out = io.StringIO()
    code = cli_app.run([*argv, "--validate-only"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0, out.getvalue()


# The branches of todo_exit_codes.py

BASE = ROOT / "examples" / "tutorial" / "todo_exit_codes.py"


def _statements(path: Path) -> list[str]:
    """Each top-level statement, decorators included and formatting ignored, without the
    module docstring and the imports"""
    tree = ast.parse(path.read_text())
    return [
        ast.unparse(node)
        for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]


def _imported(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        f"{getattr(node, 'module', None)}.{alias.name}"
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


@pytest.mark.parametrize("name", ["todo_network.py", "todo_payload.py"])
def test_a_branch_keeps_every_statement_of_todo_exit_codes(name: str) -> None:
    """A fix to todo_exit_codes.py has to reach the files that copied it: each of its
    statements is in the branch unchanged and in the same order, among the branch's own"""
    branch = BASE.with_name(name)
    theirs = _statements(branch)
    missing: list[str] = []
    at = 0
    for statement in _statements(BASE):
        if statement in theirs[at:]:
            at = theirs.index(statement, at) + 1
        else:
            missing.append(statement)
    assert missing == [], f"{name} lost or changed statements of todo_exit_codes.py"
    assert _imported(BASE) <= _imported(branch)


# Type every command's output


def test_every_result_matches_its_output_schema(tmp_path: Path) -> None:
    """treaty does not check a handler's return value against its annotation; this does"""
    app = todo_exit_codes.app
    commands = app.manifest()["commands"]
    assert isinstance(commands, dict)
    db = str(tmp_path / "todo.json")
    calls = [
        ("add", {"text": "Buy milk", "priority": "high", "db": db}),
        ("done", {"id": 1, "db": db}),
        ("list", {"all": True, "db": db}),
        ("purge", {"dry_run": True, "db": db}),
        ("purge", {"confirm_destructive": True, "db": db}),
    ]
    for name, args in calls:
        env = app.call(name, args)
        assert env.ok, env.error
        jsonschema.validate(env.data, commands[name]["output_schema"])


# Declare network commands


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def feed(tmp_path: Path) -> Iterator[str]:
    """A local server for tmp_path/feed: todo.json holds two items, bad.json is not a list"""
    root = tmp_path / "feed"
    root.mkdir()
    items = [{"text": "Buy milk", "priority": "high"}, {"text": "Walk dog", "priority": "normal"}]
    (root / "todo.json").write_text(json.dumps(items))
    (root / "bad.json").write_text('{"oops": 1}')
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_import_adds_the_items_at_a_url_marked_untrusted(feed: str, tmp_path: Path) -> None:
    db = str(tmp_path / "todo.json")
    env = todo_network.app.call("import", {"url": f"{feed}/todo.json", "db": db}, env={})
    assert env.exit_code == 0 and isinstance(env.data, dict)
    assert env.data["_source"] == "external" and env.data["_trusted"] is False
    assert [(i["id"], i["text"]) for i in env.data["added"]] == [(1, "Buy milk"), (2, "Walk dog")]


@pytest.mark.parametrize("name", ["missing.json", "bad.json"])
def test_a_url_without_a_list_of_items_changes_nothing(
    feed: str, tmp_path: Path, name: str
) -> None:
    db = tmp_path / "todo.json"
    env = todo_network.app.call("import", {"url": f"{feed}/{name}", "db": str(db)}, env={})
    assert env.exit_code == 81 and env.error is not None
    assert env.error.code == "FEED_INVALID"
    assert not db.exists()


# Declare exit codes


def test_the_strict_audit_passes() -> None:
    """The exit codes chapter's Done when"""
    code, steps = _audit("examples.tutorial.todo_exit_codes:app", "--strict")
    assert code == 0 and [s for s in steps if s["severity"] != "advice"] == []


def test_a_damaged_file_is_store_corrupt_and_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "todo.json"
    db.write_text("{not json")
    env = todo_exit_codes.app.call("add", {"text": "x", "db": str(db)})
    assert env.exit_code == 79 and env.error is not None
    assert env.error.code == "STORE_CORRUPT"
    assert db.read_text() == "{not json"


@pytest.mark.parametrize("content", ['{"id": 1}', '[{"id": 1}]', "[1]"])
def test_json_of_the_wrong_shape_is_store_corrupt(tmp_path: Path, content: str) -> None:
    db = tmp_path / "todo.json"
    db.write_text(content)
    env = todo_exit_codes.app.call("list", {"db": str(db)})
    assert env.exit_code == 79


def test_a_missing_directory_gets_a_fix_command(tmp_path: Path) -> None:
    db = tmp_path / "no dir" / "todo.json"
    env = todo_exit_codes.app.call("add", {"text": "x", "db": str(db)})
    assert env.exit_code == 80 and env.error is not None
    assert env.error.fix_command == f"mkdir -p '{tmp_path / 'no dir'}'"


@needs_posix_permissions
def test_a_failed_write_keeps_the_old_file(tmp_path: Path) -> None:
    db = tmp_path / "todo.json"
    todo_exit_codes.app.call("add", {"text": "first", "db": str(db)})
    before = db.read_text()
    tmp_path.chmod(0o555)
    try:
        env = todo_exit_codes.app.call("add", {"text": "second", "db": str(db)})
    finally:
        tmp_path.chmod(0o755)
    assert env.exit_code == 80 and env.error is not None
    assert env.error.fix_command is None
    assert db.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["todo.json"]


def test_every_store_code_is_declared_where_it_can_be_raised() -> None:
    declared = {
        path.value: {n.value for n in c.exit_codes}
        for path, c in todo_exit_codes.app.commands.items()
    }
    assert declared["add"] == {"STORE_CORRUPT", "STORE_UNWRITABLE"}
    assert declared["list"] == {"STORE_CORRUPT"}
    assert declared["done"] == {"NOT_FOUND", "STORE_CORRUPT", "STORE_UNWRITABLE"}
    assert declared["purge"] == {"STORE_CORRUPT", "STORE_UNWRITABLE"}


def test_a_retryable_code_must_leave_nothing_behind() -> None:
    with pytest.raises(RegistrationError, match="retryable exit codes must declare side_effects"):
        App("p", version="1.0.0").exit_code(
            "STORE_BUSY", 81, description="d", retryable=True, side_effects="partial"
        )


def test_an_undeclared_code_names_itself() -> None:
    probe = App("p", version="1.0.0")
    probe.exit_code("STORE_CORRUPT", 79, description="d", retryable=False, side_effects="none")

    @probe.command("go", description="go", danger_level="safe", exit_codes=[])
    def go(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        raise Exit.STORE_CORRUPT("bad")

    env = probe.call("go", {})
    assert env.exit_code == 1 and env.error is not None
    assert env.error.code == "UNDECLARED_EXIT_CODE"
    assert env.error.message == "Command go raised STORE_CORRUPT, which it does not declare."


# Run the conformance kit

PROFILE = ROOT / "examples" / "tutorial" / "conformance" / "todo.json"


def test_the_committed_profile_is_what_treaty_writes() -> None:
    """treaty conformance refuses to overwrite a differing copy; a stale one tests old probes"""
    app = todo_exit_codes.app
    want = build_profile(app, ["./todo"], probes_for(app), beside_profile=True)
    assert json.loads(PROFILE.read_text()) == want


@needs_sh_launcher
def test_todo_passes_the_kit() -> None:
    kit = SPEC_DIR / "conformance" / "run.py"
    if not kit.is_file():
        pytest.skip(f"conformance kit not found at {kit}; set TREATY_SPEC_DIR")
    if not Path(sys.executable).is_file():
        pytest.skip("launcher needs the project venv")
    result = subprocess.run(
        ["uv", "run", "--project", str(SPEC_DIR), str(kit), str(PROFILE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    levels = json.loads(result.stdout)["data"]["levels"]
    assert levels == {"level_1": "pass", "level_2": "pass", "level_3": "pass"}


# Serve commands over MCP


def test_todo_over_mcp(tmp_path: Path) -> None:
    """A real client session: the server started as a client config starts it"""
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db = str(tmp_path / "todo.json")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "treaty._mcp", "examples.tutorial.todo_exit_codes:app"],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(ROOT), "TODO_STATE_DIR": str(tmp_path / "state")},
    )

    async def session() -> dict[str, Any]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            first = {"text": "Buy milk", "db": db, "idempotency_key": "k1"}
            calls = {
                "added": await s.call_tool("add", first),
                "replayed": await s.call_tool("add", first),
                "done": await s.call_tool("done", {"id": 1, "db": db}),
                "preview": await s.call_tool("purge", {"db": db}),
                "purged": await s.call_tool("purge", {"db": db, "confirm_destructive": True}),
                "relative": await s.call_tool("add", {"text": "x", "db": "rel.json"}),
            }
            return {"tools": tools, **calls}

    got = asyncio.run(session())
    tools = got["tools"]
    assert sorted(tools) == [
        "add",
        "audit-log",
        "cleanup",
        "doctor",
        "done",
        "generate-skills",
        "list",
        "manifest",
        "mcp-validate",
        "purge",
        "status",
        "version",
    ]
    assert tools["list"].annotations.read_only_hint is True
    assert tools["purge"].annotations.destructive_hint is True
    assert "idempotency_key" in tools["add"].input_schema["properties"]
    assert "confirm_destructive" in tools["purge"].input_schema["properties"]

    def body(name: str) -> dict[str, Any]:
        result = got[name]
        assert result.is_error is (not result.structured_content["ok"])
        content: dict[str, Any] = result.structured_content
        return content

    assert body("added")["data"]["effect"] == "created"
    assert body("replayed")["data"]["effect"] == "noop"
    assert body("replayed")["meta"]["idempotency_hit"] is True
    assert body("preview")["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert body("preview")["data"]["effect"] == "would_delete"
    assert body("purged")["data"]["effect"] == "deleted"
    # A relative path resolves against the server's working directory, not the caller's
    assert body("relative")["ok"] is True and (tmp_path / "rel.json").is_file()
