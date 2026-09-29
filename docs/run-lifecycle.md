# Run lifecycle

Every run moves through `RunState` (`src/lib_python_harness/runtime/lifecycle.py`)
under one validated transition table. A transition outside this table raises
`IllegalTransitionError` — there is no silent no-op, not even for a
already-finished run (`Harness.stop()` on a `COMPLETED` run raises here,
before any signal is ever sent).

`RunState` has five members: `CREATED`, `RUNNING`, `COMPLETED`, `FAILED`,
`CANCELLED`. The illustrative "starting" state some designs use is
deliberately absent: `Harness.start()` spawns the child synchronously and
only returns once it is already running, so no caller could ever observe a
run in a "starting" state between `CREATED` and `RUNNING`.

## Transitions

- `CREATED` -> `RUNNING`
- `CREATED` -> `FAILED`
- `RUNNING` -> `COMPLETED`
- `RUNNING` -> `FAILED`
- `RUNNING` -> `CANCELLED`

`COMPLETED`, `FAILED` and `CANCELLED` are terminal: none of them has an
outgoing edge. `tests/test_lifecycle_doc.py` parses the list above and
diffs it structurally against `runtime.lifecycle._TRANSITIONS` — the two
are required to describe exactly the same edge set, nothing missing or
extra.

## Provider selection

`Harness.start()` resolves `RunSpec.provider` (`"claude"` by default,
`"codex"`, `"mistral"`) to a provider instance before anything else: an unknown name
raises `HarnessError`, and a provider that cannot honour a set field raises
`UnsupportedByProvider` from `build_launch_plan()` — in both cases before any
record is written, so no run ever reaches `CREATED`. One provider instance
serves one run (it may keep per-run state between `build_launch_plan` and
`parse_events`). On native Windows `stop()` has no graceful signal: it goes
straight to a whole-tree force kill (`taskkill /T /F`).

## What causes each edge

- `CREATED -> RUNNING`: `Harness.start()` successfully spawned the child
  process (`_spawn_detached` returned a live `Popen`).
- `CREATED -> FAILED`: `Harness.start()` raised while spawning (e.g. the
  `claude` binary is not on `PATH`) — the run never reached `RUNNING`.
- `RUNNING -> COMPLETED`: the child exited 0 and its stream-json output
  ended with a terminal `result` event that `ClaudeCliProvider.parse_events`
  could parse.
