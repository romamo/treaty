"""CLI layer: maps arguments to the domain and the result to output

uv run -m examples.hello greet world
uv run -m examples.hello greet Ada --shout --format plain
uv run -m examples.hello greet Ada --format csv
uv run -m examples.hello manifest
"""

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass

from treaty import App, Arg, Ctx, Flag, Format

from .greetings import Greeting, Name, greet


def render_csv(data: Mapping[str, object]) -> str:
    """Any flat result as a header row and one row of values"""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(data.keys())
    writer.writerow(data.values())
    return out.getvalue()


app = App("hello", version="0.1.0", description="Greet people")
app.scalar(Name, parse=Name)
app.format(Format.CSV, render=render_csv)


@dataclass(frozen=True, slots=True)
class Greet:
    name: Name = Arg(description="Who to greet")
    shout: bool = Flag(default=False, description="Print the greeting in upper case")


def render_greet(data: Mapping[str, str]) -> str:
    return f"{Greeting(**data).message}\n"


@app.command(
    "greet",
    description="Say hello",
    renderers={Format.PLAIN: render_greet},
    examples=[
        ("Greet the world", "hello greet world"),
        ("Greet Ada loudly", "hello greet Ada --shout"),
    ],
    danger_level="safe",
    exit_codes=(),
)
def greet_command(args: Greet, ctx: Ctx) -> Greeting:
    return greet(args.name, shout=args.shout)
