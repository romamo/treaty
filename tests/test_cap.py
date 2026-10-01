import io
import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from conftest import spec_validator

from treaty import App, Ctx, Flag, NoArgs, RegistrationError
from treaty._cap import MARKER, MIN_BYTES, SLACK


def big_app() -> App:
    app = App("bigctl", version="1.0.0", max_output_bytes=MIN_BYTES)

    # Opted out of pagination, so the byte cap is what bounds it
    @app.command(
        "items",
        description="Many small items",
        danger_level="safe",
        exit_codes=(),
        paginated=False,
        ordered=True,
    )
    def items(args: NoArgs, ctx: Ctx) -> list[dict[str, object]]:
        return [{"id": i, "name": f"item-{i}"} for i in range(1000)]

    @app.command(
        "log", description="One record with a huge field", danger_level="safe", exit_codes=()
    )
    def log(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"records": [{"id": 1, "log": "x" * 50_000}], "source": "api"}

    @app.command(
        "table",
        description="A wide object with no list or long string",
        danger_level="safe",
        exit_codes=(),
    )
    def table(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"name": "t", "rows": {f"k{i}": {"v": i} for i in range(2000)}}

    @app.command("small", description="Fits easily", danger_level="safe", exit_codes=())
    def small(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"ok": True}

    return app


def run(
    app: App, argv: list[str], *, env: dict[str, str] | None = None, stdin: str = ""
) -> tuple[int, str]:
    out = io.StringIO()
    code = app.run(
        argv,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=io.StringIO(),
        env=env or {},
        isatty=False,
    )
    return code, out.getvalue()


def envelope(text: str) -> dict:
    env = json.loads(text)
    spec_validator("response-envelope").validate(env)
    return env


def test_under_cap_is_untouched() -> None:
    code, out = run(big_app(), ["small"])
    env = envelope(out)
    assert code == 0 and env["data"] == {"ok": True}
    assert "truncated" not in env["meta"] and env["warnings"] == []


def test_list_is_cut_to_the_longest_prefix_that_fits() -> None:
    code, out = run(big_app(), ["items"])
    env = envelope(out)
    assert code == 0 and len(out.encode()) <= MIN_BYTES
    meta = env["meta"]
    assert meta["truncated"] is True and meta["total_count"] == 1000
    assert meta["returned_count"] == len(env["data"]) > 1
    assert env["data"] == [{"id": i, "name": f"item-{i}"} for i in range(len(env["data"]))]
    assert meta["truncation_hint"] == f"bigctl items --max-output {meta['total_bytes'] + SLACK}"
    (warning,) = env["warnings"]
    assert warning["code"] == "FIELD_TRUNCATED" and warning["context"]["field"] == "data"


def test_hint_returns_the_full_response() -> None:
    _, out = run(big_app(), ["items"])
    hint = shlex.split(envelope(out)["meta"]["truncation_hint"])
    code, out = run(big_app(), hint[1:])
    env = envelope(out)
    assert code == 0 and len(env["data"]) == 1000 and "truncated" not in env["meta"]


def test_max_output_hint_leaves_slack_for_a_slower_rerun() -> None:
    """The rerun's meta (duration_ms) may be longer by a few bytes than the cut run's"""
    _, out = run(big_app(), ["items"])
    total = envelope(out)["meta"]["total_bytes"]
    hint = shlex.split(envelope(out)["meta"]["truncation_hint"])
    assert int(hint[-1]) >= total + SLACK


def test_one_huge_item_keeps_the_item_and_cuts_its_field() -> None:
    _, out = run(big_app(), ["log"])
    env = envelope(out)
    record = env["data"]["records"][0]
    assert record["id"] == 1 and env["data"]["source"] == "api"
    assert record["log"].endswith(MARKER) and len(record["log"]) < 50_000
    (warning,) = env["warnings"]
    assert warning["context"]["field"] == "data.records[0].log"
    assert warning["context"]["original_length"] == 50_000
    assert "total_count" not in env["meta"]  # data is an object, not a list


def test_wide_object_loses_trailing_keys() -> None:
    code, out = run(big_app(), ["table"])
    env = envelope(out)
    assert code == 0 and env["meta"]["truncated"] is True
    assert len(out.encode()) <= MIN_BYTES
    rows = env["data"]["rows"]
    assert env["data"]["name"] == "t" and set(rows) == {f"k{i}" for i in range(len(rows))}
    assert [w["context"]["field"] for w in env["warnings"]] == ["data.rows"]


