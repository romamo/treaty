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
from ._command import OUTPUT_FLAG, Command, DangerLevel, Example
from ._parse import VALUED_GLOBALS
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
    deadline_seconds: int | None = None
    """A ``stream`` probe's limit on the whole stream"""
    sigint_after: int | None = None
    """A ``stream`` probe's SIGINT, sent after this many stdout lines"""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"name": self.name, "argv": list(self.argv), "kind": self.kind}
        if self.dry_run_flag is not None:
            out["dry_run_flag"] = self.dry_run_flag
        if self.deadline_seconds is not None:
            out["deadline_seconds"] = self.deadline_seconds
        if self.sigint_after is not None:
            out["signal"] = "INT"
            out["after_lines"] = self.sigint_after
        return out


def _argv_from_example(app: App, command: Command) -> tuple[str, ...] | None:
    """The first example's probe argv: its ``probe=``, else its command. An example with
    ``probe=False`` is left out (#392). Never a passthrough command's, which gets no probe
    and no argument_order run (#386)"""
    chosen = _chosen_example(app, command)
    return None if chosen is None else chosen[0]


def _chosen_example(app: App, command: Command) -> tuple[tuple[str, ...], Example] | None:
    """The probe argv of ``command`` and the example it comes from, the first one whose
    ``probe=`` or command runs it; an example with ``probe=False`` is left out (#392)"""
    for example in command.examples:
        if example.probe is False:
            continue
        argv = example_argv(
            command, example.command if example.probe is None else example.probe, app.name
        )
        if argv is not None:
            return argv, example
    return None


def _probed_explicitly(app: App, command: Command) -> bool:
    """The command's probe argv is an author's ``probe=``, such as one pointing a network
    command at a local stub: its read runs, where an example's would make real requests
    (#390, #392)"""
    chosen = _chosen_example(app, command)
    return chosen is not None and isinstance(chosen[1].probe, str)


def _opted_out(command: Command) -> bool:
    """Every example of the command says ``probe=False``: the author keeps the command out
    of the profile, so no probe falls back to its bare path either (#392)"""
    return bool(command.examples) and all(e.probe is False for e in command.examples)


def example_argv(
    command: Command, text: str, app_name: str | None = None
) -> tuple[str, ...] | None:
    """The argv a probe of ``command`` runs for the shell words ``text``, without the app's
    name, its globals, preview flags, and an ``output_file`` command's ``--output``; None
    when they do not run the command. A probe's words start with the app's name, which
    registration checked, so ``app_name`` None drops the first word"""
    tokens = shlex.split(text)
    if app_name is None or (tokens and tokens[0] == app_name):
        tokens = tokens[1:]
    # Globals may come before the path (tool --format json show x); drop them first
    preview = (*PREVIEW_FLAGS, _dry_run_flag(command))
    tokens = list(_without_globals(tuple(t for t in tokens if t not in preview)))
    if command.output_file:
        # Every kit run of the probe would write the file, an explicit probe= too (#391)
        tokens = list(_without_output(tuple(tokens)))
    if tuple(tokens[: len(command.path.parts)]) == command.path.parts:
        return tuple(tokens)
    return None


def _dry_run_flag(command: Command) -> str:
    """The flag that previews the command: ``--dry-run``, or its ``Flag(dry_run=True)``"""
    field = command.dry_run_field
    return "--dry-run" if field is None else f"--{field.flag}"


def _before_separator(argv: tuple[str, ...], *flags: str) -> tuple[str, ...]:
    """``argv`` with ``flags`` at its end, or before its ``--``: after it they would be
    positionals, and the probe would run the command instead of exiting 2 (#390)"""
    if "--" not in argv:
        return (*argv, *flags)
    cut = argv.index("--")
    return (*argv[:cut], *flags, *argv[cut:])


VERSION_PROBE = Probe("version", ("version",), "read")


def _probe_argv(app: App, command: Command) -> tuple[str, ...] | None:
    """The argv a probe runs ``command`` with: its first example, else its bare path when
    it needs no argument; None when it cannot be probed"""
    argv = _argv_from_example(app, command)
    if argv is None:
        if any(f.required for f in command.fields):
            return None
        argv = command.path.parts
    return argv


def _probed(command: Command) -> bool:
    """Whether ``probes_for`` probes the command at all: a passthrough command's tool owns
    stdout and its envelope is on stderr, so the kit's json_envelope check, which reads
    stdout, would fail every probe of it (#386); a command whose every example says
    probe=False is the author's (#392)"""
    return not command.passthrough and not _opted_out(command)


def _runnable_stream(app: App, command: Command) -> bool:
    """A stream the kit may run: safe, and without network I/O, whose every run would make
    the command's real, perhaps paid, requests, as a read probe would (#390), unless an
    author's probe= points it somewhere safe, such as a local stub, as for a read (#392)"""
    return (
        command.streaming
        and command.danger_level is DangerLevel.SAFE
        and (not command.has_network_io or _probed_explicitly(app, command))
    )


