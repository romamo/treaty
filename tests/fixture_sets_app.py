"""An app whose ordered command returns sets in a pydantic model (#387); run as a script,
it prints that command's data, but for the field whose x-ordered keeps the set's order."""

import io
import json
import sys

from pydantic import BaseModel, Field

from treaty import App, Ctx, NoArgs

TAGS = frozenset(f"tag{i}" for i in range(20))


class Tagged(BaseModel):
    name: str
    tags: set[str]


class Bag(BaseModel):
    tags: set[str]
    frozen: frozenset[str]
    names: list[str]
    items: list[Tagged]
    groups: list[set[str]]
    pinned: set[str] = Field(json_schema_extra={"x-ordered": True})


def bag() -> Bag:
    return Bag(
        tags=set(TAGS),
        frozen=TAGS,
        names=["b", "c", "a"],
        items=[Tagged(name="y", tags=set(TAGS)), Tagged(name="x", tags={"q", "p"})],
        groups=[set(TAGS), {"n", "m"}],
        pinned=set(TAGS),
    )


def sets_app(ordered: bool) -> App:
    app = App("bagctl", version="0.1.0")
    app.output_adapter(
        BaseModel,
        schema=lambda cls: cls.model_json_schema(mode="serialization"),
        dump=lambda obj: obj.model_dump(mode="json", by_alias=True),
    )

    @app.command("bag", description="Bag", danger_level="safe", exit_codes=(), ordered=ordered)
    def bag_(args: NoArgs, ctx: Ctx) -> Bag:
        return bag()

    return app


if __name__ == "__main__":
    out = io.StringIO()
    code = sets_app(True).run(
        ["bag"], stdin=io.StringIO(), stdout=out, stderr=io.StringIO(), env={}
    )
    if code != 0:
        raise SystemExit(out.getvalue())
    data = json.loads(out.getvalue())["data"]
    del data["pinned"]  # its x-ordered keeps the handler's order, which the hash seed picks
    sys.stdout.write(json.dumps(data))
