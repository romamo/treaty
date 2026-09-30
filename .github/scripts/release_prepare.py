"""The release bot's rules: decide whether a release is due, and write its commit.

    plan   print the decision as JSON: release (with the next version) or skip (with why)
    apply  write the release commit's changes for a planned version
    quiet  print the policy's quiet_minutes: how long main must stay unchanged after a push

`apply` rewrites pyproject.toml, the project's entry in uv.lock, CHANGELOG.md (Unreleased
becomes a dated section with a summary line and compare links), and the policy's
version_lines. It commits nothing.

Exit codes: 0 done (a plan may decide to skip); 2 bad input, such as a malformed policy,
version, or CHANGELOG, or a heading the policy doesn't map.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_check import UNRELEASED, InputError, check_blockers, sections  # noqa: E402

_RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_PARTS = ("patch", "minor", "major")  # ascending
_MODES = ("off", "dry-run", "release")
_POLICY_KEYS = {"mode", "min_days_between", "quiet_minutes", "bump", "version_lines"}
_MAX_QUIET = 300  # a GitHub job runs at most 6 hours; leave room for the rest of the run
_LINE_KEYS = {"file", "pattern", "replace", "when"}
_WORDS = {
    "Breaking": ("breaking change", "breaking changes"),
    "Added": ("addition", "additions"),
    "Changed": ("change", "changes"),
    "Fixed": ("fix", "fixes"),
    "Deprecated": ("deprecation", "deprecations"),
    "Removed": ("removal", "removals"),
    "Security": ("security fix", "security fixes"),
}
_ORDINALS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth"
).split()
_WIDTH = 90


@dataclass(frozen=True, slots=True)
class Release:
    """X.Y.Z, optionally with an a, b, or rc pre-release number; the forms the bot cuts"""

    major: int
    minor: int
    patch: int
    pre: str | None = None
    n: int | None = None

    @classmethod
    def parse(cls, text: str) -> Release:
        m = _RELEASE.fullmatch(text)
        if m is None:
            raise InputError(f"the bot handles X.Y.Z and X.Y.Z{{a,b,rc}}N versions, got {text!r}")
        pre, n = m.group(4), m.group(5)
        return cls(int(m[1]), int(m[2]), int(m[3]), pre, None if n is None else int(n))

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.pre is None else f"{base}{self.pre}{self.n}"

    @property
    def is_pre(self) -> bool:
        return self.pre is not None

    @property
    def semver(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.pre is None else f"{base}-{_SEMVER_PRE[self.pre]}.{self.n}"

    def next_pre(self) -> Release:
        assert self.n is not None
        return dataclasses.replace(self, n=self.n + 1)

    def bump(self, part: str) -> Release:
        if part == "major" and self.major > 0:
            return Release(self.major + 1, 0, 0)
        if part in ("major", "minor"):  # before 1.0 a breaking change bumps the minor
            return Release(self.major, self.minor + 1, 0)
        return Release(self.major, self.minor, self.patch + 1)


@dataclass(frozen=True, slots=True)
class VersionLine:
    file: str
    pattern: re.Pattern[str]
    replace: str
    prerelease_only: bool


@dataclass(frozen=True, slots=True)
class Policy:
    mode: str
    min_days_between: int
    quiet_minutes: int  # after a push to main, how long no other push must come
    bump: dict[str, str]  # CHANGELOG heading -> "major", "minor", or "patch"
    version_lines: tuple[VersionLine, ...]

    @classmethod
    def load(cls, path: Path) -> Policy:
        if not path.is_file():
            raise InputError(f"no release policy at {path}")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if unknown := set(raw) - _POLICY_KEYS:
            raise InputError(f"{path.name}: unknown keys {sorted(unknown)}")
        mode = raw.get("mode")
        if mode not in _MODES:
            raise InputError(f"{path.name}: mode must be one of {_MODES}, got {mode!r}")
        days = raw.get("min_days_between")
        if not isinstance(days, int) or isinstance(days, bool) or days < 0:
            raise InputError(f"{path.name}: min_days_between must be an integer >= 0")
        quiet = raw.get("quiet_minutes")
        if not isinstance(quiet, int) or isinstance(quiet, bool) or not 0 <= quiet <= _MAX_QUIET:
            raise InputError(f"{path.name}: quiet_minutes must be an integer in 0..{_MAX_QUIET}")
        bump: dict[str, str] = {}
        table = raw.get("bump")
        if not isinstance(table, dict) or set(table) - set(_PARTS):
            raise InputError(f"{path.name}: [bump] takes only {_PARTS}")
        for part, headings in table.items():
            if not isinstance(headings, list) or not all(isinstance(h, str) for h in headings):
                raise InputError(f"{path.name}: bump.{part} must be a list of headings")
            for heading in headings:
                if heading in bump:
                    raise InputError(f"{path.name}: heading {heading!r} is in two bump lists")
                bump[heading] = part
        lines = []
        for i, entry in enumerate(raw.get("version_lines", [])):
            if not isinstance(entry, dict) or set(entry) - _LINE_KEYS:
                raise InputError(f"{path.name}: version_lines[{i}] takes only {sorted(_LINE_KEYS)}")
            if not all(isinstance(entry.get(k), str) for k in ("file", "pattern", "replace")):
                raise InputError(f"{path.name}: version_lines[{i}] needs file, pattern, replace")
            when = entry.get("when", "always")
            if when not in ("always", "prerelease"):
                raise InputError(f"{path.name}: version_lines[{i}].when is always or prerelease")
            try:
                pattern = re.compile(entry["pattern"])
            except re.error as exc:
                raise InputError(f"{path.name}: version_lines[{i}].pattern: {exc}") from None
            lines.append(
                VersionLine(entry["file"], pattern, entry["replace"], when == "prerelease")
            )
        return cls(mode, days, quiet, bump, tuple(lines))


def project(repo: Path) -> tuple[str, Release]:
    table = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(table["name"]), Release.parse(str(table["version"]))


def unreleased_counts(changelog: str) -> dict[str, int]:
    """Top-level entries per ### heading under Unreleased, in CHANGELOG order"""
    found = [s for s in sections(changelog) if s.version == UNRELEASED]
    if len(found) != 1:
        raise InputError("CHANGELOG.md needs exactly one '## [Unreleased]' section")
    counts: dict[str, int] = {}
    heading = None
    for line in found[0].body:
        if line.startswith("### "):
            heading = line[4:].strip()
            counts.setdefault(heading, 0)
        elif line.startswith("- "):
            if heading is None:
                raise InputError(f"an entry under Unreleased has no ### heading: {line!r}")
            counts[heading] += 1
    return {h: n for h, n in counts.items() if n}


