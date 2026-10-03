"""Built-in ``datetime.date`` and ``datetime.datetime`` arguments (#297)"""

import datetime as dt
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import spec_validator

from treaty import App, Arg, Ctx, Flag, NoArgs, RegistrationError
from treaty._mcp import call_tool, tool_entries

UTC = dt.UTC
PLUS_2 = dt.timezone(dt.timedelta(hours=2))


@dataclass(frozen=True, slots=True)
class Span:
    start: dt.date
    at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class DayArgs:
    day: dt.date = Arg(description="Day")
    since: dt.date = Flag(default=dt.date(2024, 1, 31), description="Since")
    at: dt.datetime | None = Flag(default=None, description="At")
    days: tuple[dt.date, ...] = Flag(default=(), description="Days")
    span: Span | None = Flag(default=None, description="Span")


@dataclass(frozen=True, slots=True)
class Seen:
    day: dt.date
    since: dt.date
    at: dt.datetime | None
    days: list[dt.date]
    span: str
    types: str


def day_app() -> App:
    app = App("dayctl", version="1.0.0")

    @app.command(
        "go", description="Go", danger_level="safe", supports_raw_payload=True, exit_codes=()
    )
    def go(args: DayArgs, ctx: Ctx) -> Seen:
        values = [args.day, args.since, args.at, *args.days]
        return Seen(
            args.day,
            args.since,
            args.at,
            list(args.days),
            repr(args.span),
            ",".join(type(v).__name__ for v in values),
        )

    return app


def run(argv: list[str], stdin: str = "") -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = day_app().run(
        argv, stdin=io.StringIO(stdin), stdout=out, stderr=io.StringIO(), env={}, isatty=False
    )
    return code, json.loads(out.getvalue())


