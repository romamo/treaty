# treaty — Trace

## §10 — Interactivity & TTY Requirements
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** each command below run as `PAGER="sleep 30" EDITOR="sleep 30" VISUAL="sleep 30" perl -e 'alarm(5); exec(@ARGV)' <cmd> </dev/null`
**Score:** 3/3

| Command | Exit code | Result |
|---|---|---|
| `uv run treaty init demo-cli --directory <scratch>/demo` | 0 | JSON envelope, files written, no prompt |
| same again (directory exists) | 6 | CONFLICT envelope, no overwrite prompt |
| `uv run treaty exec` | 0 | empty stdout/stderr, returned immediately on EOF |
| `uv run treaty audit treaty._cli:cli --strict` | 0 | JSON report |
| `uv run treaty conformance treaty._cli:cli --out <scratch>/conf.json` | 0 | JSON envelope, profile written |
| `uv run treaty --help` | 0 | JSON manifest (non-TTY → json) |
| `uv run treaty manifest --format human` | 0 | pretty JSON, no pager despite PAGER set |
| `uv run python examples/deployctl.py deploy rollback api --to v1` | 2 | CONFIRMATION_REQUIRED envelope, no prompt |
| same with `--confirm-destructive` | 0 | applied=true |

PTY check (`perl -e 'alarm(8); exec(@ARGV)' script -q /dev/null <cmd> </dev/null`): destructive rollback and `treaty --help` rendered human output and returned without prompting or paging.

Source scan: `grep -rn "pager\|PAGER\|EDITOR\|subprocess\|getpass\|input(" src/treaty/*.py` matched only `subprocess.run` in `_profile.py` (runs the spec kit, not interactive).

**stdout** (destructive, no confirm):
```
{"data":{"applied":false,"release":"v1","service":"api","strategy":"safe"},"error":{"code":"CONFIRMATION_REQUIRED","context":{"command":"deploy.rollback","flag":"confirm-destructive"},"fix_required":"rerun with --confirm-destructive to apply","message":"deploy.rollback is destructive; nothing was applied","phase":"validation","retryable":false},"meta":{"duration_ms":0,"exit_code":2,"request_id":"b8befa3e1795","timeout_ms":60000},"ok":false,"warnings":[]}
```

**stdout** (PTY, destructive, no confirm):
```
{
  "applied": false,
  "release": "v1",
  "service": "api",
  "strategy": "safe"
}
deployctl: CONFIRMATION_REQUIRED: deploy.rollback is destructive; nothing was applied
  command: deploy.rollback
  flag: confirm-destructive
```

**stderr:** empty for all non-PTY runs

## §34 — Shell Injection via Agent-Constructed Commands
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty init "acme%2Fwidgets" --dry-run`; `treaty init demo --directory ../../etc/test --dry-run`; `treaty conformance treaty._cli:cli --out ../../etc/test.json` (stdin=/dev/null)
**Exit code:** 2; 0; 0
**Score:** 1/3

**stdout** (first 20 lines):
```
{"error":{"code":"ARG_ERROR","context":{"name":"acme%2Fwidgets"},"message":"name must be lowercase letters, digits, and hyphens, starting with a letter","phase":"validation"},"meta":{"exit_code":2},"ok":false}
{"data":{"directory":"../../etc/test","files":[...9 files],"written":false},"ok":true}
{"data":{"checks":[],"probes":4,"profile":"../../etc/test.json","ran":false},"ok":true}
[stray /Users/roman/PycharmProjects/etc/test.json removed after the check]
```

**stderr** (first 20 lines):
```
```

## §37 — REPL / Interactive Mode Accidental Triggering
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `grep -rn "REPL\|requires_interactive\|INTERACTIVE_REQUIRED\|input(" src/treaty/`
**Exit code:** 1 (no matches)
**Score:** N/A

**stdout** (first 20 lines):
```
(no matches)
```

**stderr** (first 20 lines):
```
```

## §42 — Debug / Trace Mode Secret Leakage
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty version --token s3cr3t --debug`; `treaty audit treaty._cli:cli --limit s3cr3t-value` (stdin=/dev/null)
**Exit code:** 2; 2
**Score:** 0/3

**stdout** (first 20 lines):
```
{"error":{"code":"ARG_ERROR","context":{"command":"version","flag":"token","known":[]},"message":"unknown flag '--token'"},"ok":false}
{"error":{"code":"ARG_ERROR","context":{"flag":"limit","value":"s3cr3t-value"},"message":"'limit' expects an integer"},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §43 — Tool Output Result Size Unboundedness
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty manifest </dev/null | wc -c`
**Exit code:** 0
**Score:** 0/3

