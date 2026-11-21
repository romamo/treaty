"""The frozen public surface (docs/api.md): a change here fails CI until the snapshot in
``tests/data/public_api.json`` is updated on purpose, with a changelog entry.

Regenerate after a deliberate change with
``uv run python tests/test_public_api.py > tests/data/public_api.json``.

The snapshot is also compared with ``tests/data/public_api.released.json``, the snapshot as
of the last stable release: a removed or changed entry fails unless the changelog since that
release has a ``### Breaking`` entry. Refresh the baseline after a stable release X.Y.Z with
``uv run python tests/test_public_api.py --released X.Y.Z``, which reads tag vX.Y.Z.
"""

import dataclasses
import enum
import inspect
import io
import json
import re
import subprocess
import sys
import types
from collections.abc import Iterable
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import pytest

import treaty
from treaty import App, Ctx, Group, NoArgs
from treaty._env import KNOWN, UNPREFIXED
from treaty._envelope import Envelope, ErrorDetail, Meta, WarningDetail

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "data" / "public_api.json"
RELEASED = ROOT / "tests" / "data" / "public_api.released.json"
CHANGELOG = ROOT / "CHANGELOG.md"
FRAGMENTS = ROOT / "changelog.d"


def _signature(obj: object) -> str:
    # Windows prints the address in upper case
    return re.sub(r" at 0x[0-9a-fA-F]+", "", str(inspect.signature(obj)))  # type: ignore[arg-type]


def _export(name: str) -> dict[str, object]:
    obj = getattr(treaty, name)
    if isinstance(obj, types.GenericAlias):
        return {"kind": "alias", "value": repr(obj)}
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        return {"kind": "enum", "members": [m.name for m in obj]}
    if isinstance(obj, type):
        public = sorted(
            n
            for n, v in vars(obj).items()
            if not n.startswith("_") and (callable(v) or isinstance(v, property))
        )
        entry: dict[str, object] = {"kind": "class", "methods": public}
        if dataclasses.is_dataclass(obj):
            entry["fields"] = [
                f.name for f in dataclasses.fields(obj) if not f.name.startswith("_")
            ]
        return entry
    if callable(obj):
        return {"kind": "function", "signature": _signature(obj)}
    return {"kind": type(obj).__name__}


def _keywords(fn: object) -> list[str]:
    return [
        p.name
        for p in inspect.signature(fn).parameters.values()  # type: ignore[arg-type]
        if p.name != "self"
    ]


def _sample_manifest() -> dict[str, object]:
    app = App("tool", version="1.0.0")

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app.manifest()


def _envelope_keys() -> list[str]:
    out = io.StringIO()
    App("tool", version="1.0.0").run(
        ["--version"], stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return sorted(json.loads(out.getvalue()))


def inventory() -> dict[str, object]:
    """Every name, keyword, wire key, exit code, and env var 1.0 covers by semver"""
    manifest = _sample_manifest()
    commands = manifest["commands"]
    assert isinstance(commands, dict)
    return {
        "exports": {name: _export(name) for name in sorted(treaty.__all__)},
        "keywords": {
            "App": _keywords(App.__init__),
            "App.command": _keywords(App.command),
            "App.format": _keywords(App.format),
            "App.run": _keywords(App.run),
            "Group.command": ["name", "**App.command"],
            "Flag": _keywords(treaty.Flag),
            "Arg": _keywords(treaty.Arg),
            "Deprecated": _keywords(treaty.Deprecated),
        },
        "envelope": {
            "keys": _envelope_keys(),
            "meta": [f.name for f in dataclasses.fields(Meta)],
            "error": [
                f.name for f in dataclasses.fields(ErrorDetail) if not f.name.startswith("_")
            ],
            "warning": [f.name for f in dataclasses.fields(WarningDetail)],
            "fields": [f.name for f in dataclasses.fields(Envelope) if not f.name.startswith("_")],
        },
        "manifest": {
            "keys": sorted(manifest),
            "command_entry": sorted(commands["show"]),
            "flags": sorted(manifest["flags"]),  # type: ignore[arg-type]
        },
        "exit_codes": {m.name: int(m) for m in treaty.FrameworkCode},
        "env": {
            "prefixed": sorted(v.key.upper() for v in KNOWN),
            "unprefixed": sorted(UNPREFIXED),
        },
        "group_methods": sorted(n for n in vars(Group) if not n.startswith("_")),
    }


def test_the_public_surface_matches_the_frozen_snapshot() -> None:
    assert inventory() == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_manifest_framework_version_is_treatys_version_not_the_apps() -> None:
    assert _sample_manifest()["framework_version"] == version("treaty")


# The released baseline: a removed or changed entry needs a Breaking changelog entry (#464)

_VERSION = re.compile(r"\d+\.\d+\.\d+")
_UNRELEASED = "## [Unreleased]"


@dataclasses.dataclass(frozen=True)
class Change:
    """One difference between the released snapshot and the current one"""

    kind: Literal["added", "removed", "changed"]
    path: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.path}"


def _scalars(value: list[object]) -> bool:
    return all(isinstance(item, str | int) for item in value)