def latest_release_date(changelog: str) -> dt.date | None:
    for section in sections(changelog):
        if section.version != UNRELEASED and section.date is not None:
            return dt.date.fromisoformat(section.date)
    return None


def plan(
    repo: Path, policy: Policy, today: dt.date, dry_run: bool, blockers: bool
) -> dict[str, str]:
    mode = "dry-run" if dry_run and policy.mode != "off" else policy.mode
    decision = {"mode": mode, "date": today.isoformat()}
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    _, current = project(repo)
    decision["current"] = str(current)

    def skip(reason: str) -> dict[str, str]:
        return decision | {"action": "skip", "reason": reason}

    if mode == "off":
        return skip("the policy's mode is off")
    counts = unreleased_counts(changelog)
    if not counts:
        return skip("nothing under Unreleased")
    if unknown := [h for h in counts if h not in policy.bump]:
        raise InputError(f"headings the policy doesn't map: {unknown}; add them to [bump]")
    last = latest_release_date(changelog)
    if last is not None and (today - last).days < policy.min_days_between:
        waited = (today - last).days
        return skip(
            f"the latest release is {waited} day(s) old; the policy waits {policy.min_days_between}"
        )
    if blockers and not (result := check_blockers(repo)).passed:
        return skip(result.detail)
    if current.is_pre:
        version, why = current.next_pre(), "the next pre-release"
    else:
        part = max((policy.bump[h] for h in counts), key=_PARTS.index)
        version, why = current.bump(part), f"a {part} bump"
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if head.returncode != 0:
        raise InputError(f"not a git checkout: {head.stderr.strip()}")
    listing = ", ".join(f"{n} {h}" for h, n in counts.items())
    return decision | {
        "action": "release",
        "version": str(version),
        "base": head.stdout.strip(),
        "reason": f"{listing} under Unreleased; {version} is {why}",
    }


def summary(
    name: str, version: Release, current: Release, counts: dict[str, int], policy: Policy
) -> str:
    phrases = []
    for heading, n in counts.items():
        one, many = _WORDS.get(heading, (f"{heading} entry", f"{heading} entries"))
        phrases.append(f"{n} {one if n == 1 else many}")
    if len(phrases) <= 2:
        listing = " and ".join(phrases)
    else:
        listing = ", ".join(phrases[:-1]) + f", and {phrases[-1]}"
    if version.is_pre:
        assert version.n is not None
        nth = _ORDINALS[version.n - 1] if version.n <= len(_ORDINALS) else f"{version.n}th"
        head = f"The {nth} {version.major}.{version.minor} release candidate"
    else:
        head = f"{name} {version}"
    text = f"{head}: {listing}."
    breaking = [h for h in counts if policy.bump[h] == "major"]
    if breaking:
        before = f"{current.pre}{current.n}" if current.is_pre else str(current)
        text += f" Not additive over {before}: see {' and '.join(breaking)}."
    return textwrap.fill(text, _WIDTH)


def replace_once(text: str, pattern: str | re.Pattern[str], new: str, where: str) -> str:
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    hits = len(compiled.findall(text))
    if hits != 1:
        raise InputError(f"{where}: {compiled.pattern!r} matches {hits} times, want exactly 1")
    return compiled.sub(lambda _: new, text)