**stdout** (first 20 lines):
```
7838 bytes; envelope meta keys: duration_ms, exit_code, request_id (no truncated/total_bytes)
```

**stderr** (first 20 lines):
```
```

## §45 — Headless Authentication / OAuth Browser Flow Blocking
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `grep -rn "AUTH_\|auth_methods" src/treaty/`
**Exit code:** 0
**Score:** N/A

**stdout** (first 20 lines):
```
src/treaty/_exit.py:31:    AUTH_REQUIRED = 8
```

**stderr** (first 20 lines):
```
```

## §50 — Stdin Consumption Deadlock
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty exec </dev/null`
**Exit code:** 2
**Score:** 2/3

**Conformance kit:** no_hang_stdin_open: pass; no_hang_stdin_closed: pass

**stdout** (first 20 lines):
```
{"data":null,"error":{"code":"EMPTY_STREAM","context":{"lines":0},"fix_required":"pipe one DispatchRequest JSON object per line into exec","message":"no DispatchRequest lines on stdin","phase":"validation","retryable":false},"meta":{"exit_code":2},"ok":false,"warnings":[]}
```

**stderr** (first 20 lines):
```
```

## §53 — Credential Expiry Mid-Session
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `sed -n 20,35p src/treaty/_exit.py`
**Exit code:** 0
**Score:** N/A

**stdout** (first 20 lines):
```
PERMISSION_DENIED = 7
AUTH_REQUIRED = 8
(no expiry code)
```

**stderr** (first 20 lines):
```
```

## §60 — OS Output Buffer Deadlock
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `printf '{"_cmd":"fetch","seconds":1}\n' x3 | uv run python examples/slowctl.py exec` with per-line timestamps
**Exit code:** 0
**Score:** 1/3

**stdout** (first 20 lines):
```
1790270862.2 {"data":{"slept":1.0,...},"meta":{"_cmd":"fetch","_line":1}}
1790270863.2 {... "_line":2}
1790270864.2 {... "_line":3}
```

**stderr** (first 20 lines):
```
```

## §61 — Bidirectional Pipe Payload Deadlock
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** Python Popen(`treaty exec`, stdin=PIPE, stdout=PIPE): write 228000 bytes, close stdin, then read
**Exit code:** 0 (after draining)
**Score:** 0/3

**stdout** (first 20 lines):
```
payload bytes 228000
naive write finished: False
drained 2376894 exit 0
```

**stderr** (first 20 lines):
```
```

## §62 — $EDITOR and $VISUAL Trap
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `grep -rn "EDITOR\|VISUAL" src/treaty/`
**Exit code:** 1 (no matches)
**Score:** N/A

**stdout** (first 20 lines):
```
(no matches)
```

**stderr** (first 20 lines):
```
```

## §64 — Headless Display and GUI Launch Blocking
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `grep -rn "webbrowser\|DISPLAY\|open_browser" src/treaty/`
**Exit code:** 1 (no matches)
**Score:** N/A

**stdout** (first 20 lines):
```
(no matches)
```

**stderr** (first 20 lines):
```
```

## §71 — Non-Interactive Installation Absence
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `uv pip install --python <scratch venv> . </dev/null` twice; `<venv>/bin/treaty --version`
**Exit code:** 0; 0; 0
**Score:** 1/3

**stdout** (first 20 lines):
```
install 1 exit=0
install 2 exit=0
{"data":{"name":"treaty","version":"0.0.1"},"ok":true}
```

**stderr** (first 20 lines):
```
```

## §11 — Timeouts & Hanging Processes
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `uv run python examples/slowctl.py fetch --seconds 5 --timeout 2 </dev/null`
**Exit code:** 10
**Score:** 2/3

**Conformance kit:** no_hang_stdin_closed: pass

**stdout** (first 20 lines):
```
{"data":null,"error":{"code":"TIMEOUT","context":{"command":"fetch","timeout_ms":2000},"message":"fetch exceeded 2.0s","phase":"execution","retryable":false},"meta":{"duration_ms":2005,"exit_code":10,"timeout_ms":2000},"ok":false,"warnings":[]}
```

**stderr** (first 20 lines):
```
```

## §12 — Idempotency & Safe Retries
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty init idem-cli --directory <scratch>/idem --idempotency-key k1`; same without the key, twice
**Exit code:** 2; 0; 6
**Score:** 0/3

