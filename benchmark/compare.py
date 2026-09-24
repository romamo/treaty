"""Merge harness result files into one comparison table.

Usage:
  uv run benchmark/compare.py benchmark/results/20260924-frameworks.json benchmark/results/20260924-mocks.json

Reads the per-trial runs, so cells from several files render on one table. Exit codes:
0 rendered, 2 usage error.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MODE_ORDER = ("bad", "argparse", "click", "treaty", "good")
SCENARIO_NAMES = {
    "s1": "S1 list and extract (pagination)",
    "s2": "S2 deploy with lock (retry safety)",
    "s3": "S3 discover command surface",
    "s4": "S4 diagnose a failure",
    "s5": "S5 destructive delete with dry-run",
    "s6": "S6 deep check with a hanging probe",
    "s7": "S7 deploy with a lost response",
    "s8": "S8 quote a long field verbatim",
}


def load(paths: list[Path]) -> tuple[list[dict[str, Any]], set[str]]:
    runs: list[dict[str, Any]] = []
    models: set[str] = set()
    for path in paths:
        data = json.loads(path.read_text())
        if data.get("harness_version") != "2":
            raise SystemExit(f"{path}: harness version {data.get('harness_version')!r} is not 2")
        models.add(data["model"])
        runs.extend(data["runs"])
    return runs, models


def deployed(run: dict[str, Any]) -> bool:
    return any(c["command"] == "deploy" and c["exit_code"] == 0 for c in run["tool_calls"])


def render(runs: list[dict[str, Any]], models: set[str]) -> str:
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        cells[(run["scenario"], run["mode"])].append(run)
    modes = [m for m in MODE_ORDER if any(k[1] == m for k in cells)]
    modes += sorted({k[1] for k in cells} - set(modes))
    lines = [f"Model: {', '.join(sorted(models))}", ""]
    lines.append("| Scenario | Mode | Success | Unsafe retries | Median tokens | Median tool calls | Median time |")
    lines.append("|----------|------|---------|----------------|---------------|-------------------|-------------|")
    per_mode: dict[str, list[int]] = defaultdict(list)
    for scenario in sorted({k[0] for k in cells}):
        for mode in modes:
            trials = cells.get((scenario, mode))
            if not trials:
                continue
            successes = sum(t["success"] for t in trials)
            per_mode[mode].append(successes)
            unsafe = sum(t["unsafe_retry"] for t in trials)
            tokens = int(statistics.median(t["metrics"]["total_tokens"] for t in trials))
            calls = statistics.median(t["metrics"]["tool_calls"] for t in trials)
            secs = statistics.median(t["metrics"]["time_ms"] for t in trials) / 1000
            extra = ""
            if scenario == "s2":
                extra = f" (deployed {sum(deployed(t) for t in trials)}/{len(trials)})"
            lines.append(
                f"| {SCENARIO_NAMES.get(scenario, scenario)} | {mode} | {successes}/{len(trials)}{extra} | {unsafe} "
                f"| {tokens} | {calls:g} | {secs:.0f}s |"
            )
    lines += ["", "| Mode | Successes across scenarios |", "|------|----------------------------|"]
    for mode in modes:
        total = sum(per_mode[mode])
        n = sum(len(cells[(s, mode)]) for s in {k[0] for k in cells} if (s, mode) in cells)
        lines.append(f"| {mode} | {total}/{n} |")
    lines += ["", "Failure reasons per cell:"]
    for (scenario, mode), trials in sorted(cells.items(), key=lambda kv: (kv[0][0], modes.index(kv[0][1]))):
        reasons = sorted({t["grade_reason"] for t in trials if not t["success"]})
        if reasons:
            lines.append(f"- {scenario}/{mode}: " + "; ".join(reasons))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv]
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    runs, models = load(paths)
    print(render(runs, models))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
