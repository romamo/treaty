# 03: Error contract

Size: L

Freezes the shape of the `error` object: every field an agent branches on gets its final
name, type, and producer before 1.0, so no recovery loop written against 1.0 breaks in 1.x.
Covers retry timing, the create-or-get pattern, executable fixes, credential failures,
network failures, locks, and renamed commands (ROADMAP 0.2.0 `REDIRECTED`).

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| C-014 retryable and retry_after_ms | P1 | Partial | `RATE_LIMITED` allowed without `retry_after_ms`; no `retry_strategy` | Yes: RATE_LIMITED without a delay becomes `INVALID_EXIT`; new `retry_strategy` field |
| C-028 ALREADY_EXISTS pattern | P1 | Partial | Works by convention only; no helper, check, or `conflict_id` | Yes: new `treaty.already_exists`; `conflict_id` field |
| C-030 Executable fix_command | P1 | Partial | `fix_command` emitted but never validated | Yes: new `fix_commands=` keyword; bad values fail registration or the run |
| F-063 Credential expiry error | P1 | Partial | No `CREDENTIALS_EXPIRED`; missing login uses code `AUTH_REQUIRED`; no `required_permission` | Yes: error codes renamed, `Credentials` return type widened |
| F-037 Network error context | P1 | Not started | No `error.network_context` | Yes: new field and `NetworkContext` shape (filled by 10) |
| F-033 Locks with retry_after_ms | P2 | Not started | No lock API | Additive: `ctx.lock()` and `LOCK_HELD` |
| C-007 Auto idempotency key | P1 | Partial | Key only when the caller passes one | Additive: `<APP>_SESSION` env var and `meta.idempotency_key` |
| ROADMAP REDIRECTED exit 13 | P0 (F-001) | Partial | Code 13 registered, never emitted; no `aliases` | Additive: `App.redirect()`, `error.redirect`, manifest `aliases` |

## API impact

- **`ErrorDetail` gains fields**, all optional and omitted when `None`: `retry_strategy`,
  `conflict_id`, `refresh_command`, `expires_at`, `required_permission`, `network_context`,
  `redirect`. Additive for readers; the set is frozen at 1.0, and later facts go in `context`
- **New exports**: `RetryStrategy` (StrEnum), `Redirect`, `NetworkContext`, `Expired`,
  `already_exists`
- **`CliExit` / `Exit.*` keywords**: adds `retry_strategy=`, `conflict_id=`. Breaking:
  `Exit.RATE_LIMITED(...)` without `retry_after_ms` now ends the run as `INVALID_EXIT`,
  and a `fix_command=` that fails validation does the same
- **`App.command` keyword `fix_commands: Mapping[str, str]`**: error code to invocation,
  validated before the first run. Additive
- **`App.exit_code` keywords `retry_after_ms=`, `retry_strategy=`**: defaults for the code,
  kept off the manifest entry like `suggestion` (the schema is closed). Additive
- **Breaking error codes**: `AUTH_REQUIRED` for a missing login becomes `UNAUTHENTICATED`;
  `INSUFFICIENT_SCOPES` at run time becomes `PERMISSION_DENIED` (F-063 names both)
- **`Credentials.active_scopes`** may return `treaty.Expired(at=...)`; existing
  implementations keep working. `App.command(refreshes_auth=True)` marks the refresh command
- **`App.redirect(old, *, to, reason="renamed")`**: exit 13 path; the old name appears in
  the target's manifest `aliases`. Additive
- **`ctx.lock(name, *, wait=None, retry_after_ms=1000)`** and error code `LOCK_HELD`. Additive
- **`<APP>_SESSION`** env var turns on auto-generated idempotency keys. Additive

## Design

### Retry timing (C-014)

`RetryStrategy` is `immediate | linear_backoff | exponential_backoff`. `ExitCodeEntry`
gets `retry_after_ms: int | None` and `retry_strategy: RetryStrategy | None`, both
rejected on a non-retryable entry in `__post_init__`. Framework defaults: `RATE_LIMITED`
exponential with no default delay, `UNAVAILABLE` exponential 1000 ms, `TIMEOUT` none.
In `_app.py` where `CliExit` becomes `ErrorDetail` (the `retry_after = exc.retry_after_ms`
block), the raise wins over the entry default; a `RATE_LIMITED` that ends with no delay or
`0` is `INVALID_EXIT` naming `retry_after_ms=`. `IDEMPOTENCY_KEY_BUSY` gets
`retry_after_ms` and `immediate`. Audit rule `retry-hint`: a `RATE_LIMITED` raise in a
handler without `retry_after_ms=` (AST scan like `untimed_network_calls`), fix
`retry_after_ms=<Retry-After header in ms>`.

### ALREADY_EXISTS (C-028)

`already_exists(existing: T, *, conflict_id: str, message: str | None = None) -> CliExit`
in `_errors.py`: `CONFLICT`, code `ALREADY_EXISTS`, `data=existing`, `conflict_id`. The
existing `data` path of `CliExit` already type-checks the payload against the handler's
return type, so `data` is what `get` returns. Audit rule `already-exists`: a command whose
output `effect` admits `created` (read from the output schema in `_effect.py`) and does
not declare `CONFLICT`; fix `exit_codes=("CONFLICT",)` plus `raise
treaty.already_exists(...)`. Delete of a missing resource returns `effect: "noop"` with
`status: "not_found"`; documented in the tutorial and checked by a second rule: a
destructive command declaring `NOT_FOUND` gets a warning with that fix.

