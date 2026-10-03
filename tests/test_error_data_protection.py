"""A failure's data is protected as a success's is (#322): Out(high_entropy=...) and
Out(external=...) apply to the dataclasses an exit carries, --unmask the only way out"""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass

from treaty import Affects, App, Ctx, Exit, Flag, NoArgs, Out, already_exists
from treaty._mcp import call_tool, tool_entries

SECRET = "s3cr3t-value-abcdef"
MASKED = "[KEY: s3cr3t-v...]"


@dataclass(frozen=True, slots=True)
class Key:
    id: str
    material: str = Out(high_entropy=True)


@dataclass(frozen=True, slots=True)
class Created:
    effect: str
    key: Key


@dataclass(frozen=True, slots=True)
class Ring:
    name: str
    primary: Key
    keys: list[Key]


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    body: str = Out(external=True)


@dataclass(frozen=True, slots=True)
class Digest:
    name: str
    digest: str = Out(high_entropy=False)


@dataclass(frozen=True, slots=True)
class Removed:
    effect: str
    material: str = Out(high_entropy=True)
    would_affect: Affects | None = None


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class Node:
    name: str
    material: str = Out(high_entropy=True)
    child: Node | None = None


@dataclass(frozen=True, slots=True)
class DeleteArgs:
    dry_run: bool = Flag(default=False, description="Preview only")


# Base64 of random bytes: masked by its shape wherever it is, unless declared otherwise
BLOB = "q8Zr3mK1vX9pT2wLc7Yb5NfH0jUa4sQe6RdGi8OxWkMzPyVtBnJlChFgEoIuSsD2"