def snapshot_changes(released: object, current: object, path: str = "") -> list[Change]:
    """Sort each difference: a key or list item only in ``current`` is added, one only in
    ``released`` is removed, and a different value or a new order of the kept items is
    changed"""
    if isinstance(released, dict) and isinstance(current, dict):
        changes: list[Change] = []
        for key in sorted(released.keys() | current.keys()):
            where = f"{path}/{key}" if path else str(key)
            if key not in current:
                changes.append(Change("removed", where))
            elif key not in released:
                changes.append(Change("added", where))
            else:
                changes += snapshot_changes(released[key], current[key], where)
        return changes
    if (
        isinstance(released, list)
        and isinstance(current, list)
        and _scalars(released)
        and _scalars(current)
    ):
        changes = [Change("removed", f"{path}[{item}]") for item in released if item not in current]
        changes += [Change("added", f"{path}[{item}]") for item in current if item not in released]
        kept_before = [item for item in released if item in current]
        kept_now = [item for item in current if item in released]
        if kept_before != kept_now:
            changes.append(Change("changed", f"{path} (order)"))
        return changes
    if type(released) is not type(current) or released != current:
        return [Change("changed", path)]
    return []


def has_breaking_entry(text: str) -> bool:
    """Whether ``text`` holds an entry under a ``### Breaking`` heading, read as the release
    bot's major bump reads one: a ``- `` or ``* `` line after the heading"""
    heading: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = None
        elif line.startswith("### "):
            heading = line[4:].strip()
        elif line.startswith(("- ", "* ")) and heading == "Breaking":
            return True
    return False


def changelog_since(changelog: str, released: str) -> str:
    """CHANGELOG.md's text from ``## [Unreleased]`` down to the heading of ``released``:
    what is unreleased, and any release newer than the baseline"""
    lines = changelog.splitlines()
    starts = [i for i, line in enumerate(lines) if line.rstrip() == _UNRELEASED]
    if len(starts) != 1:
        raise ValueError(f"CHANGELOG.md needs exactly one {_UNRELEASED!r} heading")
    ends = [i for i, line in enumerate(lines) if line.startswith(f"## [{released}] - ")]
    if len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError(
            f"CHANGELOG.md needs one '## [{released}] - ' heading below {_UNRELEASED!r}, "
            "the release tests/data/public_api.released.json names"
        )
    return "\n".join(lines[starts[0] : ends[0]])


def unexcused_changes(changes: list[Change], changelog: Iterable[str]) -> list[Change]:
    """The removed and changed entries, unless a changelog text has a Breaking entry"""
    breaking = [change for change in changes if change.kind != "added"]
    if breaking and any(has_breaking_entry(text) for text in changelog):
        return []
    return breaking


