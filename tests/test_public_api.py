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
from collections.abc import Callable, Iterable
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


type ParamKind = Literal[
    "positional_only", "positional_or_keyword", "keyword_only", "var_positional", "var_keyword"
]
_POSITIONAL = ("positional_only", "positional_or_keyword")
_NAME = re.compile(r"[A-Za-z_]\w*")
_CLOSE = {"[": "]", "(": ")", "{": "}", "<": ">"}


@dataclasses.dataclass(frozen=True)
class Param:
    """One parameter of a signature string, as ``inspect.signature`` prints it"""

    name: str
    kind: ParamKind
    annotation: str | None
    default: str | None


@dataclasses.dataclass(frozen=True)
class Signature:
    params: tuple[Param, ...]
    returns: str | None


def _split_top(text: str, sep: str | None) -> list[str]:
    """Split ``text`` at each ``sep`` outside quotes and brackets; with ``sep`` None, split
    once after the bracket that ``text`` opens with closes"""
    parts: list[str] = []
    stack: list[str] = []
    quote: str | None = None
    start = i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in _CLOSE:
            stack.append(_CLOSE[char])
        elif char in _CLOSE.values():
            if not stack or stack.pop() != char:
                raise ValueError(f"unbalanced {char!r} in {text!r}")
            if sep is None and not stack:
                return [text[: i + 1], text[i + 1 :]]
        elif sep is not None and not stack and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    if quote is not None or stack or sep is None:
        raise ValueError(f"unclosed quote or bracket in {text!r}")
    parts.append(text[start:])
    return parts


def parse_signature(text: str) -> Signature:
    """Parse a snapshot's signature string; raise ValueError on a form it doesn't know"""
    if not text.startswith("("):
        raise ValueError(f"a signature starts with '(': {text!r}")
    params_text, rest = _split_top(text, None)
    inner = params_text[1:-1]
    if rest == "":
        returns = None
    elif rest.startswith(" -> ") and rest[4:]:
        returns = rest[4:]
    else:
        raise ValueError(f"unexpected text after the parameters: {text!r}")
    params: list[Param] = []
    kind: ParamKind = "positional_or_keyword"
    pieces = _split_top(inner, ", ") if inner else []
    if "/" in pieces:
        kind = "positional_only"
    for piece in pieces:
        if piece == "/":
            kind = "positional_or_keyword"
            continue
        if piece == "*":
            kind = "keyword_only"
            continue
        stars = len(piece) - len(piece.lstrip("*"))
        name_match = _NAME.match(piece, stars)
        if stars > 2 or name_match is None:
            raise ValueError(f"cannot parse parameter {piece!r} of {text!r}")
        name, tail = name_match.group(), piece[name_match.end() :]
        annotation: str | None = None
        default: str | None = None
        if tail.startswith(": "):
            annotated = _split_top(tail[2:], " = ")
            if len(annotated) > 2 or not all(annotated):
                raise ValueError(f"cannot parse parameter {piece!r} of {text!r}")
            annotation = annotated[0]
            default = annotated[1] if len(annotated) == 2 else None
        elif tail.startswith("=") and tail[1:]:
            default = tail[1:]
        elif tail:
            raise ValueError(f"cannot parse parameter {piece!r} of {text!r}")
        if stars and default is not None:
            raise ValueError(f"a variadic parameter has no default: {piece!r} of {text!r}")
        if stars == 1:
            params.append(Param(name, "var_positional", annotation, None))
            kind = "keyword_only"
        elif stars == 2:
            params.append(Param(name, "var_keyword", annotation, None))
        else:
            params.append(Param(name, kind, annotation, default))
    names = [param.name for param in params]
    if len(set(names)) != len(names):
        raise ValueError(f"a parameter name repeats in {text!r}")
    return Signature(tuple(params), returns)


def signature_change(released: str, current: str, path: str) -> list[Change]:
    """A new signature is an addition when every old parameter keeps its name, kind,
    position (a positional one), annotation, and default, every new one has a default or
    is variadic, and the return annotation stays; anything else is a change"""
    before, now = parse_signature(released), parse_signature(current)
    by_name = {param.name: param for param in now.params}
    positions_before = [p.name for p in before.params if p.kind in _POSITIONAL]
    positions_now = [p.name for p in now.params if p.kind in _POSITIONAL]
    compatible = (
        before.returns == now.returns
        and all(by_name.get(param.name) == param for param in before.params)
        and positions_now[: len(positions_before)] == positions_before
    )
    old_names = {param.name for param in before.params}
    added = [param for param in now.params if param.name not in old_names]
    if not compatible or any(
        param.default is None and param.kind not in ("var_positional", "var_keyword")
        for param in added
    ):
        return [Change("changed", path)]
    return [Change("added", f"{path}({param.name})") for param in added]


