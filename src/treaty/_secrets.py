"""Secret fields: values arrive through an env var or a file, never the argument list.

REQ-C-016 forbids a direct ``--token VALUE`` flag. For every secret field the
framework exposes ``--<name>-from-env VAR`` and ``--<name>-from-file PATH``
(REQ-O-022) and a default variable ``<APP>_<NAME>`` consulted when neither is
given; the value is resolved in phase 1 and coerced like any other field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ._errors import ParseError
from ._paths import check_path

ENV_SUFFIX = "-from-env"
FILE_SUFFIX = "-from-file"
_NOT_IDENT = re.compile(r"[^A-Za-z0-9]+")


class SecretSource(StrEnum):
    ENV = "env"
    FILE = "file"

    @property
    def suffix(self) -> str:
        return ENV_SUFFIX if self is SecretSource.ENV else FILE_SUFFIX


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Where one secret field's value comes from, as the caller named it"""

    source: SecretSource
    ref: str


def default_env_var(app_name: str, field_name: str) -> str:
    """``<APP>_<FIELD>``: the variable read when no ``-from-env``/``-from-file`` is given"""
    return _NOT_IDENT.sub("_", f"{app_name}_{field_name}").strip("_").upper()


def source_flags(flag: str) -> tuple[str, str]:
    return flag + ENV_SUFFIX, flag + FILE_SUFFIX


def split_source_flag(name: str) -> tuple[str, SecretSource] | None:
    """``token-from-env`` -> (``token``, ENV); ``None`` for an ordinary flag name"""
    for source in SecretSource:
        if name.endswith(source.suffix) and len(name) > len(source.suffix):
            return name[: -len(source.suffix)], source
    return None


def resolve_secret(flag: str, ref: SecretRef, env: Mapping[str, str]) -> str:
    """The raw secret text, or a validation-phase ``ParseError`` naming the source, not the value"""
    if ref.source is SecretSource.ENV:
        value = env.get(ref.ref)
        if value is None or value == "":
            raise ParseError(
                f"environment variable {ref.ref} is not set",
                context={"flag": flag + ENV_SUFFIX, "var": ref.ref},
                suggestion=f"export {ref.ref}=<value> in the environment, not on the command line",
            )
        return value
    path = check_path(ref.ref, flag + FILE_SUFFIX)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ParseError(
            f"cannot read secret file for {flag!r}: {exc.__class__.__name__}",
            context={"flag": flag + FILE_SUFFIX, "path": ref.ref},
            suggestion="pass a readable UTF-8 file holding only the secret",
        ) from None
    text = text.removesuffix("\n").removesuffix("\r")
    if text == "":
        raise ParseError(
            f"secret file for {flag!r} is empty",
            context={"flag": flag + FILE_SUFFIX, "path": ref.ref},
        )
    return text


def direct_secret_error(flag: str) -> ParseError:
    env_flag, file_flag = source_flags(flag)
    return ParseError(
        f"{flag!r} is a secret and is not accepted on the command line",
        context={"flag": flag, "accepted": [env_flag, file_flag]},
        suggestion=f"pass --{env_flag} VAR_NAME or --{file_flag} PATH instead",
    )