### Validated fix_command (C-030)

One validator, `check_fix(app, command_text) -> str | None` (the problem, or `None`):

- Rejects `<`, `>`, `$`, backticks, `|`, `;`, `&&`, and unbalanced quotes (`shlex.split`)
- The first word must be the app name or a name in `App(companions=("git", ...))`
- For the app's own commands, `resolve_path` must find a command, and it must not be
  `DangerLevel.DESTRUCTIVE`

Declared `fix_commands=` run through it when the command table is first used
(`App.manifest()`, `App.run()`), since the target may register later; a problem raises
`RegistrationError`. The envelope fills `fix_command` from the declaration when the raise
gives none. A runtime `fix_command=` on a raise goes through the same check and becomes
`INVALID_EXIT` on failure. Criteria "runs twice harmlessly" and "reissue then succeeds"
are the author's; the conformance profile probes declared fixes of non-mutating targets.

### Credential failures (F-063)

- `_auth.not_logged_in` emits code `UNAUTHENTICATED`, `hint` = first login command
- `active_scopes` returning `Expired(at: datetime)` gives `CREDENTIALS_EXPIRED`, exit 8,
  `expires_at` (ISO-8601 UTC), and `refresh_command` plus `fix_command` from the command
  registered with `refreshes_auth=True`; with none, `fix_required` prose only
- `insufficient` at run time emits code `PERMISSION_DENIED`, exit 7, and
  `required_permission` = the first missing scope; the full list stays in `context`
- HTTP 401/403 mapping belongs to the client in [10](10-network-and-fs.md)

### Network context (F-037)

`NetworkContext(url, status_code, proxy_used, proxy_source, no_proxy, ssl_verify,
suggestion)` in `_envelope.py`; `to_json` always writes `proxy_used` (null when direct).
`proxy_used` has its userinfo redacted. Only the framework sets it: `CliExit` has no
keyword for it, so non-network errors can never carry it. Producer: `ctx.http` in 10.

### Locks (F-033)

`ctx.lock(name)` is a context manager in a new `_locks.py` on top of `_atomic.try_lock`:
a lock file in `state_dir(...)/locks/<name>.lock` holding the holder's pid and start time,
polled until `wait` (default: what is left of the command timeout). The kernel drops
`flock` on any exit including SIGTERM; the file is unlinked on release and by the
`_signals.py` handlers. On timeout: exit 4, `LOCK_HELD`, `retry_after_ms`, context
`holder_pid`, `holder_age_ms`, `lock_file`. `PRECONDITION` joins the implicit codes of a
command that calls `ctx.lock` only if declared; audit rule `lock-declared` checks it.

### Auto idempotency key (C-007)

With `<APP>_SESSION` set and no `--idempotency-key`, `_keyed` derives the key as
`sha256(session + fingerprint(path, args))`, which `fingerprint` in `_idempotency.py`
already computes, and reports it in `meta.idempotency_key`. Without the variable nothing
changes, and the manifest flag description says repeats are not deduplicated.

### Redirects (ROADMAP 0.2.0)

`App.redirect("deploy.undo", to="deploy.rollback", reason="renamed", permanent=True)`
stores a `Redirect` table checked in the unknown-command branch of `resolve_path` in
`App.run` and in the `exec` dispatcher. The reply is exit 13 with `error.redirect.command`
= `shlex.join([app, *new_parts, *remaining_tokens])`, verbatim-executable. Registering a
redirect onto a missing target, or from a live path, is a `RegistrationError`.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 03-D1 | F-033 wants `LOCK_HELD` retryable on exit 4, whose entry is non-retryable | Emit `retryable: true` for `LOCK_HELD` only; the entry describes the code's usual case, and nothing ran |
| 03-D2 | C-030 limits fixes to the app or declared companions; the tutorial uses `mkdir -p` | Require `App(companions=...)`; update the tutorial to declare `mkdir` |
| 03-D3 | Auto idempotency keys can turn a deliberate second create into a noop | Opt in per session (`<APP>_SESSION`), never by default |
| 03-D4 | Renamed error codes (`UNAUTHENTICATED`, `PERMISSION_DENIED`) break branches on the old ones | Rename now; changelog under Breaking. After 1.0 they are frozen |
| 03-D5 | Should a redirect source run as an alias instead of exiting 13 | No: exit 13 keeps one canonical name; list sources in `aliases` |

## Tasks

- [ ] `RetryStrategy`, entry defaults, `RATE_LIMITED` check, `retry-hint` audit rule
- [ ] `ErrorDetail` new fields and `to_json`; envelope schema tests
- [ ] `already_exists` helper, `conflict_id`, `already-exists` and not-found audit rules
- [ ] `check_fix`, `fix_commands=`, `companions=`; runtime check on raises; tutorial update
- [ ] `UNAUTHENTICATED`, `Expired`, `refreshes_auth=`, `required_permission`
- [ ] `NetworkContext` type (producer lands in 10)
- [ ] `ctx.lock`, `LOCK_HELD`, release on exit and SIGTERM, `lock-declared` rule
- [ ] `<APP>_SESSION` auto key and `meta.idempotency_key`
- [ ] `App.redirect`, exit 13 on argv and `exec`, manifest `aliases`, conformance probe
- [ ] Update COMPLIANCE.md rows (C-007, C-014, C-028, C-030, F-033, F-037, F-063), README, HANDOFF, ROADMAP