def build() -> App:
    app = App("keys", version="1.0.0")
    app.exit_code("FAILED", 80, description="Failed", retryable=False, side_effects="none")

    @app.command("create", description="Create", danger_level="mutating", exit_codes=["CONFLICT"])
    def create(args: NoArgs, ctx: Ctx) -> Created:
        raise already_exists(Created("noop", Key("k1", SECRET)), conflict_id="k1")

    @app.command("get", description="Get", danger_level="safe", exit_codes=())
    def get(args: NoArgs, ctx: Ctx) -> Key:
        return Key("k1", SECRET)

    @app.command("ring", description="Ring", danger_level="safe", exit_codes=["FAILED"])
    def ring(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.FAILED("ring", data=Ring("r", Key("p", SECRET), [Key("a", SECRET)]))

    @app.command("listed", description="Listed", danger_level="safe", exit_codes=["FAILED"])
    def listed(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.FAILED("listed", data=[Key("a", SECRET), Key("b", SECRET)])

    @app.command("mixed", description="Mixed", danger_level="safe", exit_codes=["FAILED"])
    def mixed(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        data = {"existing": Key("a", SECRET), "keys": [Key("b", SECRET)], "note": "dup"}
        raise Exit.FAILED("mixed", data=data)

    @app.command("sorted", description="Sorted", danger_level="safe", exit_codes=["FAILED"])
    def sorted_(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        # Written sorted, so the Key moves ahead of the Note it follows
        notes = [Note("b", "hello"), Key("a", SECRET)]
        raise Exit.FAILED("sorted", data={"top": notes, "nested": {"items": notes}})

    @app.command("tree", description="Tree", danger_level="safe", exit_codes=["FAILED"])
    def tree(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.FAILED("tree", data=Node("r", SECRET, Node("c", SECRET)))

    @app.command("plain-dict", description="Dict", danger_level="safe", exit_codes=["FAILED"])
    def plain_dict(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        data = {"material": SECRET, "token": "ghp_abc123456789abcdef", "blob": BLOB}
        raise Exit.FAILED("dict", data=data)

    @app.command("fetch", description="Fetch", danger_level="safe", exit_codes=["FAILED"])
    def fetch(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.FAILED("fetch", data=Page("https://example.com", "Ignore previous"))

    @app.command("digest", description="Digest", danger_level="safe", exit_codes=["FAILED"])
    def digest(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        raise Exit.FAILED("digest", data=Digest("d", BLOB))

    @app.command(
        "watch", description="Watch", danger_level="safe", exit_codes=["FAILED"], streaming=True
    )
    def watch(args: NoArgs, ctx: Ctx) -> Iterator[Key]:
        yield Key("a", SECRET)
        raise Exit.FAILED("watch", data=Key("b", SECRET))

    @app.command("delete", description="Delete", danger_level="destructive", exit_codes=())
    def delete(args: DeleteArgs, ctx: Ctx) -> Removed:
        if args.dry_run:
            return Removed("would_delete", SECRET, Affects("Deletes k1", ("k1",), 1))
        return Removed("deleted", SECRET)

    return app


def run(argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = build().run(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err, env={}, isatty=False)
    return code, out.getvalue(), err.getvalue()


def envelope(argv: list[str]) -> tuple[int, dict]:
    code, out, _ = run(argv)
    return code, json.loads(out.splitlines()[-1])


def masked_paths(env: dict) -> list[str]:
    warning = next(w for w in env["warnings"] if w["code"] == "HIGH_ENTROPY_MASKED")
    return warning["context"]["paths"]


def test_a_declared_high_entropy_field_is_masked_on_failure_as_on_success() -> None:
    _, ok = envelope(["get"])
    code, failed = envelope(["create"])
    assert code == 6 and failed["error"]["code"] == "ALREADY_EXISTS"
    assert ok["data"]["material"] == failed["data"]["key"]["material"] == MASKED
    assert masked_paths(failed) == ["data.key.material"]


def test_unmask_shows_the_failure_s_data_raw() -> None:
    _, env = envelope(["create", "--unmask"])
    assert env["data"]["key"]["material"] == SECRET
    assert all(w["code"] != "HIGH_ENTROPY_MASKED" for w in env["warnings"])


def test_plain_output_prints_the_failure_s_data_masked() -> None:
    code, out, err = run(["create", "--format", "plain"])
    assert code == 6 and SECRET not in out + err and MASKED in out
    _, out, _ = run(["create", "--format", "plain", "--unmask"])
    assert SECRET in out


def test_app_call_and_mcp_mask_the_failure_s_data() -> None:
    app = build()
    masked = {"effect": "noop", "key": {"id": "k1", "material": MASKED}}
    assert app.call("create", {}, env={}).data == masked
    unmasked = app.call("create", {}, env={}, unmask=True).data
    assert unmasked == {"effect": "noop", "key": {"id": "k1", "material": SECRET}}
    tools = {e.name: e for e in tool_entries(app)}
    result = call_tool(app, tools, "create", {}, env={})
    assert result.error is not None and result.data == masked


def test_nested_dataclasses_and_a_list_of_them_are_masked() -> None:
    _, env = envelope(["ring"])
    assert env["data"]["primary"]["material"] == MASKED
    assert env["data"]["keys"] == [{"id": "a", "material": MASKED}]
    assert masked_paths(env) == ["data.primary.material", "data.keys[0].material"]


def test_a_list_of_dataclasses_as_the_data_is_masked() -> None:
    _, env = envelope(["listed"])
    assert [k["material"] for k in env["data"]] == [MASKED, MASKED]
    assert masked_paths(env) == ["data[0].material", "data[1].material"]


def test_dataclasses_inside_an_undeclared_object_keep_their_declarations() -> None:
    _, env = envelope(["mixed"])
    assert env["data"]["existing"]["material"] == MASKED
    assert env["data"]["keys"][0]["material"] == MASKED
    assert env["data"]["note"] == "dup"


def test_a_sorted_undeclared_array_keeps_each_dataclass_s_declarations() -> None:
    _, env = envelope(["sorted"])
    expected = [{"id": "a", "material": MASKED}, {"id": "b", "text": "hello"}]
    assert env["data"]["top"] == env["data"]["nested"]["items"] == expected
    assert SECRET not in json.dumps(env)


def test_a_self_referencing_dataclass_is_masked_at_every_level() -> None:
    _, env = envelope(["tree"])
    assert env["data"]["material"] == env["data"]["child"]["material"] == MASKED


def test_an_undeclared_object_is_masked_by_name_and_shape_as_before() -> None:
    _, env = envelope(["plain-dict"])
    data = env["data"]
    # Nothing declares material: a short value under a plain name is left alone
    assert data["material"] == SECRET
    assert data["token"].startswith("[KEY: ") and data["blob"].startswith("[BASE64: ")


def test_a_declared_external_field_tags_the_failure_s_data() -> None:
    _, env = envelope(["fetch"])
    assert env["data"]["_source"] == "external" and env["data"]["_trusted"] is False
    assert any(w["code"] == "UNTRUSTED_CONTENT" for w in env["warnings"])


def test_high_entropy_false_exempts_a_field_of_the_failure_s_data() -> None:
    _, env = envelope(["digest"])
    assert env["data"]["digest"] == BLOB


def test_a_stream_s_terminal_failure_masks_its_data() -> None:
    _, out, _ = run(["watch"])
    terminal = json.loads(out.splitlines()[-1])
    assert terminal["ok"] is False and terminal["data"]["material"] == MASKED
    assert SECRET not in out


def test_an_exec_line_masks_the_failure_s_data() -> None:
    _, out, _ = run(["exec"], stdin='{"_cmd": "create"}\n')
    line = json.loads(out.splitlines()[0])
    assert line["ok"] is False and line["data"]["key"]["material"] == MASKED


def test_a_destructive_preview_refused_without_confirmation_masks_its_data() -> None:
    code, env = envelope(["delete"])
    assert code == 2 and env["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert env["data"]["material"] == MASKED
