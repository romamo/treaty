"""What a secret name is, and the redaction the log layers apply (REQ-F-034).

One vocabulary: an input field inferred secret, a setting ``--show-config`` hides, a
``ctx.log`` field, and an error context key printed on stderr all match ``SECRET_NAME``.
Output ``data`` is not redacted (REQ-F-034 is log-layer only); ``_protect`` masks it,
using the narrower ``secret_field`` so ``author`` and ``token_count`` stay readable.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"
"""A field declared ``audit=False``, as the audit log writes it"""

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
_PUBLIC_WORDS = frozenset({"public", "pub"})
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def secret_name(name: str) -> bool:
    """A name whose value is written as ``[REDACTED]`` in logs and diagnostics"""
    return SECRET_NAME.search(name) is not None


def secret_field(name: str) -> bool:
    """An output field whose name says it holds a credential: its last word is one, as in
    ``access_token``, ``client_secret``, or ``api_key`` (``key`` counts after another word,
    unless that word is ``public`` or ``pub``). ``token_count``, ``author``, ``public_key``,
    and a bare ``key`` of a key/value listing do not."""
    words = _words(name)
    if not words:
        return False
    if words[-1] == "key":
        return len(words) > 1 and not public_key_name(name)
    return words[-1] in _SECRET_WORDS


def public_key_name(name: str) -> bool:
    """A field named for a public key, which is meant to be shared: ``public_key``,
    ``publicKey``, ``ssh-pub-key``. Only the word right before ``key`` counts, so
    ``pub_sub_key`` and ``public_repo_deploy_key`` stay credential names."""
    words = _words(name)
    return len(words) > 1 and words[-1] == "key" and words[-2] in _PUBLIC_WORDS


def _words(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(name)]


# Context fields treaty's own errors fill with names, never values: a profile's differing
# keys, the variables a token can come from, and the setting a bad value was given for.
# Their names match SECRET_NAME, but masking them on stderr hides the fix, not a secret
NAME_CONTEXT = frozenset(
    {
        ("CONFLICT", "changed_keys"),
        ("TOKEN_REQUIRED", "token_env_vars"),
        ("CONFIG_INVALID", "key"),
    }
)


def redacted(value: object, redact: Callable[[str], str]) -> object:
    """A JSON value with the run's secret values replaced: in every string, and a number
    equal to a numeric secret (#258)"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redacted(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v, redact) for v in value]
    return _number(value, redact)


def _number(value: object, redact: Callable[[str], str]) -> object:
    """``[REDACTED]`` for a number whose decimal spelling holds a secret spelling, as an
    ``int`` secret flag's is once it is at least the minimum length; any other value as it
    is. Equal numbers match: 987654.0 is the secret 987654. So does a number the secret's
    text is part of, as it is in a string: -987654 and 9876540 hand out the secret 987654.
    A bool is no number here: ``True`` is not the secret 1"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    spellings = {str(value)}
    if isinstance(value, float) and value.is_integer():
        spellings.add(str(int(value)))
    elif isinstance(value, int) and abs(value) <= _EXACT_FLOAT:
        spellings.add(str(float(value)))
    if any(redact(spelling) != spelling for spelling in spellings):
        return REDACTED
    return value


_EXACT_FLOAT = 2**53
"""The largest integer every float spells exactly"""


def _unchanged(text: str) -> str:
    return text


def scrub(key: str, value: object, redact: Callable[[str], str] = _unchanged) -> object:
    """A JSON value on its way to a log or stderr: ``[REDACTED]`` under a secret name at
    any depth, and ``redact`` applied to every other string and number. The single entry point the
    audit log (REQ-O-030) must call."""
    if secret_name(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: scrub(str(k), v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub("", v, redact) for v in value]
    if isinstance(value, str):
        return redact(value)
    return _number(value, redact)


class StreamRedactor:
    """One streamed pipe's text, redacted as it arrives in pieces (``ctx.run(stream=True)``).

    A line longer than the stream's split arrives as several pieces, and a secret can be
    cut between two. Of a piece that does not end its line, the last ``longest - 1``
    characters are held back and read with the next piece, so a secret that starts in
    what is let through always ends in it too, and is replaced whole"""

    def __init__(self, spellings: Collection[str]) -> None:
        ordered = sorted({s for s in spellings if s}, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None
        self._hold = len(ordered[0]) - 1 if ordered else 0
        self._held = ""

    def feed(self, text: str, *, ended: bool) -> str | None:
        """The redacted text to echo now: all of it once ``ended``, else what can be let
        through, or ``None`` while it is all held"""
        buffer = self._held + text
        cut = len(buffer) if ended else max(len(buffer) - self._hold, 0)
        parts: list[str] = []
        start = 0
        if self._pattern is not None:
            for match in self._pattern.finditer(buffer):
                if match.start() >= cut:
                    break
                parts += (buffer[start : match.start()], REDACTED)
                start = match.end()
                cut = max(cut, start)  # a match begun before the cut is let through whole
        parts.append(buffer[start:cut])
        self._held = buffer[cut:]
        let_through = "".join(parts)
        return let_through if ended or let_through else None

    def flush(self) -> str | None:
        """What is still held when the pipe closes, redacted"""
        return self.feed("", ended=True) if self._held else None


def line_fragments(spellings: Collection[str], shortest: int) -> set[str]:
    """Each line of a multi-line secret, at least ``shortest`` long: a streamed child's
    output is echoed line by line, where the whole value never appears in one line"""
    return {
        line
        for spelling in spellings
        if "\n" in spelling
        for line in spelling.splitlines()
        if len(line) >= shortest
    }
