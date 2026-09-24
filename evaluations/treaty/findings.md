# treaty — Findings

| Failure mode | Title | Severity | Score | Date | Notes |
|---|---|---|---|---|---|
| §10 | Interactivity & TTY Requirements | Critical | 3/3 | 2026-09-24 | No prompt/pager/editor code paths exist; init, exec, audit, conformance and a destructive example all exit well under 5s with stdin=/dev/null and PAGER/EDITOR/VISUAL=`sleep 30`; destructive commands refuse with CONFIRMATION_REQUIRED (exit 2) instead of prompting, even under a PTY; non-TTY stdout auto-switches to JSON ✓ |
