# 06: Multi-step commands and lifecycle

Size: L

Gives handlers a step API, a batch result type, and one teardown path that runs on every
exit. Phase A: `steps=`, `rollback=`, `ctx.step()`, `Batch`, and resource `release` are
new public surface, and `cleanup=` changes when it runs, so all of it lands before the freeze.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| C-008 Step manifest | P1 | Not started | No step declaration or tracking (ROADMAP 0.3.0) | Yes: `steps=` keyword, `ctx.step()`, step fields in `data` |
| C-009 completed/failed/skipped summary | P1 | Not started | No batch summary contract | Yes: `treaty.Batch`, `Item`, `ItemError` |
| C-017 cleanup hook | P1 | Partial | `cleanup=` runs on signals and closed stdout only; no resource `release` | Yes: `cleanup=` also runs on normal exit, error, and timeout; `release` on resources |
| O-010 `--resume-from` | P2 | Not started | No flag | Yes: `resumable=` keyword, reserves `--resume-from` |
| O-011 `--rollback-on-failure` | P2 | Not started | No rollback hook | Yes: `rollback=` keyword, reserves `--rollback-on-failure` |

## API impact

- `App.command` gains `steps: Sequence[str] = ()`, `resumable: bool = False`, and
  `rollback: Rollback | None = None`, all optional; `Ctx.step(name) -> bool` is new
- New exports: `Batch`, `Item`, `ItemError`, `StepName`, `Rollback`
- A resource class may define `release(self) -> None`; treaty calls it after the handler
- **Behavior change**: `cleanup=` now runs after every handler run, not only on SIGINT,
  SIGTERM, and a closed stdout. Hooks written as "undo on cancel" run on success too;
  changelog entry under "Breaking"
- Manifest: `steps` on the command entry (a `CommandEntry` key already). `resumable` and
  `rollback_available` go in `--schema` only, since `CommandEntry` is
  `additionalProperties: false`, as with `requires_confirmation` in Level 2 plan 05
- Envelope `data` of a `steps=` command carries `completed_steps`, `failed_step`,
  `skipped_steps`, and, when resumable, `resume_from`; with rollback, `rollback_status` and
  `rollback_error`. `PARTIAL_FAILURE` (3) joins `implicit_exit_codes` for `steps=` and
  `Batch` commands
- Reserved flags `--resume-from` and `--rollback-on-failure` on the commands that declare
  them; `framework_collisions` refuses same-named fields

## Design

### Step tracking (C-008)

A new `_steps.py` holds `StepName` (a VO, `[a-z][a-z0-9_-]*`) and `StepTracker`: the
declared order, the index of the current step, the completed names, and a lock, since the
main thread reads it on timeout while the worker writes it. `_Run._ctx` builds one per run
from `command.steps` and hands it to `Ctx` as a `step_sink` field, like `warn_sink`.

```python
def migrate(args: MigrateArgs, ctx: Ctx) -> Migrated:
    if ctx.step("backup"):
        backup_database()
    if ctx.step("apply_schema"):
        apply_schema_changes()
    return Migrated(effect="updated")
```

- `ctx.step(name)` completes the current step and starts `name`. A name that is undeclared,
  repeated, or out of declared order raises `StepOrderError`, reported as `INVALID_STEP`
  (exit 1, `phase: execution`), like `INVALID_EFFECT`. The last step completes when the
  handler returns; declared steps never started are listed in `skipped_steps`
- Registration: `build_command` checks `steps` are unique `StepName`s, the output type is an
  object that can carry the step keys (`can_carry` from `_effect.py`), and none of its
  fields collide with them. `_check_ctx_calls` refuses `ctx.step("x")` with a literal not in
  `steps=`, and any `ctx.step` call on a command without `steps=`
- Failure: when the handler raises inside a step, `_exit_envelope` and `_crashed` merge the
  tracker snapshot into `data` (an object, or null becomes one; list data is
  `INVALID_EXIT`). With at least one completed step the exit becomes `PARTIAL_FAILURE` (3)
  and the handler's `error.code` and message are kept, matching the spec's `DISK_FULL`
  with exit 3; with none completed the raised exit stands. `data.partial` is true when any
  step completed
- Timeout and cancellation: the `TimeoutExpired` branch and `_cancelled` read the snapshot
  the same way, with the step in progress as `failed_step`
- Step events: `ctx.step` logs `step started` and `step completed` (with `step`, `index`,
  `total`) through the log sink, visible with `--verbose` (see 11); heartbeat lines carry
  `step`. Stream events are app-typed and get no step lines; the criteria need only
  `--schema` and the final response

### Batch results (C-009)

`Batch[T]` in `_batch.py` holds `items: tuple[Item[T], ...]`; an `Item[T]` has `id`
and exactly one of `value: T` and `error: ItemError(code, message, retryable)`. A handler
returns it like `Page[T]`, and `build_command` recognizes it.

- Serialized as `{"summary": {total, succeeded, failed}, "results": [...]}`; a result is
  `{"id", "ok": true, **value}` or `{"id", "ok": false, "error": {...}}`. The output schema
  is derived from `T`, as `_page_output` does for pages
- `ItemError.from_exit(exc: CliExit, exits)` builds an item error from a raised `CliExit`,
  taking `retryable` from the registry, so per-item errors match C-013's structure
