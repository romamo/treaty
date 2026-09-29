"""Helpers a handler in another module calls, for the audit's call following (issue 14)."""

import functools
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def enter(directory: Path) -> str:
    os.chdir(directory)
    token = os.environ.get("OTHER_TOOL_TOKEN", "")
    urllib.request.urlopen("https://example.com")
    return token


def first(directory: Path) -> None:
    second(directory)


def second(directory: Path) -> None:
    third(directory)


def third(directory: Path) -> None:
    fourth(directory)


def fourth(directory: Path) -> None:
    os.chdir(directory)  # four calls from the handler, one past the depth


def encode(value: object) -> str:
    return json.dumps(value)


class _Lazy:
    """Answers every attribute, as sh, plumbum, a lazy loader, or a mock does"""

    def __getattr__(self, name: str) -> object:
        return _Lazy()

    def __call__(self, *args: object) -> str:
        return ""


lazy = _Lazy()

fetch_lambda = lambda url: urllib.request.urlopen(url, timeout=5).read()  # noqa: E731, S310


def slug(text: str) -> str:
    """Built without any requests to a server"""
    return urllib.parse.quote(text)


@functools.cache
def cached_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return bytes(response.read())


def aliased_fetch(url: str) -> bytes:
    import requests as r

    return bytes(r.get(url, timeout=5).content)


def untimed_fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310
        return bytes(response.read())


asked: list[str] = []


def __getattr__(name: str) -> object:
    """A lazy module's hook: following helpers must never run it"""
    asked.append(name)
    raise AttributeError(name)
