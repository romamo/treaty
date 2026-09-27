# 05: Output data contract

Size: L

Fixes how a handler's result becomes `data`: bytes, array order, paths, missing values,
field limits, and line endings. Consumers cache, diff, and decode `data` byte for byte, so
any of these changed after 1.0 would break them; this workstream makes the choices once.

| Requirement | Priority | Now | Gap | API change |
|-------------|----------|-----|-----|------------|
| F-017 Binary as base64 | P1 | Done | `bytes` output is `INVALID_OUTPUT` | Yes: `bytes` and `treaty.Binary` serialize to a wrapper object |
| F-020 Stable array sorting | P2 | Done | Object keys sorted; arrays are not | Yes: arrays sorted by default; `sort_key=`, `ordered=`, `treaty.Out` |
| F-040 Absolute output paths | P2 | Done | `Path` output is written as given | Yes: every `Path` in `data` becomes absolute |
| F-064 Truncation and `max_bytes` | P1 | Done | No `max_bytes`; no way to report a backend cut | Yes: `Flag(max_bytes=)`, `FIELD_TOO_LARGE`, `ctx.truncated()` |
| F-072 LF line endings | P1 | Done | Windows stdout and stderr translate `\n` | No: behavior on Windows only |
| F-074 Null/absent/empty | P1 | Done | Nullable collections allowed; output schema marks defaulted fields optional | Yes: output schemas list every key as required; `list[T] \| None` refused |
| O-007 `--stable-output` | P3 | Done | No flag | Yes: reserved global flag, `Out(volatile=True)` |

## API impact

- **Output order changes by default** (breaking): string and number arrays are sorted,
  object arrays by a declared key or canonical JSON. `ordered=True` on `App.command` and
  `Out(ordered=True)` on a field keep the handler's order (05-D1)
- **`Path` values in `data` are absolute** (breaking): resolved against `meta.cwd` (01)
- **`output_schema` changes** in the manifest and `--schema`: every dataclass key is in
  `required`, since every key is always emitted. The manifest etag changes once
- **Registration errors** (breaking): `list[T] | None`, `tuple[...] | None`, and
  `dict[str, T] | None` in output types; `sort_key=` naming a missing or non-scalar field
- **New exports**: `Binary` (frozen: `data: bytes`, `content_type: str | None`) and
  `Out(*, default=, default_factory=, sort_key=None, ordered=False, volatile=False)`, the
  output counterpart of `Flag` (05-D2)
- **`App.command` keywords**: `sort_key: str | None = None`, `ordered: bool = False`
- **`Flag(max_bytes=N)`** and error code `FIELD_TOO_LARGE` (exit 2, validation phase)
- **`ctx.truncated(value, *, field, original_length=None) -> str`**
- **Global flag `--stable-output`** (JSON key `stable_output`); a field named so fails
  registration through `framework_collisions`

## Design

### Binary values (F-017)

`to_jsonable` in `_schema.py` turns `bytes` and `Binary` into
`{"type": "binary", "encoding": "base64", "value": ..., "size_bytes": N}`, plus
`content_type` from `Binary`. `schema_for` gives both the matching object schema, so a
field is declared binary by its annotation. The wrapper runs before `clean`, whose F-016
pass leaves base64 text alone. In `_cap.py`, `_cuttable` treats a wrapper as atomic: a
cut `value` would not decode, so the cap drops it only as a whole list element. The plain
renderer in `_plain.py` prints `<binary 1024 bytes image/png>`. Audit rule
`binary-output`: a handler calling `base64.b64encode`, fix: return `bytes` or `Binary`.

### Stable arrays (F-020)

A type-directed pass `stable_order(value, annotation)` runs in `_Run._payload` after
`to_jsonable`, walking the output annotation next to the JSON value:

- Strings sort by code point, numbers ascending, booleans false first
- Object arrays sort by `sort_key` (command-level for a top-level list, `Out(sort_key=)`
  for a field); without one, by `canonical_json` of each element, which is deterministic
- Fixed tuples (`tuple[A, B]`) keep their order; `tuple[T, ...]` sorts like a list,
  since treaty uses it as the frozen list. Untyped values (`object`, `dict[str, object]`)
  sort by value kind; mixed arrays keep handler order
- `ordered=True` or `Out(ordered=True)`: the handler's order is the contract, and the
  field's schema says so with `"x-ordered": true`

For a list command returning a plain list, `_execute` sorts before `take` in `_page.py`
slices, so pages follow one global order; a `Page` is sorted within itself, and its
`sort_key` must match the cursor order (documented). `sort_key` is checked at
registration: a field of the item dataclass typed `str`, `int`, `Enum`, or a date. Audit
rule `stable-order`: an object-array output with neither `sort_key` nor `ordered`, fix
`sort_key="<id-like field>"`.

### Absolute paths (F-040)

`to_jsonable` takes `base: Path`, the run's `meta.cwd` from 01; a relative `Path` becomes
`base / value` (lexical, no symlink resolution, so the text stays what the handler
meant). Plain `str` stays as given. The `path-typed` audit rule extends from args to
output dataclasses: a `str` field named `*_path`, `*_dir`, or `*_file`, fix `Path`. When
O-017 adds `--cwd`, `base` becomes the effective directory.

### Field limits and reported cuts (F-064)

