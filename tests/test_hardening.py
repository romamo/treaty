"""Regressions from the pre-1.0 review: cleanup's reach, ctx.http's redirects and
retries, the background pid file's home, and changelog version order."""

import io
import json
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from test_more_declarations import SLEEPER, Watching, stop
from test_network_and_fs import Recording, serving

from treaty import App, Background, Ctx, Flag, NoArgs, Retry, SideEffect
from treaty._changelog import version_key

WINDOWS = sys.platform == "win32"
BASE_ENV = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}


def run(app: App, argv: list[str], env: dict[str, str]) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={**BASE_ENV, **env},
    )
    return code, json.loads(out.getvalue())


def data_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


# cleanup


def declaring(*effects: SideEffect) -> App:
    app = App("fx", version="1.0.0")

    @app.command(
        "fetch",
        description="Fetch",
        danger_level="safe",
        exit_codes=(),
        filesystem_side_effects=list(effects),
    )
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_cleanup_does_not_follow_an_out_directory_that_is_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "precious").mkdir(parents=True)
    root = tmp_path / "temp" / f"fx-{os.getuid()}"
    root.mkdir(parents=True, mode=0o700)
    (root / "out").symlink_to(victim)
    code, envelope = run(
        declaring(), ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path / "temp")}
    )
    assert code == 0, envelope
    assert data_of(envelope)["effect"] == "noop"
    assert (victim / "precious").is_dir()


def test_cleanup_keeps_a_credential_a_wider_cache_glob_also_matches(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("secret")
    (tmp_path / "scratch").write_text("x")
    app = declaring(
        SideEffect(f"{tmp_path}/token", "credential"), SideEffect(f"{tmp_path}/*", "cache")
    )
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    assert (tmp_path / "token").read_text() == "secret"
    assert not (tmp_path / "scratch").exists()
    [warning] = [w for w in envelope["warnings"] if w["code"] == "CLEANUP_KEPT"]  # type: ignore[attr-defined]
    assert warning["context"]["paths"] == [str(tmp_path / "token")]


def test_cleanup_keeps_a_cache_directory_that_holds_a_config_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "settings.toml").write_text("region = 'eu'")
    app = declaring(
        SideEffect(f"{cache}/", "cache"), SideEffect(f"{cache}/settings.toml", "config")
    )
    code, envelope = run(app, ["cleanup", "--confirm-destructive"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    assert (cache / "settings.toml").exists()
    assert data_of(envelope)["cleaned"] == []


# ctx.http


class Hop(BaseHTTPRequestHandler):
    """Records each request; ``/to?<url>`` redirects there with a 302, and ``/busy``
    answers 503 with the server's ``retry_after``"""

    server: Recording

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.path.startswith("/to?"):
            self.send_response(302)
            self.send_header("Location", self.path.removeprefix("/to?"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 503 if self.path == "/busy" else 200
        self.send_response(status)
        if status == 503 and self.server.retry_after is not None:
            self.send_header("Retry-After", self.server.retry_after)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def hops() -> Iterator[tuple[Recording, Recording]]:
    with serving(Hop) as first, serving(Hop) as second:
        first.retry_after = second.retry_after = None  # type: ignore[attr-defined]
        yield first, second


@dataclass(frozen=True, slots=True)
class Call:
    url: str = Flag(description="URL")
    method: str = Flag(default="GET", description="HTTP method")


def http_app() -> App:
    app = App("netctl", version="1.0.0")

    @app.command(
        "call",
        description="Call a URL with credentials",
        danger_level="safe",
        exit_codes=(),
        has_network_io=True,
        retry=Retry(retries=3, delay_ms=0),
    )
    def call(args: Call, ctx: Ctx) -> dict[str, int]:
        headers = {"Authorization": "Bearer SECRET", "X-Api-Key": "KEY", "Accept": "text/plain"}
        return {"status": ctx.http.request(args.method, args.url, headers=headers).status}

    return app


def test_a_redirect_to_another_origin_drops_the_callers_credentials(
    hops: tuple[Recording, Recording],
) -> None:
    first, second = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/to?{second.url}/x"], {})
    assert code == 0, envelope
    [(_, _, headers)] = second.seen
    assert "Authorization" not in headers and "X-Api-Key" not in headers
    assert headers["Accept"] == "text/plain"


def test_a_redirect_within_the_origin_keeps_the_callers_credentials(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/to?{first.url}/x"], {})
    assert code == 0, envelope
    assert first.seen[1][2]["Authorization"] == "Bearer SECRET"


def test_a_post_that_reached_the_server_is_not_retried(hops: tuple[Recording, Recording]) -> None:
    first, _ = hops
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy", "--method", "POST"], {})
    assert code == 12, envelope
    assert len(first.seen) == 1


def test_a_get_is_retried_after_the_delay_retry_after_asks_for(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    first.retry_after = "1"  # type: ignore[attr-defined]
    started = time.monotonic()
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy", "--retries", "1"], {})
    assert code == 12, envelope
    assert len(first.seen) == 2 and time.monotonic() - started >= 1


def test_a_retry_after_too_long_to_sleep_ends_the_run_with_it(
    hops: tuple[Recording, Recording],
) -> None:
    first, _ = hops
    first.retry_after = "600"  # type: ignore[attr-defined]
    code, envelope = run(http_app(), ["call", "--url", f"{first.url}/busy"], {})
    assert code == 12, envelope
    assert len(first.seen) == 1
    error = envelope["error"]
    assert isinstance(error, dict) and error["retry_after_ms"] == 600_000


# ctx.spawn without a state directory


def stateless_watcher() -> App:
    app = App("watch", version="1.0.0")

    @app.command(
        "start-watcher",
        description="Start a watcher",
        danger_level="safe",
        exit_codes=(),
        background=Background("watch stop-watcher", max_lifetime_seconds=60),
    )
    def start(args: NoArgs, ctx: Ctx) -> Watching:
        spawned = ctx.spawn(SLEEPER)
        return Watching(spawned.pid, "watch stop-watcher", str(spawned.pid_file))

    @app.command("stop-watcher", description="Stop the watcher", danger_level="safe", exit_codes=())
    def stop_watcher(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_without_a_state_directory_pid_files_live_in_the_users_private_temp_root(
    tmp_path: Path,
) -> None:
    code, envelope = run(stateless_watcher(), ["start-watcher"], {"TMPDIR": str(tmp_path)})
    assert code == 0, envelope
    data = data_of(envelope)
    pid = data["background_pid"]
    assert isinstance(pid, int)
    try:
        background = tmp_path / f"watch-{os.getuid()}" / "background"
        assert Path(str(data["pid_file"])) == background / "start-watcher.pids"
        assert background.stat().st_mode & 0o777 == 0o700
    finally:
        stop(pid)


@pytest.mark.skipif(WINDOWS, reason="symlinks need privileges on Windows")
def test_a_planted_background_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = tmp_path / f"watch-{os.getuid()}"
    root.mkdir(mode=0o700)
    (root / "background").symlink_to(elsewhere)
    code, envelope = run(stateless_watcher(), ["start-watcher"], {"TMPDIR": str(tmp_path)})
    error = envelope["error"]
    assert isinstance(error, dict)
    assert code == 4 and error["code"] == "TEMP_DIR_UNSAFE"
    assert list(elsewhere.iterdir()) == []


# Changelog order


def test_changelog_versions_follow_semver_precedence() -> None:
    ordered = [
        "0.9.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0-rc.9",
        "1.0.0-rc.10",
        "1.0.0",
    ]
    assert sorted(reversed(ordered), key=version_key) == ordered