def _live_field_has_default(export: str, field: str) -> bool:
    fields = {f.name: f for f in dataclasses.fields(getattr(treaty, export))}
    found = fields[field]
    return (
        found.default is not dataclasses.MISSING or found.default_factory is not dataclasses.MISSING
    )


type HasDefault = Callable[[str, str], bool]
_FIELDS = re.compile(r"exports/(?P<export>[^/]+)/fields")


def fields_change(
    released: list[object], current: list[object], path: str, export: str, has_default: HasDefault
) -> list[Change]:
    """A dataclass's fields: a removed one is removed, a new one appended after the kept
    ones with a default is added, and anything else (a new field without a default, one
    inserted among the kept ones, a reorder) is a change"""
    changes = [Change("removed", f"{path}[{item}]") for item in released if item not in current]
    kept = [item for item in released if item in current]
    if current[: len(kept)] != kept:
        return [*changes, Change("changed", f"{path} (order)")]
    for item in current[len(kept) :]:
        if has_default(export, str(item)):
            changes.append(Change("added", f"{path}[{item}]"))
        else:
            changes.append(Change("changed", f"{path}[{item}] (new field without a default)"))
    return changes


def snapshot_changes(
    released: object,
    current: object,
    path: str = "",
    *,
    has_default: HasDefault = _live_field_has_default,
) -> list[Change]:
    """Sort each difference: a key or list item only in ``current`` is added, one only in
    ``released`` is removed, and a different value or a new order of the kept items is
    changed; a signature and a dataclass's fields are compared item by item, so a
    compatible extension is added"""
    if isinstance(released, dict) and isinstance(current, dict):
        changes: list[Change] = []
        for key in sorted(released.keys() | current.keys()):
            where = f"{path}/{key}" if path else str(key)
            if key not in current:
                changes.append(Change("removed", where))
            elif key not in released:
                changes.append(Change("added", where))
            else:
                changes += snapshot_changes(
                    released[key], current[key], where, has_default=has_default
                )
        return changes
    if released == current and type(released) is type(current):
        return []
    if path.endswith("/signature") and isinstance(released, str) and isinstance(current, str):
        return signature_change(released, current, path)
    if (
        isinstance(released, list)
        and isinstance(current, list)
        and _scalars(released)
        and _scalars(current)
    ):
        fields = _FIELDS.fullmatch(path)
        if fields is not None:
            return fields_change(released, current, path, fields["export"], has_default)
        changes = [Change("removed", f"{path}[{item}]") for item in released if item not in current]
        changes += [Change("added", f"{path}[{item}]") for item in current if item not in released]
        kept_before = [item for item in released if item in current]
        kept_now = [item for item in current if item in released]
        if kept_before != kept_now:
            changes.append(Change("changed", f"{path} (order)"))
        return changes
    return [Change("changed", path)]


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


_SIGNATURE = "(name: 'str', /, flag: 'bool' = False, *, fix: 'str', note: 'str | None' = None)"


