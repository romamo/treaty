"""Conformance profile generation from a registry, and running the spec kit."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._audit import user_commands
from ._command import Command, DangerLevel
from ._parse import VALUED_GLOBALS, delegated_argv
from ._values import CommandPath

if TYPE_CHECKING:
    from ._app import App

PREVIEW_FLAGS = ("--dry-run", "--confirm-destructive", "--live")
TIMEOUT_SECONDS = 10
"""The kit's limit on each run of a probe; a run past it is killed and counts as a hang"""
STREAM_SECONDS = TIMEOUT_SECONDS // 2
"""``--timeout`` of a streaming command's read probe: with ``--no-stream`` it is a deadline
for the whole stream, which then ends with one envelope (the events, or TIMEOUT). Half the
kit's limit leaves the other half for interpreter startup and cleanup on a loaded machine"""


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    argv: tuple[str, ...]
    kind: str
    dry_run_flag: str | None = None
    passthrough: bool = False
    """The probed command hands every token after its path to its tool, so a flag added
    for treaty goes before the path (#367); not part of the profile"""

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
        if command.passthrough:
            # The words after the path are its tool's, verbatim: only the globals before it
            # are dropped. An example with the command's own flags there is not one (#367)
            split = delegated_argv(tokens, {command.path: command})
            if split is not None and split.argv[-len(command.path.parts) :] == list(
                command.path.parts
            ):
                return (*command.path.parts, *split.rest)
            continue
        # Globals may come before the path (tool --format json show x); drop them first
        preview = (*PREVIEW_FLAGS, _dry_run_flag(command))
        tokens = list(_without_globals(tuple(t for t in tokens if t not in preview)))
        if tuple(tokens[: len(command.path.parts)]) == command.path.parts:
            return tuple(tokens)
    return None


def _dry_run_flag(command: Command) -> str:
    """The flag that previews the command: ``--dry-run``, or its ``Flag(dry_run=True)``"""
    field = command.dry_run_field
    return "--dry-run" if field is None else f"--{field.flag}"


def _with_flags(command: Command, argv: tuple[str, ...], *flags: str) -> tuple[str, ...]:
    """``argv`` with framework ``flags`` where treaty reads them: after it, or before the
    path of a passthrough command, whose tool gets every token after the path (#367)"""
    return (*flags, *argv) if command.passthrough else (*argv, *flags)


def probes_for(app: App) -> list[Probe]:
    probes: list[Probe] = []
    for command in user_commands(app):
        if command.passthrough:
            # Its tool owns stdout and its envelope is on stderr, so the kit's json_envelope
            # check, which reads stdout, would fail every probe of it (#386)
            continue
        argv = _argv_from_example(app, command)
        if argv is None:
            if any(f.required for f in command.fields):
                continue
            argv = command.path.parts
        label = " ".join(command.path.parts)
        if command.resumable:
            # REQ-O-010: a step the command does not declare exits 2 before anything runs
            bad = _with_flags(command, argv, "--resume-from", "no-such-step")
            probes.append(Probe(f"{label} --resume-from unknown step", bad, "invalid"))
        if command.has_network_io:
            # REQ-O-019: urllib has no SOCKS, so --proxy refuses one before anything runs
            socks = _with_flags(command, argv, "--proxy", "socks5://127.0.0.1:1080")
            probes.append(Probe(f"{label} --proxy socks5", socks, "invalid"))
        if command.recursive_traversal:
            # REQ-O-040: a walk of no levels is not a limit
            shallow = _with_flags(command, argv, "--max-depth", "0")
            probes.append(Probe(f"{label} --max-depth 0", shallow, "invalid"))
        if command.streaming:
            # JSONL, and possibly endless, while the kit expects one envelope: the probes
            # above end before the stream starts, and a read is bounded (#349). A stream is
            # safe or mutating, and a mutating one, as elsewhere, gets no probe that runs it
            if command.danger_level is DangerLevel.SAFE:
                bounded = (*_without_stream_flags(argv), "--no-stream", "--timeout")
                probes.append(Probe(label, (*bounded, str(STREAM_SECONDS)), "read"))
        elif command.danger_level is DangerLevel.DESTRUCTIVE and command.safe_default:
            # Unconfirmed, a safe_default command previews and exits 0, and --live alone is
            # its confirmation: nothing refuses, so it is probed as the read its default is
            probes.append(Probe(label, argv, "read"))
        elif command.danger_level is DangerLevel.DESTRUCTIVE:
            probes.append(Probe(label, argv, "destructive", dry_run_flag=_dry_run_flag(command)))
        elif command.danger_level is DangerLevel.SAFE:
            probes.append(Probe(label, argv, "read", passthrough=command.passthrough))
    probes.append(Probe("version", ("version",), "read"))
    if CommandPath("status") in app.builtins:
        probes.append(Probe("status built-in", ("status",), "read"))  # REQ-O-028: always 0
    if CommandPath("cleanup") in app.builtins and not any(p.kind == "destructive" for p in probes):
        # The built-in cleanup is destructive and has --dry-run, so an app with no
        # destructive command of its own still gets the kit's dry-run and refusal checks
        # (#361). The kit runs it unconfirmed, which previews and exits 2, and with
        # --dry-run; never with --confirm-destructive, so nothing is removed
        cleanup = Probe("cleanup built-in", ("cleanup",), "destructive", dry_run_flag="--dry-run")
        probes.append(cleanup)
    # REQ-O-041: an etag that is not sha256:<32 hex> exits 2 before anything runs
    probes.append(Probe("manifest malformed etag", ("manifest", "--etag", "x"), "invalid"))
    first = next(p for p in probes if p.kind != "invalid")
    unknown = (*first.argv, "--no-such-flag")
    if first.passthrough:
        unknown = ("--no-such-flag", *first.argv)  # after the path, the tool would get it
    probes.append(Probe("unknown flag", unknown, "invalid"))
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


