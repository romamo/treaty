# Framework comparison benchmark

The same `democli` built three times, with argparse, click, and treaty, run through the
CLI Agent Spec benchmark harness (five scenarios, an agent loop with one `run_cli` tool).
The spec's hand-written `bad` and `good` mocks are included as floor and ceiling.

All three builds import `cli/_fixture.py`, so the business logic is identical: 20
deployments five per page, a deploy lock that is held on the first call and free on the
second, a health check with an expired registry credential, and a staging cleanup of three
deployments. The argparse and click builds are written the way a competent developer writes
them today: text output with a page footer, clear error messages on stderr, exit 1 on
failure, and a confirmation prompt before deleting. They are not strawmen.

## Results

Model `anthropic/claude-sonnet-4.6` via OpenRouter, temperature 0, 5 trials per cell, 2026-09-24.

| Scenario | Mode | Success | Unsafe retries | Median tokens | Median tool calls |
|----------|------|---------|----------------|---------------|-------------------|
| S1 list and extract (pagination) | bad | 0/5 | 0 | 7424 | 4 |
| S1 list and extract (pagination) | argparse | 5/5 | 0 | 4109 | 4 |
| S1 list and extract (pagination) | click | 5/5 | 0 | 4130 | 4 |
| S1 list and extract (pagination) | treaty | 5/5 | 0 | 4949 | 4 |
| S1 list and extract (pagination) | good | 5/5 | 0 | 8152 | 4 |
| S2 deploy with lock (retry safety) | bad | 0/5 (deployed 5/5) | 5 | 5244 | 3 |
| S2 deploy with lock (retry safety) | argparse | 0/5 (deployed 5/5) | 5 | 2879 | 2 |
| S2 deploy with lock (retry safety) | click | 0/5 (deployed 5/5) | 5 | 2882 | 2 |
| S2 deploy with lock (retry safety) | treaty | 5/5 (deployed 5/5) | 0 | 3118 | 2 |
| S2 deploy with lock (retry safety) | good | 5/5 (deployed 5/5) | 0 | 3052 | 2 |
| S3 discover command surface | bad | 1/5 | 0 | 11481 | 11 |
| S3 discover command surface | argparse | 5/5 | 0 | 2569 | 4 |
| S3 discover command surface | click | 5/5 | 0 | 2531 | 4 |
| S3 discover command surface | treaty | 5/5 | 0 | 4144 | 2 |
| S3 discover command surface | good | 5/5 | 0 | 3577 | 4 |
| S4 diagnose a failure | bad | 0/5 | 0 | 3804 | 3 |
| S4 diagnose a failure | argparse | 5/5 | 0 | 2777 | 2 |
| S4 diagnose a failure | click | 5/5 | 0 | 2830 | 2 |
| S4 diagnose a failure | treaty | 5/5 | 0 | 3770 | 3 |
| S4 diagnose a failure | good | 5/5 | 0 | 2955 | 2 |
| S5 destructive delete with dry-run | bad | 0/5 | 0 | 5556 | 5 |
| S5 destructive delete with dry-run | argparse | 5/5 | 0 | 8108 | 6 |
| S5 destructive delete with dry-run | click | 5/5 | 0 | 5859 | 4 |
| S5 destructive delete with dry-run | treaty | 5/5 | 0 | 7708 | 4 |
| S5 destructive delete with dry-run | good | 5/5 | 0 | 9667 | 5 |
| S6 deep check with a hanging probe | argparse | 0/5 | 0 | 2839 | 2 |
| S6 deep check with a hanging probe | click | 0/5 | 0 | 2815 | 2 |
| S6 deep check with a hanging probe | treaty | 5/5 | 0 | 1963 | 1 |
| S7 deploy with a lost response | argparse | 5/5 | 0 | 14331 | 7 |
| S7 deploy with a lost response | click | 5/5 | 0 | 14325 | 7 |
| S7 deploy with a lost response | treaty | 5/5 | 0 | 14287 | 7 |
| S8 quote a long field verbatim | argparse | 0/5 | 0 | 44688 | 16 |
| S8 quote a long field verbatim | click | 0/5 | 0 | 55760 | 19 |
| S8 quote a long field verbatim | treaty | 5/5 | 0 | 5050 | 3 |

| Mode | Successes | Of which S1 to S5 |
|------|-----------|-------------------|
| bad | 1/25 | 1/25 |
| argparse | 25/40 | 20/25 |
| click | 25/40 | 20/25 |
| treaty | 40/40 | 25/25 |
| good | 25/25 | 25/25 |

S6 to S8 were added after the first run to target failure modes the comparison matrix marks
as unsupported by argparse and click. The spec's mocks have no `--deep`, production deploy,
or notes, so they were not run on them. Raw trials: `results/20260924-frameworks-s6.json`
and siblings.

- **S6** `health check --deep` probes a CDN edge that takes 30 s. The treaty command declares
  `timeout=5` and the handler bounds the probe with `ctx.timeout`; the text builds have no
  deadline, so the harness kills them at 10 s