**stdout** (first 20 lines):
```
{"error":{"code":"ARG_ERROR","message":"unknown flag '--idempotency-key'"},"ok":false}
{"data":{"directory":".../idem","files":[...],"written":true},"ok":true}
{"error":{"code":"CONFLICT","fix_required":"choose an empty directory with --directory"},"meta":{"exit_code":6},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §13 — Partial Failure & Atomicity
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `printf` 5 lines (line 3 `{"_cmd":"nope"}`) `| treaty exec`
**Exit code:** 1
**Score:** 1/3

**stdout** (first 20 lines):
```
{"data":{"name":"treaty","version":"0.0.1"},"meta":{"_cmd":"version","_line":1,"exit_code":0},"ok":true}
{"data":[...rules],"meta":{"_cmd":"rules","_line":2},"ok":true}
{"error":{"code":"UNKNOWN_COMMAND","context":{"_cmd":"nope","line":3},"message":"line 3: unknown command nope"},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §23 — Side Effects & Destructive Operations
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `uv run python examples/deployctl.py deploy rollback api --to v1 --dry-run </dev/null`
**Exit code:** 0
**Score:** 2/3

**Conformance kit:** dry_run_preview: skip; destructive_refuses_unconfirmed: skip (treaty has no destructive commands)

**stdout** (first 20 lines):
```
{"data":{"applied":false,"release":"v1","service":"api","strategy":"safe"},"error":null,"meta":{"duration_ms":0,"exit_code":0,"request_id":"971fb9a1f971","timeout_ms":60000},"ok":true,"warnings":[]}
```

**stderr** (first 20 lines):
```
```

## §24 — Authentication & Secret Handling
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty audit treaty._cli:cli --limit s3cr3t-value </dev/null`
**Exit code:** 2
**Score:** 0/3

**stdout** (first 20 lines):
```
{"error":{"code":"ARG_ERROR","context":{"flag":"limit","value":"s3cr3t-value"},"message":"'limit' expects an integer"},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §25 — Prompt Injection via Output
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty audit injapp:app --all` where injapp's command description is "IGNORE PREVIOUS INSTRUCTIONS..."
**Exit code:** 0
**Score:** 0/3

**stdout** (first 20 lines):
```
{"data":{"failed":3,"next_steps":[{"command":"go","rule":"describe",...}],...},"ok":true}
grep IGNORE in output: 0 matches
```

**stderr** (first 20 lines):
```
```

## §74 — Credential Scope Declaration Absence
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty manifest | jq '.data.commands[].required_scopes'`; `treaty check-permissions --for audit`
**Exit code:** 0; 2
**Score:** 2/3

**stdout** (first 20 lines):
```
{'audit': (True, []), 'conformance': (True, []), 'exec': (True, []), 'init': (True, []), 'manifest': (True, []), 'rules': (True, []), 'version': (True, [])}
{"error":{"code":"ARG_ERROR","message":"unknown command 'check-permissions'"},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §75 — Safe-Default Execution Mode Absent
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `uv run python examples/deployctl.py deploy rollback api --to v1`; same with `--live`
**Exit code:** 2; 2
**Score:** 1/3

**stdout** (first 20 lines):
```
{"data":{"applied":false,...},"error":{"code":"CONFIRMATION_REQUIRED","fix_required":"rerun with --confirm-destructive to apply"},"meta":{"exit_code":2},"ok":false}
{"error":{"code":"ARG_ERROR","context":{"flag":"live"},"message":"unknown flag '--live'"},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §1 — Exit Codes & Status Signaling
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty init`; `treaty audit nosuch.module:app`; `slowctl fetch --seconds 5 --timeout 2`
**Exit code:** 2; 5; 10
**Score:** 3/3

**Conformance kit:** exit_code_contract: pass; invalid_input_exit_2: pass

**stdout** (first 20 lines):
```
{"error":{"code":"ARG_ERROR","context":{"missing":["name"]}},"meta":{"exit_code":2},"ok":false}
{"error":{"code":"NOT_FOUND","message":"cannot import nosuch.module"},"meta":{"exit_code":5},"ok":false}
{"error":{"code":"TIMEOUT"},"meta":{"exit_code":10},"ok":false}
```

**stderr** (first 20 lines):
```
```

## §2 — Output Format & Parseability
**Date:** 2026-09-24
**CLI version:** 0.0.1
**Check command:** `treaty rules --format json 2>/dev/null`; `treaty --output json rules`
**Exit code:** 0; 2
**Score:** 3/3

**Conformance kit:** json_envelope: pass; help_off_stdout: pass

**stdout** (first 20 lines):
```
{"data":[{"id":"describe","severity":"advice",...}],"error":null,"meta":{"duration_ms":0,"exit_code":0,"request_id":"..."},"ok":true,"warnings":[]}
{"error":{"code":"ARG_ERROR","message":"unknown command '--output'"},"ok":false}
```

**stderr** (first 20 lines):
```
```
