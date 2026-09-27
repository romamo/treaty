"""The tool's own environment variables, all under its prefix (REQ-F-073).

A variable treaty reads for an app is ``<APP>_<KEY>``: the app name uppercased, every
run of other characters an underscore, so ``my-tool`` reads ``MY_TOOL_FORMAT``. Nothing
unprefixed is read except the conventions every tool shares (``CI``, ``NO_COLOR``,
``TERM``, ``HOME``, ``XDG_*``, proxies) and ``TOOL_TRACE_ID``, which crosses tools.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._secrets import default_env_var

UNPREFIXED = frozenset(
    {
        "CI",
        "NO_COLOR",
        "HOME",
        "USER",
        "PATH",
        "SHELL",
        "TERM",
        "PWD",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "GITHUB_ACTIONS",
        "JENKINS_URL",
        "TOOL_TRACE_ID",
    }
)
"""Read without the prefix: shared conventions and CI detection (REQ-F-073, REQ-F-008)"""


def app_var(app_name: str, key: str) -> str:
    """``<APP>_<KEY>``, such as ``DEPLOYCTL_FORMAT`` for ``deployctl`` and ``format``"""
    return default_env_var(app_name, key)


@dataclass(frozen=True, slots=True)
class EnvVar:
    """One variable treaty reads for every app, by its key after the prefix"""

    key: str
    description: str


FORMAT = EnvVar("format", "Default --format when the flag is not passed")
MAX_OUTPUT_BYTES = EnvVar("max_output_bytes", "Default --max-output, in bytes")
MAX_STDIN_BYTES = EnvVar("max_stdin_bytes", "Most bytes read from a piped stdin")
STATE_DIR = EnvVar("state_dir", "Directory for idempotency records")
CONFIG = EnvVar("config", "Config file to read instead of the project and user files")
CONTEXT = EnvVar("context", "Named context of the config files to apply")
INSTANCE_ID = EnvVar("instance_id", "Instance namespace for the user config file and state")

KNOWN: tuple[EnvVar, ...] = (
    FORMAT,
    MAX_OUTPUT_BYTES,
    MAX_STDIN_BYTES,
    STATE_DIR,
)
"""Every variable treaty itself reads for an app; settings fields add their own"""
