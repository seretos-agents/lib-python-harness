# lib-python-harness

Provider-independent subagent engine: lifecycle, dispatch and transport abstraction.

A pure Python library — no binary, no MCP, no marketplace. Consumed as
source by downstream projects via a git pin.

## Install lib-python-harness

Pin an exact tag (recommended) or the floating major-release branch:

```bash
# exact tag
pip install "git+https://github.com/seretos-agents/lib-python-harness@v0.0.1"

# floating: latest 0.x.y release
pip install "git+https://github.com/seretos-agents/lib-python-harness@release/0.x"
```

Or in a consumer's `pyproject.toml`:

```toml
dependencies = [
  "lib-python-harness @ git+https://github.com/seretos-agents/lib-python-harness@v0.0.1",
]
```

## Run API

`lib_python_harness` gives you one call that spawns a fresh, fully isolated
`claude` CLI process (no user/project settings, no plugins, no skills, no
MCP servers, no hooks, no CLAUDE.md, no auto-memory), sends it one prompt on
stdin, and returns a verified result — with an owned lifecycle
(start/poll/stop/cleanup), a per-run resumable session id, and a provenance
record plus an events log written to disk.

```python
from lib_python_harness import run, RunSpec, Isolation

result = run(RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku"))
print(result.text)
```

Every identifier below has its own section with a real, working example.

### run

The module-level convenience — a plain function, `Harness().run(spec)` with
nothing else in between. This is the surface form the ticket's own
acceptance criterion is written as: a bare `run(RunSpec(...))` call.

```python
from lib_python_harness import run, RunSpec, Isolation

result = run(RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku"))
print(result.text)
```

### Harness

The façade. `run(spec)` is `start(spec)` followed by `wait()` — one code
path, not two independent implementations of "run a prompt". `start`/`poll`
let a caller drive a long-running prompt without blocking; `stop` cancels a
run in flight; `cleanup` drops a finished run's bookkeeping record (never
the CLI transcript, never — by default — the run's cwd). `resume(run_id,
prompt)` sends a follow-up to a *finished* run as a **new run** (new `run_id`,
same `session_id`, `resumed_from` = the origin's `run_id`) with the origin's
isolation flags replayed from its recorded argv, blocking like `run()`. It
raises `HarnessError` for an unknown run, a `CREATED`/`RUNNING` origin, or a
session that already has a live run (exclusion holds across every `Harness` in
the process, not across processes), and `UnsupportedByProvider` for providers
that cannot resume (`codex`, `mistral`, a custom provider without
`build_resume_plan`). The origin's cwd is reused, or a fresh temp directory if
it is gone (`claude --resume` finds the session from any cwd, measured on
Claude Code 2.1.278). `start_resume(run_id, prompt)` is the non-blocking
variant: same validation and errors, but it returns the new run (`RUNNING`)
as soon as the follow-up child is spawned, instead of waiting for it to end;
afterwards use `poll` / `wait` / `stop` on the new `run_id`. `resume()` is
exactly `start_resume()` + `wait()`. See `docs/run-lifecycle.md`.

A time limit ends the *waiting*, never the run. `wait(run_id, timeout=None,
poll_interval=0.25)` blocks until the run is terminal and returns its result,
whichever process started it (over a shared `FileRunStore` a freshly started
process can wait a foreign run and gets the same `text`/`usage` as the
starter); `wait_for` is the same method under its older name. An expired
`timeout` returns the still-`RUNNING` result with `timed_out=True` and signals
nothing: continue with `wait(run_id)`. `run()` and `resume()` behave the same
way — on an expired `timeout` they detach (`RUNNING`, `timed_out=True`, child
alive) instead of killing the run. Only an explicit `stop()` yields
`CANCELLED`. A run whose process vanished without being finalized is finalized
by the observer (no exit code is known then, so the terminal `result` event
alone decides `COMPLETED` vs `FAILED`; a `stop()` in the starter's process is
still reported `CANCELLED`). The grace before that is anchored on durable facts
(file mtimes, `stop_requested_at`), so even repeated `wait(run_id, 0)` calls
from a process without the `Popen` finalize it. A provider child that has already written its
terminal event but keeps the OS process alive is grace-killed after a short
period and finalized from that event alone (#55) — unless the pid's identity
can't be verified, in which case `wait()` never signals it and just keeps
waiting. `list_runs()` returns a `RunSummary` per stored
run, oldest first, reconciling orphaned `RUNNING` records the same way `poll()`
does. While a run is `RUNNING`, `poll()` and a timed-out `wait()` also report
`duration_s`, `event_count`, `last_event_at` and `last_activity` (see
`RunResult`). `wait` raises `HarnessError` for an unknown `run_id`. See
`docs/run-lifecycle.md`.

```python
from lib_python_harness import Harness, RunSpec, Isolation

harness = Harness()

result = harness.run(
    RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku")
)

record = harness.start(
    RunSpec(prompt="Count to one million, one number per line.",
            isolation=Isolation.CLEAN, model="haiku")
)
harness.poll(record.run_id)
observed = harness.wait(record.run_id, timeout=5.0)  # never cancels
print(observed.timed_out, [s.label for s in harness.list_runs()])
harness.stop(record.run_id, timeout=10.0)
harness.cleanup(record.run_id, remove_cwd=False)

