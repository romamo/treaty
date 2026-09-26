"""Business logic: knows nothing about treaty, argv, or output formats"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Name:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("name must not be blank")


@dataclass(frozen=True, slots=True)
class Greeting:
    message: str


def greet(name: Name, *, shout: bool) -> Greeting:
    message = f"Hello, {name.value}!"
    return Greeting(message=message.upper() if shout else message)
