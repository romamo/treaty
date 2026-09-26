"""The smallest treaty app.

uv run examples/hello.py greet world
uv run examples/hello.py greet Ada --shout
uv run examples/hello.py manifest
"""

from collections.abc import Mapping
from dataclasses import dataclass

from treaty import App, Arg, Ctx, Flag

app = App("hello", version="0.1")


@dataclass(frozen=True, slots=True)
class Greet:
    name: str = Arg(description="Who to greet")
    shout: bool = Flag(default=False, description="Print the greeting in upper case")


@dataclass(frozen=True, slots=True)
class Greeting:
    message: str


def greet_text(data: Mapping[str, object]) -> str:
    return f"{data['message']}\n"


@app.command(
    "greet",
    description="Say hello",
    human=greet_text,
    examples=[
        ("Greet the world", "hello greet world"),
        ("Greet Ada loudly", "hello greet Ada --shout"),
    ],
)
def greet(args: Greet, ctx: Ctx) -> Greeting:
    message = f"Hello, {args.name}!"
    return Greeting(message=message.upper() if args.shout else message)


if __name__ == "__main__":
    app.main()
