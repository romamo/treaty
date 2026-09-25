"""Path fields: traversal, percent-encoding, and null bytes are rejected in phase 1 (REQ-F-045)."""

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, RegistrationError


@dataclass(frozen=True, slots=True)
class CopyArgs:
    source: Path = Arg(description="File to copy")
    dest: Path | None = Flag(default=None, description="Where to write it")
    extra: tuple[Path, ...] = Flag(default=(), description="More files")


@dataclass(frozen=True, slots=True)
class CopyOut:
    source: Path
    dest: Path | None
    extra: tuple[Path, ...]


def path_app() -> App:
    app = App("cpctl", version="1")

    @app.command("copy", description="Copy a file")
    def copy(args: CopyArgs, ctx: Ctx) -> CopyOut:
        assert isinstance(args.source, Path)
        return CopyOut(args.source, args.dest, args.extra)

    return app


def run(argv: list[str], stdin: str = "") -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = path_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue())


def test_valid_paths_pass_unchanged_and_serialize_as_strings(tmp_path: Path) -> None:
    code, env = run(["copy", "/home/user/file.txt", "--dest", "out/x.txt", "--extra", "a.txt"])
    assert code == 0
    assert env["data"] == {"source": "/home/user/file.txt", "dest": "out/x.txt", "extra": ["a.txt"]}


@pytest.mark.parametrize(
    ("raw", "pattern"),
    [
        ("../etc/passwd", "path_traversal"),
        ("docs/../../secret", "path_traversal"),
        ("%2e%2e/etc/passwd", "percent_encoded"),
        ("files%2fetc", "percent_encoded"),
        ("a\x00b", "null_byte"),
    ],
)
def test_hallucination_patterns_are_rejected_before_the_handler(raw: str, pattern: str) -> None:
    ok = "ok.txt"
    variants = (["copy", raw], ["copy", ok, "--dest", raw], ["copy", ok, "--extra", raw])
    for argv in variants:
        code, env = run(argv)
        error = env["error"]
        assert isinstance(error, dict)
        assert code == 2 and error["code"] == "ARG_ERROR" and error["phase"] == "validation"
        context = error["context"]
        assert isinstance(context, dict)
        assert context["rejected_pattern"] == pattern and context["value"] == raw


def test_suggestions_give_the_decoded_or_absolute_form() -> None:
    _, env = run(["copy", "files%2fetc"])
    assert env["error"]["suggestion"] == "pass the decoded path: --source files/etc"  # type: ignore[index]
    _, env = run(["copy", "../x"])
    assert env["error"]["suggestion"].startswith("pass the absolute path if intended: --source /")  # type: ignore[index]


def test_json_routes_apply_the_same_checks() -> None:
    line = json.dumps({"_cmd": "copy", "source": "../etc/passwd"})
    code, env = run(["exec"], stdin=line + "\n")
    assert code == 1
    assert env["error"]["context"]["rejected_pattern"] == "path_traversal"  # type: ignore[index]
    line = json.dumps({"_cmd": "copy", "source": "ok", "extra": ["%2e%2e"]})
    code, env = run(["exec"], stdin=line + "\n")
    assert code == 1
    assert env["error"]["context"]["rejected_pattern"] == "percent_encoded"  # type: ignore[index]


def test_manifest_declares_the_filepath_preset() -> None:
    _, env = run(["manifest"])
    flags = env["data"]["commands"]["copy"]["flags"]  # type: ignore[index]
    assert flags["source"]["pattern_type"] == "filepath"
    assert flags["dest"]["pattern_type"] == "filepath"
    assert flags["extra"] == {
        "type": "array",
        "required": False,
        "description": "More files",
        "default": [],
        "pattern_type": "filepath",
    }
    spec_validator("manifest-response").validate(env["data"])


def test_pattern_is_refused_on_path_fields() -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        target: Path = Flag(default=Path("."), pattern=r"[a-z]+", description="Nope")

    app = App("x", version="1")
    with pytest.raises(RegistrationError, match="filepath preset"):

        @app.command("go", description="Go")
        def go(args: Bad, ctx: Ctx) -> None:
            return None
