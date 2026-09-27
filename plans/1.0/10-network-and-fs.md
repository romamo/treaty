# 10: Network and filesystem utilities

Size: M

Gives handlers two small stdlib helpers that make the environment-sensitive parts safe by
default: an HTTP client on `ctx` that honors proxy and CA settings and fills
`error.network_context`, and a directory walker that cannot loop or run away in depth.
Phase B: all additive, and core stays dependency-free (`urllib`, `ssl`, `os.scandir`).

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-036 Proxy env var compliance | P1 | Not started | No framework HTTP client | Additive: `ctx.http` |
| O-019 `--proxy` and `--no-proxy` | P2 | Not started | No flags | Additive, but reserves two flag names on `has_network_io=True` commands |
| F-061 Symlink loop detection | P1 | Not started | No traversal utility | Additive: `ctx.walk()` |
| O-040 `--no-follow-symlinks`, `--max-depth` | P1 | Not started | No flags | Additive: `recursive_traversal=` keyword; reserves two flag names |

## API impact

- **`ctx.http`**: an `Http` object with `request(method, url, *, headers=None, body=None,
  json=None)` returning `HttpResponse(status, headers, body)` with `.text()` and
  `.json()`; `get` and `post` shortcuts. Available only on `has_network_io=True`
  commands, else `RegistrationError` (as `ctx.write_config` does)
- **Flags** `--proxy URL` and `--no-proxy` on every `has_network_io=True` command. A
  command that already defines either name now fails registration through
  `framework_collisions`: the one breaking edge, pre-1.0
- **Env vars read**: `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (and lowercase forms),
  `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, always from `ctx.env`, never `os.environ`
- **`App.command(recursive_traversal=True)`** adds `--no-follow-symlinks` and
  `--max-depth N` (default 50) to the command, its manifest entry, and `--schema`
- **`ctx.walk(root: Path) -> Walk`**: iterable of `WalkEntry(path, depth, is_dir,
  is_symlink)`; `Walk.count` and `Walk.symlinks_skipped` after iteration
- **New exports**: `HttpResponse`, `WalkEntry`. **New error codes**: `CONNECTION_FAILED`,
  `TLS_VERIFY_FAILED`, `SYMLINK_LOOP`, `DEPTH_EXCEEDED`
- **Implicit exit codes** (`implicit_exit_codes` in `_manifest.py`): `UNAVAILABLE` and
  `TIMEOUT` for network commands, `PRECONDITION` for traversal commands. Listed in the
  manifest, so existing declarations stay valid

## Design

### HTTP client (F-036, O-019)

New `_http.py`. `ProxyConfig.resolve(env, flag_proxy, no_proxy_flag)` returns, per URL,
`(proxy_url | None, source)` where source is `--proxy`, `--no-proxy`, or the env var
name. Order: `--no-proxy` (direct), `--proxy`, then `HTTPS_PROXY`/`HTTP_PROXY` by scheme,
with `NO_PROXY` checked through `urllib.request.proxy_bypass_environment`. `--proxy` and
`--no-proxy` together are an `ARG_ERROR`; a `--proxy` value that is not `http://` or
`https://` is rejected in phase 1 (urllib has no SOCKS; the error says so).

`Http` builds one `OpenerDirector` per run: `ProxyHandler` from the resolved map (it
already sends Basic proxy auth from userinfo, including on the CONNECT tunnel) and an
`HTTPSHandler` with `ssl.create_default_context(cafile=...)` from `REQUESTS_CA_BUNDLE`,
then `SSL_CERT_FILE`, else the system default. Every call passes
`timeout=ctx.timeout.seconds`, so the `network-timeout` audit rule accepts it.

`--proxy` and `--no-proxy` also reach children: `ctx.run` sets `HTTP(S)_PROXY`, or
`NO_PROXY=*`, in the child env through the existing `env=` overlay in `_subprocess.py`.

### Failure mapping (F-037 producer, F-063 HTTP half)

`Http.request` catches `urllib.error.URLError`, `ssl.SSLError`, `TimeoutError`, and
`http.client.HTTPException` (named, never `Exception`) and raises `CliExit` with the
`NetworkContext` defined in [03](03-error-contract.md):

| Condition | Exit | `error.code` |
|-----------|------|--------------|
| DNS, refused, reset | 12 `UNAVAILABLE` | `CONNECTION_FAILED` |
| Certificate failure | 12 `UNAVAILABLE`, `retryable: false` (override, as 03-D1) | `TLS_VERIFY_FAILED` |
| Socket timeout | 10 `TIMEOUT` | `TIMEOUT` |
| 401 | 8 `AUTH_REQUIRED` | `UNAUTHENTICATED` |
| 403 | 7 `PERMISSION_DENIED` | `PERMISSION_DENIED` |
| 429 | 11 `RATE_LIMITED` | `RATE_LIMITED`, `retry_after_ms` from `Retry-After` |
| 502, 503, 504 | 12 `UNAVAILABLE` | `UPSTREAM_UNAVAILABLE` |

