"""A command's own cache, which ``--no-cache`` and ``--cache-ttl`` control (REQ-O-018).

Declared with ``cache=CachePolicy(ttl_seconds=3600)``, a command gets ``ctx.cache`` and
the two flags; entries live in ``$XDG_CACHE_HOME/<app>/<command>/`` (else
``~/.cache/...``), one ``0600`` file per key, named by its sha256. An entry older than
the TTL is missing; ``--no-cache`` or ``--cache-ttl 0`` makes every read miss and every
write do nothing. ``meta.cache_used`` says whether a read hit.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ._errors import ParseError, RegistrationError
from ._page import whole_number
from ._session import private_dir, private_file

NO_CACHE_FLAG = "no-cache"
CACHE_TTL_FLAG = "cache-ttl"


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """How long a command's ``ctx.cache`` entries stay fresh, in seconds"""

    ttl_seconds: int

    def __post_init__(self) -> None:
        ttl = self.ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise RegistrationError("CachePolicy(ttl_seconds=) is a whole number of seconds >= 1")


def cache_home(env: Mapping[str, str]) -> Path | None:
    """``$XDG_CACHE_HOME``, else ``~/.cache``; None without a home"""
    xdg = env.get("XDG_CACHE_HOME")
    if xdg and Path(xdg).is_absolute():  # a relative one is ignored, as the XDG spec says
        return Path(xdg)
    home = env.get("HOME")
    return Path(home) / ".cache" if home else None


def cache_dir(app_name: str, command: str, env: Mapping[str, str]) -> Path | None:
    home = cache_home(env)
    return None if home is None else home / app_name / command


def parse_ttl(raw: object) -> int:
    """``--cache-ttl``: whole seconds; 0 is ``--no-cache``"""
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        raise ParseError("'cache-ttl' expects whole seconds", context={"flag": CACHE_TTL_FLAG})
    return whole_number(raw, CACHE_TTL_FLAG, "whole seconds; 0 turns the cache off")


class Cache:
    """``ctx.cache``: bytes by key, fresh for ``ttl_seconds``; ``directory`` None or a
    TTL of 0 turns it off"""

    def __init__(self, directory: Path | None, ttl_seconds: int) -> None:
        self.directory = directory if ttl_seconds > 0 else None
        self.ttl_seconds = ttl_seconds
        self.used = False
        """A ``get`` hit: ``meta.cache_used``"""

    def get(self, key: str) -> bytes | None:
        """The bytes ``put`` under ``key`` within the TTL, else None"""
        path = self._path(key)
        if path is None:
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age > self.ttl_seconds:
                return None
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        self.used = True
        return data

    def put(self, key: str, data: bytes) -> None:
        """Keep ``data`` under ``key``; a later run's ``get`` finds it until the TTL"""
        if not isinstance(data, bytes):
            raise TypeError(f"ctx.cache.put takes bytes, not {type(data).__name__}")
        path = self._path(key)
        if path is None:
            return
        assert self.directory is not None
        home = self.directory.parent.parent
        home.mkdir(parents=True, exist_ok=True)
        private_dir(private_dir(self.directory.parent) / self.directory.name)
        partial = private_file(path.with_name(f".{path.name}.{uuid.uuid4().hex}"))
        partial.write_bytes(data)
        os.replace(partial, path)

    def _path(self, key: str) -> Path | None:
        if not isinstance(key, str):
            raise TypeError(f"a ctx.cache key is a str, not {type(key).__name__}")
        if self.directory is None:
            return None
        return self.directory / hashlib.sha256(key.encode("utf-8")).hexdigest()