def sized_app(width: int) -> App:
    """Fixed data whose size steps with ``width``, so a sweep lands each cut on every
    byte offset of the cap, as the length of ``meta.cwd`` did in #129"""
    app = App("sizectl", version="1.0.0")

    @app.command("ls", description="List", danger_level="safe", exit_codes=(), paginated=False)
    def ls(args: NoArgs, ctx: Ctx) -> list[str]:
        return ["x" * width for _ in range(2000)]

    @app.command("log", description="One long field", danger_level="safe", exit_codes=())
    def log(args: NoArgs, ctx: Ctx) -> dict[str, object]:
        return {"log": "y" * (20_000 + width), "name": "n"}

    @app.command("tail", description="Stream", danger_level="safe", exit_codes=(), streaming=True)
    def tail(args: NoArgs, ctx: Ctx) -> Iterator[list[str]]:
        yield ["x" * width for _ in range(2000)]

    return app


CAP = 8192
EXEC = '{"_cmd": "ls"}\n{"_cmd": "log"}\n'


@pytest.mark.parametrize(
    ("argv", "stdin", "slack"),
    [
        # A string is cut to the character, so the line fills the cap
        (["log"], "", 0),
        # A list keeps whole items: at most one item and its comma short of the cap
        (["ls"], "", None),
        (["ls", "--warnings-as-errors"], "", None),
        (["tail"], "", None),
        (["exec"], EXEC, None),
    ],
)
def test_the_written_line_newline_included_never_exceeds_the_cap(
    argv: list[str], stdin: str, slack: int | None
) -> None:
    """#129: the cut measured the envelope without the newline write_envelope adds, so a
    cut that filled the cap exactly wrote cap + 1 bytes"""
    for width in range(1, 25):
        env = {"SIZECTL_MAX_OUTPUT_BYTES": str(CAP)}
        _, out = run(sized_app(width), argv, env=env, stdin=stdin)
        lines = out.splitlines(keepends=True)
        assert all(line.endswith("\n") and len(line.encode()) <= CAP for line in lines)
        # A stream's closing event is small and uncut; every cut line keeps what fits
        cut = [line for line in lines if json.loads(line)["meta"].get("truncated")]
        assert cut, (argv, width)
        room = width + 3 if slack is None else slack
        for line in cut:
            assert CAP - room <= len(line.encode()), (argv, width, len(line.encode()))


def test_total_bytes_counts_the_line_as_written() -> None:
    """``meta.total_bytes`` and the cap count the same bytes: the full line rerun under a
    cap of exactly ``total_bytes`` is not cut. ``--stable-output`` keeps ``duration_ms``
    from changing the line's length between the runs"""
    stable = ["ls", "--stable-output"]
    _, out = run(sized_app(10), stable, env={"SIZECTL_MAX_OUTPUT_BYTES": str(CAP)})
    total = envelope(out)["meta"]["total_bytes"]
    _, full = run(sized_app(10), [*stable, "--max-output", str(total + SLACK)])
    assert len(full.encode()) <= total + SLACK and "truncated" not in envelope(full)["meta"]
    _, cut = run(sized_app(10), [*stable, "--max-output", str(len(full.encode()) - 1)])
    assert envelope(cut)["meta"]["truncated"] is True
    _, exact = run(sized_app(10), [*stable, "--max-output", str(len(full.encode()))])
    assert "truncated" not in envelope(exact)["meta"]


def test_env_var_raises_the_cap_and_flag_overrides_it() -> None:
    raised = {"BIGCTL_MAX_OUTPUT_BYTES": "10000000"}
    _, out = run(big_app(), ["items"], env=raised)
    assert "truncated" not in envelope(out)["meta"]
    _, out = run(big_app(), ["items", f"--max-output={MIN_BYTES}"], env=raised)
    assert envelope(out)["meta"]["truncated"] is True


@pytest.mark.parametrize(
    ("argv", "env", "source"),
    [
        (["small", "--max-output", "lots"], {}, "--max-output"),
        (["small", "--max-output", "100"], {}, "--max-output"),
        (["small"], {"BIGCTL_MAX_OUTPUT_BYTES": "-1"}, "BIGCTL_MAX_OUTPUT_BYTES"),
    ],
)
def test_invalid_cap_is_arg_error(argv: list[str], env: dict[str, str], source: str) -> None:
    code, out = run(big_app(), argv, env=env)
    error = envelope(out)["error"]
    assert code == 2 and error["code"] == "ARG_ERROR" and error["context"]["source"] == source


def test_exec_caps_each_line() -> None:
    code, out = run(big_app(), ["exec"], stdin='{"_cmd": "items"}\n{"_cmd": "small"}\n')
    first, second = (envelope(line) for line in out.splitlines())
    assert code == 0
    assert first["meta"]["truncated"] is True and first["meta"]["_line"] == 1
    assert "truncated" not in second["meta"]


def test_plain_mode_is_not_capped() -> None:
    out = io.StringIO()
    big_app().run(["items"], stdout=out, stderr=io.StringIO(), env={}, isatty=True)
    assert out.getvalue().count("name: item-") == 1000


