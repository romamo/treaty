"""Checks a release tag must pass before the publish workflow builds it.

Run from a checkout at the tagged commit:

    uv run --no-project python .github/scripts/release_check.py --tag v1.0.0rc5 --main origin/main

Exit codes: 0 every check passed; 1 a check failed; 2 bad arguments or an unreadable repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# PEP 440 release, with optional pre, post, and dev parts; no epoch or local version
_VERSION = re.compile(r"\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?")
_SECTION = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\S+))?\s*$")
_COMPARE = re.compile(
    r"^\[(?P<name>[^\]]+)\]: (?P<base>https://github\.com/[^/\s]+/[^/\s]+)/compare/(?P<range>\S+)\s*$"
)
UNRELEASED = "Unreleased"
BLOCKER_LABEL = "release-blocker"


class InputError(Exception):
    """A malformed tag, or a repo the checks can't read"""


@dataclass(frozen=True, slots=True)
class Version:
    """A PEP 440 release version, as the project and its CHANGELOG spell it"""

    text: str

    def __post_init__(self) -> None:
        if not _VERSION.fullmatch(self.text):
            raise InputError(f"not a PEP 440 release version: {self.text!r}")

    @classmethod
    def of_tag(cls, tag: str) -> Version:
        if not tag.startswith("v"):
            raise InputError(f"a release tag starts with 'v', got {tag!r}")
        return cls(tag[1:])

    @property
    def tag(self) -> str:
        return f"v{self.text}"


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Section:
    version: str
    date: str | None
    line: int
    body: tuple[str, ...]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def sections(changelog: str) -> list[Section]:
    found: list[Section] = []
    lines = changelog.splitlines()
    heads = [(i, m) for i, line in enumerate(lines) if (m := _SECTION.match(line))]
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = tuple(line for line in lines[i + 1 : end] if not _COMPARE.match(line))
        found.append(Section(m.group("version"), m.group("date"), i + 1, body))
    return found


def check_version(repo: Path, version: Version) -> Result:
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = str(project["version"])
    if declared == version.text:
        return Result("version", True, f"pyproject.toml declares {declared}")
    return Result("version", False, f"tag {version.tag} but pyproject.toml declares {declared}")


def check_on_main(repo: Path, main: str) -> Result:
    if git(repo, "rev-parse", "--verify", f"{main}^{{commit}}").returncode != 0:
        raise InputError(
            f"no such ref {main!r}: fetch it first (actions/checkout needs fetch-depth: 0)"
        )
    if git(repo, "merge-base", "--is-ancestor", "HEAD", main).returncode == 0:
        return Result("on-main", True, f"the tagged commit is on {main}")
    return Result("on-main", False, f"the tagged commit is not on {main}: tag a commit that landed")


def check_changelog(changelog: str, version: Version, today: dt.date) -> list[Result]:
    found = sections(changelog)
    names = [s.version for s in found]
    results: list[Result] = []

    unreleased = next((s for s in found if s.version == UNRELEASED), None)
    if unreleased is None:
        results.append(Result("unreleased-empty", False, "no '## [Unreleased]' section"))
    else:
        left = [line for line in unreleased.body if line.strip()]
        results.append(
            Result("unreleased-empty", True, "nothing left under Unreleased")
            if not left
            else Result(
                "unreleased-empty",
                False,
                f"Unreleased still holds entries, first: {left[0].strip()!r}",
            )
        )

    if version.text not in names:
        results.append(Result("section", False, f"no '## [{version.text}] - YYYY-MM-DD' section"))
        return results
    section = found[names.index(version.text)]
    if section.date is None:
        results.append(Result("section", False, f"'## [{version.text}]' has no date"))
    else:
        try:
            date = dt.date.fromisoformat(section.date)
        except ValueError:
            results.append(Result("section", False, f"'{section.date}' is not a YYYY-MM-DD date"))
        else:
            if date > today + dt.timedelta(days=1):
                results.append(Result("section", False, f"dated {date}, after today ({today})"))
            elif not [line for line in section.body if line.strip()]:
                results.append(Result("section", False, f"'## [{version.text}]' is empty"))
            else:
                results.append(
                    Result(
                        "section", True, f"'## [{version.text}] - {date}' at line {section.line}"
                    )
                )

    links = {m.group("name"): m for line in changelog.splitlines() if (m := _COMPARE.match(line))}
    released = [s.version for s in found if s.version != UNRELEASED]
    older = released[released.index(version.text) + 1 :]
    expected = {UNRELEASED: f"{version.tag}...HEAD"}
    if older:
        expected[version.text] = f"v{older[0]}...{version.tag}"
    broken: list[Result] = []
    for name, want in expected.items():
        link = links.get(name)
        if link is None:
            broken.append(Result("links", False, f"no '[{name}]: .../compare/{want}' link"))
        elif link.group("range") != want:
            broken.append(
                Result("links", False, f"[{name}] compares {link.group('range')}, want {want}")
            )
    ok = Result("links", True, ", ".join(f"[{n}] {w}" for n, w in expected.items()))
    return results + (broken or [ok])


def check_blockers(repo: Path) -> Result:
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--label",
            BLOCKER_LABEL,
            "--state",
            "open",
            "--json",
            "number,title",
        ],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    if proc.returncode != 0:
        raise InputError(f"gh issue list failed: {proc.stderr.strip()}")
    open_blockers = json.loads(proc.stdout)
    if not open_blockers:
        return Result("blockers", True, f"no open '{BLOCKER_LABEL}' issues")
    listed = ", ".join(f"#{i['number']} {i['title']}" for i in open_blockers)
    return Result("blockers", False, f"open '{BLOCKER_LABEL}' issues: {listed}")


def run(repo: Path, tag: str, main: str, today: dt.date, blockers: bool) -> list[Result]:
    version = Version.of_tag(tag)
    changelog = repo / "CHANGELOG.md"
    if not changelog.is_file() or not (repo / "pyproject.toml").is_file():
        raise InputError(f"{repo} has no CHANGELOG.md or pyproject.toml")
    results = [check_version(repo, version), check_on_main(repo, main)]
    results += check_changelog(changelog.read_text(encoding="utf-8"), version, today)
    if blockers:
        results.append(check_blockers(repo))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v1.0.0rc5")
    parser.add_argument(
        "--main", default="origin/main", help="the branch the tagged commit must be on"
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="checkout at the tagged commit"
    )
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, help="YYYY-MM-DD; default: today in UTC"
    )
    parser.add_argument(
        "--check-blockers", action="store_true", help=f"fail on open '{BLOCKER_LABEL}' issues (gh)"
    )
    args = parser.parse_args()
    today = args.today or dt.datetime.now(dt.UTC).date()
    try:
        results = run(args.repo.resolve(), args.tag, args.main, today, args.check_blockers)
    except InputError as exc:
        print(f"release_check: {exc}", file=sys.stderr)
        return 2
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.detail}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
