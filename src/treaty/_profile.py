"""Conformance profile generation from a registry, and running the spec kit."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._audit import user_commands
from ._command import Command, DangerLevel

if TYPE_CHECKING:
    from ._app import App

PREVIEW_FLAGS = ("--dry-run", "--confirm-destructive")


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    argv: tuple[str, ...]
    kind: str
    dry_run_flag: str | None = None

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "argv": list(self.argv), "kind": self.kind}
        if self.dry_run_flag is not None:
            out["dry_run_flag"] = self.dry_run_flag
        return out


def _argv_from_example(app: App, command: Command) -> tuple[str, ...] | None:
    for example in command.examples:
        tokens = shlex.split(example.command)
        if tokens and tokens[0] == app.name:
            tokens = tokens[1:]
        if tuple(tokens[: len(command.path.parts)]) == command.path.parts:
            return tuple(t for t in tokens if t not in PREVIEW_FLAGS)
    return None


def probes_for(app: App) -> list[Probe]:
    probes: list[Probe] = []
    for command in user_commands(app):
        argv = _argv_from_example(app, command)
        if argv is None:
            if any(f.required for f in command.fields):
                continue
            argv = command.path.parts
        label = " ".join(command.path.parts)
        if command.danger_level is DangerLevel.DESTRUCTIVE:
            probes.append(Probe(label, argv, "destructive", dry_run_flag="--dry-run"))
        elif command.danger_level is DangerLevel.SAFE:
            probes.append(Probe(label, argv, "read"))
    probes.append(Probe("version", ("version",), "read"))
    first = probes[0]
    probes.append(Probe("unknown flag", (*first.argv, "--no-such-flag"), "invalid"))
    return probes


def build_profile(app: App, command: Sequence[str], probes: Sequence[Probe]) -> dict[str, object]:
    """The kit resolves a slash-containing executable against the profile's directory,
    so anything relative is made absolute against the current directory here"""
    argv = list(command)
    if argv and "/" in argv[0] and not Path(argv[0]).is_absolute():
        argv[0] = str(Path(argv[0]).resolve())
    return {
        "schema_version": "1.0",
        "tool": f"{app.name} {app.version}",
        "command": argv,
        "timeout_seconds": 10,
        "manifest": ["manifest"],
        "probes": [p.to_json() for p in probes],
    }


def write_profile(profile: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")


SPEC_FALLBACK = Path("../cli-agent-ergonomics")


def has_kit(spec_dir: Path) -> bool:
    return (spec_dir / "conformance" / "run.py").is_file()


@dataclass(frozen=True, slots=True)
class KitRun:
    exit_code: int
    envelope: dict[str, object] | None
    stderr: str


def run_kit(spec_dir: Path, profile: Path, timeout: float | None) -> KitRun:
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv is not on PATH; the conformance kit runs through uv")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    result = subprocess.run(
        [
            uv,
            "run",
            "--project",
            str(spec_dir),
            str(spec_dir / "conformance" / "run.py"),
            str(profile),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        envelope = None
    return KitRun(result.returncode, envelope, result.stderr)
