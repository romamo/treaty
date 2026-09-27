import io

import pytest

from examples.hello.cli import app
from examples.hello.greetings import Greeting, Name, greet


def test_greet_builds_the_message() -> None:
    assert greet(Name("Ada"), shout=False) == Greeting(message="Hello, Ada!")


def test_greet_shouts() -> None:
    assert greet(Name("Ada"), shout=True) == Greeting(message="HELLO, ADA!")


@pytest.mark.parametrize("value", ["", "   "])
def test_name_rejects_blank(value: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        Name(value)


def test_cli_returns_the_greeting() -> None:
    env = app.call("greet", {"name": "Ada", "shout": True})
    assert env.exit_code == 0 and env.data == {"message": "HELLO, ADA!"}


def test_cli_turns_a_blank_name_into_an_arg_error() -> None:
    env = app.call("greet", {"name": " "})
    assert env.exit_code == 2 and env.error is not None
    assert env.error.code == "ARG_ERROR" and "blank" in env.error.message


def test_cli_prints_the_greeting_in_plain_mode() -> None:
    out = io.StringIO()
    code = app.run(["greet", "Ada", "--format", "plain"], stdout=out, stderr=io.StringIO())
    assert code == 0 and out.getvalue() == "Hello, Ada!\n"


def test_cli_prints_the_greeting_as_csv() -> None:
    out = io.StringIO()
    code = app.run(["greet", "Ada", "--format", "csv"], stdout=out, stderr=io.StringIO())
    assert code == 0 and out.getvalue() == 'message\n"Hello, Ada!"\n'
