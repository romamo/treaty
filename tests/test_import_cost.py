"""``import treaty`` loads only what a typical run uses (#360): the HTTP client, the MCP
server, and package metadata load when a command needs them."""

import os
import subprocess
import sys

import pytest

import treaty
import treaty._http
import treaty._mcp_serve
import treaty._mcp_shared
import treaty._network

DEFERRED = ("http.client", "treaty._http", "treaty._mcp_serve", "importlib.metadata")


def _import_chains(module: str) -> dict[str, str]:
    """Each module ``import <module>`` loads, mapped to its importer chain, read from
    ``-X importtime`` in an isolated interpreter (``-I``: no PYTHON* variables, user site,
    or cwd on the path) with an empty environment, so no customisation imports anything"""
    keep = ("SYSTEMROOT",)  # Windows cannot start an interpreter without it
    done = subprocess.run(
        [sys.executable, "-I", "-X", "importtime", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=True,
        env={name: os.environ[name] for name in keep if name in os.environ},
    )
    # Each row is "import time: <self us> | <cumulative us> | <name indented 2 per level>",
    # and a module's row follows its children's, so reversed, an importer precedes them
    rows = [
        line.rpartition("|")[2]
        for line in done.stderr.splitlines()
        if line.startswith("import time:") and not line.endswith("| imported package")
    ]
    chains: dict[str, str] = {}
    stack: list[str] = []
    for field in reversed(rows):
        name = field.strip()
        del stack[(len(field) - len(field.lstrip()) - 1) // 2 :]
        stack.append(name)
        chains.setdefault(name, " <- ".join(reversed(stack)))
    assert module in chains, done.stderr
    return chains


@pytest.mark.parametrize(
    ("module", "deferred"),
    [
        ("treaty", DEFERRED),
        # treaty's own CLI builds App("treaty", version=__version__) at import, so it reads
        # package metadata on every run; the HTTP client and MCP server stay deferred
        ("treaty._cli", tuple(name for name in DEFERRED if name != "importlib.metadata")),
    ],
)
def test_import_loads_no_http_client_mcp_server_or_metadata(
    module: str, deferred: tuple[str, ...]
) -> None:
    chains = _import_chains(module)
    assert [chains[name] for name in deferred if name in chains] == []


def test_the_import_chain_names_each_importer() -> None:
    chains = _import_chains("treaty._cli")
    assert chains["importlib.metadata"] == "importlib.metadata <- treaty._cli"
    assert chains["treaty._app"] == "treaty._app <- treaty <- treaty._cli"


def test_the_deferred_names_keep_their_identity() -> None:
    assert treaty.McpServe is treaty._mcp_serve.McpServe
    assert treaty.McpTool is treaty._mcp_serve.McpTool
    assert treaty.NetworkSettings is treaty._http.NetworkSettings
    assert treaty.HttpResponse is treaty._http.HttpResponse
    assert treaty._mcp_serve.Bindings is treaty._mcp_shared.Bindings
    assert treaty._mcp_serve.NO_BINDINGS is treaty._mcp_shared.NO_BINDINGS
    assert treaty._http.NetworkFailure is treaty._network.NetworkFailure
    assert treaty._http.ProxyConfig is treaty._network.ProxyConfig


def test_version_and_star_import_resolve_the_deferred_names() -> None:
    from importlib.metadata import version

    assert treaty.__version__ == version("treaty")
    scope: dict[str, object] = {}
    exec("from treaty import *", scope)
    assert scope["McpServe"] is treaty._mcp_serve.McpServe
    assert set(treaty.__all__) <= set(scope)


def test_dir_lists_the_deferred_names_before_their_first_use() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import treaty; print(*dir(treaty))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert {"__version__", "McpServe", "McpTool", "App"} <= set(done.stdout.split())


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="NoSuchName"):
        treaty.NoSuchName  # noqa: B018
