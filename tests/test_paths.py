"""Path fields: traversal, percent-encoding, and null bytes are rejected in phase 1 (REQ-F-045)."""

import io
import json
import shlex
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
    app = App("cpctl", version="1.0.0")

    @app.command("copy", description="Copy a file", danger_level="safe", exit_codes=())
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


def test_valid_paths_pass_and_serialize_as_absolute_strings(tmp_path: Path) -> None:
    code, env = run(["copy", "/home/user/file.txt", "--dest", "out/x.txt", "--extra", "a.txt"])
    assert code == 0
    assert env["data"] == {
        "source": str(Path("/home/user/file.txt").absolute()),  # D:\\home\\... on Windows
        "dest": str(Path.cwd() / "out/x.txt"),
        "extra": [str(Path.cwd() / "a.txt")],
    }


@pytest.mark.parametrize(
    ("raw", "pattern"),
    [
        ("../etc/passwd", "path_traversal"),
        ("docs/../../secret", "path_traversal"),
        ("%2e%2e/etc/passwd", "percent_encoded"),
        ("files%2fetc", "percent_encoded"),
        ("a\x00b", "null_byte"),
        ("a\nb", "newline"),
        ("a\rb", "carriage_return"),
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
        # A null byte is echoed as U+FFFD: the envelope is valid UTF-8 text (REQ-F-016)
        echoed = raw.replace("\x00", "\ufffd")
        assert context["rejected_pattern"] == pattern and context["value"] == echoed


def suggested(suggestion: object) -> str:
    """The path a suggestion names, read as a shell reads it"""
    assert isinstance(suggestion, str)
    return shlex.split(suggestion.split(": ", 1)[1])[-1]


def test_suggestions_give_the_decoded_or_absolute_form() -> None:
    _, env = run(["copy", "files%2fetc"])
    assert env["error"]["suggestion"] == "pass the decoded path: --source files/etc"  # type: ignore[index]
    _, env = run(["copy", "../x"])
    prefix = "pass the absolute path if intended: --source "
    suggestion = env["error"]["suggestion"]  # type: ignore[index]
    assert suggestion.startswith(prefix) and Path(suggested(suggestion)).is_absolute()


@pytest.mark.parametrize(
    "raw", ["%2e%2e/etc/passwd", "docs/%2E%2E/%2e%2e/x", "files%2fetc", "../x", "a/../../b"]
)
def test_a_suggested_path_passes_the_checks(raw: str) -> None:
    """Following the suggestion never ends in a second refusal: %2e%2e decodes to a climb"""
    _, env = run(["copy", raw])
    code, _ = run(["copy", suggested(env["error"]["suggestion"])])  # type: ignore[index]
    assert code == 0


@pytest.mark.parametrize(
    ("raw", "want"),
    [("my%20file.txt", "my file.txt"), ("it%27s.txt", "it's.txt"), ("a b/../c.txt", None)],
)
def test_a_suggested_path_is_quoted_for_the_shell(raw: str, want: str | None) -> None:
    """A space or a quote in the path survives a copy and paste into a shell"""
    _, env = run(["copy", raw])
    suggestion = env["error"]["suggestion"]  # type: ignore[index]
    path = suggested(suggestion)
    assert path == (want if want is not None else str(Path.cwd() / "c.txt"))
    assert suggestion.endswith(shlex.quote(path))
    code, _ = run(["copy", path])
    assert code == 0


@pytest.mark.parametrize("raw", ["../x.txt", "%2e%2e/x.txt"])
def test_a_suggested_absolute_path_is_under_cwd(raw: str, tmp_path: Path) -> None:
    """A relative argument resolves under --cwd, so the path it meant is there too"""
    (tmp_path / "sub").mkdir()
    _, env = run(["copy", raw, "--cwd", str(tmp_path / "sub")])
    assert suggested(env["error"]["suggestion"]) == str(tmp_path / "x.txt")  # type: ignore[index]


def test_every_collected_path_error_is_suggested_under_cwd(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    _, env = run(["copy", "../a", "--dest", "../b", "--cwd", str(tmp_path / "sub")])
    errors = env["error"]["errors"]  # type: ignore[index]
    assert [suggested(e["suggestion"]) for e in errors] == [
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]


@pytest.mark.parametrize("raw", ["a%00b", "a%0Ab", "%252e%252e/x"])
def test_no_path_is_suggested_when_no_decoded_form_passes(raw: str) -> None:
    """%00 decodes to a null byte and %252e to another encoding: the generic advice stays"""
    code, env = run(["copy", raw])
    error = env["error"]
    assert isinstance(error, dict)
    assert code == 2 and error["context"]["rejected_pattern"] == "percent_encoded"
    assert error["suggestion"] == "correct the arguments and reissue"


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

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="filepath preset"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Bad, ctx: Ctx) -> None:
            return None
