"""Output data contract: REQ-F-017, F-020, F-040, F-064, F-072, F-074, O-007"""

import base64
import io
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import spec_validator

from treaty import App, Binary, Ctx, Flag, NoArgs, Out, RegistrationError
from treaty._audit import audit
from treaty._cap import MARKER, MIN_BYTES
from treaty._mcp import call_tool, tool_entries
from treaty._plain import render_plain

OUTCTL = Path(__file__).resolve().parent / "fixture_output_app.py"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + bytes(range(256))


def run(app: App, argv: list[str], stdin: str = "", **kw: object) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, **kw)  # type: ignore[arg-type]
    return code, json.loads(out.getvalue())


def outctl(argv: list[str], cwd: Path, **kw: object) -> subprocess.CompletedProcess[bytes]:
    env = {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    return subprocess.run(
        [sys.executable, str(OUTCTL), *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
        **kw,  # type: ignore[arg-type]
    )


def findings(app: App, rule: str) -> list:
    report = audit(app, "x:app", limit=100)
    return next(list(r.findings) for r in report.rules if r.id == rule)


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Tagged:
    tags: list[str]
    sizes: tuple[int, ...]
    pair: tuple[str, str]
    users: list[User] = Out(sort_key="id")
    ranking: list[str] = Out(ordered=True)


def tagged_app() -> App:
    app = App("tagctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Tagged:
        return Tagged(
            tags=["b", "B", "a", "é"],
            sizes=(10, 2, -1, 2.5),  # type: ignore[arg-type]
            pair=("z", "a"),
            users=[User("u2", "bob"), User("u10", "zed"), User("u1", "alice")],
            ranking=["third", "first", "second"],
        )

    @app.command("users", description="Users", danger_level="safe", exit_codes=(), sort_key="id")
    def users(args: NoArgs, ctx: Ctx) -> list[User]:
        return [User(f"u{i:02}", f"n{50 - i}") for i in reversed(range(50))]

    return app


# REQ-F-017


@dataclass(frozen=True, slots=True)
class Image:
    name: str
    bytes: Binary
    thumbnail: bytes


def image_app() -> App:
    app = App("imgctl", version="1.0.0", max_output_bytes=MIN_BYTES)

    @app.command("get-image", description="Get", danger_level="safe", exit_codes=())
    def get_image(args: NoArgs, ctx: Ctx) -> Image:
        return Image("logo.png", Binary(PNG, content_type="image/png"), PNG[:8])

    @app.command("images", description="Many", danger_level="safe", exit_codes=(), ordered=True)
    def images(args: NoArgs, ctx: Ctx) -> list[bytes]:
        return [bytes([i]) * 1500 for i in range(8)]

    return app


def test_f017_raw_png_bytes_produce_valid_json_with_a_base64_wrapper() -> None:
    code, env = run(image_app(), ["get-image"])
    assert code == 0
    wrapper = env["data"]["bytes"]
    assert wrapper["type"] == "binary" and wrapper["encoding"] == "base64"
    assert env["data"]["thumbnail"]["type"] == "binary"
    assert "content_type" not in env["data"]["thumbnail"]


def test_f017_size_bytes_matches_the_original_byte_length() -> None:
    _, env = run(image_app(), ["get-image"])
    assert env["data"]["bytes"]["size_bytes"] == len(PNG)
    assert env["data"]["thumbnail"]["size_bytes"] == 8


def test_f017_a_caller_recovers_the_original_bytes_by_decoding_value() -> None:
    _, env = run(image_app(), ["get-image"])
    assert base64.b64decode(env["data"]["bytes"]["value"]) == PNG


def test_f017_content_type_passes_through_and_the_schema_declares_the_field() -> None:
    app = image_app()
    _, env = run(app, ["get-image"])
    assert env["data"]["bytes"]["content_type"] == "image/png"
    _, schema = run(app, ["get-image", "--output-schema"])
    prop = schema["data"]["properties"]["bytes"]
    assert prop["properties"]["encoding"] == {"type": "string", "enum": ["base64"]}
    assert prop["required"] == ["type", "encoding", "value", "size_bytes"]


def test_f017_the_byte_cap_drops_a_wrapper_whole_and_never_cuts_its_value() -> None:
    code, env = run(image_app(), ["images"])
    assert code == 0 and env["meta"]["truncated"] is True
    assert 1 <= len(env["data"]) < 8
    for wrapper in env["data"]:
        assert len(base64.b64decode(wrapper["value"])) == wrapper["size_bytes"] == 1500


def test_f017_plain_prints_a_summary_of_binary_values() -> None:
    _, env = run(image_app(), ["get-image"])
    text = render_plain(env["data"])
    assert f"bytes: <binary {len(PNG)} bytes image/png>" in text
    assert "thumbnail: <binary 8 bytes>" in text


def test_binary_output_rule_flags_a_handler_that_encodes_base64_itself() -> None:
    app = App("b64ctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"png": base64.b64encode(PNG).decode()}

    (finding,) = findings(app, "binary-output")
    assert "treaty.Binary" in finding.fix
    assert findings(image_app(), "binary-output") == []


# REQ-F-020


def test_f020_two_invocations_with_the_same_arguments_produce_byte_identical_data(
    tmp_path: Path,
) -> None:
    first, second = (outctl(["find-config"], tmp_path) for _ in range(2))
    assert first.returncode == second.returncode == 0
    data = [json.loads(p.stdout)["data"] for p in (first, second)]
    del data[0]["fetched_at"], data[1]["fetched_at"]  # declared volatile
    assert json.dumps(data[0], sort_keys=True) == json.dumps(data[1], sort_keys=True)


def test_f020_a_string_array_output_field_is_always_in_lexicographic_order() -> None:
    _, env = run(tagged_app(), ["get"])
    assert env["data"]["tags"] == ["B", "a", "b", "é"]  # by code point


def test_f020_an_object_array_output_field_is_sorted_by_the_declared_primary_key() -> None:
    _, env = run(tagged_app(), ["get"])
    assert [u["id"] for u in env["data"]["users"]] == ["u1", "u10", "u2"]
    _, env = run(tagged_app(), ["users", "--limit", "0"])
    assert [u["id"] for u in env["data"]] == [f"u{i:02}" for i in range(50)]


def test_numbers_sort_ascending_and_fixed_tuples_keep_their_order() -> None:
    _, env = run(tagged_app(), ["get"])
    assert env["data"]["sizes"] == [-1, 2, 2.5, 10]
    assert env["data"]["pair"] == ["z", "a"]


def test_ordered_keeps_the_handler_order_and_the_schema_says_so() -> None:
    app = tagged_app()
    _, env = run(app, ["get"])
    assert env["data"]["ranking"] == ["third", "first", "second"]
    _, schema = run(app, ["get", "--output-schema"])
    assert schema["data"]["properties"]["ranking"]["x-ordered"] is True


def test_a_list_command_sorts_before_paging_so_pages_follow_one_order() -> None:
    app = tagged_app()
    _, first = run(app, ["users", "--limit", "20"])
    token = first["meta"]["pagination"]["next_cursor"]
    _, second = run(app, ["users", "--limit", "20", "--cursor", token])
    ids = [u["id"] for u in first["data"] + second["data"]]
    assert ids == [f"u{i:02}" for i in range(40)]


def test_objects_without_a_key_sort_by_canonical_json() -> None:
    app = App("objctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"rows": [{"b": 1}, {"a": 2}], "mixed": ["x", 1, None]}

    _, env = run(app, ["get"])
    assert env["data"] == {"rows": [{"a": 2}, {"b": 1}], "mixed": ["x", 1, None]}


@pytest.mark.parametrize(
    ("declare", "match"),
    [
        ({"sort_key": "missing"}, "sort_key='missing' must name"),
        ({"sort_key": "id", "ordered": True}, "pick one"),
    ],
)
def test_sort_key_must_name_a_scalar_field_of_the_items(declare: dict, match: str) -> None:
    app = App("badctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("ls", description="ls", danger_level="safe", exit_codes=(), **declare)
        def ls(args: NoArgs, ctx: Ctx) -> list[User]:
            return []


def test_out_sort_key_is_checked_at_registration() -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        names: list[str] = Out(sort_key="id")

    app = App("badctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="Bad.names: sort_key='id'"):

        @app.command("get", description="Get", danger_level="safe", exit_codes=())
        def get(args: NoArgs, ctx: Ctx) -> Bad:
            return Bad([])


def test_stable_order_rule_suggests_a_key_for_undeclared_object_arrays() -> None:
    @dataclass(frozen=True, slots=True)
    class Team:
        members: list[User]

    app = App("teamctl", version="1.0.0")

    @app.command("ls", description="ls", danger_level="safe", exit_codes=())
    def ls(args: NoArgs, ctx: Ctx) -> list[User]:
        return []

    @app.command("team", description="team", danger_level="safe", exit_codes=())
    def team(args: NoArgs, ctx: Ctx) -> Team:
        return Team([])

    fixes = [f.fix for f in findings(app, "stable-order")]
    assert fixes[0].startswith('sort_key="id"')
    assert fixes[1].startswith('members: ... = treaty.Out(sort_key="id")')
    assert findings(tagged_app(), "stable-order") == []


# REQ-F-040


def test_f040_a_relative_path_field_is_resolved_against_the_cwd(tmp_path: Path) -> None:
    proc = outctl(["find-config"], tmp_path)
    data = json.loads(proc.stdout)["data"]
    cwd = json.loads(proc.stdout)["meta"]["cwd"]
    assert data["config_path"] == str(Path(cwd) / "src" / ".toolrc")
    assert data["output_dir"] == str(Path(cwd) / "dist")


def test_f040_the_resolved_path_is_absolute_regardless_of_the_invoking_cwd(
    tmp_path: Path,
) -> None:
    for where in (tmp_path / "a", tmp_path / "b" / "c"):
        where.mkdir(parents=True)
        env = json.loads(outctl(["find-config"], where).stdout)
        config = Path(env["data"]["config_path"])
        assert config.is_absolute() and Path(env["meta"]["cwd"]).samefile(where)
        assert config == Path(env["meta"]["cwd"]) / "src" / ".toolrc"


def test_f040_a_relative_path_is_expanded_against_the_effective_cwd_at_invocation() -> None:
    @dataclass(frozen=True, slots=True)
    class Where:
        path: Path

    app = App("pathctl", version="1.0.0")

    @app.command("where", description="Where", danger_level="safe", exit_codes=())
    def where(args: NoArgs, ctx: Ctx) -> Where:
        return Where(Path("../x.txt"))

    _, env = run(app, ["where"])
    assert env["data"]["path"] == str(Path(env["meta"]["cwd"]) / ".." / "x.txt")


def test_path_typed_rule_covers_output_fields() -> None:
    @dataclass(frozen=True, slots=True)
    class Located:
        config_path: str

    app = App("strctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Located:
        return Located("x")

    (finding,) = findings(app, "path-typed")
    assert "config_path" in finding.message and "Path" in finding.fix


# REQ-F-064


@dataclass(frozen=True, slots=True)
class Update:
    title: str = Flag(description="Title", max_bytes=255)
    labels: tuple[str, ...] = Flag(default=(), description="Labels", max_bytes=8)
    count: int = Flag(default=1, description="How many")


written: list[str] = []


def issue_app() -> App:
    app = App("issuectl", version="1.0.0")

    @app.command(
        "update",
        description="Update an issue",
        danger_level="safe",
        exit_codes=(),
        supports_raw_payload=True,
    )
    def update(args: Update, ctx: Ctx) -> dict[str, str]:
        written.append(args.title)
        return {"title": args.title}

    @app.command("get", description="Get an issue", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        body = "x" * 255  # what the backend kept of 4200
        return {"body": ctx.truncated(body, field="body", original_length=4200)}

    return app


def test_f064_writing_500_bytes_to_a_max_bytes_255_field_exits_2_before_writing() -> None:
    written.clear()
    code, env = run(issue_app(), ["update", "--title", "x" * 500])
    assert code == 2 and written == []
    error = env["error"]
    assert error["code"] == "FIELD_TOO_LARGE" and error["phase"] == "validation"
    assert error["context"] == {"field": "title", "max_bytes": 255, "actual_bytes": 500}
    assert "x" * 20 not in json.dumps(env)  # never the value


@pytest.mark.parametrize("route", ["exec", "call", "raw-payload"])
def test_f064_every_input_route_checks_max_bytes(route: str) -> None:
    app = issue_app()
    payload = {"title": "é" * 200}  # 400 UTF-8 bytes
    if route == "call":
        envelope = app.call("update", payload)
        assert envelope.error is not None and envelope.error.code == "FIELD_TOO_LARGE"
        return
    if route == "exec":
        _, env = run(app, ["exec"], json.dumps({"_cmd": "update", **payload}) + "\n")
    else:
        _, env = run(app, ["update", "--raw-payload", json.dumps(payload)])
    assert env["error"]["code"] == "FIELD_TOO_LARGE"
    assert env["error"]["context"]["actual_bytes"] == 400


def test_f064_a_too_large_value_joins_the_other_field_errors() -> None:
    code, env = run(
        issue_app(), ["update", "--title", "x" * 300, "--labels", "far-too-long", "--count", "z"]
    )
    assert code == 2
    codes = [e.get("code") for e in env["error"]["errors"]]
    assert codes.count("FIELD_TOO_LARGE") == 2 and len(codes) == 3


def test_f064_max_bytes_is_published_in_the_schema_and_the_manifest() -> None:
    app = issue_app()
    _, schema = run(app, ["update", "--schema"])
    props = schema["data"]["raw_payload_schema"]["properties"]
    assert props["title"]["x-max-bytes"] == 255
    assert props["labels"]["items"]["x-max-bytes"] == 8
    _, manifest = run(app, ["manifest"])
    flags = manifest["data"]["commands"]["update"]["flags"]
    assert flags["title"]["description"] == "Title (at most 255 bytes)"


@pytest.mark.parametrize(
    ("annotation", "declare"),
    [(int, {"default": 1}), (Path, {"default": Path("x")}), (str, {"secret": True})],
)
def test_max_bytes_is_for_plain_text_fields(annotation: type, declare: dict) -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        value: annotation = Flag(description="V", max_bytes=10, **declare)  # type: ignore[valid-type]

    app = App("badctl", version="1.0.0")
    with pytest.raises(RegistrationError, match="max_bytes"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Args, ctx: Ctx) -> dict[str, str]:
            return {}


def test_f064_a_backend_truncated_value_gives_meta_truncated_and_a_field_truncated_warning() -> (
    None
):
    code, env = run(issue_app(), ["get"])
    assert code == 0 and env["meta"]["truncated"] is True
    assert [w["code"] for w in env["warnings"]] == ["FIELD_TRUNCATED"]
    assert env["data"]["body"] == "x" * 255 + MARKER


def test_f064_the_warning_context_has_field_truncated_length_and_original_length() -> None:
    _, env = run(issue_app(), ["get"])
    assert env["warnings"][0]["context"] == {
        "field": "data.body",
        "truncated_length": 255,
        "original_length": 4200,
    }


def test_f064_stdout_parses_as_one_complete_envelope_when_truncation_is_present() -> None:
    out = io.StringIO()
    issue_app().run(["get"], stdout=out, stderr=io.StringIO(), env={})
    envelope = json.loads(out.getvalue())
    spec_validator("response-envelope").validate(envelope)


def test_field_limits_rule_flags_hand_checked_sizes_and_cuts() -> None:
    @dataclass(frozen=True, slots=True)
    class Post:
        body: str = Flag(description="Body")

    app = App("postctl", version="1.0.0")

    @app.command("post", description="Post", danger_level="safe", exit_codes=())
    def post(args: Post, ctx: Ctx) -> dict[str, str]:
        if len(args.body) > 255:
            raise ValueError("too long")
        return {"body": ctx.env.get("X", "")[:10], "cut": args.body[:100]}

    size, cut = findings(app, "field-limits")
    assert "max_bytes=N" in size.fix and "ctx.truncated" in cut.fix


# REQ-F-072


def test_f072_stdout_and_stderr_contain_no_carriage_return(tmp_path: Path) -> None:
    for argv in (["find-config"], ["--help"], ["greet", "--bogus"], ["greet", "--name", "a"]):
        proc = outctl(argv, tmp_path)
        assert proc.stdout and b"\r" not in proc.stdout, argv
        assert b"\r" not in proc.stderr, argv
    # ctx.log reached stderr
    assert b"greeting" in outctl(["greet", "--verbose"], tmp_path).stderr


def test_f072_json_output_written_to_a_file_contains_no_carriage_return(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    with target.open("wb") as stdout:
        subprocess.run(
            [sys.executable, str(OUTCTL), "get-image"],
            cwd=tmp_path,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
    raw = target.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw and json.loads(raw)["ok"] is True


# REQ-F-074


@dataclass(frozen=True, slots=True)
class Resource:
    name: str
    description: str | None
    tags: list[str] = Out(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    deprecated_at: str | None = None


def resource_app() -> App:
    app = App("resctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Resource:
        return Resource("my-resource", None)

    return app


def test_f074_empty_collections_are_returned_as_empty_never_null_or_absent() -> None:
    _, env = run(resource_app(), ["get"])
    assert env["data"] == {
        "name": "my-resource",
        "description": None,
        "tags": [],
        "labels": {},
        "deprecated_at": None,
    }


@pytest.mark.parametrize(
    "annotation", [list[str] | None, tuple[int, ...] | None, dict[str, int] | None]
)
def test_f074_a_nullable_collection_in_an_output_type_is_refused(annotation: object) -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        items: annotation  # type: ignore[valid-type]

    app = App("badctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=r"Bad.items: an output collection is never null"):

        @app.command("get", description="Get", danger_level="safe", exit_codes=())
        def get(args: NoArgs, ctx: Ctx) -> Bad:
            return Bad(None)


def test_f074_the_schema_marks_each_field_required_nullable_or_optional() -> None:
    app = resource_app()
    _, schema = run(app, ["get", "--output-schema"])
    data = schema["data"]
    # Every key is always written, so each is required; nullable ones also allow null
    assert data["required"] == ["name", "description", "tags", "labels", "deprecated_at"]
    assert data["properties"]["description"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert data["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    # Input schemas still let a defaulted field be left out
    _, schema = run(issue_app(), ["update", "--schema"])
    assert schema["data"]["raw_payload_schema"]["required"] == ["title"]


def test_f074_two_calls_under_the_same_conditions_produce_the_same_set_of_keys() -> None:
    keys = [set(run(resource_app(), ["get"])[1]["data"]) for _ in range(2)]
    assert keys[0] == keys[1] == set(Resource.__dataclass_fields__)


# REQ-O-007


def test_o007_two_invocations_with_stable_output_produce_byte_identical_stdout(
    tmp_path: Path,
) -> None:
    first, second = (outctl(["find-config", "--stable-output"], tmp_path) for _ in range(2))
    assert first.returncode == 0 and first.stdout == second.stdout


def test_o007_request_id_and_timestamp_are_omitted_with_stable_output(tmp_path: Path) -> None:
    env = json.loads(outctl(["--stable-output", "find-config"], tmp_path).stdout)
    assert "request_id" not in env["meta"] and "timestamp" not in env["meta"]
    assert env["meta"]["duration_ms"] == 0
    spec_validator("response-envelope").validate(env)
    plain = json.loads(outctl(["find-config"], tmp_path).stdout)
    assert {"request_id", "timestamp"} <= set(plain["meta"])


def test_o007_stable_output_implies_sorted_arrays(tmp_path: Path) -> None:
    data = json.loads(outctl(["find-config", "--stable-output"], tmp_path).stdout)["data"]
    assert data["tags"] == ["alpha", "beta", "gamma"]
    assert [u["id"] for u in data["users"]] == ["u1", "u2", "u3"]


def test_o007_volatile_fields_are_left_out_and_not_required(tmp_path: Path) -> None:
    data = json.loads(outctl(["find-config", "--stable-output"], tmp_path).stdout)["data"]
    assert "fetched_at" not in data
    assert "fetched_at" in json.loads(outctl(["find-config"], tmp_path).stdout)["data"]
    schema = json.loads(outctl(["find-config", "--output-schema"], tmp_path).stdout)["data"]
    assert schema["properties"]["fetched_at"]["x-volatile"] is True
    assert "fetched_at" not in schema["required"]


def test_o007_exec_lines_and_mcp_calls_take_stable_output() -> None:
    app = tagged_app()
    plan = "\n".join(
        json.dumps(line) for line in ({"_cmd": "get", "stable_output": True}, {"_cmd": "get"})
    )
    out = io.StringIO()
    app.run(["exec"], stdin=io.StringIO(plan + "\n"), stdout=out, stderr=io.StringIO(), env={})
    stable, plain = (json.loads(line) for line in out.getvalue().splitlines())
    assert "request_id" not in stable["meta"] and "request_id" in plain["meta"]
    entries = {e.name: e for e in tool_entries(app)}
    envelope = call_tool(app, entries, "get", {"stable_output": True})
    assert envelope.meta.request_id is None and envelope.meta.timestamp is None
    assert "stable_output" in entries["get"].input_schema["properties"]  # type: ignore[attr-defined]


def test_o007_stable_output_turns_heartbeats_off() -> None:
    app = App("slowctl", version="1.0.0")

    @app.command("wait", description="Wait", danger_level="safe", exit_codes=(), heartbeat=True)
    def wait(args: NoArgs, ctx: Ctx) -> dict[str, int]:
        time.sleep(0.3)
        return {"n": 1}

    out = io.StringIO()
    app.run(["wait", "--heartbeat-ms", "50", "--stable-output"], stdout=out, env={})
    assert len(out.getvalue().splitlines()) == 1


def test_volatile_data_rule_accepts_declared_fields_and_suggests_out() -> None:
    @dataclass(frozen=True, slots=True)
    class Stamped:
        id: str
        fetched_at: str

    app = App("stampctl", version="1.0.0")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Stamped:
        return Stamped("a", "now")

    (finding,) = findings(app, "volatile-data")
    assert "treaty.Out(volatile=True)" in finding.fix
    from fixture_output_app import app as declared

    assert findings(declared, "volatile-data") == []


def test_o007_a_stable_exec_line_leaves_the_next_lines_unstable() -> None:
    lines = ({"_cmd": "get", "stable_output": True}, {"_cmd": "get", "bogus": 1}, {"_cmd": "no"})
    plan = "\n".join(json.dumps(line) for line in lines)
    out = io.StringIO()
    tagged_app().run(
        ["exec", "--ignore-errors"],
        stdin=io.StringIO(plan + "\n"),
        stdout=out,
        stderr=io.StringIO(),
        env={},
    )
    stable, *refused = (json.loads(line) for line in out.getvalue().splitlines())
    assert "request_id" not in stable["meta"]
    assert len(refused) == 2 and all("request_id" in e["meta"] for e in refused)
