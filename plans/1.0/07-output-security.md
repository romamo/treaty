# 07: Output security

Size: M

Makes `data` safe to put in an agent's context by default: credentials and opaque blobs are
masked, content that came from outside the tool is labeled untrusted, and the two escapes
are explicit argv flags. Phase A because both defaults change what `data` contains for
apps that never asked, which 1.x could not do.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-034 Secret field redaction in logs | P1 | Partial | Two secret-name lists (`_SECRET_NAME_PARTS`, `_SECRET_KEY`); plain-mode error context on stderr is not scrubbed by name; no audit log to redact | No: stderr and logs only; stdout is unchanged by spec |
| F-035 External data trust tagging | P1 | Not started | No way to mark data external; no `_source`/`_trusted` | Yes: `external=` keyword, `Field` marker, tags in `data`, `UNTRUSTED_CONTENT` warning |
| F-058 High-entropy field masking | P1 | Not started | Tokens, JWTs, and blobs reach stdout raw | Yes: default masking of `data` strings; `Field(high_entropy=)` |
| O-023 `--no-injection-protection` | P3 | Not started | No flag | Yes: reserved global flag, `meta.injection_protection`, warning code |
| O-037 `--unmask` | P2 | Not started | No flag | Yes: reserved global flag |

## API impact

- **Breaking default (F-058)**: every string in `data` matching the JWT or base64 rules, and
  every output field whose name matches the secret pattern, is replaced by a summary unless
  `--unmask` is passed. An app that returns a token for the caller to use now needs the
  caller to pass `--unmask`. Changelog entry under "Breaking"
- **New export `treaty.Field`**: the output-field marker, the counterpart of `Flag` and `Arg`
  for output dataclasses: `Field(high_entropy: bool | None = None, external: bool = False)`.
  `high_entropy=None` infers from the name, `False` exempts a field (a content hash an
  agent must compare), `True` always masks
- **New `App.command` keyword `external: bool = False`**: the whole `data` is external.
  Additive; tags appear only on commands that declare
- **`data` gains `_source` and `_trusted`** on external commands; output dataclasses may not
  declare fields with those names (`RegistrationError`)
- **Reserved global flags `--unmask` and `--no-injection-protection`**: added to
  `GLOBAL_FLAGS`, so an app field named `unmask` or `no_injection_protection` fails
  registration. Breaking for such apps, and only possible before the freeze
- **Envelope**: warning codes `HIGH_ENTROPY_MASKED`, `UNTRUSTED_CONTENT`,
  `INJECTION_PROTECTION_DISABLED`; `meta.injection_protection: false` when the flag is set
- **Manifest and `--schema`**: both flags in the manifest's root `flags` map with a security
  warning in the description; `--schema` of every command gains a `security_flags` object
  describing them (not a `ManifestResponse` key, like `requires_confirmation`); an output
  schema marks masked fields with `"x-high-entropy": true`, and an external command's lists
  `_source` and `_trusted`
- **No environment variables**: neither escape can be set from the environment, a config
  file, an exec `_opts` object, or an MCP argument (O-037 forbids the env; the same rule
  keeps O-023 an explicit act)

## Design

### One secret-name rule (F-034)