`Flag(max_bytes=255)` is checked in phase 1 on every input route, on UTF-8 byte length,
per item for arrays. Too large: exit 2, `FIELD_TOO_LARGE`, context `field`, `max_bytes`,
`actual_bytes` (never the value), joined into `error.errors` with other field errors.
`raw_payload_schema` carries `"x-max-bytes"`, and `--schema` a `max_bytes` map by flag;
`FlagEntry` has `additionalProperties: false`, so the manifest does not.

For a value a backend already cut, `ctx.truncated(text, field="body",
original_length=4200)` returns `text` plus `MARKER` from `_cap.py`, adds a
`FIELD_TRUNCATED` warning (`field` as `data.body`, `truncated_length`,
`original_length`), and sets `meta.truncated: true`, as the byte cap does. Audit rule
`field-limits`: `len(args.x)` compared with a constant, fix `Flag(max_bytes=N)`; a
returned string sliced `[:N]`, fix `ctx.truncated`.

### LF line endings (F-072)

`App.main` opens the envelope stream with `newline="\n"` and calls
`sys.stderr.reconfigure(newline="\n")`, so Windows writes no `\r`. JSON, help, errors,
and `ctx.log` all pass through these two streams; `--output` files are already written
byte-exact by `_atomic.py`. `App.run` callers own their streams. A Windows CI test runs
the example through a subprocess and asserts no `\r` byte in stdout or stderr for a
result, `--help`, and an error.

### Null, absent, empty (F-074)

- Dataclass outputs always emit every key; `_dataclass_fields_schema` gets an output
  mode that lists every field in `required`. `X | None` is nullable (present, maybe null)
- A nullable collection in an output type is a `RegistrationError` with the fix
  `list[T] = field(default_factory=list)`, so empty is always `[]` (05-D3)
- "Optional, absent when unsupported" is a different `schema_version` (01), never a key
  that comes and goes between runs
- `""` versus `null` cannot be told apart by type: documented, not checked. `dict`
  outputs are free-form and already flagged by the `typed-output` audit rule

### `--stable-output` (O-007)

A global flag in `_FIXED_GLOBAL_FLAGS`, parsed by `split_globals`, and a JSON key on the
exec and MCP routes. It omits `meta.request_id`, `timestamp`, and `retries`, writes
`duration_ms: 0` (05-D4), drops `Out(volatile=True)` fields from `data` (such fields are
left out of `required`), and turns off heartbeat lines, which depend on timing. Sorting
needs no switch; it is always on.

## Decisions

| ID | Question | Recommendation |
|----|----------|----------------|
| 05-D1 | Sort arrays by default, reordering existing output, or only on declaration | By default, as F-020 requires; `ordered=True` is the escape for ranked results, and the `stable-order` rule points to it |
| 05-D2 | Name of the output-field declaration | `treaty.Out`, beside `Flag` and `Arg`; `Field` would clash with pydantic and dataclasses in user code |
| 05-D3 | Nullable collection in an output type: registration error or audit warning | Registration error: F-074 forbids `null` for collections, and the fix is one line |
| 05-D4 | `duration_ms` is required in `ResponseMeta` but breaks byte identity | Write `0` under `--stable-output`; byte identity is the flag's purpose |
| 05-D5 | Make every output `Path` absolute or add an opt-in path type | Every `Path`: it is already treaty's path type on input, and a second type would split the convention |

## Tasks

- [x] `Out`; output-mode dataclass schemas with every key required; nullable collections refused
- [x] `bytes` and `Binary` wrapper, schema, atomic in the cap, plain rendering, `binary-output` rule
- [x] `stable_order`, `sort_key=`, `ordered=`, sort before `take`, `stable-order` rule.
  Landed as `_out.arrange`. `ordered=True` also covers arrays inside untyped content
  (the manifest command uses it, since positional order is meaning), and is allowed on
  untyped outputs as well as arrays. `sort_key` is checked against dataclass items only;
  an array of `dict` items uses `ordered=True` or canonical-JSON order
- [x] Absolute `Path` output against `meta.cwd`; `path-typed` covers output types
- [x] `Flag(max_bytes=)`, `FIELD_TOO_LARGE`, `ctx.truncated`, `field-limits` rule.
  `--schema` shows the limit as `x-max-bytes` in `raw_payload_schema` and the manifest
  description says "(at most N bytes)"; no separate `max_bytes` map. `max_bytes` is refused
  on secrets and paths. `field-limits` flags `len(args.x)` compared with a number, and a
  slice `obj.attr[:N]`. The byte cap still names fields `$.x` while `ctx.truncated` uses
  `data.x` as the spec shows; unifying them is left for 07, which owns `_present`
- [x] LF streams in `App.main`; Windows CI byte test (a subprocess test that runs on every
  platform)
- [x] `--stable-output` with `Out(volatile=True)`; exec and MCP keys. No conformance
  probe: the kit lives in the spec repo; the byte-identity test runs the fixture app twice
- [x] Migrate `examples/`, scaffold, and the treaty CLI; regenerate their schemas (no
  committed schemas to regenerate; the scaffold needed no change)
- [x] Update COMPLIANCE.md rows (and the F-016 note on `bytes`), README, HANDOFF, ROADMAP.
  No `CHANGELOG.md` exists yet (15 creates it); the breaking changes are listed in ROADMAP
