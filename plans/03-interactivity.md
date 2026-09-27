# 03: Interactivity

Nothing ever waits for input an agent cannot give.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-009 Non-interactive auto-detection | 1 | Not started | No prompt API; `input()` on `/dev/null` crashes the handler with exit 1 |
| F-047 REPL prohibition in non-TTY | 2 | Partial | `input()` is not intercepted; no flag hint |
| C-005 `--yes` and `--non-interactive` | 2 | Not started | No `interactive=` declaration (ROADMAP 0.2.0) |
| F-055 `$EDITOR` no-op in non-TTY | 2 | Not started | No editor handling; the child-process half is in 04 |

The F-009 part is in M1; the rest follows 04.

## Design

### Prompt API (F-009, C-005)

```python
@app.command("init", description="...", interactive=True, ...)
def init(args: InitArgs, ctx: Ctx) -> Result:
    name = ctx.prompt("Project name", flag="name")
    if not ctx.confirm("Overwrite existing files?"):
        ...
```

- `interactive=True` on `@app.command` adds `--yes` and `--non-interactive` to the command
  and `interactive: true` to its manifest entry and `--schema`
- `ctx.prompt(text, *, flag)` names the flag that supplies the answer non-interactively;
  `ctx.confirm(text)` is answered by `--yes`
- A prompt is allowed only when stdin and stdout are both TTYs and `--non-interactive` is
  absent. Otherwise it raises the framework `INPUT_REQUIRED`: exit `4`, `phase: execution`,
  `retryable: false`, `suggestion` naming `--<flag>` or `--yes`, `context.prompt` with the text
- `--yes` on a command that never prompts is accepted and has no effect
- Calling `ctx.prompt` or `ctx.confirm` from a command without `interactive=True` raises
  `RegistrationError` at run time as a framework bug (exit 1, `HANDLER_CRASHED`), and the
  `interactive-declared` audit rule finds the calls statically by AST scan

Exit `4` is `PRECONDITION` in `_exit.py`, so `INPUT_REQUIRED` is an error code on that exit,
like `CONFIRMATION_REQUIRED` on exit 2. `PRECONDITION` joins the implicit exit codes of
`interactive=True` commands, the way it already does for mutating ones.

### Stray `input()` (F-047)

While a handler runs in a non-TTY context, replace `sys.stdin` with a reader whose
`readline` and `read` raise `InputRequired` (a `BaseException`, like `Cancelled`, so an
`except Exception` in user code cannot hide it). `_execute` turns it into the same
`INPUT_REQUIRED` envelope, with the suggestion "declare interactive=True and use
ctx.prompt with a flag" for the author and "no non-interactive form exists" for the agent.
The `exec` built-in keeps the real stdin, because it is the stdin consumer.

The no-arguments case already renders root help and exits 0 (`_app.py:504`); add a test
that pins it under a non-TTY stdin.

### Editor launches (F-055, in-process half)

`ctx.edit(initial: str, *, flag: str) -> str` opens `$VISUAL` or `$EDITOR` only on a TTY;
otherwise it raises `INPUT_REQUIRED` with `alternatives` listing `--<flag>` and
`--<flag>-from-file`. Commands using it declare `requires_editor=True` (C-023, P1, comes
free) and get the `alternatives` list in the manifest. 04 makes children ignore the editor.

## Tasks

- [ ] `interactive=`, `--yes`, `--non-interactive`; manifest and `--schema` fields
- [ ] `ctx.prompt`, `ctx.confirm`, `ctx.edit`; `INPUT_REQUIRED` exit 4
- [ ] Guarded `sys.stdin` during handler execution in non-TTY runs
- [ ] Audit rule `interactive-declared`
- [ ] README section "Prompts" with the one example above

## Tests

- `ctx.prompt` with stdin `/dev/null`: exit 4, `INPUT_REQUIRED`, suggestion names the flag,
  finishes in under a second
- Same command with a PTY on stdin and stdout prompts and reads the answer
- `--yes` answers `ctx.confirm` true; `--yes` on a non-interactive command is a no-op
- `--non-interactive` with a PTY still exits 4
- A handler calling `input()` under a pipe: exit 4, not `HANDLER_CRASHED`
- `ctx.edit` without a TTY exits 4 with `alternatives`