follow_up = harness.resume(result.run_id, "Now reply with exactly DONE")
print(follow_up.session_id == result.session_id)
pending = harness.start_resume(result.run_id, "Reply with exactly LATER")  # returns at once
print(pending.state, harness.wait(pending.run_id, timeout=60).text)
```

### RunSpec

What to run, and under what isolation. `cwd=None` (the default) means "spawn
in a fresh, empty temp directory the harness creates" — this is what
actually makes the `Isolation.CLEAN` no-auto-memory guarantee hold, since
auto-memory is keyed by cwd and a directory the harness just created has
never had `claude` run in it. A caller-supplied `cwd` must exist, must not
sit inside (or under) a git repository, and must be empty unless
`allow_nonempty_cwd=True` is set — the one recorded opt-out, meant for
diagnostics that need to plant files into the child's cwd, not for everyday
use (an *emptied* directory can still map onto a project whose auto-memory
is populated, since memory lives outside the cwd itself). `label` is an
optional human-readable name stored on the run's record and shown by
`Harness.list_runs()` (the prompt itself is never stored, only its hash).

```python
from lib_python_harness import RunSpec, Isolation

spec = RunSpec(
    prompt="Reply with exactly OK",
    isolation=Isolation.CLEAN,
    model="haiku",
    effort="high",
    system_prompt="You are a terse assistant.",
    label="nightly-summary",
)
```

### RunResult

The result envelope `Harness.run`/`start`/`poll`/`stop`/`wait` all return.
Content fields (`text`, `is_error`, `subtype`, `structured_output`, `usage`,
`cost`) come from the CLI's own terminal `result` event; `session_id`,
`transcript_path`, `state` and `duration_s` are the harness's own
run-identity/lifecycle bookkeeping, which no single stream event carries.
On a terminal result `duration_s` runs from creation to the end of the run
(its terminal event / observed exit), not to whenever it was finalized.
`timed_out` is `True` only on a `Harness.wait`/`wait_for`/`run`/`resume`
result whose timeout expired while the run kept running (`state` stays
`RUNNING`; continue with `wait(run_id)` — a time limit never cancels). While
`state` is `RUNNING`, `duration_s` (seconds since the run was created),
`event_count` (complete lines in the run's `events.jsonl`), `last_event_at`
(that file's modification time, epoch seconds — how long ago the run last
wrote, i.e. working or hung) and `last_activity` (provider-derived label of
the newest event, e.g. `tool_use:Bash`, `text`, `tool_result`, `init`; `None`
for providers without one or an unrecognizable stream) show live progress; on
a terminal result `event_count` / `last_event_at` / `last_activity` are
`0` / `None` / `None`. `abandoned_background_tasks` names every
`run_in_background: true` task a `claude` run launched but never reported
finished (by notification, a poll, or `TaskStop`) before its own turn ended —
`()` for every other provider and every claude run that launched none —
and such a run ends `FAILED` rather than `COMPLETED` (see
`docs/run-lifecycle.md`).

```python
from lib_python_harness import Harness, RunSpec, Isolation, RunResult


def describe(result: RunResult) -> str:
    return (
        f"session={result.session_id} "
        f"transcript={result.transcript_path} "
        f"state={result.state} "
        f"duration_s={result.duration_s}"
    )


harness = Harness()
result = harness.run(
    RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku")
)
print(describe(result))
```

### RunSummary

One row of `Harness.list_runs()`: `run_id`, `state` (a `RunState`), `model`,
`cwd` (a `Path` or `None`), `created_at` (epoch seconds) and `label` (from
`RunSpec.label`, else `None`). Frozen. It deliberately carries no prompt, argv
or environment.

```python
from lib_python_harness import Harness, RunSummary

summaries: list[RunSummary] = Harness().list_runs()
```

### Isolation

The isolation profile a run is spawned under. Only `CLEAN` exists in this
ticket: no user/project settings, no plugins, no skills, no MCP servers, no
hooks, no CLAUDE.md, no auto-memory. A later ticket that wants a different
profile (e.g. one that keeps project settings) adds a new `Isolation`
member — it does not repurpose `CLEAN`.

```python
from lib_python_harness import RunSpec, Isolation

spec = RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku")
assert spec.isolation is Isolation.CLEAN
```

### RunState

The run lifecycle enum — not a free string. `CREATED`, `RUNNING`,
`COMPLETED`, `FAILED`, `CANCELLED`; an illegal transition (e.g. `stop()` on
an already-`COMPLETED` run) raises `IllegalTransitionError` rather than
silently no-op'ing. See `docs/run-lifecycle.md` for the full transition
table.

```python
from lib_python_harness import RunState


def is_finished(state: RunState) -> bool:
    return state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)


assert not is_finished(RunState.CREATED)
assert not is_finished(RunState.RUNNING)
assert is_finished(RunState.COMPLETED)
```

### HarnessError

The base class every error this library raises on purpose inherits from.
Catch this to handle any harness-originated failure without enumerating the
concrete subclasses (`IllegalTransitionError`, `RunIdentityUnverifiedError`,
`UnsafeCwdError`).

```python
from lib_python_harness import HarnessError

try:
    raise HarnessError("something the harness itself raised on purpose")
except HarnessError as exc:
    print(f"harness error: {exc}")
```

### IllegalTransitionError

Raised when a `RunState` transition falls outside the documented table —
most visibly, `Harness.stop()` on a run whose record is already
`COMPLETED`/`FAILED`/`CANCELLED`. This is a typed error, not a silent no-op:
the check is the first thing `stop()` does, before any signal is ever sent.

```python
from lib_python_harness import Harness, IllegalTransitionError

harness = Harness()
try:
    harness.stop("a-run-id-that-is-already-completed")
except IllegalTransitionError:
    print("that run already finished; stop() is not a no-op here")
```

### RunIdentityUnverifiedError

Raised by `Harness.stop()` when the recorded pid's identity cannot be
verified (no `psutil`, `/proc` unreadable, ...) *and* this process no longer
holds the child's own `Popen` — signalling would risk killing an unrelated
process that happens to have reused the pid, so `stop()` refuses instead of
guessing.

```python
from lib_python_harness import Harness, RunIdentityUnverifiedError

