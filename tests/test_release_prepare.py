"""The release bot's rules (.github/scripts/release_prepare.py)"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from treaty._values import ToolVersion

SCRIPTS = Path(__file__).resolve().parent.parent / ".github" / "scripts"
BASE = "https://github.com/romamo/treaty/compare"
POLICY = """\
mode = "{mode}"
min_days_between = {days}
quiet_minutes = {quiet}

[bump]
major = ["Breaking"]
minor = ["Added", "Changed"]
patch = ["Fixed"]

[[version_lines]]
file = "AGENTS.md"
pattern = '<!-- cli-version: \\S+ -->'
replace = "<!-- cli-version: {{semver}} -->"

[[version_lines]]
file = "plan.md"
pattern = 'Soak the release candidate \\(rc\\d+, \\d{{4}}-\\d\\d-\\d\\d\\)'
replace = "Soak the release candidate ({{pre}}, {{date}})"
when = "prerelease"
"""


def changelog(version: str, unreleased: str, date: str = "2026-09-30") -> str:
    return (
        "# Changelog\n\n## [Unreleased]\n\n"
        f"{unreleased}"
        f"## [{version}] - {date}\n\n- Shipped\n\n"
        f"[Unreleased]: {BASE}/v{version}...HEAD\n"
        f"[{version}]: {BASE}/v0.9.0...v{version}\n"
    )


def repo(
    tmp_path: Path,
    version: str = "1.0.0rc5",
    unreleased: str = "### Added\n\n- A flag\n\n### Fixed\n\n- A crash\n- A leak\n\n",
    mode: str = "release",
    date: str = "2026-09-30",
    days: int = 7,
    quiet: str = "30",
) -> Path:
    files = {
        "pyproject.toml": f'[project]\nname = "treaty"\nversion = "{version}"\n',
        "uv.lock": (
            f'[[package]]\nname = "treaty"\nversion = "{version}"\nsource = {{ editable = "." }}\n'
        ),
        "CHANGELOG.md": changelog(version, unreleased, date),
        "AGENTS.md": f"<!-- cli-version: {ToolVersion.of_release(version)} -->\n# AGENTS.md\n",
        "plan.md": f"- [ ] Soak the release candidate (rc5, {date}) with both consumers\n",
        ".github/release-policy.toml": POLICY.format(mode=mode, days=days, quiet=quiet),
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "."]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "Init",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "release_prepare.py"), *args, "--repo", str(root)],
        capture_output=True,
        text=True,
    )


def plan(root: Path, today: str = "2026-10-07", *flags: str) -> dict[str, str]:
    proc = run(root, "plan", "--today", today, *flags)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_pre_release_with_entries_plans_the_next_pre_release(tmp_path: Path) -> None:
    decision = plan(repo(tmp_path))
    assert decision["action"] == "release" and decision["version"] == "1.0.0rc6"
    assert decision["reason"].startswith("1 Added, 2 Fixed under Unreleased")


@pytest.mark.parametrize(
    ("unreleased", "version"),
    [
        ("### Fixed\n\n- A crash\n\n", "1.2.4"),
        ("### Added\n\n- A flag\n\n### Fixed\n\n- A crash\n\n", "1.3.0"),
        ("### Breaking\n\n- A rename\n\n### Fixed\n\n- A crash\n\n", "2.0.0"),
    ],
)
def test_a_stable_version_bumps_by_its_largest_heading(
    tmp_path: Path, unreleased: str, version: str
) -> None:
    decision = plan(repo(tmp_path, version="1.2.3", unreleased=unreleased))
    assert decision["version"] == version


def test_before_1_0_a_breaking_change_bumps_the_minor(tmp_path: Path) -> None:
    decision = plan(repo(tmp_path, version="0.4.2", unreleased="### Breaking\n\n- A rename\n\n"))
    assert decision["version"] == "0.5.0"


@pytest.mark.parametrize(
    ("kwargs", "today", "reason"),
    [
        ({"unreleased": ""}, "2026-10-07", "nothing under Unreleased"),
        ({}, "2026-10-02", "the latest release is 2 day(s) old; the policy waits 7"),
        ({"mode": "off"}, "2026-10-07", "the policy's mode is off"),
    ],
)
def test_the_bot_skips_when_the_policy_says_so(
    tmp_path: Path, kwargs: dict[str, str], today: str, reason: str
) -> None:
    decision = plan(repo(tmp_path, **kwargs), today)
    assert decision["action"] == "skip" and decision["reason"] == reason


def test_dry_run_overrides_release_mode_but_not_off(tmp_path: Path) -> None:
    assert plan(repo(tmp_path / "on"), "2026-10-07", "--dry-run")["mode"] == "dry-run"
    off = plan(repo(tmp_path / "off", mode="off"), "2026-10-07", "--dry-run")
    assert off["mode"] == "off" and off["action"] == "skip"


def test_a_heading_the_policy_does_not_map_stops_the_bot(tmp_path: Path) -> None:
    root = repo(tmp_path, unreleased="### Improved\n\n- Faster\n\n")
    proc = run(root, "plan", "--today", "2026-10-07")
    assert proc.returncode == 2 and "headings the policy doesn't map: ['Improved']" in proc.stderr


def test_apply_writes_a_release_commit_that_passes_release_check(tmp_path: Path) -> None:
    root = repo(tmp_path)
    proc = run(root, "apply", "--version", "1.0.0rc6", "--date", "2026-10-07")
    assert proc.returncode == 0, proc.stderr
    assert (root / "AGENTS.md").read_text().startswith("<!-- cli-version: 1.0.0-rc.6 -->")
    assert "(rc6, 2026-10-07)" in (root / "plan.md").read_text()
    assert 'version = "1.0.0rc6"' in (root / "uv.lock").read_text()
    log = (root / "CHANGELOG.md").read_text()
    assert (
        "## [1.0.0rc6] - 2026-10-07\n\nThe sixth 1.0 release candidate: 1 addition and 2 fixes."
        in log
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "Release",
        ],
        cwd=root,
        check=True,
    )
    check = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "release_check.py"),
            "--tag",
            "v1.0.0rc6",
            "--main",
            "HEAD",
            "--repo",
            str(root),
            "--today",
            "2026-10-07",
        ],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_a_breaking_release_says_it_is_not_additive(tmp_path: Path) -> None:
    root = repo(tmp_path, unreleased="### Breaking\n\n- A rename\n\n")
    assert run(root, "apply", "--version", "1.0.0rc6", "--date", "2026-10-07").returncode == 0
    assert (
        "1 breaking change. Not additive over rc5: see Breaking."
        in (root / "CHANGELOG.md").read_text()
    )


@pytest.mark.parametrize("version", ["1.0.0rc6", "1.0.0a1", "1.0.0b2", "1.2.3", "2.0.0rc1"])
def test_semver_spelling_matches_treaty(version: str) -> None:
    sys.path.insert(0, str(SCRIPTS))
    from release_prepare import Release

    assert Release.parse(version).semver == str(ToolVersion.of_release(version))


def test_a_newline_in_the_reason_cannot_add_github_outputs() -> None:
    sys.path.insert(0, str(SCRIPTS))
    from release_prepare import github_output

    lines = github_output({"action": "skip", "reason": "blocked by #1 x\naction=release"})
    assert lines == "action=skip\nreason=blocked by #1 x action=release\n"


def test_with_no_minimum_wait_a_batch_releases_the_same_day(tmp_path: Path) -> None:
    decision = plan(repo(tmp_path, days=0), "2026-09-30")
    assert decision["action"] == "release" and decision["version"] == "1.0.0rc6"


def test_quiet_prints_the_policys_quiet_minutes(tmp_path: Path) -> None:
    proc = run(repo(tmp_path, quiet="45"), "quiet")
    assert proc.returncode == 0 and proc.stdout == "45\n"


@pytest.mark.parametrize("quiet", ["-1", "301", "true", '"30"'])
def test_a_quiet_window_outside_0_to_300_minutes_is_refused(tmp_path: Path, quiet: str) -> None:
    proc = run(repo(tmp_path, quiet=quiet), "quiet")
    assert proc.returncode == 2 and "quiet_minutes must be an integer in 0..300" in proc.stderr
