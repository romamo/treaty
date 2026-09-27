"""Defects the 1.0 final review found in the workstreams after the Phase A review"""

import io
import json
from dataclasses import dataclass

from treaty import App, Ctx, NoArgs


@dataclass(frozen=True, slots=True)
class Keyed:
    region: str = "eu"
    api_token: str = ""

    def __post_init__(self) -> None:
        if self.api_token and not self.api_token.startswith("tk-"):
            raise ValueError(f"api_token {self.api_token!r} lacks the tk- prefix")


def keyed_app() -> App:
    app = App("my-tool", version="1.0.0", description="Rows", settings=Keyed)

    @app.command("show", description="Show the region", danger_level="safe", exit_codes=())
    def show(args: NoArgs, ctx: Ctx, settings: Keyed) -> dict[str, str]:
        return {"region": settings.region}

    return app


def run(app: App, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdout=out, stderr=err, env=env, isatty=False)
    return code, out.getvalue(), err.getvalue()


def test_a_settings_invalid_message_never_echoes_a_secret_setting_value() -> None:
    code, out, err = run(keyed_app(), ["show"], {"MY_TOOL_API_TOKEN": "hunter2-secret"})
    assert code == 2 and json.loads(out)["error"]["code"] == "CONFIG_INVALID"
    assert "hunter2-secret" not in out + err
    assert "[REDACTED]" in json.loads(out)["error"]["message"]


def test_the_effective_config_hash_does_not_cover_secret_setting_values() -> None:
    def hash_of(env: dict[str, str]) -> str:
        code, out, _ = run(keyed_app(), ["show"], env)
        assert code == 0, out
        return str(json.loads(out)["meta"]["effective_config_hash"])

    assert hash_of({"MY_TOOL_API_TOKEN": "tk-1"}) == hash_of({"MY_TOOL_API_TOKEN": "tk-2"})
    assert hash_of({"MY_TOOL_REGION": "us"}) != hash_of({})