harness = Harness()
try:
    harness.stop("a-run-whose-process-identity-cannot-be-verified")
except RunIdentityUnverifiedError:
    print("refusing to signal a possibly-recycled pid")
```

### UnsafeCwdError

Raised by the `claude` provider when a caller-supplied `RunSpec.cwd` fails
the `Isolation.CLEAN` recipe: it does not exist, it sits inside (or under) a
git repository, or it is non-empty without `allow_nonempty_cwd=True`.

```python
from lib_python_harness import run, RunSpec, Isolation, UnsafeCwdError

try:
    run(RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="haiku", cwd="/some/git/repo"))
except UnsafeCwdError:
    print("cwd is inside a git repo, or not empty, or does not exist")
```

### __version__

The installed package version, read from the distribution metadata. When the
package is imported from a source tree without being installed, it is
`"0.0.0+unknown"`.

```python
import lib_python_harness

print(lib_python_harness.__version__)
```

### Provider

The seam a CLI adapter implements: `build_launch_plan(spec, *, session_id,
run_dir)` turns a `RunSpec` into a `LaunchPlan`, and `parse_events(lines)`
turns the adapter's own event stream into a `RunResult`;
`build_resume_plan(provider_argv=..., session_id=..., cwd=..., prompt=...)`
turns a finished run's recorded argv into a follow-up turn on the same session
(`Harness.resume`; `ClaudeCliProvider` implements it, `CodexCliProvider` and
`MistralCliProvider` raise `UnsupportedByProvider`). `ClaudeCliProvider`
`CodexCliProvider` and `MistralCliProvider` are the implementations;
`RunSpec.provider` selects one by name (`"claude"`, the default, `"codex"` or
`"mistral"`), and `Harness(provider=...)`
registers a custom instance under its own `name`. Each provider carries a
`name` and the `binary_argv` the harness prepends at spawn time.

```python
from pathlib import Path

from lib_python_harness import LaunchPlan, Provider, RunResult, RunSpec


def describe(provider: Provider, spec: RunSpec) -> LaunchPlan:
    return provider.build_launch_plan(spec, session_id="example-session", run_dir=Path("/tmp"))
```

### LaunchPlan

The fully-built command line a `Provider` wants spawned: `argv` (never
including the binary itself — `Harness.claude_argv`, else the provider's
`binary_argv`, is prepended at spawn time), `cwd`, `env`, and `stdin` (the prompt travels on stdin, never argv).

```python
from lib_python_harness import LaunchPlan

plan = LaunchPlan(argv=["-p", "--model", "haiku"], cwd="/tmp/example", env={}, stdin="hi")
print(plan.argv, plan.cwd)
```

### ClaudeCliProvider

The `Provider` implementation that drives the real `claude` CLI, enforcing
the `Isolation.CLEAN` recipe (scrubbed env, `--setting-sources ""`,
`--strict-mcp-config`, no tools, no slash commands) when it builds a
`LaunchPlan`.

**`model`/`effort` validation.** `build_launch_plan()` rejects an unusable
`model` or `effort` synchronously — before any run record, artifact
directory or child process exists — raising `UnsupportedByProvider` with a
message naming the acceptable values (see "UnsupportedByProvider" below).
`model` is checked by namespace, not by an exact list (the real CLI accepts
open-ended Bedrock/Vertex/gateway ids it never enumerates): a value is
accepted if it is one of the aliases `sonnet`, `opus`, `haiku`, `fable`,
`default`, `inherit` (case-insensitive), or if splitting it on any run of
non-alphanumeric characters yields at least one token in `claude`,
`anthropic`, `sonnet`, `opus`, `haiku`, `fable` — so `claude-opus-5[1m]` and
`anthropic.claude-3-5-sonnet-20241022-v2:0` are both accepted, while a typo
like `opuss` is rejected. An empty, absent, whitespace-only, or
leading-dash model is rejected the same way. `effort` is checked by exact
membership in `low`, `medium`, `high`, `xhigh`, `max` (the claude CLI's own
set; `effort=None` is always valid — the flag is simply not emitted).

```python
from pathlib import Path
import uuid

from lib_python_harness import ClaudeCliProvider, Isolation, RunSpec

