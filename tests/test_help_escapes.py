"""--help and the completion menu show the app's own text the way plain output does: a
control character, terminal escape, or bidi override as its escape (#203)"""

import io
from dataclasses import dataclass
from typing import Literal

from treaty import App, Arg, Ctx, Flag, NoArgs

TITLE = "\x1b]0;pwned\x07"  # OSC 0: sets the window title
OVERRIDE = "\u202e"  # RIGHT-TO-LEFT OVERRIDE: reverses how the rest of the line displays
ACTIVE = ("\x1b", "\x07", "\r", "\x08", OVERRIDE)


@dataclass(frozen=True, slots=True)
class PaintArgs:
    name: str = Arg(description=f"Layer{TITLE} name")
    mode: Literal["fast", "slow"] = Flag(default="fast", description=f"How{OVERRIDE}fast\r")


def hostile() -> App:
    app = App("paint", version="1.0.0", description=f"Paint{TITLE} things")
    canvas = app.group("canvas", description=f"Work on{OVERRIDE} the canvas")

    @canvas.command(
        "add",
        description="Add a layer\x08\x08\x08\x08erase",
        danger_level="safe",
        exit_codes=(),
        examples=[(f"Add{TITLE} one", f"paint canvas add top{OVERRIDE}")],
    )
    def add(args: PaintArgs, ctx: Ctx) -> dict[str, str]:
        return {"name": args.name}

    return app


def plain_app() -> App:
    app = App("paint", version="1.0.0", description="Paint things")

    @app.command(
        "add",
        description="Add a layer\n\tover the last one",
        danger_level="safe",
        exit_codes=(),
        examples=[("Add one", "paint add top")],
    )
    def add(args: NoArgs, ctx: Ctx) -> dict[str, str]:
        return {}

    return app


def run(app: App, argv: list[str]) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = app.run(argv, stdin=io.StringIO(), stdout=out, stderr=err, env={}, isatty=False)
    assert code == 0, err.getvalue()
    return out.getvalue(), err.getvalue()


def inert(text: str) -> bool:
    return not any(c in text for c in ACTIVE)


def test_root_help_shows_the_app_and_group_descriptions_controls_as_escapes() -> None:
    out, _ = run(hostile(), ["--help", "--format", "plain"])
    assert inert(out)
    assert "paint: Paint\\x1b]0;pwned\\x07 things\n" in out
    assert "Work on\\u202e the canvas" in out


def test_group_help_lists_the_command_description_escaped() -> None:
    out, _ = run(hostile(), ["canvas", "--help", "--format", "plain"])
    assert inert(out)
    assert "Add a layer\\x08\\x08\\x08\\x08erase" in out


def test_command_help_escapes_descriptions_flags_and_examples() -> None:
    out, _ = run(hostile(), ["canvas", "add", "--help", "--format", "plain"])
    assert inert(out)
    assert "\nAdd a layer\\x08\\x08\\x08\\x08erase\n" in out
    assert "Layer\\x1b]0;pwned\\x07 name" in out
    assert "How\\u202efast\\r" in out
    assert "  # Add\\x1b]0;pwned\\x07 one\n  paint canvas add top\\u202e\n" in out


def test_help_on_stderr_in_json_mode_is_escaped_too() -> None:
    _, err = run(hostile(), ["canvas", "add", "--help", "--format", "json"])
    assert inert(err)
    assert "Layer\\x1b]0;pwned\\x07 name" in err


def test_newlines_and_tabs_in_a_description_keep_their_layout() -> None:
    out, _ = run(plain_app(), ["add", "--help", "--format", "plain"])
    assert "\nAdd a layer\n\tover the last one\n" in out
    assert "\\n" not in out and "\\t" not in out


def test_the_completion_menu_shows_a_description_control_as_its_escape() -> None:
    out, _ = run(hostile(), ["completion", "zsh", "--format", "plain"])
    assert inert(out)
    assert "Work on\\u202e the canvas" in out
