"""CLI layer: maps arguments to the domain and the result to output

uv run -m examples.hello greet world
uv run -m examples.hello greet Ada --shout --format human
uv run -m examples.hello manifest
"""

from collections.abc import Mapping
from dataclasses import dataclass

from treaty import App, Arg, Ctx, Flag

from .greetings import Greeting, Name, greet

app = App("hello", version="0.1")
app.scalar(Name, parse=Name)


@dataclass(frozen=True, slots=True)
class Greet:
    name: Name = Arg(description="Who to greet")
    shout: bool = Flag(default=False, description="Print the greeting in upper case")


def greet_text(data: Mapping[str, str]) -> str:
    return f"{Greeting(**data).message}\n"


@app.command(
    "greet",
    description="Say hello",
    human=greet_text,
    examples=[
        ("Greet the world", "hello greet world"),
        ("Greet Ada loudly", "hello greet Ada --shout"),
    ],
)
def greet_command(args: Greet, ctx: Ctx) -> Greeting:
    return greet(args.name, shout=args.shout)