- Any failed item: exit `PARTIAL_FAILURE` (3), error `PARTIAL_FAILURE` "2 of 5 items
  failed", `data` kept, `data.partial` true when any item succeeded
- Mutating batches: `effect_problem` checks `effect` on each successful item's value; the
  top-level `effect` is `noop` when nothing succeeded, else `updated`

### Teardown on every exit (C-017, ROADMAP 0.1.1)

A new `_lifecycle.py` has `Teardown`: the resources acquired this run that define
`release`, in acquisition order, and the command's `cleanup`. `run()` releases them in
reverse order, then calls `cleanup`, collecting failures instead of stopping at the first.
A lock and a done flag make it run at most once per run, whatever paths race, which is
what the idempotency criterion needs from the framework.

- `Resolver.get` registers each acquired instance with the run's `Teardown`;
  `resource_spec` checks that a `release` takes only `self`
- `_invoke` wraps the handler in `try`/`finally: teardown.run()` on the worker, so normal
  exit and errors are covered; `_execute`'s timeout branch and `_cancelled` join the worker
  for `GRACE_SECONDS` and then call `teardown.run()`, which is a no-op if the worker got
  there first. `output_closed` calls it instead of `command.cleanup` directly. A stream's
  teardown runs when its generator is exhausted or closed in `stream()`
- Framework resources join the same `Teardown`: `ctx.lock` from 03, and a session temp
  directory if one is added; `ctx.run` children are already stopped by `_stop_children`
- A failed `release` or `cleanup` writes its traceback to stderr and adds a
  `CLEANUP_FAILED` warning naming the hook; the exit code stays. Today's
  `context.cleanup_failed` on `CANCELLED` stays
- Development-time warning: new audit rule `resource-release` flags a resource class with
  `close`, `__exit__`, `terminate`, or `unlink` but no `release`, with the fix
  `def release(self) -> None: self.close()`. The `cleanup` rule for network commands stays
- Not applicable: "MAY register a no-op cleanup hook"; `cleanup=None` already means no-op

### `--resume-from` (O-010)

`resumable=True` requires `steps=` and adds a `FrameworkFlag` `resume-from` (type `enum` of
the step names). Its `parse` raises `ParseError` with `context.available` for an unknown
name, so the run exits 2 before the handler. `StepTracker` starts at that index: `ctx.step`
returns False for earlier steps and lists them in `skipped_steps`, which is how the handler
skips them (the spec leaves the skip to the command). The failure `data` of a resumable
command includes `resume_from: failed_step`. Audit rule `resume-guard` flags a resumable
handler whose `ctx.step(...)` result is not used as an `if` test. `effect` is the handler's
own, so it reflects only the resumed work.

### `--rollback-on-failure` (O-011)

`rollback=` takes `Callable[[Any, Ctx, tuple[StepName, ...]], None]`, called with the args,
the ctx, and the completed steps, newest first; it requires `steps=`. It adds the
`rollback-on-failure` switch. When the switch is on and a step fails, `_invoke` runs the
rollback on the worker, inside the remaining timeout, before `Teardown`, then re-raises.
`data.rollback_status` is `completed`, `failed` (with `rollback_error: {code, message}` and
the traceback on stderr), or `not_attempted` (no completed steps, flag absent, or timeout
and cancellation). The exit is `PARTIAL_FAILURE` (3) whenever a step failed after one
completed, so a successful rollback never exits 0.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 06-D1 | Step fields go in `data` (spec wire) or `meta` (no collision with app fields) | `data`, as the spec shows; registration refuses colliding output fields |
| 06-D2 | A step failure after completed steps: keep the raised exit code or force 3 | Force `PARTIAL_FAILURE` (3), keep `error.code`; the spec wire does both |
| 06-D3 | On timeout the handler may still run: release its lock beside it after the grace, or wait | Release after `GRACE_SECONDS`, as `_cancelled` already does for `cleanup=`; the process is ending |
| 06-D4 | A batch where every item failed: exit 3 or 1 | Exit 3 with `partial: false`; one rule for any failed item is easier to branch on |

## Tasks

- [ ] `StepName`, `StepTracker`, `ctx.step`, `steps=` registration checks, `INVALID_STEP`
- [ ] Step fields on success, failure, timeout, and `CANCELLED`; `PARTIAL_FAILURE` rewrite; `steps` in manifest
- [ ] Step log lines and `step` in heartbeat lines
- [ ] `Batch`, `Item`, `ItemError.from_exit`; schema, serialization, exit 3, effect check
- [ ] `Teardown`; `release` on resources; `_invoke` finally, timeout, cancel, closed stdout, stream paths
- [ ] `CLEANUP_FAILED` warning; audit rule `resource-release`; migrate `examples/slowctl.py` and docs for the `cleanup=` change
- [ ] `resumable=`, `--resume-from`, `resume_from` in failure data; audit rule `resume-guard`
- [ ] `rollback=`, `--rollback-on-failure`, `rollback_status`; `resumable` and `rollback_available` in `--schema`
- [ ] Conformance profile: probes for `--resume-from` with a bad step name (exit 2)
- [ ] Update COMPLIANCE rows C-008, C-009, C-017, O-010, O-011; README, HANDOFF, ROADMAP (0.1.1 `release`, 0.3.0 step manifest)