def test_command_flag_named_like_a_global_is_rejected() -> None:
    @dataclass(frozen=True, slots=True)
    class Args:
        max_output: int = Flag(default=0, description="Shadowed by the global flag")

    app = App("x", version="1.0.0")
    with pytest.raises(RegistrationError, match="max-output"):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Args, ctx: Ctx) -> dict[str, str]:
            return {}


def test_in_process_call_is_capped_like_stdout() -> None:
    envelope = big_app().call("items", {}, env={})
    assert envelope.extra_meta.get("truncated") is True
    assert {w.code for w in envelope.warnings} == {"FIELD_TRUNCATED"}


def test_a_call_without_argv_names_the_apps_own_cap_variable() -> None:
    """An exec line or MCP call cannot be rerun with --max-output: the hint names BIGCTL_..."""
    _, out = run(big_app(), ["exec"], stdin='{"_cmd": "items"}\n')
    hint = json.loads(out.splitlines()[0])["meta"]["truncation_hint"]
    assert "BIGCTL_MAX_OUTPUT_BYTES" in hint and "<APP>" not in hint


@pytest.mark.parametrize(
    ("argv", "stdin"),
    [(["ls"], ""), (["exec"], '{"_cmd": "ls"}\n')],
)
def test_a_second_cut_after_settling_keeps_the_first_cuts_report(
    argv: list[str], stdin: str
) -> None:
    """#134: the error --warnings-as-errors adds to a cut envelope makes it cut again; the
    second cut reported the first cut's size as ``total_bytes`` and the field twice"""
    flags = [*argv, "--stable-output", "--warnings-as-errors"]
    capped = {"SIZECTL_MAX_OUTPUT_BYTES": str(CAP)}
    code, out = run(sized_app(10), flags, env=capped, stdin=stdin)
    line = out.splitlines(keepends=True)[0]
    env = envelope(line)
    assert code != 0 and env["error"]["code"] == "WARNINGS_AS_ERRORS"
    assert env["error"]["context"]["count"] == 1 and len(line.encode()) <= CAP
    (warning,) = env["warnings"]
    assert warning["code"] == "FIELD_TRUNCATED" and warning["context"]["original_length"] == 2000
    meta = env["meta"]
    assert meta["total_count"] == 2000 and meta["returned_count"] == len(env["data"])
    # Uncut, nothing warns, so nothing is an error: the full response is the plain line
    raised = {"SIZECTL_MAX_OUTPUT_BYTES": "1000000"}
    _, full = run(sized_app(10), flags, env=raised, stdin=stdin)
    assert meta["total_bytes"] == len(full.splitlines(keepends=True)[0].encode())


def blob_app() -> App:
    app = App("blobctl", version="1.0.0")

    @dataclass(frozen=True, slots=True)
    class Args:
        pad: int = Flag(default=0, description="Length of the warning's message")

    @app.command("blob", description="A blob no cut enters", danger_level="safe", exit_codes=())
    def blob(args: Args, ctx: Ctx) -> dict[str, object]:
        ctx.warn("PADDING", "p" * args.pad)
        return {"blob": {"type": "binary", "encoding": "base64", "value": "A" * 2000}}

    return app


def test_data_dropped_whole_is_measured_with_its_report() -> None:
    """#134: when no cut of data fits, data is dropped, and that envelope was never
    measured: its truncation meta and warning passed the cap over a base just under it.
    Where the report fits in its short form, the line is within the cap; where nothing
    can make it fit, the line passes the cap by that short report alone"""

    def line(pad: int, cap: int) -> str:
        stdin = json.dumps({"_cmd": "blob", "pad": pad}) + "\n"
        env = {"BLOBCTL_MAX_OUTPUT_BYTES": str(cap)}
        _, out = run(blob_app(), ["exec", "--stable-output"], env=env, stdin=stdin)
        return out.splitlines(keepends=True)[0]

    whole = line(0, 1_000_000)
    data = len(json.dumps(json.loads(whole)["data"], separators=(",", ":"))) - len("null")
    base = len(whole.encode()) - data  # the line with no data, at pad 0
    within = 0
    for pad in range(CAP - base - 400, CAP - base + 1, 5):
        cut = line(pad, CAP)
        env = envelope(cut)
        assert env["data"] is None, pad
        assert [w["code"] for w in env["warnings"]] == ["PADDING", "FIELD_TRUNCATED"], pad
        total = env["meta"]["total_bytes"]
        brief = f"the full response is {total} bytes, over BLOBCTL_MAX_OUTPUT_BYTES"
        size = len(cut.encode())
        assert size <= CAP or env["meta"]["truncation_hint"] == brief, (pad, size)
        within += size <= CAP and env["meta"]["truncation_hint"] == brief
    assert within  # a window the long hint overran is now within the cap
