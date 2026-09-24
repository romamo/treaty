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

| Mode | Successes |
|------|-----------|
| bad | 1/25 |
| argparse | 20/25 |
| click | 20/25 |
| treaty | 25/25 |
| good | 25/25 |

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
