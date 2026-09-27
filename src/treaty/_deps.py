"""The tool's runtime dependencies (REQ-O-031) and the external tools commands need
(REQ-C-018), as ``doctor`` checks them.

A version is dotted numbers compared as numbers, ``1.19`` equal to ``1.19.0``; no
``packaging`` dependency. ``check_command`` is an argument list, since treaty never runs a
shell; the manifest shows it joined with ``shlex.join`` for an agent to read and run.
"""

from __future__ import annotations

import re
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ._envelope import NetworkContext, without_userinfo
from ._errors import RegistrationError
from ._http import NetworkFailure
from ._resources import refuse_async
from ._values import InvalidValue

if TYPE_CHECKING:
    from ._context import Ctx

_VERSION_RE = re.compile(r"\d{1,9}(\.\d{1,9}){0,7}")
_FIRST_VERSION = r"(\d+(?:\.\d+)+)"
DEPENDENCY_ABOVE_MAX = "DEPENDENCY_ABOVE_MAX"


@dataclass(frozen=True, slots=True)
class Version:
    """Dotted numeric version, such as ``1.19.0``; trailing zeros do not count"""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _VERSION_RE.fullmatch(self.value):
            raise InvalidValue(f"version {self.value!r} is not dotted numbers, such as 1.19.0")

    @property
    def key(self) -> tuple[int, ...]:
        parts = [int(p) for p in self.value.split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    def __lt__(self, other: Version) -> bool:
        return self.key < other.key

    def __gt__(self, other: Version) -> bool:
        return self.key > other.key


def version(value: object, where: str) -> Version:
    try:
        return Version(value)  # type: ignore[arg-type]  # checked by Version itself
    except InvalidValue as exc:
        raise RegistrationError(f"{where}: {exc}") from None


@dataclass(frozen=True, slots=True)
class Dependency:
    """An external binary, runtime, or library the tool needs, checked by ``doctor``

    ``check_command`` is the argument list that prints the installed version, such as
    ``("terraform", "version")``; ``version_regex`` extracts it from the output, its first
    group when it has one. ``fix_command`` installs or upgrades it, run verbatim by an
    agent. Above ``max_version`` is a compatibility warning, not a failure.
    """

    name: str
    check_command: Sequence[str]
    min_version: str
    fix_command: str
    version_regex: str = _FIRST_VERSION
    max_version: str | None = None

    def __post_init__(self) -> None:
        where = f"Dependency({self.name!r})"
        if not isinstance(self.name, str) or not self.name.strip():
            raise RegistrationError("Dependency(name=) is the tool's name, such as 'terraform'")
        argv = self.check_command
        if isinstance(argv, str) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise RegistrationError(
                f"{where}: check_command is an argument list, such as "
                f"({self.name!r}, '--version'); treaty never runs a shell"
            )
        object.__setattr__(self, "check_command", tuple(argv))
        version(self.min_version, f"{where}: min_version")
        if self.max_version is not None:
            version(self.max_version, f"{where}: max_version")
        if not isinstance(self.fix_command, str) or not self.fix_command.strip():
            raise RegistrationError(f"{where}: fix_command is the command that installs it")
        try:
            re.compile(self.version_regex)
        except (re.error, TypeError) as exc:
            raise RegistrationError(f"{where}: version_regex does not compile: {exc}") from None

    @property
    def minimum(self) -> Version:
        return Version(self.min_version)

    @property
    def maximum(self) -> Version | None:
        return None if self.max_version is None else Version(self.max_version)

    def to_json(self) -> dict[str, str]:
        """``DependencyEntry`` of the manifest root"""
        return {
            "name": self.name,
            "check_command": shlex.join(self.check_command),
            "version_regex": self.version_regex,
            "min_version": self.min_version,
            "fix_command": self.fix_command,
        }


def check_dependencies(dependencies: Sequence[object], app_name: str) -> tuple[Dependency, ...]:
    if isinstance(dependencies, (str, bytes)) or not all(
        isinstance(d, Dependency) for d in dependencies
    ):
        raise RegistrationError(f"App {app_name}: dependencies is a list of treaty.Dependency")
    names = [d.name for d in dependencies if isinstance(d, Dependency)]
    if len(set(names)) != len(names):
        raise RegistrationError(f"App {app_name}: dependencies names a tool twice")
    return tuple(d for d in dependencies if isinstance(d, Dependency))


def check_required_tools(where: str, tools: Mapping[str, str]) -> dict[str, Version]:
    """``required_tools={"dpkg-deb": "1.19.0"}``: program names to minimum versions"""
    if not isinstance(tools, Mapping):
        raise RegistrationError(
            f"{where}: required_tools maps a program to its minimum version, "
            'such as {"git": "2.30"}'
        )
    out: dict[str, Version] = {}
    for tool, minimum in tools.items():
        if not isinstance(tool, str) or not tool or tool != tool.strip() or " " in tool:
            raise RegistrationError(f"{where}: required_tools key {tool!r} is not a program name")
        out[tool] = version(minimum, f"{where}: required_tools[{tool!r}]")
    return out


@dataclass(frozen=True, slots=True)
class Found:
    """What ``doctor`` found for one program: the version, or why there is none"""

    version: Version | None
    error: str | None = None


def find(argv: Sequence[str], pattern: str, ctx: Ctx) -> Found:
    """Run ``argv`` (never a shell) and read the version from its output"""
    if shutil.which(argv[0], path=ctx.env.get("PATH")) is None:
        return Found(None, f"{argv[0]} is not on PATH")
    done = ctx.run(list(argv), check=False)
    match = re.search(pattern, done.stdout + "\n" + done.stderr)
    if match is None:
        return Found(None, f"no version in the output of {shlex.join(argv)}")
    text = match.group(1) if match.groups() else match.group(0)
    try:
        return Found(Version(text))
    except InvalidValue:
        return Found(None, f"{shlex.join(argv)} printed {text!r}, not a dotted version")


def dependency_result(dep: Dependency, found: Found, ctx: Ctx) -> dict[str, object]:
    """One entry of ``doctor``'s ``data.dependencies`` (REQ-O-031)"""
    ok = found.version is not None and not found.version < dep.minimum
    entry: dict[str, object] = {
        **dep.to_json(),
        "found_version": None if found.version is None else found.version.value,
        "ok": ok,
    }
    if dep.max_version is not None:
        entry["max_version"] = dep.max_version
    if found.error is not None:
        entry["error"] = found.error
    maximum = dep.maximum
    if ok and found.version is not None and maximum is not None and found.version > maximum:
        entry["compatible"] = False
        ctx.warn(
            DEPENDENCY_ABOVE_MAX,
            f"{dep.name} {found.version.value} is newer than the {maximum.value} it is known "
            "to work with",
            name=dep.name,
            found_version=found.version.value,
            max_version=maximum.value,
        )
    return entry


def tool_check(
    tool: str, minimum: Version, found: Found, fix: str | None, commands: Sequence[str]
) -> dict[str, object]:
    """One entry of ``doctor``'s ``data.checks``: a command's required tool (REQ-C-018)"""
    ok = found.version is not None and not found.version < minimum
    entry: dict[str, object] = {
        "name": tool,
        "ok": ok,
        "version": None if found.version is None else found.version.value,
        "required": minimum.value,
        "commands": sorted(commands),
    }
    if not ok:
        entry["fix"] = fix or f"install {tool} {minimum.value} or newer"
        entry["error"] = found.error or f"{tool} {entry['version']} is older than {minimum.value}"
    return entry


@dataclass(frozen=True, slots=True)
class Check:
    """One result of an ``App(checks=)`` callable, listed in ``doctor``'s ``data.checks``
    (REQ-O-026)

    A failing check (``ok=False``) carries ``fix``, the shell command that resolves it;
    one without fails ``doctor`` with ``INVALID_OUTPUT`` rather than leave an agent stuck.
    ``version`` is what was found and ``required`` what is needed, when they apply;
    ``network`` is how a failed network check went out (REQ-F-037).
    """

    name: str
    ok: bool
    fix: str | None = None
    version: str | None = None
    required: str | None = None
    error: str | None = None
    network: NetworkContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise RegistrationError("Check(name=) is what the check looks at, such as 'api'")
        if not isinstance(self.ok, bool):
            raise RegistrationError(f"Check({self.name!r}): ok is True or False")

    def to_json(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "name": self.name,
            "ok": self.ok,
            "version": self.version,
            "required": self.required,
        }
        if self.error is not None:
            entry["error"] = self.error
        if self.fix is not None:
            entry["fix"] = self.fix
        if self.network is not None:
            entry["network_context"] = self.network.to_json()
        return entry


CheckFn = Callable[["Ctx"], Check]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A ``doctor`` check that ``url`` answers, made by ``treaty.endpoint``"""

    url: str
    fix: str
    name: str

    def __call__(self, ctx: Ctx) -> Check:
        try:
            reply = ctx.http.get(self.url)
        except NetworkFailure as exc:
            return Check(self.name, False, self.fix, error=exc.message, network=exc.network)
        if reply.status < 400:
            return Check(self.name, True)
        route = ctx.http.proxies.route(self.url)
        network = NetworkContext(
            url=self.url,
            proxy_used=route.proxy,
            proxy_source=route.source,
            no_proxy=ctx.http.proxies.no_proxy,
            ssl_verify=True,
            suggestion=shlex.join(["curl", "-v", without_userinfo(self.url)]),
            status_code=reply.status,
        )
        error = f"{without_userinfo(self.url)} answered HTTP {reply.status}"
        return Check(self.name, False, self.fix, error=error, network=network)


def endpoint(url: str, *, fix: str, name: str | None = None) -> Endpoint:
    """A ``doctor`` check that a GET of ``url`` answers below 400, through ``ctx.http``
    so the proxy variables apply (REQ-F-036); a failure's ``error`` and
    ``network_context`` say how it went out (REQ-F-037). ``fix`` is the shell command to
    run when it fails; ``name`` defaults to the URL's host."""
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise RegistrationError(f"endpoint({url!r}): the URL is http:// or https:// with a host")
    if not isinstance(fix, str) or not fix.strip():
        raise RegistrationError(f"endpoint({url!r}): fix is the shell command that resolves it")
    return Endpoint(url, fix, parts.hostname if name is None else name)


def check_checks(checks: Sequence[object], app_name: str) -> tuple[CheckFn, ...]:
    """``App(checks=)``: callables of the ctx returning a ``treaty.Check``"""
    if isinstance(checks, (str, bytes)) or not all(callable(c) for c in checks):
        raise RegistrationError(
            f"App {app_name}: checks is a list of functions of the ctx returning a "
            "treaty.Check, or treaty.endpoint(url, fix=...)"
        )
    for check in checks:
        refuse_async(check, f"App {app_name}: checks")
    return tuple(checks)  # type: ignore[arg-type]  # each is callable, checked above
