"""A tool run as a real process by test_output_data: its cwd, stdout bytes, and line
endings are its own"""

import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Binary, Ctx, Flag, NoArgs, Out

app = App("outctl", version="1.0.0", description="Report output data")

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + bytes(range(256))


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Found:
    config_path: Path
    output_dir: Path
    tags: list[str]
    users: list[User] = Out(sort_key="id")
    fetched_at: str = Out(default="", volatile=True)


@dataclass(frozen=True, slots=True)
class Image:
    name: str
    bytes: Binary


@dataclass(frozen=True, slots=True)
class Named:
    name: str = Flag(default="x", description="A name")


@app.command(
    "find-config",
    description="Find the config, from wherever the tool runs",
    danger_level="safe",
    exit_codes=(),
)
def find_config(args: NoArgs, ctx: Ctx) -> Found:
    tags = ["gamma", "alpha", "beta"]
    users = [User("u2", "bob"), User("u1", "alice"), User("u3", "carol")]
    random.shuffle(tags)
    random.shuffle(users)
    return Found(
        Path("./src/.toolrc"),
        Path("dist"),
        tags,
        users,
        fetched_at=dt.datetime.now(dt.UTC).isoformat(),
    )


@app.command("get-image", description="Get the logo", danger_level="safe", exit_codes=())
def get_image(args: NoArgs, ctx: Ctx) -> Image:
    return Image("logo.png", Binary(PNG, content_type="image/png"))


@app.command("greet", description="Greet someone", danger_level="safe", exit_codes=())
def greet(args: Named, ctx: Ctx) -> dict[str, str]:
    ctx.log("greeting", name=args.name)
    return {"greeting": f"hello {args.name}"}


if __name__ == "__main__":
    app.main()