def _released() -> tuple[str, dict[str, object]]:
    baseline = json.loads(RELEASED.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or sorted(baseline) != ["snapshot", "version"]:
        raise ValueError(f"{RELEASED.name} must be an object with 'snapshot' and 'version'")
    released, snapshot = baseline["version"], baseline["snapshot"]
    if not isinstance(released, str) or not _VERSION.fullmatch(released):
        raise ValueError(f"{RELEASED.name}: version {released!r} is not X.Y.Z")
    if not isinstance(snapshot, dict):
        raise ValueError(f"{RELEASED.name}: snapshot is not an object")
    return released, snapshot


def _changelog_texts(released: str) -> list[str]:
    since = changelog_since(CHANGELOG.read_text(encoding="utf-8"), released)
    fragments = sorted(p for p in FRAGMENTS.glob("*.md") if p.name != "README.md")
    return [since, *(p.read_text(encoding="utf-8") for p in fragments)]


def test_a_removed_or_changed_public_entry_needs_a_breaking_changelog_entry() -> None:
    released, baseline = _released()
    current = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    failing = unexcused_changes(snapshot_changes(baseline, current), _changelog_texts(released))
    assert not failing, (
        f"tests/data/public_api.json removes or changes {len(failing)} public API "
        f"entries treaty {released} released:\n"
        + "".join(f"  {change}\n" for change in failing)
        + "Restore them, or, if the break is meant, add a changelog.d/<issue>-<slug>.md "
        "fragment with a '### Breaking' entry saying how to migrate, so the release bumps "
        "the major version (docs/api.md)"
    )


_APP: dict[str, object] = {"kind": "class", "methods": ["command", "run"]}
_RUN: dict[str, object] = {"kind": "function", "signature": "(argv=None)"}
_BASE: dict[str, object] = {
    "exports": {"App": _APP, "run": _RUN},
    "exit_codes": {"OK": 0, "ARG_ERROR": 2},
}
_BREAKING = "### Breaking\n\n- `App.run` is gone: call `App.call` (#1)\n"
_FRAGMENT_TEXTS: list[tuple[str, ...]] = [(), ("### Added\n\n- `App.call` (#1)\n",)]


def _with(**exports: object) -> dict[str, object]:
    return {**_BASE, "exports": {"App": _APP, "run": _RUN, **exports}}


def test_an_added_name_passes_without_a_breaking_entry() -> None:
    current = _with(Group={"kind": "class", "methods": ["command"]})
    changes = snapshot_changes(_BASE, current)
    assert changes == [Change("added", "exports/Group")]
    assert unexcused_changes(changes, []) == []


def test_an_added_method_or_keyword_passes_without_a_breaking_entry() -> None:
    current = _with(App={"kind": "class", "methods": ["call", "command", "run"]})
    changes = snapshot_changes(_BASE, current)
    assert changes == [Change("added", "exports/App/methods[call]")]
    assert unexcused_changes(changes, []) == []


@pytest.mark.parametrize(
    "current",
    [
        pytest.param({**_BASE, "exports": {"App": _APP}}, id="export"),
        pytest.param(_with(App={"kind": "class", "methods": ["command"]}), id="method"),
    ],
)
@pytest.mark.parametrize("fragments", _FRAGMENT_TEXTS)
def test_a_removed_name_fails_without_a_breaking_entry(
    current: dict[str, object], fragments: tuple[str, ...]
) -> None:
    failing = unexcused_changes(snapshot_changes(_BASE, current), fragments)
    assert [change.kind for change in failing] == ["removed"]


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(_with(run={"kind": "function", "signature": "(argv)"}), id="signature"),
        pytest.param({**_BASE, "exit_codes": {"OK": 0, "ARG_ERROR": 3}}, id="exit-code"),
        pytest.param(_with(App={"kind": "class", "methods": ["run", "command"]}), id="order"),
        pytest.param(_with(run={"kind": "class", "methods": []}), id="kind"),
    ],
)
@pytest.mark.parametrize("fragments", _FRAGMENT_TEXTS)
def test_a_changed_entry_fails_without_a_breaking_entry(
    current: dict[str, object], fragments: tuple[str, ...]
) -> None:
    failing = unexcused_changes(snapshot_changes(_BASE, current), fragments)
    assert failing
    assert {change.kind for change in failing} <= {"changed", "removed"}


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(_with(App={"kind": "class", "methods": ["command"]}), id="removed"),
        pytest.param(_with(run={"kind": "function", "signature": "(argv)"}), id="changed"),
    ],
)
@pytest.mark.parametrize(
    "changelog",
    [
        pytest.param(("## [Unreleased]\n\n" + _BREAKING,), id="unreleased"),
        pytest.param(("", _BREAKING), id="fragment"),
        pytest.param(("", "### Breaking\n\n* `App.run` is gone (#1)\n"), id="star"),
    ],
)
def test_a_removed_or_changed_entry_passes_with_a_breaking_entry(
    current: dict[str, object], changelog: tuple[str, ...]
) -> None:
    changes = snapshot_changes(_BASE, current)
    assert changes
    assert unexcused_changes(changes, changelog) == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("### Breaking\n\n", id="heading-without-entry"),
        pytest.param("#### Breaking\n\n- x\n", id="h4"),
        pytest.param("### Breaking changes\n\n- x\n", id="other-heading"),
        pytest.param("### Breaking\n\n## [1.0.0] - 2026-11-03\n\n- x\n", id="next-section"),
        pytest.param("### Fixed\n\n- Breaking: x\n", id="word-in-entry"),
    ],
)
def test_only_an_entry_under_a_breaking_heading_counts(text: str) -> None:
    assert not has_breaking_entry(text)


_CHANGELOG = """# Changelog

## [Unreleased]

### Added

- x (#3)

## [2.0.0] - 2026-12-01

### Breaking

- y (#2)

## [1.1.0] - 2026-11-07

### Breaking

- z (#1)
"""


def test_the_changelog_since_the_baseline_holds_newer_releases_and_not_older_ones() -> None:
    assert has_breaking_entry(changelog_since(_CHANGELOG, "2.0.0")) is False
    assert has_breaking_entry(changelog_since(_CHANGELOG, "1.1.0")) is True


@pytest.mark.parametrize("released", ["3.0.0", "1.1"])
def test_a_baseline_release_missing_from_the_changelog_is_an_error(released: str) -> None:
    with pytest.raises(ValueError, match=re.escape(f"## [{released}] - ")):
        changelog_since(_CHANGELOG, released)


def test_the_baseline_names_a_stable_release() -> None:
    released, snapshot = _released()
    assert _VERSION.fullmatch(released)
    assert set(snapshot) == set(inventory())


def _write_released_baseline(released: str) -> None:
    """Write the baseline from tag ``v<released>``'s snapshot"""
    if not _VERSION.fullmatch(released):
        raise SystemExit(f"--released takes a stable version X.Y.Z, not {released!r}")
    shown = subprocess.run(
        ["git", "show", f"v{released}:tests/data/public_api.json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    baseline = {"snapshot": json.loads(shown.stdout), "version": released}
    RELEASED.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    match sys.argv[1:]:
        case []:
            sys.stdout.write(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")
        case ["--released", released]:
            _write_released_baseline(released)
        case _:
            raise SystemExit(
                "usage: test_public_api.py [--released X.Y.Z]: print the current snapshot, "
                "or write tests/data/public_api.released.json from tag vX.Y.Z"
            )