provider = ClaudeCliProvider()
plan = provider.build_launch_plan(
    RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN, model="haiku"),
    session_id=str(uuid.uuid4()),
    run_dir=Path("/tmp/example-run"),
)
print(plan.argv)
```

**Harness runs get a different default tool set than native subagents
(#37).** An `Isolation.INHERIT` child process starts from the real `claude`
CLI's own entrypoint default session tool set — a broad set that, measured
against a plain `claude -p` session's own `init` event (`--model haiku
--output-format stream-json --verbose`, no `--tools`, `claude` v2.1.278),
includes `ListAgents`, `ReportFindings`, `ScheduleWakeup`, and the
`Cron*`/`Task*`/`RemoteTrigger`/`PushNotification` families — tools a
*dispatched* subagent (native or through this library) is not normally
meant to have, since dispatching further agents, filing findings, or
scheduling wakeups is ordinarily the parent session's job, not a
subagent's. This library does not itself reference any of those seven
names anywhere under `src/` (grep-verified: `ListAgents|ReportFindings|
ScheduleWakeup|CronCreate|TaskCreate|RemoteTrigger|PushNotification` over
`src/` returns 0 matches), so narrowing them away cannot break a feature
here — their presence on an unnarrowed run comes from the CLI's own
entrypoint default, before this library contributes anything, not from
this library depending on them.

Where a dispatched definition's `tools:` frontmatter is set, `resolve()`
puts it on `RunSpec.tools`, and `_build_inherit_plan` now emits it as a
top-level `--tools` session allowlist (comma-joined, reusing the same
`_split_tools` normalizer as the `--agents` payload), in addition to
whatever agent-scope carrier (`--agents` JSON `tools` array, or the
materialized file's own `tools:` line) the dispatch mode already uses —
one carrier per value would be simpler, but the session-level allowlist
and the agent-scope declaration answer different questions to the CLI, so
both are emitted. The same probe verified: `--tools Read,Glob` narrows the
child's own `init` event `tools` key to exactly `Read`/`Glob` plus a
residue of `mcp__*` MCP-server tool names — never any of the seven
forbidden names — so an exact allowlist genuinely narrows the session, not
just the agent scope.

**Scope — what this proves and does not.** The `init` event's `tools` key
enumerates both directly-callable tools (`ListAgents`, `ReportFindings`,
`ScheduleWakeup` observed directly in the probe's plain-session list) and
the deferred families (`Cron*`, `Task*`, `RemoteTrigger`,
`PushNotification`, also observed present in the same probed list), so a
run's own `events.jsonl` is evidence for both halves of the symptom, not
only the directly-callable one.

**One deliberate, documented deviation** — a run is *not* narrowed by
this mechanism in exactly this case:

- **No `tools:` at all.** A definition that never sets `tools:` gets no
  top-level `--tools` flag; the child keeps the entrypoint's full default
  set (the same asymmetry CLEAN's own `tools` row above documents).

A definition that sets `disallowedTools:` (with or without `tools:`) *is*
narrowed at the session level too: `resolve()` puts it on
`RunSpec.disallowed_tools`, and both `_build_clean_plan` and
`_build_inherit_plan` now emit it as a top-level `--disallowedTools`
session denylist (comma-joined, same `_split_tools` normalizer), in
addition to whatever agent-scope carrier (`--agents` JSON
`disallowedTools` array, or the materialized file's own `disallowedTools:`
line) the dispatch mode already uses.

### CodexCliProvider

The `Provider` implementation that drives the OpenAI `codex` CLI
(`codex exec --json`), selected with `RunSpec(provider="codex")` (the default
is `"claude"`; an unknown name raises `HarnessError` listing the known ones).
It supports `Isolation.CLEAN` only and emits
`exec --json --ephemeral --ignore-user-config --ignore-rules
--skip-git-repo-check -s read-only -c project_doc_max_bytes=0` (the last pair
is what keeps a planted `AGENTS.md` in the run's cwd from being obeyed), plus `-m <model>`,
`-c model_reasoning_effort=<effort>` when `effort` is set and
`--output-schema <run_dir>/output-schema.json` when `json_schema` is set. The
prompt travels on stdin. `codex` always reads `$CODEX_HOME/AGENTS.md` and no
flag disables that, so each CLEAN run gets a **scrubbed `CODEX_HOME`**: a
private per-run temp dir (mode 0700 where the OS supports it, never under the
artifacts dir) containing only a copy of `auth.json` from the caller's real
home (`$CODEX_HOME`, else `~/.codex`). `Harness` deletes it when the run
completes, fails, is cancelled by `stop()`, fails to spawn, or on `cleanup()`
(via `LaunchPlan.cleanup_paths`). If the real home has no `auth.json` (e.g.
API-key setups), the scrubbed home is simply empty; `OPENAI_API_KEY` etc. are
scrubbed too, so such a run has no credential and will fail to authenticate.
`provenance.json` records `"scrubbed_home": true` only, never paths or
credential contents. `OPENAI_API_KEY`, `OPENAI_BASE_URL` and
`OPENAI_ORGANIZATION` are scrubbed. `codex exec` has no
approval flag (its policy is effectively "never"), so none is emitted. Cost is
`None` (codex reports tokens, not dollars). `provenance.json` records
`provider` and `cli_version` (`claude_version` stays as an alias).

The `thread_id` of the stream is reported as `RunResult.session_id`, but a
CLEAN run is `--ephemeral`: `codex exec resume <thread_id>` fails with "no
rollout found", so that id is **not resumable** (`Harness.resume` raises
`UnsupportedByProvider` for a codex run).

```python
from pathlib import Path
import uuid

from lib_python_harness import CodexCliProvider, Isolation, RunSpec

