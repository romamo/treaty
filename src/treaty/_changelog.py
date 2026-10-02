"""The schema changelog: what each release added, removed, and retyped in the manifest
(REQ-O-029).

``App(schema_changelog=path)`` names a JSON array of entries, each ``version``, ``date``,
``breaking``, ``added``, ``removed``, ``changed`` (field paths such as
``deploy.flags.target``, ``deploy.output.url``, ``deploy.exit_codes.10``), and the
manifest ``etag`` at that version. ``treaty changelog-add module:app`` writes it by diffing
the manifest snapshot kept beside it; the ``changelog`` built-in serves it. This is not
the prose ``CHANGELOG.md``, whose text no field list can come from.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ._errors import RegistrationError
from ._values import InvalidValue, ToolVersion

_KEYS = ("version", "date", "breaking", "added", "removed", "changed", "etag")
_NOTES = frozenset({"description", "title", "examples", "properties", "required", "items"})


type PreRelease = tuple[tuple[int, int, str], ...]


def version_key(version: str) -> tuple[int, int, int, int, PreRelease]:
    """Semver precedence (semver 11): a pre-release sorts before its release, and its
    dot-separated identifiers compare numerically when numeric, so ``rc.10`` follows
    ``rc.9``, and before any alphanumeric one; build metadata is ignored"""
    core, dash, pre = version.partition("+")[0].partition("-")
    major, minor, patch = (int(p) for p in core.split("."))
    identifiers = tuple(
        (0, int(part), "") if part.isascii() and part.isdigit() else (1, 0, part)
        for part in pre.split(".")
    )
    return major, minor, patch, 0 if dash else 1, identifiers if dash else ()


@dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """One release of the schema changelog"""

    version: str
    date: str
    breaking: bool
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    etag: str

    @classmethod
    def parse(cls, raw: object, where: str) -> ChangelogEntry:
        if not isinstance(raw, dict) or set(raw) != set(_KEYS):
            raise RegistrationError(f"{where}: an entry is an object with keys {', '.join(_KEYS)}")
        try:
            ToolVersion(raw["version"])
            dt.date.fromisoformat(raw["date"])
        except (InvalidValue, TypeError, ValueError) as exc:
            raise RegistrationError(f"{where}: {exc}") from None
        added, removed, changed = (raw[k] for k in ("added", "removed", "changed"))
        for paths in (added, removed, changed):
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise RegistrationError(f"{where}: added, removed, and changed are lists of paths")
        if not isinstance(raw["breaking"], bool) or not isinstance(raw["etag"], str):
            raise RegistrationError(f"{where}: breaking is true or false, etag a string")
        return cls(
            raw["version"],
            raw["date"],
            raw["breaking"],
            tuple(added),
            tuple(removed),
            tuple(changed),
            raw["etag"],
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "date": self.date,
            "breaking": self.breaking,
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
            "etag": self.etag,
        }


def load_changelog(path: Path, app_name: str) -> tuple[ChangelogEntry, ...]:
    """The entries, newest first; a missing file has none yet, a malformed one fails"""
    where = f"App {app_name}: schema_changelog {path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        raise RegistrationError(f"{where} cannot be read as JSON: {exc}") from None
    if not isinstance(raw, list):
        raise RegistrationError(f"{where} is a JSON array of entries")
    entries = [ChangelogEntry.parse(e, f"{where}, entry {n}") for n, e in enumerate(raw)]
    versions = [e.version for e in entries]
    if len(set(versions)) != len(versions):
        raise RegistrationError(f"{where} lists a version twice")
    return tuple(sorted(entries, key=lambda e: version_key(e.version), reverse=True))


def dump_changelog(entries: tuple[ChangelogEntry, ...]) -> str:
    return json.dumps([e.to_json() for e in entries], indent=2) + "\n"


def _signature(schema: object) -> str:
    """What a reader relies on in a schema, without its notes and nested fields"""
    if not isinstance(schema, dict):
        return json.dumps(schema, sort_keys=True)
    return json.dumps({k: v for k, v in schema.items() if k not in _NOTES}, sort_keys=True)


def _output_fields(schema: object, prefix: str, out: dict[str, str]) -> None:
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            out[f"{prefix}.{name}"] = _signature(sub)
            _output_fields(sub, f"{prefix}.{name}", out)
    if isinstance(schema.get("items"), dict):
        _output_fields(schema["items"], f"{prefix}[]", out)


def fields(manifest: Mapping[str, object]) -> tuple[dict[str, str], set[str]]:
    """Every field path of the manifest with its type signature, and the required flags"""
    out: dict[str, str] = {}
    required: set[str] = set()
    commands = manifest.get("commands", {})
    assert isinstance(commands, dict)
    for path, entry in commands.items():
        out[path] = "command"
        inputs = [(f"{path}.flags.{name}", flag) for name, flag in entry.get("flags", {}).items()]
        inputs += [(f"{path}.args.{arg['name']}", arg) for arg in entry.get("positionals", ())]
        for key, spec in inputs:
            out[key] = _signature(spec.get("type"))
            if spec.get("required"):
                required.add(key)
        for code, exit_entry in entry.get("exit_codes", {}).items():
            out[f"{path}.exit_codes.{code}"] = str(exit_entry.get("name"))
        _output_fields(entry.get("output_schema"), f"{path}.output", out)
    return out, required


@dataclass(frozen=True, slots=True)
class Diff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    breaking: bool

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff(old: Mapping[str, object] | None, new: Mapping[str, object]) -> Diff:
    """What changed between two manifests; breaking when anything was removed or retyped
    or a required flag or argument appeared"""
    before, was_required = fields(old) if old is not None else ({}, set())
    after, now_required = fields(new)
    # A passthrough entry lists no flags since ManifestResponse 3.9: the ones it takes go
    # before its path (REQ-C-031), so none of an older snapshot's is gone
    commands_now = new.get("commands", {})
    assert isinstance(commands_now, dict)
    passthrough = {p for p, e in commands_now.items() if e.get("arguments") == "passthrough"}
    before = {k: v for k, v in before.items() if not _passthrough_flag(k, passthrough)}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    # ManifestResponse 3.7 types an object flag "object", the same JSON-text argv token a
    # "string" took; a caller that passed it still does
    retyped = [k for k in changed if (before[k], after[k]) != (_STRING, _OBJECT)]
    # A new command's required flags break no caller; a required flag on an old one does
    commands = {k for k, v in before.items() if v == "command"}
    newly_required = {k for k in now_required - was_required if _owner(k) in commands}
    return Diff(
        tuple(added), tuple(removed), tuple(changed), bool(removed or retyped or newly_required)
    )


_STRING = _signature("string")
_OBJECT = _signature("object")


def _passthrough_flag(key: str, passthrough: set[str]) -> bool:
    """A ``<path>.flags.<name>`` key of a command in ``passthrough``"""
    path, kind, _ = key.partition(".flags.")
    return bool(kind) and path in passthrough


def _owner(key: str) -> str:
    """``deploy.rollback`` of ``deploy.rollback.flags.target``"""
    for kind in (".flags.", ".args."):
        if kind in key:
            return key.partition(kind)[0]
    return key


def record(
    entries: tuple[ChangelogEntry, ...], change: Diff, version: str, etag: str, today: dt.date
) -> tuple[tuple[ChangelogEntry, ...], ChangelogEntry]:
    """The changelog with ``change`` recorded under ``version``: a new entry, or merged
    into the latest when it already has that version"""
    latest = entries[0] if entries else None
    if latest is not None and latest.version == version:
        entry = ChangelogEntry(
            version,
            today.isoformat(),
            latest.breaking or change.breaking,
            tuple(sorted({*latest.added, *change.added} - set(change.removed))),
            tuple(sorted({*latest.removed, *change.removed} - set(change.added))),
            tuple(sorted({*latest.changed, *change.changed})),
            etag,
        )
        return (entry, *entries[1:]), entry
    entry = ChangelogEntry(
        version,
        today.isoformat(),
        change.breaking,
        change.added,
        change.removed,
        change.changed,
        etag,
    )
    return (entry, *entries), entry
