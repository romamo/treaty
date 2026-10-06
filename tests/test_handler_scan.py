"""Registration scans each handler's source once (#359): ``ctx_calls``, ``ctx_attribute``,
and ``shell_calls`` read one cached walk of its tree."""

import json
import subprocess
import sys
import textwrap

import pytest

from treaty import App, Ctx, NoArgs, RegistrationError
from treaty._scan import ctx_attribute, ctx_calls, handler_scan, shell_calls


def test_registering_treatys_own_cli_walks_each_handler_once() -> None:
    # A fresh interpreter, so the counts are those of importing treaty._cli alone
    script = textwrap.dedent(
        """
        import json
        from treaty import _scan
        from treaty._cli import cli
        handlers = {c.handler for c in cli._commands.values()}
        info = _scan._cached_scan.cache_info()
        print(json.dumps({"handlers": len(handlers), "misses": info.misses, "hits": info.hits}))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    counts = json.loads(done.stdout)
    assert counts["misses"] == counts["handlers"], counts
    # ctx_attribute twice, shell_calls, and ctx_calls twice read the same scan
    assert counts["hits"] >= 4 * counts["handlers"], counts


def test_the_scan_is_shared_and_its_results_are_copies() -> None:
    def handler(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.run(["git", "status"])
        return {"proxy": str(ctx.network)}

    first = ctx_calls(handler)
    first.clear()
    assert [c.method for c in ctx_calls(handler)] == ["run"]
    assert handler_scan(handler) is handler_scan(handler)
    assert ctx_attribute(handler, "network") == 3
    assert shell_calls(handler) == []


def test_two_apps_registering_one_handler_get_the_same_verdict() -> None:
    def handler(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        ctx.open_url("https://example.com")
        return {}

    for name in ("one", "two"):
        app = App(name, version="1.0.0")
        command = app.command("open", description="Open", danger_level="safe", exit_codes=())
        with pytest.raises(RegistrationError, match="gui_operations"):
            command(handler)


def test_closures_of_one_definition_are_scanned_apart() -> None:
    def factory(shell: bool) -> object:
        if shell:

            def handler(args: NoArgs, ctx: Ctx) -> None:
                ctx.run("git status")

        else:

            def handler(args: NoArgs, ctx: Ctx) -> None:
                ctx.run(["git", "status"])

        return handler

    [shell] = ctx_calls(factory(True))  # type: ignore[arg-type]
    [argv] = ctx_calls(factory(False))  # type: ignore[arg-type]
    assert shell.shell and not argv.shell


def test_a_handler_without_source_scans_empty() -> None:
    scope: dict[str, object] = {"NoArgs": NoArgs, "Ctx": Ctx}
    exec("def handler(args: NoArgs, ctx: Ctx) -> None:\n    ctx.run('git status')\n", scope)
    handler = scope["handler"]
    assert ctx_calls(handler) == []  # type: ignore[arg-type]
    assert shell_calls(handler) == []  # type: ignore[arg-type]
