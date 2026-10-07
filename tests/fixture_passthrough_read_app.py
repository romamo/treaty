"""A safe, network passthrough command (#386): its tool owns stdout, so the envelope is on
stderr, where the conformance kit's json_envelope check does not read it"""

from treaty import App, Ctx, NoArgs

app = App("gitw", version="1.0.0", description="Wrap git")


@app.command(
    "git",
    description="Run git",
    danger_level="safe",
    exit_codes=(),
    passthrough=True,
    has_network_io=True,
    examples=[("Log", "gitw git log --format oneline --max-count 3")],
)
def git(args: NoArgs, ctx: Ctx) -> int:
    print(" ".join(ctx.argv_rest))
    return 0


if __name__ == "__main__":
    app.main()