provider = CodexCliProvider()
plan = provider.build_launch_plan(
    RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN,
            model="gpt-5.6-luna", provider="codex"),
    session_id=str(uuid.uuid4()),
    run_dir=Path("/tmp/example-run"),
)
print(plan.argv)
```

Use one provider instance per run (`Harness` does): whether a `json_schema`
was requested — which decides `structured_output` — is remembered from
`build_launch_plan` when `parse_events` runs.

**`model`/`effort` validation.** Same synchronous check as
`ClaudeCliProvider` (see above), with codex's own namespace: `model` has no
CLI-side alias, so it is accepted only via the family-token rule, against
`gpt`, `o3`, `o4`, `codex`, `openai` (e.g. `gpt-5.6-luna`). `effort` is
checked against codex's own closed set — `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max` — which includes `none`/`minimal`, values
the claude CLI does not accept.

On native Windows `codex` is an npm `.cmd` shim: the harness resolves it via
`PATH`/`PATHEXT` and `stop()` kills the whole process tree (`taskkill /T /F`).

**RunSpec field -> Codex flag mapping**

| `RunSpec` field | Codex |
| --- | --- |
| `prompt` | stdin |
| `model` | `-m <model>` |
| `effort` | `-c model_reasoning_effort=<effort>` |
| `json_schema` | `--output-schema <run_dir>/output-schema.json` |
| `cwd` / `allow_nonempty_cwd` | same CLEAN cwd recipe as Claude (spawn cwd) |
| `isolation=CLEAN` | the flag set above |
| `isolation=INHERIT` | unsupported: raises `UnsupportedByProvider` |
| `permission_mode`, `tools`, `disallowed_tools`, `skills`, `max_turns`, `hooks`, `omit_claude_md`, `agent_name`, `setting_sources`, `strict_mcp`, `memory`, `system_prompt` | unsupported: raise `UnsupportedByProvider` |
| `mcp_servers` | unsupported: raises `UnsupportedByProvider` (known limitation, see below) |
| `description` | silently unsupported: ignored, never an error |

**Global instructions.** `codex` unconditionally reads
`$CODEX_HOME/AGENTS.md`; because the CLEAN run's `CODEX_HOME` is the scrubbed
per-run dir described above (auth only), the user's own `AGENTS.md`,
`config.toml`, rules and MCP servers are not reachable.

`mcp_servers` limitation: a live survey showed `-c mcp_servers.<name>.command=...`
/ `.args=[...]` can register a server under `--ignore-user-config`, but a call
to it then fails with "MCP tool call requires approval, but approval policy is
never" unless `-c mcp_servers.<name>.default_tools_approval_mode="approve"` is
also passed. That approval-mode override widens what the child may do, so this
release keeps `mcp_servers` unsupported rather than emit it.

### MistralCliProvider

The `Provider` implementation that drives the Mistral Vibe CLI (`vibe`,
surveyed against 2.25.4), selected with `RunSpec(provider="mistral")`. Like
`CodexCliProvider` it supports `Isolation.CLEAN` only; `Isolation.INHERIT`
raises `UnsupportedByProvider` before anything is spawned.

The CLEAN recipe is `vibe -p --output streaming --enabled-tools
__harness_no_tools__`, with the prompt on stdin. In `-p` mode `--enabled-tools
NAME` disables every other tool, so a name that matches nothing switches all
tools (built-in, MCP and connector tools) off. `vibe` has no `--model` flag:
`RunSpec.model` is passed as `VIBE_ACTIVE_MODEL` (a config alias such as
`mistral-medium-3.5`; an unknown alias makes Vibe exit 1, so the run is
`FAILED`). `--trust` is deliberately omitted: an untrusted workdir makes Vibe
ignore project config, which is the stronger isolation.

`Harness.resume` is unsupported for Mistral runs (`UnsupportedByProvider`).

**`model` validation.** Same synchronous namespace check as the other two
providers (see "ClaudeCliProvider" above): `vibe` has no CLI-side alias
either, so `model` is accepted only via the family-token rule, against
`mistral`, `magistral`, `ministral`, `devstral`, `codestral`, `pixtral`
(e.g. `mistral-medium-3.5`). Mistral has no `effort` concept — `effort` is
already rejected outright as an unsupported *field* (see the mapping table
below), so there is no separate value check for it.

Isolation: `VIBE_HOME` relocates config, `AGENTS.md`, skills, agents, plugins,
hooks, `.env`, MCP servers and session logs, so each run gets a fresh empty
private `VIBE_HOME` (deleted by `Harness` when the run ends) and every
`VIBE_*` variable of the caller is scrubbed. `VIBE_ENABLE_CONNECTORS=false`
closes the account-side connectors, which a fresh home cannot reach. Auth is
`MISTRAL_API_KEY`, else the OS keyring (`vibe --setup` stores it there,
outside `VIBE_HOME`), so `MISTRAL_API_KEY` is preserved and no credential is
copied. Without a key, `-p` mode prints to stderr and exits 1.

```python
from lib_python_harness import Harness, Isolation, MistralCliProvider, RunSpec

# provider="mistral" selects MistralCliProvider from the registry; passing an
# instance to Harness registers it explicitly under its own name.
harness = Harness(provider=MistralCliProvider())
result = harness.run(
    RunSpec(prompt="Reply with exactly OK", isolation=Isolation.CLEAN,
            model="mistral-medium-3.5", provider="mistral"))
print(result.text)  # "OK"; needs vibe on PATH + MISTRAL_API_KEY or keyring
```

Field mapping:

| `RunSpec` field | Mistral behaviour |
| --- | --- |
| `prompt` | stdin |
| `model` | `VIBE_ACTIVE_MODEL` env var |
| `max_turns` | `--max-turns N` |
| `cwd` | fresh empty dir (or an empty caller dir) |
| `description` | silently ignored |
| `effort`, `json_schema`, `system_prompt`, `permission_mode`, `tools`, `disallowed_tools`, `skills`, `hooks`, `mcp_servers`, `omit_claude_md`, `agent_name`, `setting_sources`, `strict_mcp`, `memory` | `UnsupportedByProvider` |

`--output streaming` writes one JSON history entry per line; the result text
is the last completed assistant `message`, `session_id` is its `sessionId`.
The stream carries no token or cost totals, so `RunResult.usage` is `{}` and
`cost` is `None`. A stream with no completed assistant message is treated as
truncated. The events file is read as UTF-8 (Vibe does not ASCII-escape JSON).

### UnsupportedByProvider

A `HarnessError` raised by a provider's `build_launch_plan()` — before
anything is spawned or recorded — in two situations:

- the `RunSpec` sets a **field** that provider cannot honour at all (the
  message names every offending field at once); or
- the `RunSpec` sets a **`model` or `effort` value** the provider cannot
  honour (the message names the field, the rejected value, and the
  acceptable values/namespace for that provider — see each provider's
  section above for its exact rule). This closes two symptoms an unchecked
  typo used to produce: an unknown `model` used to return `RUNNING`
  immediately and only fail seconds later, asynchronously, with a message
  that referenced the CLI's own `--model` flag rather than the caller's
  `RunSpec`; an unknown `effort` used to be silently accepted (or silently
  dropped, depending on the CLI) with no indication the run had used a
  different effort than requested.

The rule is the same whether the field/value came from the caller or from
`.seretos/harness.yml`.

```python
from lib_python_harness import Isolation, RunSpec, UnsupportedByProvider, run

