# /// script
# requires-python = ">=3.14"
# dependencies = ["pydantic-settings>=2.15"]
# ///
"""The pydantic-settings migration's starting point: todo_argparse.py written with CliApp

The inline metadata lets uv run it without adding pydantic-settings to the project:

uv run examples/tutorial/todo_pydantic.py add "Buy milk" --priority high --db tmp/todo.json
uv run examples/tutorial/todo_pydantic.py list --db tmp/todo.json
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    CliApp,
    CliImplicitFlag,
    CliPositionalArg,
    CliSubCommand,
    SettingsConfigDict,
)

Item = dict[str, Any]


def load(db: Path) -> list[Item]:
    if not db.exists():
        return []
    items: list[Item] = json.loads(db.read_text())
    return items


def save(db: Path, items: list[Item]) -> None:
    db.write_text(json.dumps(items, indent=2))


class Common(BaseModel):
    db: Path = Field(Path.home() / ".todo.json", description="Where the items are stored")


class Add(Common):
    """Add an item"""

    text: CliPositionalArg[str] = Field(description="What to do")
    priority: Literal["low", "normal", "high"] = Field("normal", description="How urgent it is")

    def cli_cmd(self) -> None:
        items = load(self.db)
        next_id = max((i["id"] for i in items), default=0) + 1
        items.append({"id": next_id, "text": self.text, "priority": self.priority, "done": False})
        save(self.db, items)
        print(f"Added #{next_id}: {self.text}")


class ListItems(Common):
    """List open items"""

    all: CliImplicitFlag[bool] = Field(
        False, validation_alias=AliasChoices("a", "all"), description="Include completed items"
    )

    def cli_cmd(self) -> None:
        for item in load(self.db):
            if self.all or not item["done"]:
                mark = "x" if item["done"] else " "
                print(f"[{mark}] #{item['id']} {item['text']} ({item['priority']})")


class Done(Common):
    """Mark an item completed"""

    id: CliPositionalArg[int] = Field(description="Item number")

    def cli_cmd(self) -> None:
        items = load(self.db)
        for item in items:
            if item["id"] == self.id:
                item["done"] = True
                save(self.db, items)
                print(f"Completed #{self.id}")
                return
        raise SystemExit(f"no item #{self.id}")


class Purge(Common):
    """Delete completed items"""

    yes: CliImplicitFlag[bool] = Field(
        False, validation_alias=AliasChoices("y", "yes"), description="Do not ask for confirmation"
    )

    def cli_cmd(self) -> None:
        items = load(self.db)
        completed = [i for i in items if i["done"]]
        if not self.yes and input(f"Delete {len(completed)} completed items? [y/N]: ") != "y":
            raise SystemExit("Aborted!")
        save(self.db, [i for i in items if not i["done"]])
        print(f"Deleted {len(completed)} items")


class Todo(BaseSettings):
    """Track todo items"""

    model_config = SettingsConfigDict(cli_prog_name="todo")

    add: CliSubCommand[Add]
    list: CliSubCommand[ListItems]
    done: CliSubCommand[Done]
    purge: CliSubCommand[Purge]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


if __name__ == "__main__":
    CliApp.run(Todo)
