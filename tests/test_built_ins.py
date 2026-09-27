"""Built-in commands: REQ-O-026, REQ-O-027, REQ-O-028, REQ-O-029, REQ-O-034, REQ-O-035,
REQ-O-041, and the ``status --show-side-effects`` half of REQ-C-011."""

import io
import json
import os

from conftest import spec_validator

from treaty import App, Ctx, NoArgs


def run(
    app: App, argv: list[str], env: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    out = io.StringIO()
    code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
        env={"PATH": os.environ["PATH"], **(env or {})},
    )
    return code, json.loads(out.getvalue())


def data_of(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def plain_app() -> App:
    app = App("plain", version="1.2.0", description="A plain tool")

    @app.command("hello", description="Say hello", danger_level="safe", exit_codes=())
    def hello(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {"greeting": "hello"}

    return app


# REQ-O-041


def test_tool_manifest_etag_hash_returns_meta_not_modified_true_when_unchanged() -> None:
    app = plain_app()
    _, envelope = run(app, ["manifest"])
    etag = data_of(envelope)["etag"]
    assert isinstance(etag, str)
    code, envelope = run(app, ["manifest", "--etag", etag])
    assert code == 0 and envelope["ok"] is True and envelope["data"] is None
    meta = envelope["meta"]
    assert isinstance(meta, dict) and meta["not_modified"] is True
    spec_validator("response-envelope").validate(envelope)


def test_a_stale_etag_returns_the_full_manifest() -> None:
    app = plain_app()
    code, envelope = run(app, ["manifest", "--etag", "sha256:" + "0" * 32])
    assert code == 0 and "commands" in data_of(envelope)
    meta = envelope["meta"]
    assert isinstance(meta, dict) and "not_modified" not in meta


def test_a_malformed_etag_exits_2() -> None:
    code, envelope = run(plain_app(), ["manifest", "--etag", "abc"])
    assert code == 2
    error = envelope["error"]
    assert isinstance(error, dict) and error["phase"] == "validation"


def test_not_modified_reaches_app_call_and_exec() -> None:
    app = plain_app()
    etag = app.manifest()["etag"]
    called = app.call("manifest", {"etag": etag}).to_json()
    assert called["data"] is None and called["meta"]["not_modified"] is True
    out = io.StringIO()
    line = json.dumps({"_cmd": "manifest", "etag": etag})
    app.run(["exec"], stdin=io.StringIO(line + "\n"), stdout=out, stderr=io.StringIO(), env={})
    answered = json.loads(out.getvalue().splitlines()[0])
    assert answered["data"] is None and answered["meta"]["not_modified"] is True