def probes_for(app: App) -> list[Probe]:
    probes: list[Probe] = []
    # The first probe that runs a command: the unknown-flag probe adds its flag to it
    unknown_from: Probe | None = None
    interrupted = False
    # The SIGINT probe goes to the first endless stream, which only a signal ends, else
    # to the first stream the kit may run (#389)
    endless = any(
        _probed(c) and _runnable_stream(app, c) and c.endless and _probe_argv(app, c) is not None
        for c in user_commands(app)
    )
    for command in user_commands(app):
        if not _probed(command):
            continue
        argv = _probe_argv(app, command)
        if argv is None:
            continue
        label = " ".join(command.path.parts)
        if command.resumable:
            # REQ-O-010: a step the command does not declare exits 2 before anything runs
            bad = _before_separator(argv, "--resume-from", "no-such-step")
            probes.append(Probe(f"{label} --resume-from unknown step", bad, "invalid"))
        if command.has_network_io:
            # REQ-O-019: urllib has no SOCKS, so --proxy refuses one before anything runs
            socks = _before_separator(argv, "--proxy", "socks5://127.0.0.1:1080")
            probes.append(Probe(f"{label} --proxy socks5", socks, "invalid"))
        if command.recursive_traversal:
            # REQ-O-040: a walk of no levels is not a limit
            shallow = _before_separator(argv, "--max-depth", "0")
            probes.append(Probe(f"{label} --max-depth 0", shallow, "invalid"))
        run: Probe | None = None
        streams: list[Probe] = []
        if command.streaming:
            # JSONL, and possibly endless, while a read probe expects one envelope: the
            # probes above end before the stream starts, and a read is bounded (#349). A
            # stream is safe or mutating, and a mutating one, as elsewhere, gets no probe
            # that runs it
            streamed = _without_stream_flags(argv)
            if command.danger_level is DangerLevel.SAFE:
                bounded = (*streamed, "--no-stream", "--timeout")
                run = Probe(label, (*bounded, str(STREAM_SECONDS)), "read")
            if _runnable_stream(app, command):
                # REQ-O-004's lines, within the kit's limit, unless the stream never ends
                # on its own; one stream is also interrupted after its first line, for
                # REQ-F-069's CANCELLED line (#389). A network stream gets neither unless
                # its probe is an author's probe=, as with its read probe (#390, #392)
                deadline = TIMEOUT_SECONDS
                if not command.endless:
                    streams.append(
                        Probe(f"{label} stream", streamed, "stream", deadline_seconds=deadline)
                    )
                if not interrupted and (command.endless or not endless):
                    interrupted = True
                    streams.append(
                        Probe(
                            f"{label} SIGINT",
                            streamed,
                            "stream",
                            deadline_seconds=deadline,
                            sigint_after=1,
                        )
                    )
        elif command.danger_level is DangerLevel.DESTRUCTIVE and command.safe_default:
            # Unconfirmed, a safe_default command previews and exits 0, and --live alone is
            # its confirmation: nothing refuses, so it is probed as the read its default is
            run = Probe(label, argv, "read")
        elif command.danger_level is DangerLevel.DESTRUCTIVE:
            run = Probe(label, argv, "destructive", dry_run_flag=_dry_run_flag(command))
        elif command.danger_level is DangerLevel.SAFE:
            run = Probe(label, argv, "read")
        if run is None:
            continue
        if unknown_from is None:
            # An unknown flag exits 2 before anything runs, so a network command's run is
            # one to add it to, though the profile has no read probe of it
            unknown_from = run
        if run.kind == "read" and command.has_network_io and not _probed_explicitly(app, command):
            # Each kit run of a read would make the command's real, perhaps paid, requests:
            # its probes are the ones above, which end before the network (#390). An
            # author's probe= points it somewhere safe, such as a local stub (#392)
            continue
        probes.append(run)
        probes.extend(streams)
    probes.append(VERSION_PROBE)
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
    first = VERSION_PROBE if unknown_from is None else unknown_from
    unknown = _before_separator(first.argv, "--no-such-flag")
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


def _without_output(argv: tuple[str, ...]) -> tuple[str, ...]:
    """An ``output_file`` command's ``--output PATH`` or ``--output=PATH``, with its value,
    so a probe returns the data in its envelope instead of writing a file (#391)"""
    out: list[str] = []
    skip = False
    flag = f"--{OUTPUT_FLAG}"
    for i, tok in enumerate(argv):
        if skip:
            skip = False
        elif tok == "--":
            return (*out, *argv[i:])  # after it, an --output token is a positional
        elif tok == flag:
            skip = True
        elif not tok.startswith(f"{flag}="):
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
    and never ``--confirm-destructive``, so one whose only option is that flag is skipped,
    as is a network command's that has no read probe, safe or ``safe_default``, which would
    make its real requests (#390). Without one, the built-in ``manifest --etag``"""
    for command in user_commands(app):
        if command.danger_level is DangerLevel.MUTATING or command.streaming:
            continue
        if command.passthrough:
            continue  # the kit moves --format after the path, where the tool would get it
        if (
            command.has_network_io
            and (command.danger_level is DangerLevel.SAFE or command.safe_default)
            and not _probed_explicitly(app, command)
        ):
            # Each run would make its real requests, as a read probe would (#390); a
            # safe_default command previews unconfirmed, so it runs as the read it is probed as.
            # An author's probe= runs as its read probe does (#392)
            continue
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