- New `_redact.py` owns `SECRET_NAME` (the `_SECRET_KEY` regex from `_app.py`, a superset of
  F-034's six substrings), `REDACTED`, and `scrub(key, value, redact)`, moved from `_app.py`.
  `_flags.py` infers `Flag(secret=None)` from the same regex instead of `_SECRET_NAME_PARTS`,
  so input and output agree on what a secret name is
- `_Run._emit_text` prints `error.context` on stderr in plain mode; it now goes through
  `scrub`, so a context key such as `password` prints `[REDACTED]`
- stdout is not modified: the spec says F-034 is log-layer only. Output secrets are F-058's
  job, which masks rather than redacts
- The audit-log criteria (`--api-token` and response fields in `audit.jsonl`) need F-026,
  which has no log yet. `scrub` is the single entry point that log writer must call; until
  then those two criteria are not applicable. A secret-named flag never carries its value on
  argv anyway (C-016 forces `--api-token-from-env`)

### Masking (F-058, O-037)

New `_protect.py`, dependency-free:

```python
@dataclass(frozen=True, slots=True)
class Masked:
    data: object
    paths: tuple[str, ...]  # "items[3].token", for the warning
```

`mask(data, declared: Mapping[str, bool]) -> Masked` walks `data` and replaces each match:

- JWT: three base64url segments whose first decodes to a JSON object with `alg`; summary
  `[JWT: sub=<sub>, exp=<ISO 8601 UTC>]`, dropping a missing claim. A version string such as
  `1.4.0` or a host name never decodes, so it is left alone
- Base64: the whole string matches `[A-Za-z0-9+/]{40,}={0,2}`, decodes, is not all hex (git
  SHAs and digests stay readable), and has at least 4.5 bits of entropy per character
  (paths and prose fall below); summary `[BASE64: <decoded bytes> bytes]`
- Key: a field declared `high_entropy=True` or inferred from `SECRET_NAME`, whatever its
  shape; summary `[KEY: <first 8>...]`
- `declared` comes from the output type at registration (`Command.masked_fields`), keyed by
  dotted field path; dict keys at run time are matched against `SECRET_NAME` too
- Applied at emission, not in `_execute`: `_Run._present(envelope)` runs in `_write`,
  `_emit_text`, `to_file`, and `App.call`, before `cap_envelope`. The idempotency store keeps
  the raw result, so a replay with `--unmask` still returns it
- When anything was masked: warning `HIGH_ENTROPY_MASKED` with `context.paths` and
  `suggestion: "rerun with --unmask to get the raw values"`
- `--unmask` is a switch in `split_globals`, never read from `env`; in `exec` it applies to
  the whole plan. `App.call(..., unmask=False)` is the in-process equivalent; the MCP
  adapter does not expose it

### Trust tagging (F-035, O-023)

- `external=True` on `App.command`, or `Field(external=True)` on any output field: when the
  command returns and the external content is present (a non-null marked field, or any data
  for `external=True`), `_present` adds `"_source": "external", "_trusted": false` at the top
  of `data`, and warning `UNTRUSTED_CONTENT`
- List data: each object item is tagged. Registration refuses `external=True` on a command
  whose items are not objects (`list[str]` has nowhere to put the tags). Stream events and
  exec lines are tagged per envelope
- `--no-injection-protection` (switch in `split_globals`): no tags and no `UNTRUSTED_CONTENT`;
  adds `meta.injection_protection: false`, warning `INJECTION_PROTECTION_DISABLED`, and a
  line on stderr, which is the audit record until F-026 writes one. `--help` lists it under
  global options as "Security: returns external content without untrusted markers"
- O-023's "explicit acknowledgment in non-interactive mode": treaty never prompts, and the
  flag can only come from argv, so passing it is the acknowledgment
- Audit rule `external-data` (warning): a command with `has_network_io=True`, or whose
  handler calls `open`, `Path.read_text`, `Path.read_bytes`, or `ctx.run`, and declares no
  `external=` or `Field(external=True)`. Fix: `external=True` on the command
- Audit rule `high-entropy` (advice): lists fields masked by name inference, with the fix
  `Field(high_entropy=False)` for ones that are not secrets (`primary_key`, `keyboard`)

### Order of output transforms

`_present` is shared with workstream 12: mask, then tag, then `--fields` projection, then
token budget, then the byte cap. Masking first means neither the projection nor a budget
ever sees a raw secret, and tags survive `--fields` because framework keys are kept.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 07-D1 | Secret-named output fields: mask by name inference, or only when declared? F-058's example masks a short `token` no pattern would catch | Infer from `SECRET_NAME`, exempt with `Field(high_entropy=False)`; the `high-entropy` advice lists every inferred field |
| 07-D2 | The spec's base64 and JWT patterns match SHAs, paths, and dotted names | Keep the patterns and add the decode, hex, and entropy guards above; record as a deviation in COMPLIANCE |
| 07-D3 | O-023's wire format wraps a field as `{"_value": ..., "_trusted": false}`; F-035's tags the top of `data` | Follow F-035 for both: top-level tags keep output schemas unchanged; a field marker only decides when tags appear |
| 07-D4 | Marker name `Field` next to `Flag` and `Arg` | Accept; decide in the 15 API review together with `id_field=` from 12 |

## Tasks

- [ ] `_redact.py`: one `SECRET_NAME`; `scrub` moved; `Flag(secret=None)` uses it; plain-mode error context scrubbed
- [ ] Reserve `unmask` and `no-injection-protection` in `GLOBAL_FLAGS`; parse both in `split_globals`; root manifest entries and help rows
- [ ] `treaty.Field` marker; `Command.masked_fields` and `Command.external_fields` from the output type; refuse `_source`/`_trusted` field names
- [ ] `_protect.mask` with JWT, base64, and key summaries and their guards; unit tests per shape
- [ ] `_Run._present` at every sink (`_write`, `_emit_text`, `to_file`, `App.call`); `HIGH_ENTROPY_MASKED`; `--unmask`
- [ ] `external=` keyword; tagging for objects, list items, streams, exec lines; `UNTRUSTED_CONTENT`; output schema tags
- [ ] `--no-injection-protection`: meta field, warning, stderr record
- [ ] Audit rules `external-data` and `high-entropy` with generated fixes
- [ ] Tests named after each acceptance criterion, including env var cannot unmask and replay with `--unmask`
- [ ] COMPLIANCE rows F-034 (stays Partial until F-026), F-035, F-058, O-023, O-037; README, HANDOFF, ROADMAP, changelog "Breaking"
