# 08: Auth and scopes

Declarations and gates for commands that need credentials. Treaty supplies the contract;
the app supplies the credential logic (decision D4).

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| C-021 Auth commands declare headless support | 2 | Not started | No declaration |
| O-033 `--headless` and `--token-env-var` | 2 | Not started | No flags |
| C-029 Required scopes | 2 | Partial | No notion of an auth-requiring command; `[]` default never checked |
| O-047 `check-permissions` built-in | 2 | Partial | Scopes declared, but no active-scope check |

## Design

### The app's credential protocol

```python
class Credentials(Protocol):
    def active_scopes(self, ctx: Ctx) -> frozenset[Scope] | None: ...  # None: not logged in

app = App("deployctl", version="1.4.0", credentials=MyCredentials())
```

One method, implemented by the app. Treaty never stores or refreshes tokens.

### Auth-requiring commands (C-029)

- `requires_auth=True` on a command makes `required_scopes=` mandatory (a registration
  error names the command); `required_scopes=()` is accepted only without `requires_auth`
- Before the handler, the framework calls `active_scopes`: `None` exits 8 `AUTH_REQUIRED`;
  missing scopes exit 7 `PERMISSION_DENIED` with `error.context.missing_scopes`; extra
  scopes add an `OVER_PRIVILEGED` warning (O-047). Zero declared scopes on an
  unauthenticated call is never over-privileged
- The breadth rule of C-029 becomes the `broad-scope` audit rule: flags `admin`, `owner`,
  `*`, and `root` scopes unless the command description mentions why

### Login commands (C-021, O-033)

- `auth="browser" | "device" | "token"` on a command marks it a login command; the manifest
  and `--schema` show `headless_supported` (true for `device` and `token`) and
  `token_env_vars` (default `<APP>_TOKEN`, extendable with `token_env_vars=`)
- Browser login commands get `--headless` and `--token-env-var NAME`. Without a TTY,
  `--headless` is implied. In headless mode the framework reads the token from the named
  variable, or the first set variable in `token_env_vars`, and passes it as `ctx.token`
  (a secret, redacted everywhere); if none is set, exit 4 `INPUT_REQUIRED` listing the
  variables
- `device` commands run in non-TTY mode unchanged; they use `ctx.log` for the code and URL

### `check-permissions` built-in (O-047)

Registered only when `credentials=` is set. `check-permissions --for <cmd>` returns
`required_scopes`, `active_scopes`, `over_privileged`; without `--for`, `data.commands`
maps every `requires_auth` command to its coverage. Insufficient scopes exit 8 with
`missing_scopes` in the error context. `safe`, no network I/O unless the protocol does it.

## Tasks

- [x] `Credentials` protocol and `App(credentials=)`
- [x] `requires_auth=`; pre-handler scope gate; `OVER_PRIVILEGED` warning
- [x] `auth=` kinds; `--headless`, `--token-env-var`; `ctx.token`
- [x] `check-permissions` built-in
- [x] Audit rules `broad-scope` and `auth-declared` (a command named `login` or `auth.*`
      without `auth=`)
- [x] Example `examples/authctl.py` with a fake credential store, used by the conformance kit

## Tests

- `requires_auth=True` without `required_scopes` fails registration
- Not logged in: exit 8; missing scope: exit 7 with `missing_scopes`; extra scope: exit 0
  with `OVER_PRIVILEGED`
- Browser login under a pipe without a token env var exits 4 listing `AUTHCTL_TOKEN`; with
  it set, succeeds and never prints the token
- `--token-env-var MY_TOKEN` reads `MY_TOKEN`
- `check-permissions` with and without `--for`

## Deviations as built

- **Two login kinds, not three.** `auth="browser"` (`headless_supported: false`) and
  `auth="device"` (`true`); a `token` kind is dropped because every login command already
  reads a pre-acquired token into `ctx.token`
- **A missing token is `TOKEN_REQUIRED`, exit 4.** The spec's examples disagree (`NO_TTY`
  in C-021, `AUTH_REQUIRED` with exit 8 in O-033; both criteria say exit 4), so the code
  names the fix. `auth_methods` sits on the error beside `context.token_env_vars`. An
  explicit `--token-env-var NAME` whose variable is empty exits 4 on device logins too:
  the caller asked for that token
- **The warning is `CREDENTIAL_OVER_PRIVILEGED`**, the spec's code, with `command`,
  `excess_scopes`, and `required_scopes` in its context. Over-privileged means a strict
  superset: a credential that lacks a scope is never also over-privileged
- **The gate runs where the handler runs.** `active_scopes` is app code, so `_invoke` calls
  it on the worker thread under the command timeout; an exception from it is
  `HANDLER_CRASHED`. `requires_auth=True` without `App(credentials=)` is a registration
  error, and gated commands list 7 and 8 in their manifest entry
- **`check-permissions` details.** Not logged in exits 8 (`AUTH_REQUIRED`); `--for` takes
  `deploy rollback` or `deploy.rollback`, and an unknown command exits 5 (`NOT_FOUND`,
  `UNKNOWN_COMMAND`) because the built-in's arguments cannot see the registry in phase 1.
  A command without `requires_auth` reports its empty `required_scopes` and is never
  over-privileged. `--for` needed a general rule: a field named after a Python keyword
  with a trailing underscore (`for_`) is spelled without it (`--for`, JSON key `for`)
- **`ctx.warn(code, message, **context)` is public.** The built-in needed it, and the
  0.1.0 roadmap already listed it
- **`auth-declared` matches login names, not `auth.*`.** A last path segment of `login`,
  `signin`, `sign-in`, or `authenticate`; `auth.logout` and `auth.status` are not logins.
  `broad-scope` passes when the description names the scope
- **Login commands cannot stream**, and the example is not a kit profile: the kit has no
  auth checks, so `tests/test_auth.py` drives `examples/authctl.py` through `App.run`, like
  `slowctl.py`
