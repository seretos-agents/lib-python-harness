"""Provider-independent types: `Isolation`, `RunSpec`, `LaunchPlan`,
`RunResult`, and the `Provider` protocol every CLI adapter implements.

`ClaudeCliProvider` (`providers.claude_cli`), `CodexCliProvider`
(`providers.codex_cli`) and `MistralCliProvider` (`providers.mistral_cli`)
are the implementations; `RunSpec.provider` picks one
by name. A `Provider` carries its `name`, the `binary_argv` the harness
prepends at spawn time, and three responsibilities:

- `build_launch_plan(spec, session_id=..., run_dir=...)` — turn a `RunSpec`
  into an argv/cwd/env/stdin `LaunchPlan`, enforcing whatever isolation
  recipe its `Isolation` member implies.
- `parse_events(lines)` — turn the provider's own event stream (one JSON
  object per line for `ClaudeCliProvider`) into a `RunResult`.
- `build_resume_plan(provider_argv=..., session_id=..., cwd=..., prompt=...)`
  — turn a finished run's recorded argv into the plan for a follow-up turn on
  the same session (`Harness.resume`), replaying its isolation flags. A
  provider whose CLI cannot do that raises `UnsupportedByProvider`; an
  injected provider without the method is refused the same way.

What a further (e.g. Mistral) provider is explicitly *not* required to add to
this protocol (named per the plan-critic note that a blanket "anything else
is open" sentence documents no boundary at all):

- streaming/partial-output callbacks mid-run (`parse_events` is a one-shot,
  whole-stream parse; a provider may still emit partial events to disk, but
  nothing in this protocol subscribes to them as they arrive);
- non-JSON-lines event formats (`parse_events` takes `Iterable[str]`, one
  JSON object per line — a provider whose CLI emits something else must
  translate to that shape itself, outside this protocol);
- tool/plugin/MCP passthrough configuration (out of scope for `Isolation.CLEAN`
  by definition; a future non-CLEAN isolation profile would need a new
  `Isolation` member, not a `Provider` method);
- multi-turn conversation assembly (`RunSpec` carries exactly one `prompt`
  per call; a follow-up turn is a separate run, see `Harness.resume`).
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from ..errors import UnsupportedByProvider


class Isolation(enum.Enum):
    """The isolation profile a run is spawned under.

    `CLEAN`: no user/project settings, no plugins, no skills, no MCP
    servers, no hooks, no CLAUDE.md, no auto-memory.

    `INHERIT`: the opposite recipe — the child runs with the parent's own
    cwd, CLAUDE.md discovery, settings (`--setting-sources
    user,project,local`), permission mode and MCP servers, plus whichever of
    the 10 agent-definition fields (`permission_mode`, `tools`,
    `disallowed_tools`, `skills`, `max_turns`, `hooks`, `mcp_servers`,
    `omit_claude_md`, `agent_name`, `description`) the caller set on the
    `RunSpec` — letting a plugin-authored Claude Code subagent run through
    the harness at all. Added by ticket #2 for exactly this purpose; a
    further profile is a later ticket's addition, not this one's.
    """

    CLEAN = "clean"
    INHERIT = "inherit"


@dataclass
class RunSpec:
    """What a caller wants run, and under what isolation.

    `cwd=None` means "spawn in a fresh, empty temp directory the harness
    creates" — the default, and the only way the `CLEAN` no-auto-memory
    guarantee is actually enforced (memory is keyed by cwd; a directory the
    harness just created has never had `claude` run in it, so it has no
    memory to load). A caller-supplied `cwd` must exist, must not sit inside
    (or under) a git repository, and must be empty unless
    `allow_nonempty_cwd=True` — the one recorded opt-out, meant for
    diagnostics/probes that need to plant files into the child's cwd.
    """

    prompt: str
    isolation: Isolation
    model: str
    provider: str = "claude"
    effort: str | None = None
    system_prompt: str | None = None
    json_schema: dict[str, Any] | None = None
    cwd: str | Path | None = None
    allow_nonempty_cwd: bool = False
    artifacts_dir: str | Path | None = None
    timeout: float | None = None
    label: str | None = None

    # -- Isolation.INHERIT only (all None-defaulted -> CLEAN's argv is
    # unaffected by their mere presence on the dataclass). Each one is a
    # distinct Claude Code agent-definition frontmatter field the ticket's
    # goal sentence names; `resolve()` (agents/model.AgentDefinition ->
    # RunSpec) is the one place that fills them in from a real definition,
    # but any caller may set them directly (as the tests here do).
    permission_mode: str | None = None
    tools: str | None = None
    disallowed_tools: str | None = None
    skills: list[str] | None = None
    max_turns: int | None = None
    hooks: dict[str, Any] | None = None
    mcp_servers: dict[str, Any] | None = None
    omit_claude_md: bool | None = None
    agent_name: str | None = None
    description: str | None = None

    # -- config-driven (ticket #3): filled by `config.apply.apply_config`
    # from `.seretos/harness.yml`; `None` everywhere = today's behaviour.
    # `setting_sources` replaces `--setting-sources`' value; `strict_mcp`
    # makes INHERIT emit `--strict-mcp-config`; `memory=False` gives an
    # INHERIT run a fresh cwd (no project memory) plus `--add-dir` of the
    # original one.
    setting_sources: list[str] | None = None
    strict_mcp: bool | None = None
    memory: bool | None = None

    # -- Claude-only (#61): how long (ms) a headless `claude -p` child waits for
    # its `run_in_background` tasks before giving up; set as
    # `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` in the child env. `None` = the
    # provider's default (one hour); `0` is passed through verbatim. Other
    # providers ignore it.
    background_wait_ceiling_ms: int | None = None


@dataclass(frozen=True)
class LaunchPlan:
    """The fully-built command line a `Provider` wants spawned.

    `argv` never includes the binary itself — `Harness.claude_argv` (default
    `None`, an override for tests/alternate installs) or else the provider's
    own `binary_argv` is prepended by the harness at spawn time. The prompt travels on `stdin`, never `argv`.
    """

    argv: list[str]
    cwd: str
    env: dict[str, str]
    stdin: str
    # Paths the provider created for this run alone (e.g. a scrubbed private
    # home holding a credential copy). `Harness` removes them when the run
    # reaches a terminal state (completed/failed/cancelled/spawn-failed) and
    # on `cleanup()`. Never placed under the artifacts dir.
    cleanup_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """The result envelope. `Provider.parse_events()` fills the content
    fields (`text`, `is_error`, `subtype`, `structured_output`, `usage`,
    `cost`, `session_id`); `Harness` fills the run-identity/lifecycle fields
    (`run_id`, `transcript_path`, `state`, `duration_s`) that no single
    event in the stream carries.
    """

    run_id: str | None = None
    text: str = ""
    is_error: bool = False
    subtype: str | None = None
    structured_output: Any | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None
    session_id: str | None = None
    transcript_path: Path | None = None
    state: Any | None = None
    duration_s: float | None = None
    timed_out: bool = False
    event_count: int = 0
    last_event_at: float | None = None
    last_activity: str | None = None
    # #42: tool_use ids of every `run_in_background: true` launch the stream
    # never reported finished -- by a `<tool-use-id>` notification, a poll
    # (`TaskOutput`) whose `<status>` turned non-"running", or a `TaskStop`
    # on the same task id. Populated by `ClaudeCliProvider.parse_events`;
    # `Harness._finalize` fails a run that ends with any of these still
    # pending, so the deferred/real answer is never silently discarded in
    # favour of a stale interim `text`. `()` for every provider/run that
    # never launches a background task (codex, mistral, and any claude run
    # with no `run_in_background` tool_use keep this default untouched).
    abandoned_background_tasks: tuple[str, ...] = ()


# Splits a lowercased model string into namespace tokens for the family
# check below: any run of characters that is not `[a-z0-9]` is a separator
# (covers `-`, `.`, `_`, `:`, `[`, `]`, ...).
_MODEL_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def check_spec_values(
    spec: RunSpec,
    *,
    provider: str,
    aliases: frozenset[str] = frozenset(),
    families: frozenset[str] = frozenset(),
    efforts: frozenset[str] | None = None,
) -> None:
    """Raise `UnsupportedByProvider` if `spec.model` or `spec.effort` is a
    value `provider` cannot honour. Called as the **first statement** of
    every provider's `build_launch_plan`, before any run record, artifact
    directory or child process exists (`Harness.start` calls
    `build_launch_plan` before `_launch` creates any of those).

    **Model** — a namespace rule, not a fixed list (round-2 plan P2: the
    real CLIs take the model value verbatim and pass it straight to the
    provider's API, so an exact allowlist would reject legal Bedrock/Vertex/
    gateway ids and dated full ids). The value is lowercased; it is accepted
    if it equals one of `aliases` verbatim, or if splitting it on any
    non-alphanumeric run yields at least one token present in `families`.
    An empty/absent/whitespace-only value, or one with a leading `-` (which
    the CLI itself would read as a flag, not an operand), is rejected by
    this same check — it is a sub-case of "no alias, no family token", not a
    separate mechanism.

    **Effort** — exact membership in `efforts`, the provider's own closed,
    per-provider set (P1: the sets differ per CLI, e.g. `minimal`/`none` are
    codex-only). `spec.effort=None` is always valid — the flag is simply
    never emitted. `efforts=None` means this provider has no effort concept
    to check here at all (mistral: it already rejects the `effort` *field*
    outright via its own `_UNSUPPORTED_FIELDS`, so nothing further to
    validate for value membership).
    """
    model = spec.model
    lowered = (model or "").strip().lower()
    is_valid_model = bool(lowered) and not lowered.startswith("-") and (
        lowered in aliases
        or any(
            token in families
            for token in _MODEL_TOKEN_SPLIT.split(lowered)
            if token
        )
    )
    if not is_valid_model:
        raise UnsupportedByProvider(
            f"model {model!r} is not usable by the {provider} provider; "
            f"expected an alias ({', '.join(sorted(aliases))}) or a model id "
            f"in the {provider} namespace ({', '.join(sorted(families))})"
        )

    if efforts is not None and spec.effort is not None and spec.effort not in efforts:
        raise UnsupportedByProvider(
            f"effort {spec.effort!r} is not supported by the {provider} "
            f"provider; valid values: {', '.join(sorted(efforts))}"
        )


class Provider(Protocol):
    """The seam a CLI adapter implements. See module docstring for the
    explicitly-named boundary of what else this protocol does and does not
    cover.
    """

    name: str
    binary_argv: list[str]

    def build_launch_plan(
        self, spec: RunSpec, *, session_id: str, run_dir: Path
    ) -> LaunchPlan:
        """Build the argv/cwd/env/stdin for `spec`, enforcing `spec.isolation`.

        Raises `UnsafeCwdError` if `spec.cwd` fails the CLEAN cwd recipe, and
        `UnsupportedByProvider` if `spec` sets a field this provider cannot
        honour (before anything is spawned).
        """
        ...

    def parse_events(self, lines: Iterable[str]) -> RunResult:
        """Parse one JSON-object-per-line event stream into a `RunResult`.

        Raises if the stream ends without a terminal result event — a
        truncated stream is never silently accepted as success.
        """
        ...

    def build_resume_plan(
        self,
        *,
        provider_argv: list[str],
        session_id: str,
        cwd: str | Path | None,
        prompt: str,
    ) -> LaunchPlan:
        """Plan a follow-up turn on the finished session `session_id`.

        `provider_argv` is the origin run's recorded argv without the binary;
        `cwd` its recorded working directory. Raises `UnsupportedByProvider`
        when the CLI cannot resume in a way the harness can replay isolated.
        """
        ...
