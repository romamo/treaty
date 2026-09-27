"""A tool run as a real process by test_config_layer: its env and cwd are its own"""

from dataclasses import dataclass

from treaty import App, Arg, Ctx, NoArgs


@dataclass(frozen=True, slots=True)
class Settings:
    region: str = "us-east-1"
    retries: int = 3
    api_token: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Shown:
    region: str
    retries: int
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetRegion:
    region: str = Arg(description="Region to store")


@dataclass(frozen=True, slots=True)
class Written:
    effect: str
    path: str


app = App("configctl", version="1.0.0", description="Read layered config", settings=Settings)


@app.command("show", description="Show the settings", danger_level="safe", exit_codes=())
def show(args: NoArgs, ctx: Ctx, settings: Settings) -> Shown:
    return Shown(settings.region, settings.retries, settings.tags)


@app.command(
    "config.set",
    description="Store the region in the config file",
    danger_level="mutating",
    exit_codes=(),
    config_write_scope="local",
)
def config_set(args: SetRegion, ctx: Ctx) -> Written:
    path = ctx.write_config(f'region = "{args.region}"\n')
    return Written("updated", str(path))


if __name__ == "__main__":
    app.main()
