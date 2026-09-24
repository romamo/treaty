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