try:
    run(RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="gpt-5.6-luna",
                provider="codex", permission_mode="plan"))
except UnsupportedByProvider as exc:
    print(f"codex cannot do that: {exc}")

try:
    run(RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="opuss"))
except UnsupportedByProvider as exc:
    print(f"unusable model: {exc}")
```

### RunStore

The protocol `Harness` uses to persist a run's *record* (metadata — state,
pid, paths — not its artifacts, which always land on real disk regardless of
store choice). `Harness(store=...)` takes any `RunStore`; `InMemoryRunStore`
(the default) and `FileRunStore` are the two implementations this release
ships.

```python
from lib_python_harness import InMemoryRunStore, RunStore


def run_count(store: RunStore) -> int:
    return len(store.list())


print(run_count(InMemoryRunStore()))
```

### InMemoryRunStore

The default `RunStore`: no disk I/O, records live only as long as the
process does. What a plain `Harness()` call uses so it never leaves a stray
`record.json` around, while still writing a real `provenance.json` file.

```python
from lib_python_harness import InMemoryRunStore

store = InMemoryRunStore()
store.put("run-1", {"state": "RUNNING"})
print(store.get("run-1"))
```

### FileRunStore

A `RunStore` that persists each run's record to
`<artifacts_dir>/<run_id>/record.json`, needed when a record living only in
one process's memory cannot satisfy "the run is inspectable across process
boundaries".

```python
import tempfile

from lib_python_harness import FileRunStore