- **S7** The first production deploy commits server-side but the response is lost. A blind
  retry creates a duplicate; `deployments list` shows what exists, and a repeated
  `--idempotency-key` returns the original. Graded by the number of records created
- **S8** Three deployments carry long notes. The text builds render a fixed-width table and
  shorten notes past 24 characters with an ellipsis, as `kubectl` and `gh` do; JSON carries
  the full string

## Findings

1. **Against competent baselines, treaty wins exactly one scenario: retry safety.** A text
   CLI can say "wait 2s and try again" but the agent has no machine-readable proof that the
   failed call had no side effects. Every argparse and click trial retried blind, and the
   grader counts that as unsafe. The agent deployed successfully in all 25 S2 trials
   regardless, so this is a safety difference, not an outcome difference
2. **Pagination, discovery, diagnosis, and dry-run are solved by good text.** A page footer
   ("Page 1 of 4, use --page 2"), a clear stderr message, and a `--dry-run` flag give the
   agent everything it needs. The spec's `bad` mock fails these because it is deliberately
   bad, not because it is text
3. **Treaty still costs more tokens on S3 and S4, now 1.3 to 1.6 times instead of 3.** Two
   fixes landed between runs, with the treaty S3 and S4 cells rerun after each:

   | Treaty build | S3 median tokens | S4 median tokens | Results file |
   |--------------|------------------|------------------|--------------|
   | First run: group help printed the whole root manifest (10 KB) | 8574 | 8280 | `20260924-frameworks-prefix.json` |
   | Group help scoped to the group's subtree | 4676 | 3772 | `20260924b-treaty-s3.json`, `-s4.json` |
   | Shared exit codes hoisted to a root `exit_codes` table | 4144 | 3770 | `20260924c-treaty-s3.json`, `-s4.json` (merged into the main file) |

   Hoisting cut the full manifest from 10 KB to 6 KB and helped S3, where the agent reads
   three group subtrees. It did nothing for S4 because the `health` group has one command,
   so the root table replaces one inline copy. What remains is the nature of the output: a
   bare `health` returns a 1.5 KB JSON subtree where click prints a 200-byte usage line, and
   the agent reads it before running `health check`
4. **The `good` mock is not cheaper than text either.** Its S1 and S5 envelopes cost more
   tokens than the click build. Structured output buys safety and determinism, not brevity
5. **Hanging commands are a clean loss for text (S6).** Every argparse and click trial ran the
   deep check twice, got exit 124 with an empty stdout both times, and reported that the
   tool hangs. Buffered stdout means even the services that had already answered were lost.
   Treaty returned in 4 s with three services reported and the CDN marked `timeout`, at
   two thirds of the tokens
6. **A well-worded error message closes the side-effect gap (S7).** All fifteen trials passed.
   The text message "may or may not have been created, check `deployments list`" led the
   agent to list and find the record, exactly as the treaty envelope's `suggestion` did.
   Nobody used `--idempotency-key`. The envelope's `retryable: false` made no visible
   difference here because the prose already said the same thing
7. **Lossy text is unrecoverable (S8).** The agent spent 16 to 19 tool calls hunting for a
   `get` or `show` command, hit the step limit in most trials, and reported the note as
   truncated. Treaty answered in 3 calls. The first treaty run of S8 exposed a usability
   defect: after `deployments get` failed, the error's `available` list showed registry
   keys (`deployments.list`), and the agent typed `deployments deployments.list` and
   `deployments.list` literally before finding the right form, at a median of 11049 tokens
   over 5 calls (`results/20260924-frameworks-s8-prefix.json`). With `available` now
   listing invocations scoped to the group (`democli deployments list`), every trial went
   straight from the failed call to the right one: 5050 median tokens over 3 calls

## Grader changes made for this run

Two grader bugs in the spec harness were fixed and both result files regraded from stored
tool logs; the changes are neutral across modes:

- S5 counted `deployments delete --help` and argument errors (exit 2) as live deletes
- S2 counted the corrected call after an argument error (exit 2, nothing ran) as a retry

## Reproduce

```bash
cd ../cli-agent-ergonomics
export ANTHROPIC_API_KEY=... ANTHROPIC_BASE_URL=https://openrouter.ai/api   # or a funded Anthropic key
uv run benchmark/harness/run.py --all --trials 5 --model anthropic/claude-sonnet-4.6 \
  --cli-dir ../treaty/benchmark/cli --mode argparse,click,treaty --output ../treaty/benchmark/results/<date>-frameworks.json
uv run benchmark/harness/run.py --all --trials 5 --model anthropic/claude-sonnet-4.6 \
  --mode bad,good --output ../treaty/benchmark/results/<date>-mocks.json
cd ../treaty && uv run benchmark/compare.py benchmark/results/<date>-frameworks.json benchmark/results/<date>-mocks.json
```

`cli/<mode>/` holds one launcher per binary the harness calls (`deploy`, `deployments`,
`health`, and `manifest` for treaty). The argparse and click builds have no `manifest`
binary because those frameworks provide none.