def _without_stream_flags(argv: tuple[str, ...]) -> tuple[str, ...]:
    """An example's own ``--stream``, ``--no-stream``, or ``--timeout`` would repeat or
    contradict the ones a streaming read probe adds"""
    out: list[str] = []
    skip = False
    for tok in argv:
        if skip:
            skip = False
        elif tok in ("--stream", "--no-stream"):
            continue
        elif tok == "--timeout":
            skip = True
        elif not tok.startswith("--timeout="):
            out.append(tok)
    return tuple(out)


def argument_order_for(app: App) -> dict[str, object] | None:
    """The first example whose tokens after its positionals start with an option, so the kit
    can move ``--format`` around it (REQ-F-079). A destructive one runs with its dry-run flag
    and never ``--confirm-destructive``, so one whose only option is that flag is skipped.
    Without one, the built-in ``manifest --etag``"""
    for command in user_commands(app):
        if command.danger_level is DangerLevel.MUTATING or command.streaming:
            continue
        if command.passthrough:
            continue  # the kit moves --format after the path, where the tool would get it
        argv = _argv_from_example(app, command)
        if argv is None:
            continue
        head = len(command.path.parts)
        while head < len(argv) and not argv[head].startswith("-"):
            head += 1
        local = list(argv[head:])
        if command.danger_level is DangerLevel.DESTRUCTIVE:
            # Never confirmed: a handler that ignores its dry-run flag would apply for real
            # on the kit's machine (#373). The kit needs two tokens, so the dry-run flag
            # alone moves the search on
            local.append(_dry_run_flag(command))
        if len(local) < 2 or "--" in local:
            continue
        return _order(list(argv[:head]), local)
    if CommandPath("manifest") in app.builtins:
        # No example has a local option: the built-in manifest has one and is safe. An
        # etag no manifest has, so the manifest is printed in full, never not_modified (#361)
        return _order(["manifest"], ["--etag", UNMATCHED_ETAG])
    return None


UNMATCHED_ETAG = "sha256:" + "0" * 32
"""A well-formed etag that is never a manifest's, for the argument_order fallback"""


def _order(command_path: list[str], local_args: list[str]) -> dict[str, object]:
    return {
        "command_path": command_path,
        "local_args": local_args,
        "global_flag": "--format",
        "value": "json",
        "alternate_value": "plain",
    }


