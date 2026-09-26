"""File templates for ``treaty init``. Generated projects pass ``treaty audit`` as-is."""

from __future__ import annotations

import keyword
import re
import sys
from dataclasses import dataclass

from ._errors import ParseError

# PEP 508 names end in a letter or digit; no doubled hyphens
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# The scaffold's own directories and dependencies: a project named after one breaks
# (dist and build are gitignored; pluggy, iniconfig, packaging, pygments are pytest's)
_TAKEN = frozenset(
    {
        *("treaty", "pytest", "tests", "conformance", "dist", "build"),
        *("pluggy", "iniconfig", "packaging", "pygments"),
    }
)


@dataclass(frozen=True, slots=True)
class ProjectName:
    value: str

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.value):
            raise ParseError(
                "name must be lowercase letters, digits, and single hyphens, starting with a "
                "letter and ending with a letter or digit",
                context={"name": self.value},
            )
        package = self.value.replace("-", "_")
        if keyword.iskeyword(package):
            raise ParseError(
                f"{self.value!r} is a Python keyword, so its package cannot be imported",
                context={"name": self.value},
            )
        if package in _TAKEN or package in sys.stdlib_module_names:
            raise ParseError(
                f"{self.value!r} would collide with the {package} module or directory it uses",
                context={"name": self.value},
            )

    @property
    def package(self) -> str:
        return self.value.replace("-", "_")


def _toml_string(value: str) -> str:
    """A TOML basic string: a Windows path's backslashes and any quote survive"""
    escaped = "".join(
        f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c
        for c in value.replace("\\", "\\\\").replace('"', '\\"')
    )
    return f'"{escaped}"'


def render(name: ProjectName, treaty_source: str | None = None) -> dict[str, str]:
    """Relative path to file contents for a new project"""
    n, pkg = name.value, name.package
    sources = (
        "\n[tool.uv.sources]\n"
        f"treaty = {{ path = {_toml_string(treaty_source)}, editable = true }}\n"
        if treaty_source
        else ""
    )
    return {
        "pyproject.toml": f'''[project]
name = "{n}"
version = "0.1.0"
description = "{n}: an agent-ready CLI built on treaty"
requires-python = ">=3.14"
dependencies = ["treaty"]

[project.scripts]
{n} = "{pkg}.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["uv_build>=0.8,<0.13"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
{sources}''',
        f"src/{pkg}/__init__.py": f'"""{n}."""\n',
        f"src/{pkg}/cli.py": f'''"""Command-line entry point for {n}."""

from dataclasses import dataclass

from treaty import App, Arg, Ctx, Exit, Flag

app = App("{n}", version="0.1.0", description="Describe what {n} does")
app.exit_code(
    "ALREADY_EXISTS",
    79,
    description="An item with that name already exists",
    retryable=False,
    side_effects="none",
)
app.exit_code(
    "ITEM_NOT_FOUND",
    80,
    description="No item with that name exists",
    retryable=False,
    side_effects="none",
)


@dataclass(frozen=True, slots=True)
class ItemArgs:
    name: str = Arg(description="Item name")


@dataclass(frozen=True, slots=True)
class CreateArgs:
    name: str = Arg(description="Item name")
    note: str | None = Flag(default=None, description="Optional note")
    dry_run: bool = Flag(default=False, description="Show what would be created")


@dataclass(frozen=True, slots=True)
class DeleteArgs:
    name: str = Arg(description="Item name")
    dry_run: bool = Flag(default=False, description="Show what would be deleted")


@dataclass(frozen=True, slots=True)
class Item:
    name: str
    note: str | None
    exists: bool


@dataclass(frozen=True, slots=True)
class Creation:
    effect: str
    name: str
    note: str | None


@dataclass(frozen=True, slots=True)
class Deletion:
    effect: str
    name: str


@app.command(
    "status",
    description="Report whether an item exists",
    examples=[("Check an item", "{n} status widget")],
)
def status(args: ItemArgs, ctx: Ctx) -> Item:
    return Item(name=args.name, note=None, exists=False)


@app.command(
    "create",
    description="Create an item",
    danger_level="mutating",
    exit_codes=["ALREADY_EXISTS"],
    supports_raw_payload=True,
    examples=[("Create an item", "{n} create widget --note first")],
)
def create(args: CreateArgs, ctx: Ctx) -> Creation:
    if args.name == "taken":
        raise Exit.ALREADY_EXISTS("item exists", context={{"name": args.name}})
    effect = "would_create" if args.dry_run else "created"
    return Creation(effect=effect, name=args.name, note=args.note)


@app.command(
    "delete",
    description="Delete an item",
    danger_level="destructive",
    exit_codes=["ITEM_NOT_FOUND"],
    examples=[("Preview a deletion", "{n} delete widget --dry-run")],
)
def delete(args: DeleteArgs, ctx: Ctx) -> Deletion:
    if args.name == "missing":
        raise Exit.ITEM_NOT_FOUND("no such item", context={{"name": args.name}})
    return Deletion(effect="would_delete" if args.dry_run else "deleted", name=args.name)


def main() -> None:
    app.main()
''',
        "tests/__init__.py": "",
        "tests/test_cli.py": f"""import io
import json

from {pkg}.cli import app


def run(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    code = app.run(argv, stdout=out, stderr=io.StringIO(), env={{}}, isatty=False)
    return code, json.loads(out.getvalue())


def test_status() -> None:
    code, envelope = run(["status", "widget"])
    assert code == 0 and envelope["data"]["name"] == "widget"


def test_create_conflict_uses_declared_exit_code() -> None:
    code, envelope = run(["create", "taken"])
    assert code == 79 and envelope["error"]["code"] == "ALREADY_EXISTS"


def test_delete_needs_confirmation() -> None:
    code, envelope = run(["delete", "widget"])
    assert code == 2 and envelope["error"]["code"] == "CONFIRMATION_REQUIRED"
    code, envelope = run(["delete", "widget", "--confirm-destructive"])
    assert code == 0 and envelope["data"]["effect"] == "deleted"
""",
        f"conformance/{n}.json": f'''{{
  "schema_version": "1.0",
  "tool": "{n}",
  "command": ["./{n}"],
  "timeout_seconds": 5,
  "manifest": ["manifest"],
  "argument_order": {{
    "command_path": ["delete", "widget"],
    "local_args": ["--dry-run", "--confirm-destructive"],
    "global_flag": "--format",
    "value": "json",
    "alternate_value": "human"
  }},
  "probes": [
    {{ "name": "status", "argv": ["status", "widget"], "kind": "read" }},
    {{ "name": "unknown flag", "argv": ["status", "widget", "--no-such-flag"], "kind": "invalid" }},
    {{
      "name": "delete",
      "argv": ["delete", "widget"],
      "kind": "destructive",
      "dry_run_flag": "--dry-run"
    }}
  ]
}}
''',
        f"conformance/{n}": f"""#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
exec "$here/../.venv/bin/{n}" "$@"
""",
        "README.md": f"""# {n}

Built on [treaty](https://github.com/romamo/treaty).

```bash
uv sync
uv run {n} status widget
uv run pytest
uv run treaty audit {pkg}.cli:app
uv run treaty conformance {pkg}.cli:app --run
```
""",
        ".gitignore": ".venv/\n__pycache__/\ndist/\n.pytest_cache/\n",
    }


EXECUTABLE = frozenset({"conformance/{name}"})
