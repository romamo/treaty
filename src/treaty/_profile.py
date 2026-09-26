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
from ._parse import VALUED_GLOBALS

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
        # Globals may come before the path (tool --format json show x); drop them first
        tokens = list(_without_globals(tuple(t for t in tokens if t not in PREVIEW_FLAGS)))
        if tuple(tokens[: len(command.path.parts)]) == command.path.parts:
            return tuple(tokens)
    return None


def probes_for(app: App) -> list[Probe]:
    probes: list[Probe] = []
    for command in user_commands(app):
        if command.streaming:
            continue  # JSONL, and possibly endless: the kit's probes expect one envelope
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


def _without_globals(argv: tuple[str, ...]) -> tuple[str, ...]:
    """An example's own --format json would conflict with the value the kit moves around"""
    out: list[str] = []
    skip = False
    for tok in argv:
        name = tok[2:].partition("=")[0] if tok.startswith("--") else ""
        if skip:
            skip = False
        elif name in VALUED_GLOBALS:
            skip = "=" not in tok
        elif tok not in ("--help", "-h", "--schema"):
            out.append(tok)
    return tuple(out)


def argument_order_for(app: App) -> dict[str, object] | None:
    """The first example whose tokens after its positionals start with an option, so the kit
    can move ``--format`` around it (REQ-F-079); destructive ones are run with ``--dry-run``"""
    for command in user_commands(app):
        if command.danger_level is DangerLevel.MUTATING or command.streaming:
            continue
        argv = _argv_from_example(app, command)
        if argv is None:
            continue
        head = len(command.path.parts)
        while head < len(argv) and not argv[head].startswith("-"):
            head += 1
        local = list(argv[head:])
        if command.danger_level is DangerLevel.DESTRUCTIVE:
            local.append("--dry-run")
            if len(local) < 2:
                # Still a preview: the handler sees dry_run=True; the kit needs two tokens
                local.append("--confirm-destructive")
        if len(local) < 2 or "--" in local:
            continue
        return {
            "command_path": list(argv[:head]),
            "local_args": local,
            "global_flag": "--format",
            "value": "json",
            "alternate_value": "human",
        }
    return None


def build_profile(
    app: App, command: Sequence[str], probes: Sequence[Probe], *, beside_profile: bool = False
) -> dict[str, object]:
    """The kit resolves a slash-containing executable against the profile's directory,
    so a relative one the caller gave is made absolute against the current directory.
    ``beside_profile`` keeps a launcher treaty found next to the profile relative.
    ``absolute()``, not ``resolve()``: a venv's bin/python is a symlink that must stay one."""
    argv = list(command)
    if argv and "/" in argv[0] and not Path(argv[0]).is_absolute() and not beside_profile:
        argv[0] = str(Path(argv[0]).absolute())
    profile: dict[str, object] = {
        "schema_version": "1.0",
        "tool": f"{app.name} {app.version}",
        "command": argv,
        "timeout_seconds": 10,
        "manifest": ["manifest"],
    }
    order = argument_order_for(app)
    if order is not None:
        profile["argument_order"] = order
    profile["probes"] = [p.to_json() for p in probes]
    return profile


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