def default_command(
    name: str, profile_dir: Path, scripts_dir: Path, *, windows: bool
) -> tuple[list[str], bool]:
    """The probes' argv when none is given, and whether it is relative to the profile

    On POSIX, the scaffold's ``/bin/sh`` launcher next to the profile. Windows cannot run
    it, so there the app's console script in ``scripts_dir`` (the running venv) takes its
    place, written relative to the profile like the launcher. Otherwise the app name, which
    the kit resolves through PATH."""
    if windows:
        script = scripts_dir / f"{name}.exe"
        if script.is_file():
            try:
                relative = Path(os.path.relpath(script, profile_dir)).as_posix()
            except ValueError:
                # On another drive than the profile, so only an absolute path reaches it
                return [str(script)], False
            # A bare name would be looked up on PATH instead of next to the profile
            return [relative if "/" in relative else f"./{relative}"], True
    else:
        launcher = profile_dir / name
        if launcher.is_file() and os.access(launcher, os.X_OK):
            return [f"./{name}"], True
    return [name], False


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
        "timeout_seconds": TIMEOUT_SECONDS,
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


def _probes_by_name(probes: object) -> dict[str, object]:
    if not isinstance(probes, list):
        return {}
    return {
        str(p["name"]): p for p in probes if isinstance(p, dict) and isinstance(p.get("name"), str)
    }


# Derived from the machine (the launcher, or the console script on Windows), never written
# by hand, so a profile that differs only here is refreshed without a conflict
_MACHINE_KEYS = frozenset({"command"})


def profile_matches(path: Path, profile: dict[str, object]) -> bool:
    """The profile at ``path`` equals ``profile`` as JSON; formatting and key order aside"""
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")) == profile)
    except json.JSONDecodeError:
        return False


def profile_drift(path: Path, profile: dict[str, object]) -> dict[str, object] | None:
    """How the profile at ``path`` differs from ``profile`` as JSON outside the machine keys,
    or None when it does not"""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"reason": f"not valid JSON: {exc.msg} at line {exc.lineno}"}
    if not isinstance(existing, dict):
        return {"reason": "not a JSON object"}
    if {k: v for k, v in existing.items() if k not in _MACHINE_KEYS} == {
        k: v for k, v in profile.items() if k not in _MACHINE_KEYS
    }:
        return None
    keys = sorted((existing.keys() | profile.keys()) - {"probes"} - _MACHINE_KEYS)
    ours, theirs = _probes_by_name(profile.get("probes")), _probes_by_name(existing.get("probes"))
    return {
        "changed_keys": [k for k in keys if existing.get(k) != profile.get(k)],
        "probes_only_in_file": sorted(theirs.keys() - ours.keys()),
        "probes_only_generated": sorted(ours.keys() - theirs.keys()),
        "probes_changed": sorted(n for n in ours.keys() & theirs.keys() if ours[n] != theirs[n]),
        "probe_order_changed": list(ours) != list(theirs),
    }


SPEC_FALLBACK = Path("../cli-agent-ergonomics")


def has_kit(spec_dir: Path) -> bool:
    return (spec_dir / "conformance" / "run.py").is_file()


@dataclass(frozen=True, slots=True)
class KitRun:
    exit_code: int
    envelope: dict[str, object] | None
    stderr: str


def run_kit(spec_dir: Path, profile: Path, timeout: float | None, env: Mapping[str, str]) -> KitRun:
    """The spec kit on ``profile``, through ``uv`` in the run's environment. The kit runs
    in a process group of its own, so a timeout stops ``uv`` and the kit it started,
    rather than orphaning the kit."""
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv is None:
        raise FileNotFoundError("uv is not on PATH; the conformance kit runs through uv")
    argv = [uv, "run", "--project", str(spec_dir), str(spec_dir / "conformance" / "run.py")]
    group: dict[str, Any]
    if sys.platform == "win32":
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group = {"start_new_session": True}
    proc = subprocess.Popen(
        [*argv, str(profile)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={k: v for k, v in env.items() if k != "VIRTUAL_ENV"},
        **group,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        proc.communicate()
        raise
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    return KitRun(proc.returncode, envelope, stderr)


def _kill_group(pid: int) -> None:
    """Kill ``pid`` and every process it started"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group ended on its own meanwhile
