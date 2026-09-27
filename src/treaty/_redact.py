"""What a secret name is, and the redaction the log layers apply (REQ-F-034).

One vocabulary: an input field inferred secret, a setting ``--show-config`` hides, a
``ctx.log`` field, and an error context key printed on stderr all match ``SECRET_NAME``.
Output ``data`` is not redacted (REQ-F-034 is log-layer only); ``_protect`` masks it,
using the narrower ``secret_field`` so ``author`` and ``token_count`` stay readable.
"""

from __future__ import annotations

import re
from collections.abc import Callable

REDACTED = "[REDACTED]"

# REQ-F-034's six substrings plus cookie and a pass segment: API_KEY, *_TOKEN, DB_PASS,
# Authorization, Cookie, X-Api-Key, AUTH_URL, ...
SECRET_NAME = re.compile(
    r"token|secret|password|key|credential|auth|cookie|(^|[_-])pass($|[_-])", re.IGNORECASE
)

# The last word of an output field that holds a credential by its name
_SECRET_WORDS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "pass",
        "credential",
        "credentials",
        "cookie",
        "auth",
        "authorization",
        "apikey",
    }
)
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def secret_name(name: str) -> bool:
    """A name whose value is written as ``[REDACTED]`` in logs and diagnostics"""
    return SECRET_NAME.search(name) is not None


def secret_field(name: str) -> bool:
    """An output field whose name says it holds a credential: its last word is one, as in
    ``access_token``, ``client_secret``, or ``api_key`` (``key`` counts after another word).
    ``token_count``, ``author``, and a bare ``key`` of a key/value listing do not."""
    words = [w.lower() for w in _WORD.findall(name)]
    if not words:
        return False
    return words[-1] in _SECRET_WORDS or (words[-1] == "key" and len(words) > 1)


def redacted(value: object, redact: Callable[[str], str]) -> object:
    """Every string of a JSON value with the run's secret values replaced"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redacted(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, redact) for v in value]
    return value


def _unchanged(text: str) -> str:
    return text


def scrub(key: str, value: object, redact: Callable[[str], str] = _unchanged) -> object:
    """A JSON value on its way to a log or stderr: ``[REDACTED]`` under a secret name at
    any depth, and ``redact`` applied to every other string. The single entry point the
    audit log (REQ-F-026) must call."""
    if secret_name(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: scrub(str(k), v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub("", v, redact) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value