store = FileRunStore(tempfile.mkdtemp())
store.put("run-1", {"state": "RUNNING"})
print(store.list())
```

## Agent dispatch (Isolation.INHERIT)

Ticket #2's surface: discover every Claude Code subagent definition
available in this session (project `.claude/agents/*.md`, user
`<config>/agents/*.md`, enabled-plugin `agents/*.md`), and dispatch one as a
child `claude` process under `Isolation.INHERIT` — the parent's own cwd,
CLAUDE.md discovery, settings, permission mode and MCP servers, plus the
definition's own fields.

```python
from lib_python_harness import HostContext, discover, resolve, run

context = HostContext(cwd="/path/to/project")
context.complete()

definitions = discover(context)
definition = definitions["reviewer"]

spec = resolve(definition, context)
result = run(spec)
print(result.text)
```

### AgentDefinition

One subagent, normalized from its `.md` frontmatter: the documented
camelCase fields as snake_case attributes, plus `body` (everything after
the frontmatter fence), `source_scope` (`"project"` | `"user"` |
`"plugin"`), `path`, and `qualified_name` (`name` at project/user scope,
`f"{plugin}:{name}"` at plugin scope — the key `discover()` returns).

```python
from pathlib import Path

from lib_python_harness import AgentDefinition

definition = AgentDefinition(
    name="reviewer",
    description="Reviews code",
    body="You are a careful code reviewer.",
    source_scope="project",
    path=Path("/repo/.claude/agents/reviewer.md"),
    qualified_name="reviewer",
)
print(definition.qualified_name)
```

### DefinitionSource

The protocol `discover()` walks: anything with an `iter_definitions()`
method yielding `AgentDefinition`s. `ClaudeMarkdownSource` is the only
implementation this release ships.

```python
from lib_python_harness import AgentDefinition, DefinitionSource


def count_definitions(source: DefinitionSource) -> int:
    return len(list(source.iter_definitions()))
```

### ClaudeMarkdownSource

Walks one directory (non-recursively, sorted) for `*.md` agent-definition
files. `discover()` builds one per scope (project/user/each enabled
plugin); a missing directory yields nothing, not an error.

```python
from lib_python_harness import ClaudeMarkdownSource

source = ClaudeMarkdownSource("/repo/.claude/agents", scope="project")
for definition in source.iter_definitions():
    print(definition.qualified_name)
```

### HostContext

A snapshot of the parent Claude Code session: `cwd`, `model`,
`permission_mode`, `effort`, `mcp_servers`, `enabled_plugins`.
`complete()` fills `model` (from the session transcript's last assistant
turn) and `enabled_plugins` (from the merged settings files) when unset —
fields already set are never overwritten. `mcp_servers` is caller-supplied
only; `complete()` never populates it (collecting the parent's actually
active MCP servers is a later ticket's job).

```python
from lib_python_harness import HostContext

context = HostContext(cwd="/repo", session_id="example-session-id")
context.complete()
print(context.model, context.enabled_plugins)
```

### discover

Every subagent Claude Code would offer in this session, keyed by
`qualified_name` — project, then user, then each enabled plugin, first
writer of a name wins (so a project agent shadows a same-named user or
plugin one).

```python
from lib_python_harness import HostContext, discover

context = HostContext(cwd="/repo")
for qualified_name, definition in discover(context).items():
    print(qualified_name, definition.description)
```

### resolve

Turns one `AgentDefinition` plus a `HostContext` into a `RunSpec` under
`Isolation.INHERIT`, once: `model`/`permission_mode`/`effort` are
definition-else-context; `tools`/`disallowed_tools`/`skills`/`max_turns`/
`description` always come from the definition; `omit_claude_md`/`hooks`/
`mcp_servers` come from the definition too, but are dropped at plugin scope
(a documented assumption about how the parent Claude Code loads plugin
agents, not independently verified). The returned `RunSpec` is ready for
`run()`.

`resolve(definition, context, config=None, *, task=None)`: `task` is
keyword-only. Without it the definition's body is both the run's instructions
and its user message (`prompt == system_prompt == body`). With `task` set,
`RunSpec.prompt` is the task and the body travels only as the agent's
system prompt (the `--agents` `prompt` / materialized `.md` body), so one
definition can be dispatched for varying jobs and the body no longer reaches
the child twice. `task=""` is a task, not "unset".

```python
spec = resolve(definition, context, task="Review src/app.py")
```

```python
from lib_python_harness import HostContext, discover, resolve

context = HostContext(cwd="/repo")
context.complete()
definition = next(iter(discover(context).values()))
spec = resolve(definition, context)
print(spec.agent_name, spec.isolation)
```

#### Frontmatter field → dispatch carrier

`resolve()`'s `RunSpec` is emitted one of two ways
(`providers.claude_cli.dispatch_mode`, whole-definition, never per-field):
**payload** (`--agents '{"<name>": {...}}'` + `--agent <name>`) when every
field the definition sets fits the `--agents` JSON schema; **materialized**
(`--add-dir <dir>` + `--agent <stem>`, a real `.md` file with full
frontmatter under `<dir>/.claude/agents/<stem>.md`) the moment one field
does not (verified against the real CLI: `hooks`/`mcpServers` are always
rejected by the JSON schema, and `tools`/`disallowedTools` need a JSON
array rather than the scalar frontmatter carries — both cases still work,
just through the materialized path or a converted array).

| Frontmatter field | `payload` carrier            | `materialized` carrier | Notes                                   |
| ------------------ | ----------------------------- | ----------------------- | ---------------------------------------- |
| `description`       | `--agents` `description`      | `description:`           | never dropped/defaulted by `resolve()` — the real frontmatter value flows through unchanged; `payload` mode simply omits the JSON key when the definition has none, while `materialized` mode substitutes a synthesized filler in that case (its own loader silently drops a description-less file from discovery, live-verified) |
| `body` (with `task`) | `--agents` `prompt`           | the file's body          | the definition body always travels here (never on stdin when `task` is given; `task` is the user message on stdin) |
| `model`             | top-level `--model`           | `model:`                 | `model: inherit` passes through literally |
| `permissionMode`    | top-level `--permission-mode` | `permissionMode:`        | dropped at plugin scope                  |
| `effort`            | top-level `--effort`          | (not carried)            | definition-else-context                  |
| `tools`             | `--agents` `tools` (as array) | `tools:` (scalar)        | never a top-level `--allowedTools`; *also* becomes a top-level `--tools` session allowlist (comma-joined), independent of `payload`/`materialized` — see "Harness runs get a different default tool set than native subagents" below |
| `disallowedTools`   | `--agents` `disallowedTools`  | `disallowedTools:`       | *also* a top-level `--disallowedTools` (comma-joined), independent of `payload`/`materialized` |
| `skills`            | `--agents` `skills`           | `skills:`                |                                           |
| `maxTurns`          | `--agents` `maxTurns`         | `maxTurns:`              | never a top-level `--max-turns` (no such flag) |
| `hooks`             | (forces `materialized`)       | `hooks:`                 | dropped at plugin scope                  |
| `mcpServers`        | (forces `materialized`)       | `mcpServers:`            | dropped at plugin scope; also drives top-level `--mcp-config` when non-empty |
| `omitClaudeMd`      | top-level `--setting-sources` | `omitClaudeMd:`          | drops `project` from `--setting-sources` (`user,local` instead of `user,project,local`); live-verified — no `--settings instructionFiles` key has any observable effect on CLAUDE.md loading against the real CLI; dropped at plugin scope. Known trade-off: also drops project-level `.claude/settings.json`, since "project" carries both |
| `background`, `color`, `initialPrompt`, `isolation`, `memory` | not carried | not carried | parsed by the frontmatter reader, silently dropped by `load_agent_definition` — a *frontmatter* `memory`/`isolation` never reaches the `RunSpec`; both are settable only through a project's `.seretos/harness.yml` (see *Project overrides*) |

### FrontmatterError

Raised by the hand-rolled frontmatter parser when a `.md` file's `---`
header is genuinely malformed (e.g. an opening fence with no closing
fence) — never for a file that merely omits recognised fields, which loads
with a fallback description instead.

```python
from lib_python_harness import FrontmatterError

try:
    raise FrontmatterError("frontmatter fence not closed")
except FrontmatterError as exc:
    print(f"malformed agent definition: {exc}")
```

## Project overrides (`.seretos/harness.yml`)

A project can retune any discovered agent by qualified name, without
forking the plugin that ships it. Files are layered `~/.seretos/harness.yml`
-> outer repos -> the inner repo (inner wins; `harness.yaml` is read too);
layering and merging come from `lib-python-config`, validation is a strict
pydantic model (an unknown key is a `ConfigError`, never a silent no-op).

```yaml
defaults:
  isolation: inherit            # inherit | clean | <named profile>
agents:
  some-plugin:reviewer:
    provider: claude            # later: codex, mistral, api, lmstudio
    model: fable                # plugin says opus; here it runs on fable
    isolation: clean            # nothing inherited, no memories, no user settings
    permissionMode: dontAsk
    tools: { remove: [Bash], add: [WebFetch] }
    mcpServers: { add: [my-extra-mcp], remove: [serena] }   # child gets MCPs the parent lacks
    canSpawn: false             # the harness dispatch tool is not in this agent's tool set
  my-project:planner:
    canSpawn: true              # nesting depth is set by these flags, never by a global limit
profiles:
  clean-with-git:               # named isolation profiles between inherit and clean
    settingSources: []
    strictMcp: true
    tools: [Bash, Read, Grep]
    omitClaudeMd: true
    memory: false
```

**Resolution order for one field:** the file's `agents:` entry > the file's
`defaults:` > the agent definition's frontmatter > the host context. A
value the file states also overrides `resolve()`'s plugin-scope drop of
`permissionMode`/`mcpServers`/`omitClaudeMd` (that drop is about a plugin's
own frontmatter, not project policy). Later layers of the file itself
replace earlier ones key by key; lists are replaced, never concatenated
across layers - use `{add, remove}` to compose. An agent that no layer
mentions and that no `defaults:` key concerns is resolved exactly as
without a config.

Semantics worth knowing:

- **Lists.** `tools`/`disallowedTools` accept a plain list (replace) or
  `{add, remove}` (`remove` first, then `add`, no duplicates), applied to
  **the definition's own list**, never the host's: a definition without
  `tools:` stays without after `remove: [Bash]`. `mcpServers` takes
  `{add, remove}` and patches the set `resolve()` chose (the definition's,
  else `HostContext.mcp_servers`); a bare name in `add` is looked up in the
  caller-supplied `HostContext.available_mcp_servers`, and a name missing
  there is a `ConfigError`. The file carries no commands or secrets.
- **`isolation`.** `inherit`, `clean`, or a profile name from `profiles:`
  (unknown -> `ConfigError`; a profile from `~/.seretos` is usable from a
  project). `clean` runs in a fresh temp directory (never the parent's
  cwd) and carries only what the file states: `model`, `permissionMode`, a
  `--tools` allowlist and an `--mcp-config` set (its base is empty). A
  clean child has no `--agents`/`--agent` binding; a stated
  `disallowedTools` is still carried, as a top-level `--disallowedTools`.
  A named profile is `inherit` plus its fields: `settingSources` -> `--setting-sources`
  (`[]` emits the flag with an empty operand), `strictMcp`, `omitClaudeMd`,
  and `memory: false`. `tools` is *not* profile-specific: any INHERIT
  dispatch whose resolved `RunSpec.tools` is set — profile, plain agent
  frontmatter, or a bare `resolve()` call alike — gets the same top-level
  `--tools` session allowlist (see "Harness runs get a different default
  tool set than native subagents" below); a profile's own `tools:` is just
  one more way to set that same field.
- **`memory: false`** gives the run a fresh empty working directory (auto
  memory is keyed by cwd, so a directory `claude` has never run in has none
  to load) and adds the original cwd with `--add-dir`, so the project's
  files stay reachable. `memory: true` or unset changes nothing.
- **`canSpawn`** (per agent only; `defaults.canSpawn` is rejected - nesting
  is opt-in, never a blanket default) defaults to `false`: the dispatch
  server, named by `HostContext.dispatch_mcp_server_name`, is removed from
  the child's `--mcp-config`. `true` requires that name to be set and
  present in `available_mcp_servers`, and adds it. To make "absent from
  `--mcp-config`" mean "unreachable", every config-driven run emits
  `--strict-mcp-config`; a profile may opt out with `strictMcp: false`, but
  `canSpawn: false` is then unenforceable (inherited settings can still
  reach the server). Consequence: a config-driven run *computes* its MCP set,
  so the caller must supply `HostContext.mcp_servers` (the parent's active
  servers) to keep them - `HostContext.complete()` never collects it. With
  no config at all nothing changes: the unconfigured command line is
  byte-identical, so `canSpawn` cannot be enforced there.
- **`provider`** is validated against `claude` only for now.

### HarnessConfig

The validated result of layering every `.seretos/harness.yml`: `defaults`,
`agents` (qualified name -> override) and `profiles`. Pass it to
`resolve(definition, context, config=config)`; `config=None` (the default)
is byte-identical to a build without this feature.

```python
from lib_python_harness import HarnessConfig, HostContext, discover, resolve

context = HostContext(cwd="/repo")
config: HarnessConfig | None = None  # from load_harness_config(...)
for definition in discover(context).values():
    spec = resolve(definition, context, config=config)
    print(spec.agent_name, spec.model, spec.isolation)
```

### load_harness_config

`load_harness_config(cwd, *, home_default=True)` returns the merged
`HarnessConfig`, or `None` when no `.seretos/harness.yml` exists anywhere
(an existing but empty file yields an empty config, which changes nothing).
Each layer is validated on its own first, so an error names the file it came
from. `home_default=False` ignores `~/.seretos`.

```python
from lib_python_harness import load_harness_config

config = load_harness_config("/repo")
if config is not None:
    print(sorted(config.agents), sorted(config.profiles))
```

### ConfigError

A `HarnessError` raised for an unreadable or invalid layer (malformed YAML,
an unknown key such as `modle`, an unsupported `provider`), naming the file,
the qualified agent (or profile) and the offending key; and by `resolve()`
for a config value that cannot be applied (undefined profile, `add` of an
unknown MCP server, `canSpawn: true` without a dispatch server).

```python
from lib_python_harness import ConfigError, load_harness_config

try:
    config = load_harness_config("/repo")
except ConfigError as exc:
    print(f"bad .seretos/harness.yml: {exc}")
```

## Development

```bash
pip install -e ".[test]"
python -m pytest

# live tests: needs the installed `claude` CLI + subscription auth
python -m pytest -m requires_claude
# only the task hand-over probe (`resolve(..., task="abc")` must reply `cba`)
python -m pytest -m requires_claude -k task -q -s

# live tests: needs the installed `codex` CLI + ChatGPT/API auth
# (model: HARNESS_CODEX_MODEL, default gpt-5.6-luna)
python -m pytest -m requires_codex -q -s

# live tests: needs the installed `vibe` CLI + Mistral auth
# (model: HARNESS_MISTRAL_MODEL, default mistral-medium-3.5)
python -m pytest -m requires_mistral -q -s
```

`requires_codex` and `requires_mistral` tests never run in the default suite
(`addopts` excludes `requires_claude`, `requires_codex` and `requires_mistral`).

## Version policy

Semantic versioning. The `version` in `pyproject.toml` is a placeholder
on `main` — the release workflow stamps it onto the `release/Nx` branch
and the `vX.Y.Z` tag. Don't hand-bump it. `__version__` is derived from the
installed distribution's metadata and is never hand-edited.
