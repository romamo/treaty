"""The publish workflow's release checks (.github/scripts/release_check.py)"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "release_check.py"
BASE = "https://github.com/romamo/treaty/compare"
TODAY = "2026-09-30"


def changelog(unreleased: str = "", date: str = TODAY, links: str | None = None) -> str:
    links = links or f"[Unreleased]: {BASE}/v1.1.0...HEAD\n[1.1.0]: {BASE}/v1.0.0...v1.1.0\n"
    return (
        "# Changelog\n\n## [Unreleased]\n\n"
        f"{unreleased}"
        f"## [1.1.0] - {date}\n\n### Fixed\n\n- A fix\n\n"
        "## [1.0.0] - 2026-09-01\n\n- First\n\n"
        f"{links}"
    )


def repo(tmp_path: Path, version: str = "1.1.0", log: str | None = None) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog() if log is None else log)
    git("add", ".")
    git("commit", "-q", "-m", "Release")
    return tmp_path


def check(root: Path, tag: str = "v1.1.0") -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tag",
            tag,
            "--main",
            "main",
            "--repo",
            str(root),
            "--today",
            TODAY,
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_a_complete_release_passes_every_check(tmp_path: Path) -> None:
    code, out = check(repo(tmp_path))
    assert code == 0, out
    assert [line.split(":")[0] for line in out.splitlines()] == [
        "PASS version",
        "PASS on-main",
        "PASS unreleased-empty",
        "PASS section",
        "PASS links",
    ]


def test_a_tag_that_differs_from_the_project_version_fails(tmp_path: Path) -> None:
    code, out = check(repo(tmp_path, version="1.0.9"))
    assert code == 1 and "FAIL version: tag v1.1.0 but pyproject.toml declares 1.0.9" in out


def test_entries_left_under_unreleased_fail(tmp_path: Path) -> None:
    code, out = check(repo(tmp_path, log=changelog(unreleased="### Fixed\n\n- Not released\n\n")))
    assert code == 1 and "FAIL unreleased-empty" in out and "'### Fixed'" in out


def test_a_missing_or_wrong_compare_link_fails(tmp_path: Path) -> None:
    wrong = f"[Unreleased]: {BASE}/v1.0.0...HEAD\n[1.1.0]: {BASE}/v1.0.0...v1.1.0\n"
    code, out = check(repo(tmp_path, log=changelog(links=wrong)))
    assert (
        code == 1 and "FAIL links: [Unreleased] compares v1.0.0...HEAD, want v1.1.0...HEAD" in out
    )


def test_a_section_dated_after_today_fails(tmp_path: Path) -> None:
    code, out = check(repo(tmp_path, log=changelog(date="2026-12-01")))
    assert code == 1 and "FAIL section: dated 2026-12-01" in out


def test_a_commit_that_is_not_on_main_fails(tmp_path: Path) -> None:
    root = repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=root, check=True)
    (root / "extra.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Side"], cwd=root, check=True, capture_output=True)
    code, out = check(root)
    assert code == 1 and "FAIL on-main: the tagged commit is not on main" in out


def test_a_malformed_tag_is_an_input_error(tmp_path: Path) -> None:
    root = repo(tmp_path)
    for tag in ("1.1.0", "v1.1.0+local", "vnext"):
        code, out = check(root, tag)
        assert code == 2 and out.startswith("release_check: "), (tag, out)
