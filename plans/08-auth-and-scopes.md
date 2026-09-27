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

- [ ] `Credentials` protocol and `App(credentials=)`
- [ ] `requires_auth=`; pre-handler scope gate; `OVER_PRIVILEGED` warning
- [ ] `auth=` kinds; `--headless`, `--token-env-var`; `ctx.token`
- [ ] `check-permissions` built-in
- [ ] Audit rules `broad-scope` and `auth-declared` (a command named `login` or `auth.*`
      without `auth=`)
- [ ] Example `examples/authctl.py` with a fake credential store, used by the conformance kit

## Tests

- `requires_auth=True` without `required_scopes` fails registration
- Not logged in: exit 8; missing scope: exit 7 with `missing_scopes`; extra scope: exit 0
  with `OVER_PRIVILEGED`
- Browser login under a pipe without a token env var exits 4 listing `AUTHCTL_TOKEN`; with
  it set, succeeds and never prints the token
- `--token-env-var MY_TOKEN` reads `MY_TOKEN`
- `check-permissions` with and without `--for`
