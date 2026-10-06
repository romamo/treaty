"""``import treaty`` loads only what a typical run uses (#360): the HTTP client, the MCP
server, and package metadata load when a command needs them."""

import subprocess
import sys

import pytest

import treaty
import treaty._http
import treaty._mcp_serve
import treaty._mcp_shared
import treaty._network

DEFERRED = ("http.client", "treaty._http", "treaty._mcp_serve", "importlib.metadata")


def test_import_treaty_loads_no_http_client_mcp_server_or_metadata() -> None:
    done = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import treaty"],
        capture_output=True,
        text=True,
        check=True,
    )
    # Each line ends "| <self us> | <cumulative us> | <indented module name>"
    loaded = {line.rpartition("|")[2].strip() for line in done.stderr.splitlines()}
    assert "treaty" in loaded, done.stderr
    assert sorted(loaded.intersection(DEFERRED)) == []


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


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="NoSuchName"):
        treaty.NoSuchName  # noqa: B018
