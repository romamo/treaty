"""Did-you-mean for an unknown command: the registered names closest to what was typed"""

from __future__ import annotations

from collections.abc import Iterable

_MOST = 3
"""Most suggestions an error carries"""
_SHORTEST = 3
"""Fewest characters typed for any suggestion: two letters match too much"""
_PREFIX = 0.5
"""The distance of a one-word name that starts with the typed word, ``gen`` for
``generate``: closer than one typo, farther than an exact match. A dot path such as
``deploy.start`` gets no such credit"""
_SPREAD = 0.5
"""How much farther than the best a suggestion may be: a one-typo match stays beside a
prefix match, and one needing another edit is dropped"""


def closest(typed: str, names: Iterable[tuple[str, str]]) -> list[str]:
    """The suggestions for ``typed``, best first: each name is a key to compare, such as
    ``rollback``, and the suggestion to give when it matches, such as ``app deploy rollback``;
    several keys may lead to one suggestion. A key matches within one edit (an insertion,
    deletion, substitution, or swap of neighbors) per three characters typed"""
    word = typed.lower()
    if len(word) < _SHORTEST:
        return []
    allowed = len(word) // _SHORTEST
    best: dict[str, float] = {}
    for key, suggestion in names:
        far: float = distance(word, key)
        if "." not in key and key.startswith(word):
            far = min(far, _PREFIX)
        if far <= allowed and far < best.get(suggestion, far + 1):
            best[suggestion] = far
    # On a tie the shorter invocation first: ``status`` before ``job status``
    ranked = sorted(best, key=lambda s: (best[s], len(s), s))
    return [s for s in ranked if best[s] <= best[ranked[0]] + _SPREAD][:_MOST]


def distance(a: str, b: str) -> int:
    """Edits from ``a`` to ``b``, a swap of neighbors counting as one (optimal string
    alignment)"""
    before: list[int] = []
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            cost = min(row[j] + 1, current[j - 1] + 1, row[j - 1] + (x != y))
            if i > 1 and j > 1 and x == b[j - 2] and a[i - 2] == y:
                cost = min(cost, before[j - 2] + 1)
            current.append(cost)
        before, row = row, current
    return row[-1]


def hint(suggestions: list[str]) -> str | None:
    """The error's suggestion: the best match, the others after it"""
    if not suggestions:
        return None
    first, *others = suggestions
    also = "" if not others else f" (or {', '.join(others)})"
    return f"did you mean {first}{also}?"