Other statuses return an `HttpResponse`; the handler decides. 401, 403, and 429 raise
only when the command declares those codes; otherwise the response is returned, so the
mapping never produces `UNDECLARED_EXIT_CODE`. `suggestion` is `curl -v [--proxy P] URL`
with userinfo removed from both, built with `shlex.join`.

### Traversal (F-061, O-040)

New `_walk.py`: an explicit-stack depth-first walk over `os.scandir`, no recursion. Each
frame keeps the `(st_dev, st_ino)` of its directory; the set of ancestors on the current
path detects a cycle when a followed symlink resolves to one of them, raising exit 4
`SYMLINK_LOOP` with `path`, `loop_target`, and `completed_count` in `context` (10-D3).
A directory at depth `max_depth + 1` raises exit 4 `DEPTH_EXCEEDED` with `max_depth` and
`path` in `context` and `hint: "Use --max-depth to adjust the limit"`. With
`--no-follow-symlinks`, symlinks are yielded as entries but never entered, and counted in
`symlinks_skipped`. `ctx.walk` needs
`recursive_traversal=True`, else `RegistrationError`.

Audit rule `recursive-traversal`: a handler calling `os.walk`, `Path.rglob`,
`Path.walk`, `shutil.rmtree`, `shutil.copytree`, or `glob.glob(recursive=True)`, AST-scanned
like `shell_calls`; fix `recursive_traversal=True` and `for entry in ctx.walk(root)`.
Audit rule `http-client`: a network command calling `urllib.request.urlopen`, `requests`,
or `httpx` directly; fix `ctx.http.get(url)`, since only `ctx.http` fills
`network_context`. Both are warnings.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 10-D1 | F-061 says track visited inodes; a global set also flags two links to one directory, which is not a loop | Track ancestors on the current path only; that is exactly a circular symlink |
| 10-D2 | F-061 says `--max-depth` "limits" traversal; O-040 says exceeding it exits 4 | Exit 4 `DEPTH_EXCEEDED`: silent truncation would let a delete or copy report success on part of a tree |
| 10-D3 | F-061 and O-040 place `path`, `loop_target`, `max_depth` at the top of `error`; treaty keeps facts in `context` | Put them in `context`; 03 freezes the top-level field set. O-040's schema text already says "context" |
| 10-D4 | O-019 shows `error.context.network`; F-037 shows `error.network_context` | Follow F-037, the framework requirement; one location only |

## Tasks

- [x] `ProxyConfig` with env, `--proxy`, `--no-proxy` precedence; unit tests per criterion.
  `NO_PROXY` is matched by treaty's own `bypassed()`: `urllib.request.proxy_bypass*` read
  `os.environ` or the macOS system settings, and urllib's `ProxyHandler` does too, so a
  small `_Proxied` handler routes each request (redirects included) instead
- [x] `--proxy`/`--no-proxy` framework flags on network commands; env overlay for `ctx.run`
  (`--no-proxy` sets `NO_PROXY=*`); both together exit 2 from `Invocation.__post_init__`
- [x] `Http`, `HttpResponse`, CA bundle selection, timeout from `ctx.timeout`. Each request
  waits what is left of the deadline, not the whole timeout; an unreadable CA bundle exits
  4 `CA_BUNDLE_INVALID`
- [x] Failure mapping with `NetworkContext`; implicit `UNAVAILABLE` (`TIMEOUT` was already
  implicit); tests against a local `http.server` proxy (CONNECT tunnel included), a TLS
  origin with a checked-in self-signed test certificate, and a refused port. `ctx.http`
  retries through `Retrier.call(on=, give_up=)`, so exhaustion keeps the network error
  and its `network_context` with `retries_exhausted` instead of `Retry.exhausted`'s code
- [x] `_walk.py`: `ctx.walk`, ancestor loop detection, depth limit, `symlinks_skipped`.
  Any entry deeper than `--max-depth` raises, not only a directory; `hint` comes from a
  `TraversalStopped` subclass, as `AuthFailure` carries its own
- [x] `recursive_traversal=`, `--no-follow-symlinks`, `--max-depth`, manifest and `--schema`
- [x] Audit rules `http-client` and `recursive-traversal`; conformance probes for the flags
  (`--proxy socks5://...` and `--max-depth 0`, both `invalid`)
- [x] Update COMPLIANCE.md rows (F-036, F-061, O-019, O-040, and F-037 with 03), README,
  HANDOFF, ROADMAP
