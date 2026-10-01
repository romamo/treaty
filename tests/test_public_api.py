"""The frozen public surface (docs/api.md): a change here fails CI until the snapshot in
``tests/data/public_api.json`` is updated on purpose, with a changelog entry.

Regenerate after a deliberate change with
``uv run python tests/test_public_api.py > tests/data/public_api.json``.
"""

import dataclasses
import enum
import inspect
import io
import json
import re
import sys
import types
from importlib.metadata import version
from pathlib import Path

import treaty
from treaty import App, Ctx, Group, NoArgs
from treaty._env import KNOWN, UNPREFIXED
from treaty._envelope import Envelope, ErrorDetail, Meta, WarningDetail

SNAPSHOT = Path(__file__).resolve().parent / "data" / "public_api.json"


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
            "fields": [f.name for f in dataclasses.fields(Envelope)],
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


if __name__ == "__main__":
    sys.stdout.write(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")
