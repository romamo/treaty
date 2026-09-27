# Implementation plan: Level 1 and Level 2

This plan closes the 39 open mandatory requirements listed in
[`COMPLIANCE.md`](../COMPLIANCE.md), taking treaty from 62% of Level 1 and 47% of Level 2 to
a verifiable Level 2 claim. Level 3 (P1 to P3) is out of scope; each plan file names the
P1 requirements that come nearly free with its work.

Every workstream follows the house rule from `ROADMAP.md`: each new declaration gets a
matching audit rule, so adoption never requires reading the spec.

## Workstreams

| File | Workstream | Requirements | Level | Size |
|------|------------|--------------|-------|------|
| [01](01-output-hygiene.md) | Output hygiene | F-006, F-007, F-008, F-010, C-013, F-005, F-051 | 1, 2 | M |
| [02](02-validation-phase.md) | Validation phase | F-002, F-015, F-044 (newline rule) | 1, 2 | S |
| [03](03-interactivity.md) | Interactivity | F-009, F-047, C-005, F-055 | 1, 2 | M |
| [04](04-subprocess-api.md) | Subprocess API | F-044, F-046, F-062, F-065, F-055, F-057, F-008, F-010 | 2 | L |
| [05](05-declarations.md) | Required declarations | C-001, C-002, C-004, C-012, O-021, O-048 | 1, 2 | M |
| [06](06-pagination.md) | Pagination | F-018, F-019, O-003, F-052 | 2 | M |
| [07](07-io-and-streams.md) | I/O limits and streams | F-011, F-014, F-053, F-054, O-001 | 2 | M |
| [08](08-auth-and-scopes.md) | Auth and scopes | C-021, C-029, O-033, O-047 | 2 | M |
| [09](09-jobs-and-config.md) | Async jobs and config writes | C-022, C-025 | 2 | M |

Coverage check: all 39 open Level 1 and Level 2 requirements appear in the table above.
Some appear twice where two workstreams each own part of the criteria (F-008 and F-010
set the environment in 01 and pass it to children in 04; F-044 rejects newlines in 02 and
forbids shells in 04; F-055 refuses editors in 03 and neutralizes them for children in 04).

## Order

```
M1  Level 1   01 ─┬─ 02 ─┬─ 03 (F-009 only) ─┬─ 05 (C-004 only)
                  │      │                   │
M2  Level 2       └─ 04 ─┴─ 03 (rest) ── 05 (rest) ── 06 ── 07 ── 08 ── 09
```

- **M1, Level 1** (join the 0.1.0 release): 01, 02, the F-009 part of 03, and the C-004
  part of 05. These are small and close the Level 1 claim
- **M2, Level 2** (the 0.2.0 release in `ROADMAP.md`): 04 first, because 03, 07, and 08
  use its environment and child-process rules; then the rest in table order. 06 and 07
  are independent of each other and can run in parallel with 08 and 09

## Decisions

All four recommendations were accepted on 2026-09-27; each plan file treats them as settled.

| ID | Question | Decision | Applies to |
|----|----------|----------|------------|
| D1 | A closed stdout exits 141 (`OUTPUT_CLOSED`); F-014 requires `0` | Follow the spec: exit `0` when stdout closes after at least one complete envelope was written, keep 141 when it closes before any was written, and record the choice in HANDOFF | 07 |
| D2 | A handler-raised `ParseError` exits 2 after user code ran, which F-002 forbids | Keep exit 2 only for `ParseError` raised in args `__post_init__` and scalar parsers (phase 1); from a handler body or a resource's `acquire`, exit 1 with `VALIDATION_AFTER_START` and `phase: execution` | 02 |
| D3 | C-001 and C-002 require `exit_codes=` and `danger_level=` on every command; this breaks every existing app | Make both keyword arguments required with no default; `exit_codes=()` stays valid as an explicit empty declaration. Pre-1.0, so no deprecation window; update examples, scaffold, and the treaty CLI in the same change | 05 |
| D4 | Auth, async jobs, and config writes (C-021, C-022, C-025, O-033, O-047) are domain features treaty has no consumer for | Build declarations, manifest fields, gates, and one built-in each, not an auth or job system; the app supplies the domain logic through small protocols | 08, 09 |

## Definition of done, per workstream

- Every acceptance criterion of every listed requirement has a test named after it
- `uv run pytest`, `uv run mypy src`, and `uv run ruff check src tests examples` are clean
- The conformance kit passes for `examples/deployctl.py` and a fresh `treaty init` project
- New declarations have an audit rule with a generated fix, listed by `treaty rules`
- `README.md`, `HANDOFF.md`, and `ROADMAP.md` are updated; the row in `COMPLIANCE.md` moves
  to Done (regenerate it with `tmp/gen_compliance.py` or update it by hand)