@pytest.mark.parametrize(
    ("current", "added"),
    [
        pytest.param(_SIGNATURE, [], id="same"),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, fix: 'str', note: 'str | None' = None, "
            "audit: 'bool' = True)",
            ["audit"],
            id="optional-keyword",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, extra: 'int' = 0, fix: 'str', "
            "note: 'str | None' = None)",
            ["extra"],
            id="optional-keyword-among-others",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, depth=1, *, fix: 'str', "
            "note: 'str | None' = None)",
            ["depth"],
            id="optional-positional-appended",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *rest: 'str', fix: 'str', "
            "note: 'str | None' = None, **extra: 'object')",
            ["rest", "extra"],
            id="variadic",
        ),
    ],
)
def test_a_compatible_signature_extension_is_added(current: str, added: list[str]) -> None:
    changes = signature_change(_SIGNATURE, current, "exports/f/signature")
    assert changes == [Change("added", f"exports/f/signature({name})") for name in added]
    assert unexcused_changes(changes, []) == []


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, fix: 'str', note: 'str | None' = None, "
            "level: 'int')",
            id="new-required-keyword",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = True, *, fix: 'str', note: 'str | None' = None)",
            id="changed-default",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'int' = False, *, fix: 'str', note: 'str | None' = None)",
            id="changed-annotation",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, fix: 'str')", id="removed-parameter"
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, fixes: 'str', note: 'str | None' = None)",
            id="renamed-parameter",
        ),
        pytest.param(
            "(name: 'str', /, *, flag: 'bool' = False, fix: 'str', note: 'str | None' = None)",
            id="changed-kind",
        ),
        pytest.param(
            "(name: 'str', /, depth=1, flag: 'bool' = False, *, fix: 'str', "
            "note: 'str | None' = None)",
            id="positional-inserted",
        ),
        pytest.param(
            "(flag: 'bool' = False, /, name: 'str', *, fix: 'str', note: 'str | None' = None)",
            id="positional-reordered",
        ),
        pytest.param(
            "(name: 'str', /, flag: 'bool' = False, *, fix: 'str', note: 'str | None' = None) "
            "-> 'int'",
            id="changed-return",
        ),
    ],
)
def test_an_incompatible_signature_is_changed(current: str) -> None:
    changes = signature_change(_SIGNATURE, current, "exports/f/signature")
    assert changes == [Change("changed", "exports/f/signature")]
    assert unexcused_changes(changes, []) == changes
    assert unexcused_changes(changes, [_BREAKING]) == []


def test_a_signature_in_the_snapshot_is_compared_by_parameter() -> None:
    current = _with(run={"kind": "function", "signature": "(argv=None, *, env=None)"})
    changes = snapshot_changes(_BASE, current)
    assert changes == [Change("added", "exports/run/signature(env)")]
    assert unexcused_changes(changes, []) == []


@pytest.mark.parametrize(
    "text",
    [
        "argv",
        "(argv",
        "(argv) int",
        "(argv: 'int)",
        "(***argv)",
        "(1argv)",
        "(argv: int = )",
        "(*args=1)",
        "(argv, argv)",
        "(argv: 'Callable[[int], int]'",
    ],
)
def test_a_signature_the_parser_does_not_know_fails_loudly(text: str) -> None:
    with pytest.raises(ValueError):
        parse_signature(text)


def _signatures(snapshot: object) -> list[str]:
    assert isinstance(snapshot, dict)
    exports = snapshot["exports"]
    assert isinstance(exports, dict)
    return [entry["signature"] for entry in exports.values() if "signature" in entry]


@pytest.mark.parametrize("path", [SNAPSHOT, RELEASED], ids=["snapshot", "released"])
def test_every_signature_in_the_snapshots_parses(path: Path) -> None:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    signatures = _signatures(snapshot.get("snapshot", snapshot))
    assert signatures
    for signature in signatures:
        parsed = parse_signature(signature)
        assert signature_change(signature, signature, "s") == []
        assert parsed.params or signature.startswith("()")


_DEFAULTS = {"summary": False, "count": True, "extra": True, "required": False}


def _has_default(export: str, field: str) -> bool:
    assert export == "Affects"
    return _DEFAULTS[field]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            ["summary", "count", "extra"],
            [Change("added", "exports/Affects/fields[extra]")],
            id="appended-with-default",
        ),
        pytest.param(
            ["summary", "count", "required"],
            [Change("changed", "exports/Affects/fields[required] (new field without a default)")],
            id="appended-required",
        ),
        pytest.param(
            ["summary", "extra", "count"],
            [Change("changed", "exports/Affects/fields (order)")],
            id="inserted-among-kept",
        ),
        pytest.param(
            ["count", "summary"],
            [Change("changed", "exports/Affects/fields (order)")],
            id="reordered",
        ),
        pytest.param(
            ["summary"], [Change("removed", "exports/Affects/fields[count]")], id="removed"
        ),
    ],
)
def test_a_dataclass_field_is_added_only_appended_with_a_default(
    fields: list[str], expected: list[Change]
) -> None:
    released = {"exports": {"Affects": {"kind": "class", "fields": ["summary", "count"]}}}
    current = {"exports": {"Affects": {"kind": "class", "fields": fields}}}
    assert snapshot_changes(released, current, has_default=_has_default) == expected


def test_a_live_dataclass_field_default_is_read_from_treaty() -> None:
    fields = {f.name: f for f in dataclasses.fields(treaty.Affects)}
    for name, found in fields.items():
        expected = (
            found.default is not dataclasses.MISSING
            or found.default_factory is not dataclasses.MISSING
        )
        assert _live_field_has_default("Affects", name) is expected


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