def errors_of(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    error = env["error"]
    found = error["context"].get("errors") or [error]
    return {e["context"].get("flag") or e["context"].get("field"): e for e in found}


def test_date_and_datetime_flags_reach_the_handler_parsed() -> None:
    span = '{"start": "2024-03-01", "at": "2024-03-01T08:00:00+02:00"}'
    argv = ["go", "2024-02-29", "--at", "2024-01-01T10:00:00.5+02:00", "--days", "2024-01-01"]
    code, env = run([*argv, "--days", "2024-01-02", "--span", span])
    assert code == 0, env
    assert env["data"] == {
        "day": "2024-02-29",
        "since": "2024-01-31",
        "at": "2024-01-01T10:00:00.500000+02:00",
        "days": ["2024-01-01", "2024-01-02"],
        "span": repr(Span(dt.date(2024, 3, 1), dt.datetime(2024, 3, 1, 8, tzinfo=PLUS_2))),
        "types": "date,date,datetime,date,date",
    }
    code, env = run(["go", "2024-02-29", "--at", "2024-01-01T10:00:00Z"])
    assert code == 0 and env["data"]["at"] == "2024-01-01T10:00:00Z"


@pytest.mark.parametrize(
    "value",
    [
        "2024-13-01",
        "2023-02-29",
        "2024-1-1",
        "20240101",
        "2024-W01-1",
        "2024-01-01T00:00:00Z",
        " 2024-01-01",
        "２０２４-01-01",
        "",
    ],
)
def test_a_date_argument_refuses_anything_but_a_calendar_yyyy_mm_dd(value: str) -> None:
    code, env = run(["go", value])
    assert code == 2, env
    assert env["error"]["code"] == "ARG_ERROR"
    assert "'day'" in errors_of(env)["day"]["message"]


@pytest.mark.parametrize(
    "value",
    [
        "2024-01-01T10:00:00",
        "2024-01-01",
        "2024-01-01 10:00:00Z",
        "2024-01-01t10:00:00z",
        "2024-01-01T10:00Z",
        "2024-01-01T10:00:00+0200",
        "2024-01-01T10:00:00.1234567Z",
        "2024-01-01T25:00:00Z",
        "20240101T100000Z",
    ],
)
def test_a_datetime_argument_refuses_naive_and_non_rfc3339_text(value: str) -> None:
    code, env = run(["go", "2024-01-01", "--at", value])
    assert code == 2, env
    error = errors_of(env)["at"]
    assert "'at'" in error["message"]


def test_a_malformed_date_names_the_flag_and_the_form() -> None:
    code, env = run(["go", "2024-1-1"])
    error = errors_of(env)["day"]
    assert error["message"] == "Value for 'day' is not a YYYY-MM-DD date."
    assert "YYYY-MM-DD" in error["suggestion"]
    code, env = run(["go", "2024-01-01", "--at", "2024-01-01T10:00:00"])
    error = errors_of(env)["at"]
    assert error["message"] == "Value for 'at' is not a date-time with an offset."
    assert "2024-01-31T09:30:00Z" in error["suggestion"]
    # The text has the form but names no day: the parser's own reason
    code, env = run(["go", "2024-02-30"])
    assert code == 2
    assert "day 30 must be in range" in errors_of(env)["day"]["message"]


def test_exec_and_raw_payload_take_iso_strings() -> None:
    line = json.dumps(
        {"_cmd": "go", "day": "2024-02-29", "at": "2024-01-01T10:00:00Z", "days": ["2024-01-02"]}
    )
    code, env = run(["exec"], stdin=line + "\n")
    assert code == 0, env
    assert env["data"]["types"] == "date,date,datetime,date"
    payload = '{"day": "2024-02-29", "span": {"start": "2024-03-01"}}'
    code, env = run(["go", "--raw-payload", payload])
    assert code == 0, env
    assert env["data"]["span"] == repr(Span(dt.date(2024, 3, 1)))
    for bad in ('{"day": "20240229"}', '{"day": 20240229}', '{"day": "2024-02-29T00:00:00Z"}'):
        code, env = run(["go", "--raw-payload", bad])
        assert code == 2 and "day" in errors_of(env), (bad, env)


def test_app_call_takes_date_objects_and_refuses_a_datetime_for_a_date() -> None:
    at = dt.datetime(2024, 1, 1, 10, tzinfo=PLUS_2)
    env = day_app().call("go", {"day": dt.date(2024, 2, 29), "at": at}, env={})
    assert env.ok, env
    assert isinstance(env.data, dict)
    assert env.data["day"] == "2024-02-29" and env.data["at"] == "2024-01-01T10:00:00+02:00"
    env = day_app().call("go", {"day": dt.datetime(2024, 2, 29, tzinfo=UTC)}, env={})
    assert env.error is not None and env.error.code == "ARG_ERROR"
    assert env.error.message == "'day' expects a date as a string."
    env = day_app().call("go", {"day": dt.date(2024, 2, 29), "at": dt.datetime(2024, 1, 1)}, env={})
    assert env.error is not None and env.error.code == "ARG_ERROR"
    assert "offset" in env.error.message


def test_argument_schema_says_format_and_pattern_and_output_is_unchanged() -> None:
    code, env = run(["go", "--schema"])
    assert code == 0, env
    args = env["data"]["raw_payload_schema"]["properties"]
    day = {k: v for k, v in args["day"].items() if k != "description"}
    assert day == {"type": "string", "format": "date", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
    at = args["at"]["anyOf"][0]
    assert at["format"] == "date-time" and at["type"] == "string"
    assert args["days"]["items"]["format"] == "date"
    assert args["span"]["anyOf"][0]["properties"]["start"]["format"] == "date"
    # As before #297, so a schema lock of a date output sees no change
    assert env["data"]["output_schema"]["properties"]["day"] == {"type": "string", "format": "date"}
    manifest = day_app().manifest()
    spec_validator("manifest-response").validate(manifest)
    command = manifest["commands"]["go"]
    flags = command["flags"]
    assert flags["since"]["type"] == "string"
    assert flags["since"]["default"] == "2024-01-31"
    assert flags["since"]["pattern"] == "^(?:[0-9]{4}-[0-9]{2}-[0-9]{2})$"
    assert command["positionals"][0]["type"] == "string"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("2024-01-01", True),
        ("2024-13-01", False),  # the pattern matches; the parser refuses the month
        ("20240101", False),
        ("2024-01-01T10:00:00Z", True),
        ("2024-01-01T10:00:00.123456-05:30", True),
        ("2024-01-01T10:00:00", False),
        ("2024-01-01t10:00:00z", False),
        ("2024-01-01T10:00:00.1234567Z", False),
    ],
)
def test_the_published_pattern_matches_what_the_parser_accepts(value: str, ok: bool) -> None:
    props = day_app().manifest()["commands"]["go"]["flags"]
    flag = "since" if "T" not in value.upper() else "at"
    pattern = props[flag]["pattern"]
    code, _ = run(["go", "2024-01-01", f"--{flag}", value])
    assert (code == 0) is ok
    if ok:
        assert re.search(pattern, value)


def test_plain_output_shows_iso_text() -> None:
    out = io.StringIO()
    argv = ["go", "2024-02-29", "--at", "2024-01-01T10:00:00Z", "--format", "plain"]
    assert day_app().run(argv, stdout=out, stderr=io.StringIO(), env={}) == 0
    assert "2024-02-29" in out.getvalue() and "2024-01-01T10:00:00Z" in out.getvalue()


def test_mcp_tool_takes_iso_strings_and_refuses_a_naive_datetime() -> None:
    app = day_app()
    entries = {e.name: e for e in tool_entries(app)}
    day = entries["go"].input_schema["properties"]["day"]
    assert day["type"] == "string" and day["format"] == "date"
    env = call_tool(app, entries, "go", {"day": "2024-02-29"}, env={})
    assert env.ok, env
    env = call_tool(app, entries, "go", {"day": "2024-02-29", "at": "2024-02-29T10:00:00"}, env={})
    assert env.error is not None and env.error.code == "ARG_ERROR"


