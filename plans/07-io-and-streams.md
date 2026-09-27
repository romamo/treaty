# 07: I/O limits and streams

Bounded input, timely output, and every format the spec names.

| Requirement | Level | Now | Gap |
|-------------|-------|-----|-----|
| F-011 Default timeout per command | 2 | Partial | Streaming commands default to no timeout |
| F-014 SIGPIPE handling | 2 | Partial | Exits 141; spec requires 0 (decision D1) |
| F-053 Stdout unbuffering | 2 | Partial | No `PYTHONUNBUFFERED`, no heartbeats |
| F-054 Stdin cap with `--input-file` | 2 | Partial | Only `exec` has it; `fix_required` instead of `hint` |
| O-001 `--format` | 2 | Partial | No `jsonl`; no `--output` file target |

## Design

### Streaming timeout (F-011)

F-011 wants every command to finish within `default_timeout + 5s`. Streams keep running as
long as they produce events, so the timeout for a stream becomes an idle timeout: the
deadline resets on every yield. The default is the app default (60 s), `--timeout 0` opts
out explicitly. `meta.timeout_ms` is present on every stream envelope; the manifest shows
`timeout_kind: idle` for streams. This replaces `Timeout(None)` in `App.command`.

### SIGPIPE (F-014, D1)

`output_closed()` exits `0` when at least one complete envelope or stream event reached
the reader, which is the `tool list | head -1` case; it keeps 141 (`OUTPUT_CLOSED`) when
nothing was delivered, because then the reader got no answer. No stderr output in either
case (already true). Update the manifest's `OUTPUT_CLOSED` entry description.

### Unbuffering and heartbeats (F-053)

- `App.main()` sets `PYTHONUNBUFFERED=1` in `os.environ` for children and reconfigures
  `sys.stdout` with `line_buffering=True` when stdout is not a TTY. Envelopes are already
  flushed one by one
- `@app.command(..., heartbeat=True)` (or `App(heartbeat_ms=...)` for all commands with
  `has_network_io`) writes `{"status": "running", "heartbeat": true, "elapsed_ms": N}` lines
  to stdout every `heartbeat_ms` (default 10 000) while a non-streaming handler runs, from
  the main thread that already waits on `call_with_timeout`. `--heartbeat-ms` (O-038, P1)
  overrides it. The manifest declares `heartbeat_ms` so an agent knows which lines to skip

### Stdin payloads (F-054, O-039)

`stdin_input=True` on a command adds a `payload: str` source read from stdin under the
same `max_stdin_bytes` cap as `exec`, plus `--input-file PATH` (`-` means capped stdin).
The handler receives it as `ctx.stdin_text` (or a `StdinPayload` resource). Over the cap:
exit 2, `STDIN_TOO_LARGE`, with a `hint` field naming `--input-file`. Add `hint` to
`ErrorDetail` (keep `fix_required` too); `exec` switches to the same helper.

### Formats (O-001)

- Add `Format.JSONL`: one compact envelope per line; for a stream it equals the JSON
  stream, for `exec` it is the current per-line output, so `exec --format jsonl` stops
  failing. Offered by every app without a renderer
- `--output PATH` becomes a framework flag on commands that declare `output_file=True`:
  the rendered `data` goes to the file, the envelope (with `data: {"path": ..., "bytes": N}`)
  to stdout. `--format jsn` still exits 2 before any file is opened
- `tsv` and `csv` remain app-registered renderers; add a built-in `tabular` renderer for
  `list[dataclass]` outputs that apps can register with one line

## Tasks

- [x] Idle timeout for streams; `timeout_kind` in `--schema` (see deviations)
- [x] SIGPIPE exit rule per D1
- [x] `PYTHONUNBUFFERED`, line buffering, heartbeat thread-free loop in `_execute`
- [x] `stdin_input=`, `--input-file`, `hint`; `exec` refactor onto it
- [x] `Format.JSONL`; `output_file=` and `--output`; built-in tabular renderer

## Tests

- A stream that yields once and then sleeps forever exits with `TIMEOUT` after the idle
  window; one yielding every second runs past it
- `app list | head -1` exits 0 with empty stderr; closing stdout before any output exits 141
- A 30 s command with `heartbeat_ms=10000` writes at least 2 heartbeat lines before the envelope
- 65 537 bytes on stdin exits 2 with `hint` naming `--input-file`; 65 535 bytes succeeds
- `--format jsonl` on a normal command, a stream, and `exec` parses line by line
- `--format csv --output r.csv` writes the file and a JSON envelope on stdout

## Deviations as built

- **Manifest keys go to `--schema`.** `CommandEntry` admits no extra keys, so
  `timeout_kind: idle`, `heartbeat_ms`, and `stdin_input` are in `--schema` only; the
  manifest carries the `--heartbeat-ms` and `--input-file` flag entries with their defaults
- **A buffered stream keeps a whole-stream deadline.** Under `--no-stream` and in
  `App.call` (MCP) nothing reaches the reader before the end, so there the timeout still
  bounds the whole stream; a live stream's timeout is the idle limit
- **A cancelled stream waits `GRACE_SECONDS` for its worker.** With streams now under a
  timeout, `next()` runs on a worker thread; a signal would otherwise leave the generator
  unclosed and its `finally` unrun
- **Heartbeats are opt-in per command, JSON mode, argv only.** `heartbeat=True` adds
  `--heartbeat-ms` (default 10 000, `0` off). No `App(heartbeat_ms=...)` and no automatic
  heartbeat for `has_network_io` commands: an agent that does not expect the extra lines
  would misread them. `exec` and `App.call` never beat, since they have one reader for
  many results. Heartbeats do not count as delivered output for D1
- **`jsonl` is mapped to `json`** right after the mode is resolved: treaty's JSON output is
  already one compact envelope per line. Handlers see `ctx.mode` as `json`; only
  `--output` writes a different file (one compact item per line)
- **`tsv` is built in, `csv` is not.** O-001 names `tsv` as a minimum, so every app offers
  it; `treaty.table(delimiter)` is the renderer, and `app.format(Format.CSV,
  render=table(","))` is the one line for CSV. Both use the `csv` module's quoting, so a
  tab or quote inside a value round-trips through `csv.reader`. In a stream every event is
  rendered on its own, header included
- **`stdin_input` reads in `_Run.execute`**, before the idempotency and safe-default paths,
  and passes the text as `ctx.stdin_text` (no `StdinPayload` resource). In `exec` and
  `App.call` stdin is not the payload's, so those need `input_file` and otherwise exit `2`
  with `STDIN_UNAVAILABLE`. `<APP>_MAX_STDIN_BYTES` joins `TREATY_MAX_STDIN_BYTES`
- **`--output` is argv-only**, and stdout gets the JSON envelope in every format. A failed
  run writes no file; a renderer failure (`RENDER_FAILED`) or an unwritable path
  (`OUTPUT_UNWRITABLE`) exits `1` with the result kept in `data`. Format names refused as
  `--output` values: `json`, `jsonl`, `tsv`, `csv`, `plain`, `table`, `id`, `yaml`
- **An unknown `--format` still writes the `ARG_ERROR` envelope to stdout.** O-001 says such
  a run "writes nothing to stdout"; F-004 requires an envelope on every exit, so treaty
  reads the criterion as no result output and no file
- `hint` is a new `ErrorDetail` field (the envelope schema allows extra keys); `fix_required`
  stays on the same errors
