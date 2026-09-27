# 02: Validation phase

Guarantees that exit `2` means nothing ran, so an agent can fix input and reissue safely.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-002 Exit 2 reserved for validation | 1 | Partial | A handler-raised `ParseError` exits 2 after user code ran |
| F-015 Validate before execute | 2 | Partial | Same cause; no declared hook for cross-field validation |
| F-044 Shell argument escaping (newline rule) | 2 | Not started | A newline in an argument is accepted; the subprocess half is in 04 |

Follows decision D2 in [README](README.md).

## Current behavior

- Phase 1 is `_parse.py`: flags, scalars, `Path` checks, secrets, then
  `command.args_type(**values)` at `_parse.py:498`, so an args `__post_init__` raising
  `ParseError` is already phase 1
- `_Run._execute` catches `ParseError` from the handler and returns `arg_error(...)`,
  exit 2 with `phase: validation` (added in 0.0.x as "handler validating its own input")
- Resources are acquired after phase 1 and before the handler; a `ParseError` from
  `acquire` takes the same path
- `CONFIRMATION_REQUIRED` exits 2 after running the handler with `dry_run=True`. This is
  compatible with F-002 because C-004 obliges the dry-run path to write nothing, and
  `effect_problem` already rejects a non-`would_*` effect on a preview

## Design

### Handler-raised `ParseError` (D2)

- `ParseError` from a handler body or `acquire` becomes exit `1`, code
  `VALIDATION_AFTER_START`, `phase: execution`, `retryable: false`, with the original
  message, context, and suggestion preserved, and `fix_required` pointing the author to
  `__post_init__` or a scalar parser
- `GENERAL_ERROR`'s manifest entry already covers exit 1, so no new exit code
- Migration note in README: move cross-field checks into the args dataclass's
  `__post_init__`, which runs in phase 1 and reports through `error.errors`

### Cross-field validation in phase 1 (F-015)

`__post_init__` runs only after every field parsed, so its errors come after the field
errors in a separate run. Collect them in the same run instead: when `errors.finish()`
passes, call `args_type(**values)` inside the same `ErrorBag`, so a `ParseError` or a
`ParseErrors` raised by `__post_init__` joins `error.errors`. Add `raise ParseErrors([...])`
support so one `__post_init__` can report several problems.

The "execute before validate" registration error in F-015 cannot occur in treaty, because
the framework owns the order; state that in the HANDOFF decisions list.

### Newlines in arguments (F-044, phase-1 half)

Reject `\n`, `\r`, and NUL in every `str`-typed argument in phase 1, on argv, `exec`,
`--raw-payload`, and MCP routes, with `rejected_pattern` in the context, like `check_path`.
Opt out per field with `Flag(multiline=True)` for message bodies; the manifest exposes
`multiline: true` so an agent knows it may send newlines there. The `multiline-flag` audit
rule suggests the opt-out for fields named `message`, `body`, `description`, `text`.

## Tasks

- [ ] `VALIDATION_AFTER_START` in `_execute` and in the resource acquisition path
- [ ] `__post_init__` errors collected with field errors; `ParseErrors`
- [ ] Newline and NUL rejection for `str` fields; `Flag(multiline=True)`; manifest field
- [ ] Audit rule `multiline-flag`
- [ ] Update the ROADMAP line "Handler-raised `ParseError` becomes a validation-phase exit 2"

## Tests

- A handler that writes a file and then raises `ParseError`: exit 1, `VALIDATION_AFTER_START`,
  `phase: execution`
- An args `__post_init__` raising `ParseError` exits 2 with `phase: validation` and nothing
  from the handler runs (a handler with a side-effect sentinel)
- One bad flag plus a failing `__post_init__` report both entries in one run
- `--note $'a\nb'` exits 2 on every route; `Flag(multiline=True)` accepts it
- Property test over every framework path that returns exit 2: the handler sentinel is untouched
  (the preview path is the one exception and asserts dry-run instead)