def apply(repo: Path, policy: Policy, version: Release, date: dt.date) -> list[str]:
    name, current = project(repo)
    if version == current:
        raise InputError(f"{version} is already the project's version")
    changed = []

    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        replace_once(
            pyproject.read_text(encoding="utf-8"),
            rf'(?m)^version = "{re.escape(str(current))}"$',
            f'version = "{version}"',
            "pyproject.toml",
        ),
        encoding="utf-8",
    )
    changed.append("pyproject.toml")

    lock = repo / "uv.lock"
    if lock.is_file():
        entry = (
            rf'name = "{re.escape(name)}"\nversion = "{re.escape(str(current))}"\n'
            r'source = \{ editable = "\." \}'
        )
        new = f'name = "{name}"\nversion = "{version}"\nsource = {{ editable = "." }}'
        lock.write_text(
            replace_once(lock.read_text(encoding="utf-8"), entry, new, "uv.lock"), encoding="utf-8"
        )
        changed.append("uv.lock")

    path = repo / "CHANGELOG.md"
    changelog = path.read_text(encoding="utf-8")
    counts = unreleased_counts(changelog)
    if not counts:
        raise InputError("nothing under Unreleased to release")
    lines = changelog.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"## [{UNRELEASED}]")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    body = "".join(lines[start + 1 : end]).strip("\n")
    heading = f"## [{version}] - {date.isoformat()}"
    section = f"{heading}\n\n{summary(name, version, current, counts, policy)}\n\n{body}\n\n"
    changelog = "".join(lines[: start + 1]) + "\n" + section + "".join(lines[end:])
    link = re.compile(
        rf"(?m)^\[{UNRELEASED}\]: (\S+)/compare/v{re.escape(str(current))}\.\.\.HEAD$"
    )
    m = link.search(changelog)
    if m is None or len(link.findall(changelog)) != 1:
        raise InputError(
            f"CHANGELOG.md needs one '[{UNRELEASED}]: .../compare/v{current}...HEAD' link"
        )
    base = m.group(1)
    links = (
        f"[{UNRELEASED}]: {base}/compare/v{version}...HEAD\n"
        f"[{version}]: {base}/compare/v{current}...v{version}"
    )
    path.write_text(changelog[: m.start()] + links + changelog[m.end() :], encoding="utf-8")
    changed.append("CHANGELOG.md")

    values = {"version": str(version), "semver": version.semver, "date": date.isoformat()}
    values["pre"] = f"{version.pre}{version.n}" if version.is_pre else ""
    for line in policy.version_lines:
        if line.prerelease_only and not version.is_pre:
            continue
        target = repo / line.file
        if not target.is_file():
            raise InputError(f"version_lines names {line.file}, which doesn't exist")
        text = replace_once(
            target.read_text(encoding="utf-8"),
            line.pattern,
            line.replace.format(**values),
            line.file,
        )
        target.write_text(text, encoding="utf-8")
        changed.append(line.file)
    return changed


def github_output(decision: dict[str, str]) -> str:
    """``key=value`` lines for $GITHUB_OUTPUT, one per key. A reason can quote an issue
    title, and a newline in it would otherwise let the title set outputs such as action."""
    return "".join(f"{k}={' '.join(v.splitlines())}\n" for k, v in decision.items())


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "quiet"):
        p = sub.add_parser(name)
        p.add_argument(
            "--repo", type=Path, default=Path.cwd(), help="checkout of the default branch"
        )
        p.add_argument("--policy", type=Path, help="default: .github/release-policy.toml in --repo")
    p_plan = sub.choices["plan"]
    p_plan.add_argument(
        "--today", type=dt.date.fromisoformat, help="YYYY-MM-DD; default: today in UTC"
    )
    p_plan.add_argument(
        "--dry-run", action="store_true", help="plan as dry-run whatever the policy's mode"
    )
    p_plan.add_argument(
        "--check-blockers", action="store_true", help="skip while release-blocker issues are open"
    )
    p_plan.add_argument(
        "--github-output", type=Path, help="also append the decision as key=value lines"
    )
    p_apply = sub.choices["apply"]
    p_apply.add_argument("--version", required=True, help="the planned version, e.g. 1.0.0rc6")
    p_apply.add_argument("--date", required=True, type=dt.date.fromisoformat, help="YYYY-MM-DD")
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        policy = Policy.load(args.policy or repo / ".github" / "release-policy.toml")
        if args.command == "quiet":
            print(policy.quiet_minutes)
        elif args.command == "plan":
            today = args.today or dt.datetime.now(dt.UTC).date()
            decision = plan(repo, policy, today, args.dry_run, args.check_blockers)
            print(json.dumps(decision, indent=2))
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as out:
                    out.write(github_output(decision))
        else:
            for changed in apply(repo, policy, Release.parse(args.version), args.date):
                print(f"updated {changed}")
    except InputError as exc:
        print(f"release_prepare: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