- `RUNNING -> FAILED`: the child exited non-zero, or its stream ended
  without a terminal `result` event (a truncated stream never counts as
  success), or (claude only, #42) it ended with a `run_in_background: true`
  task still unresolved — `RunResult.abandoned_background_tasks` names it.
- `RUNNING -> CANCELLED`: `Harness.stop()` was called on a still-`RUNNING`
  run: graceful signal (`SIGTERM`) -> bounded wait -> force kill (`SIGKILL`)
  -> reap, all identity-checked against the pid's captured start time so a
  recycled pid is never signalled.

## Observing a run from another process

The starter of a run and its observer need not be the same process: with a
shared `FileRunStore`, a `Harness` that holds no `Popen` can watch the run. No
new `RunState` and no new edge are added to the table above.

- `Harness.wait(run_id, timeout, poll_interval)` (`wait_for` is the same
  method) polls the record until it is terminal, in the starter's process or
  any other. On timeout it returns the `RUNNING` result with `timed_out=True`
  and signals nothing; it never calls `stop()`. `run()` and `resume()` go
  through it, so an expired time limit detaches instead of cancelling. Only an
  explicit `stop()` yields `CANCELLED`.
- **Orphan reconciliation.** A `RUNNING` record whose recorded `pid` +
  `start_time` no longer names a live process (an unreaped zombie counts as
  not alive) is finalized by `poll()`, `list_runs()` and `wait()` without a
  `Popen`: no exit code is known, so the terminal `result` event alone decides
  `COMPLETED` vs `FAILED` (no such event -> `FAILED`). A trailing half-written
  `events.jsonl` line is ignored. `wait` waits a short grace period so a
  `stop()` running in the starter's process is reported `CANCELLED`, not
  `FAILED`. The grace is measured from a durable anchor -- the latest of
  `created_at`, the `events.jsonl`/`stderr.txt` mtimes and the record's
  `stop_requested_at` (set by `stop()` before it signals a live pid) -- not
  from a per-call timer, so repeated `wait(run_id, 0)` calls, even from a
  process without the `Popen`, finalize a gone run once that anchor is
  older than the grace period. `duration_s` of a finalized run ends at the
  terminal event (or the exit this process observed, or the last sign of
  life), not at whenever an observer got round to finalizing. On Windows the start time comes from
  `GetProcessTimes` via `ctypes`; `psutil` is not required.
- **Post-completion grace-kill (#55).** A provider child that has already
  written its terminal event to `events.jsonl` (scoped by
  `Provider.has_terminal_event`, when the provider has one — today only
  `ClaudeCliProvider`) but keeps its OS process alive is killed by `wait()`
  once that event has aged past `_FINALIZE_GRACE_S`, and the run is finalized
  from the terminal event alone, same as orphan reconciliation above (its
  `duration_s` ends at that event, not at the kill); the
  grace window is anchored to the events file's own mtime, not to when a
  particular `wait()` call started, so it survives repeated short calls and
  late-arriving observers alike. It never signals a pid whose identity can't
  be verified — that case just keeps waiting, exactly like orphan
  reconciliation's own identity check.
- **Idempotent finalization.** Before writing, finalization re-reads the stored
  record and keeps it when it is already terminal. There is no cross-process
  lock, so this narrows the race rather than closing it.
- `FileRunStore.put` writes a temp file and `os.replace`s it, so a reader never
  sees a half-written `record.json`.
- While `RUNNING`, `poll()` and a timed-out `wait()` report `duration_s`,
  `event_count`, `last_event_at` and `last_activity` from
  `events.jsonl`; `list_runs()` returns `RunSummary` rows.

## Resume

`Harness.resume(run_id, prompt, timeout=None)` sends a follow-up message to a
finished run. It is a **new run**, not a transition of the old one: a new
`run_id`, the origin's `session_id`, and `resumed_from` (record and
`provenance.json`) naming the origin. The origin's record is untouched and the
new run walks the ordinary `CREATED -> RUNNING -> terminal` path, so no edge is
added to the table above.

`Harness.start_resume(run_id, prompt)` is the **non-blocking** variant: it does
the same validation and spawn, but returns the new run (`state == RUNNING`,
new `run_id`, the origin's `session_id`) as soon as the follow-up child is
started instead of waiting for it to end. `resume()` is exactly
`start_resume()` followed by `wait()` on the new run. After `start_resume()`
use `poll` / `wait` / `stop` on the new `run_id`. Everything below applies
to both.

- **Terminal origins only.** `CREATED`/`RUNNING` origins raise `HarnessError`;
  a `CANCELLED` origin resumes mechanically, but only the transcript the CLI
  actually wrote exists, so the answer may rest on a partial turn.
- **One live run per session.** If any record of the same `session_id` is
  `CREATED`/`RUNNING`, `resume()` raises `HarnessError`. The check and the new
  record's insertion are one process-wide critical section, so it holds across
  every `Harness` instance in a process. It is **not** enforced across
  processes (a shared `FileRunStore` has no cross-process lock).
- **Isolation is replayed, not rebuilt.** The provider copies the origin's
  recorded argv, swaps `--session-id <id>` for `--resume <session_id>`, and
  sends the new prompt on stdin. Providers whose CLI cannot do this (`codex`,
  `mistral`, a custom provider without `build_resume_plan`) raise
  `UnsupportedByProvider`.

## Resume after `cleanup()`

`cleanup(run_id, remove_cwd=False)` drops the run's record from the store
and stops tracking its process handle; it never touches the CLI transcript,
and by default (`remove_cwd=False`) never removes the run's cwd either (a cwd
may hold files the caller cares about).

`claude --resume <session_id>` does **not** need the origin's cwd. Measured
with Claude Code 2.1.278 (Windows, Haiku 4.5, ticket #11): resumed from an
empty foreign cwd, the session is found, the history is intact (the codeword
from the origin turn is answered) and the reported `session_id` is the
origin's; the new turn is written into the origin's transcript. The same
holds through `Harness.resume` after the origin cwd was deleted (it then runs
in a fresh temp directory). `tests/test_resume_live.py` prints these
measurements (`python -m pytest -m requires_claude tests/test_resume_live.py -q -s`),
and `tests/test_harness_end_to_end.py::test_resume_after_cleanup` drives a raw
`claude --resume` round trip after `cleanup()`.
