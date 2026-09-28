"""``Deprecated``: how a command or flag is retired without breaking an agent (REQ-F-075).

A deprecated command or flag keeps working for at least one minor release; every run that
uses it says so on stderr and in ``warnings``, naming what replaces it. Once it is removed,
``App.redirect`` answers the old command path with exit 13, and ``treaty audit --baseline``
fails on a removal that has neither.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._errors import RegistrationError
from ._values import InvalidValue, ToolVersion


@dataclass(frozen=True, slots=True)
class Deprecated:
    """``since`` is the tool version that deprecated it; ``replacement`` the command path
    (for a command) or flag name (for a flag) to use instead; ``removed_in`` the version
    that will drop it. Versions are semver or PEP 440 releases, as ``App(version=)``
    takes them, and are kept in their semver spelling: ``2.0.0rc1`` is ``2.0.0-rc.1``"""

    since: str
    replacement: str | None = None
    removed_in: str | None = None

    def __post_init__(self) -> None:
        for name in ("since", "removed_in"):
            value = getattr(self, name)
            if value is None and name == "removed_in":
                continue
            try:
                spelled = ToolVersion.of_release(value).value
            except InvalidValue, TypeError:
                raise RegistrationError(
                    f"Deprecated({name}={value!r}): a tool version such as 1.2.0 or 1.2.0rc1"
                ) from None
            object.__setattr__(self, name, spelled)  # frozen: the one normalizing write
        if self.replacement is not None and not (
            isinstance(self.replacement, str) and self.replacement
        ):
            raise RegistrationError("Deprecated(replacement=...) names what to use instead")

    def to_json(self) -> dict[str, str]:
        """The ``--schema`` keys of F-075"""
        out = {"deprecated_in": self.since}
        if self.replacement is not None:
            out["replacement"] = self.replacement
        if self.removed_in is not None:
            out["removed_in"] = self.removed_in
        return out