@pytest.mark.parametrize(
    ("annotation", "default", "match"),
    [
        (dt.date, dt.datetime(2024, 1, 1, tzinfo=UTC), "not a YYYY-MM-DD date"),
        (dt.datetime, dt.datetime(2024, 1, 1), "not a date-time with an offset"),
        (dt.date, "2024-01-01", "is not a date"),
    ],
)
def test_a_default_must_be_what_an_argument_could_parse_to(
    annotation: object, default: object, match: str
) -> None:
    @dataclass(frozen=True, slots=True)
    class Bad:
        when: annotation = Flag(default=default, description="When")  # type: ignore[valid-type]

    app = App("dayctl", version="1.0.0")
    with pytest.raises(RegistrationError, match=match):

        @app.command("go", description="Go", danger_level="safe", exit_codes=())
        def go(args: Bad, ctx: Ctx) -> None: ...


@dataclass(frozen=True, slots=True)
class DaySettings:
    since: dt.date | None = None
    until: dt.datetime | None = None


def settings_app() -> App:
    app = App("setctl", version="1.0.0", settings=DaySettings)

    @app.command("show", description="Show", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: DaySettings) -> dict[str, object]:
        return {
            "since": None if settings.since is None else type(settings.since).__name__,
            "until": None if settings.until is None else settings.until.isoformat(),
        }

    return app


def run_settings(argv: list[str], **env: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = settings_app().run(
        argv, stdout=out, stderr=io.StringIO(), env={"SETCTL_AUDIT_LOG": "0", **env}
    )
    return code, json.loads(out.getvalue())


def test_a_toml_native_date_and_offset_datetime_are_settings_values(tmp_path: Path) -> None:
    config = tmp_path / "setctl.toml"
    config.write_text("since = 2024-02-29\nuntil = 2024-03-01T10:00:00+02:00\n")
    code, env = run_settings(["show", "--config", str(config)])
    assert code == 0, env
    assert env["data"] == {"since": "date", "until": "2024-03-01T10:00:00+02:00"}
    config.write_text('since = "2024-02-29"\n')
    code, env = run_settings(["show", "--config", str(config)])
    assert code == 0 and env["data"]["since"] == "date", env
    code, env = run_settings(["show", "--no-config"], SETCTL_SINCE="2024-02-29")
    assert code == 0 and env["data"]["since"] == "date", env


@pytest.mark.parametrize(
    "line",
    [
        "until = 2024-03-01T10:00:00\n",  # a TOML local date-time names no instant
        "since = 2024-03-01T10:00:00Z\n",  # a date-time is not a date
        "since = 10:00:00\n",
        'since = "20240301"\n',
    ],
)
def test_a_bad_toml_date_is_config_invalid_naming_the_key(tmp_path: Path, line: str) -> None:
    config = tmp_path / "setctl.toml"
    config.write_text(line)
    code, env = run_settings(["show", "--config", str(config)])
    assert code == 2, env
    error = env["error"]
    assert error["code"] == "CONFIG_INVALID", error
    assert error["context"]["key"] == line.split(" ")[0]


def test_an_app_registered_date_replaces_the_built_in() -> None:
    app = App("dayctl", version="1.0.0")
    app.scalar(
        dt.date,
        parse=lambda text: dt.datetime.strptime(text, "%d.%m.%Y").date(),
        pattern=r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}",
        serialize=lambda d: d.strftime("%d.%m.%Y"),
    )

    @dataclass(frozen=True, slots=True)
    class Args:
        day: dt.date = Flag(description="Day")

    @dataclass(frozen=True, slots=True)
    class Out:
        day: dt.date

    @app.command("go", description="Go", danger_level="safe", exit_codes=())
    def go(args: Args, ctx: Ctx) -> Out:
        return Out(args.day)

    out = io.StringIO()
    code = app.run(["go", "--day", "29.02.2024"], stdout=out, stderr=io.StringIO(), env={})
    assert code == 0
    assert json.loads(out.getvalue())["data"] == {"day": "29.02.2024"}
    command = app.manifest()["commands"]["go"]
    assert command["flags"]["day"]["pattern"] == r"^(?:[0-9]{2}\.[0-9]{2}\.[0-9]{4})$"
    # The output schema is the app's scalar, as its serialize= writes it
    assert command["output_schema"]["properties"]["day"]["pattern"] == (
        r"^(?:[0-9]{2}\.[0-9]{2}\.[0-9]{4})$"
    )


def test_an_idempotency_fingerprint_of_date_arguments_is_stable() -> None:
    from treaty._idempotency import _canonical
    from treaty._scalars import EMPTY

    at = dt.datetime(2024, 1, 1, 10, tzinfo=PLUS_2)
    assert _canonical([dt.date(2024, 2, 29), at], EMPTY, 0) == [
        "2024-02-29",
        "2024-01-01T10:00:00+02:00",
    ]
