"""The one `Provider` implementation this ticket ships: the real `claude` CLI.

Always spawns `-p --output-format stream-json --verbose`: stream-json is the
only mode that yields a partial events log (the cancel criterion needs it on
disk even for a run that gets stopped mid-flight), its terminal
`{"type": "result"}` event is the same object `--output-format json` would
have printed, and one parse path cannot drift from another. `--bare` is
never emitted — auth stays subscription/OAuth via `CLAUDE_CONFIG_DIR`, never
`ANTHROPIC_API_KEY`.

`Isolation.INHERIT` (ticket #2) is the opposite recipe from `Isolation.
CLEAN`: the child keeps the parent's cwd, CLAUDE.md discovery, settings,
permission mode and MCP servers, plus whichever agent-definition fields the
caller set on the `RunSpec`. `ISOLATION_ARGV` holds each profile's own fixed
token sequence; `_resolve_and_validate_cwd` picks the matching cwd recipe by
the same enum. Env scrubbing (`_scrub_env`) is unchanged for both — it is a
property of what must never reach any child regardless of isolation, not of
the isolation profile itself.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ..errors import UnsafeCwdError
from .base import Isolation, LaunchPlan, RunResult, RunSpec, check_spec_values
from .isolation import _raises_if_git_ancestor, _resolve_clean_cwd, scrub_env  # noqa: F401

# Verified against the installed `claude` CLI binary (plan P1/P2, round 2):
# the alias set the CLI's own `--model` help text names, plus `default` (the
# CLI's own "use whatever the profile default is" alias) and `inherit` (a
# legal *agent-definition* model value — not a CLI flag alias, but
# `resolve()` passes it straight through into `RunSpec.model`, and it must
# keep starting a run, P3). `_MODEL_FAMILIES` is the namespace-rule token set
# a model id (full dated id, Bedrock/Vertex/gateway id, ...) is checked
# against when it is not one of the aliases above.
_MODEL_ALIASES: frozenset[str] = frozenset(
    {"sonnet", "opus", "haiku", "fable", "default", "inherit"}
)
_MODEL_FAMILIES: frozenset[str] = frozenset(
    {"claude", "anthropic", "sonnet", "opus", "haiku", "fable"}
)

# The claude CLI's own closed `--effort` value set (plan P1, grepped from the
# installed binary: `cu=["low","medium","high","xhigh","max"]`). Deliberately
# does NOT include "minimal"/"none" — those are codex-only; claude silently
# warns and falls back to the default effort for a value outside this set,
# which is exactly the ticket's silent-typo symptom this check closes.
_EFFORT_VALUES: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

# Explicit, enumerable names this build scrubs from the child's environment —
# a fixed constant (not a runtime-derived pattern) so a test can plant each
# one with a nonce before building a plan and assert it is gone afterwards.
# `CLAUDE_CODE_*` variables are scrubbed too (see `_scrub_env` below), by
# prefix rather than by name, since the set of such variables is open-ended;
# `CLAUDE_CONFIG_DIR` deliberately does *not* match either rule and survives
# unchanged — it is what carries OAuth credentials and session transcripts
# (`claude --resume <id>` and subscription auth both depend on it). Every
# other parent env var (e.g. a caller's own `HARNESS_CANARY`-style variable)
# survives untouched for both `Isolation.CLEAN` and `Isolation.INHERIT` —
# scrubbing is about credentials/self-identification, not about isolation.
SCRUBBED_ENV: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
)

_SCRUBBED_ENV_PREFIX = "CLAUDE_CODE_"

# The canonical `Isolation.CLEAN` flag/value subsequence, as a flat list of
# argv tokens (flag immediately followed by its fixed value, where it has
# one). Kept under its original name for backward compatibility
# (tests/test_harness_offline.py imports it directly) and reused, unchanged,
# as `ISOLATION_ARGV[Isolation.CLEAN]` below — this is *not* what
# `tests/test_claude_cli_flags.py` asserts against: that test carries its
# own, independently written literal, because asserting argv against the
# same constant the builder consumed would be vacuous (plan Mechanism
# balance).
CLEAN_ARGV_FLAGS: tuple[str, ...] = (
    "--setting-sources", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools", "",
    "--output-format", "stream-json",
    "--verbose",
)

# `Isolation.INHERIT`'s own fixed portion: load the parent's user/project/
# local settings (the opposite of CLEAN's `--setting-sources ""`), and never
# `--strict-mcp-config` (INHERIT's whole point is inheriting the parent's
# MCP servers, not confining them to a per-run `--mcp-config`). This is the
# default (`omit_claude_md` falsy) form; see `_INHERIT_ARGV_OMIT_CLAUDE_MD`
# below for the real, live-verified CLAUDE.md-suppression mechanism.
_INHERIT_ARGV: tuple[str, ...] = (
    "--setting-sources", "user,project,local",
)

# Flags whose value slot `_profile_argv` fills per run: one computed value
# per flag, decided in one place.
_SETTING_SOURCES_FLAG = "--setting-sources"
_TOOLS_FLAG = "--tools"

# `omit_claude_md=True`'s real mechanism (see `_profile_argv`) - live-verified
# against installed `claude` v2.1.277 by direct A/B testing in a temp git
# repo with a CLAUDE.md, round 2 of ticket #2: CLAUDE.md loading is gated
# specifically by the "project" entry of `--setting-sources`, not by any
# `--settings instructionFiles` key (`{"instructionFiles": []}` and
# `{"instructionFiles": {"mode": "managed-only"}}` were both probed and
# *neither* suppresses CLAUDE.md). Dropping "project" from
# `--setting-sources` (keeping "user" and "local") does suppress it.
# Known trade-off, not a design choice: this also drops project-level
# *settings* (`.claude/settings.json`), since "project" is the one source
# that carries both.

# One table, one source of truth per isolation profile — replaces having a
# bare `CLEAN_ARGV_FLAGS` module constant be the *only* fixed-token source,
# now that a second profile needs its own. `--output-format`/`--verbose` are
# not isolation-keyed: both profiles need stream-json regardless (see module
# docstring), so they are appended once in `build_launch_plan`, not per-entry
# here.
ISOLATION_ARGV: dict[Isolation, tuple[str, ...]] = {
    Isolation.CLEAN: CLEAN_ARGV_FLAGS,
    Isolation.INHERIT: _INHERIT_ARGV,
}

# The `--agents <json>` schema's accepted per-agent keys, verified against
# the installed `claude` v2.1.277 binary (plan step 0): each key below was
# probed with a minimal `--agents` payload and produced no
# "Invalid --agents configuration" error; `hooks`/`mcpServers` were probed
# too and *do* produce that error, so they are deliberately absent — any
# definition that sets either of those two always takes the `materialized`
# path (see `dispatch_mode`), never `payload`. `tools`/`disallowedTools` are
# accepted, but only as a JSON array (a comma-separated scalar string, which
# is how `agents.model.AgentDefinition`/`RunSpec` carry them, is rejected —
# `_build_agent_payload` converts).
AGENT_JSON_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "prompt",
        "tools",
        "disallowedTools",
        "model",
        "permissionMode",
        "maxTurns",
        "skills",
    }
)

# RunSpec attribute -> --agents JSON key, for every field that carrier could
# possibly accept. `dispatch_mode` walks this to decide "does every field
# this run actually sets fit in `accepted_keys`?"; `_build_agent_payload`
# walks it again to build the JSON itself — one table, not two independently
# maintained lists that could drift apart.
_SPEC_TO_AGENT_JSON_KEY: tuple[tuple[str, str], ...] = (
    ("prompt", "prompt"),
    ("description", "description"),
    ("tools", "tools"),
    ("disallowed_tools", "disallowedTools"),
    ("model", "model"),
    ("permission_mode", "permissionMode"),
    ("max_turns", "maxTurns"),
    ("skills", "skills"),
    ("hooks", "hooks"),
    ("mcp_servers", "mcpServers"),
)


def dispatch_mode(spec: RunSpec, accepted_keys: Iterable[str] = AGENT_JSON_KEYS) -> str:
    """`"payload"` when every field `spec` actually sets (non-`None`) has a
    slot in `accepted_keys`; `"materialized"` — the carrier of last resort —
    the moment even one set field does not (today, in production: `hooks`
    or `mcp_servers`). Whole-definition, never per-field: one rejected field
    routes the *entire* run through the materialized path, so the two
    carriers never have to be reconciled key by key. Pure — no I/O, no
    dependence on `AGENT_JSON_KEYS` unless the caller lets the default
    stand, so a test can pin either branch deterministically.
    """
    accepted = set(accepted_keys)
    for spec_field, json_key in _SPEC_TO_AGENT_JSON_KEY:
        if getattr(spec, spec_field, None) is not None and json_key not in accepted:
            return "materialized"
    return "payload"


def _split_tools(value: str | None) -> list[str] | None:
    """`RunSpec.tools`/`disallowed_tools` carry a frontmatter-style
    comma-separated scalar (`"Read, Glob"`); the `--agents` JSON schema
    wants an array (verified: a scalar string is rejected). Converted only
    here, for the JSON payload — the materialized frontmatter path
    (`materialize_agent_dir`) keeps the original scalar form, since that is
    what a real agent `.md` file's own `tools:` line looks like.
    """
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _agent_body(spec: RunSpec) -> str:
    """The agent definition's body: `system_prompt` when set (a `resolve(...,
    task=...)` spec, where `prompt` is the task), else `prompt` (specs that
    set only `prompt=`). `is not None`, not `or`: an empty body stays empty."""
    if spec.system_prompt is not None:
        return spec.system_prompt
    return spec.prompt or ""


def _build_agent_payload(spec: RunSpec, accepted_keys: Iterable[str]) -> dict[str, Any]:
    accepted = set(accepted_keys)
    candidate: dict[str, Any] = {
        "description": spec.description,
        "prompt": _agent_body(spec),
        "tools": _split_tools(spec.tools),
        "disallowedTools": _split_tools(spec.disallowed_tools),
        "model": spec.model,
        "permissionMode": spec.permission_mode,
        "maxTurns": spec.max_turns,
        "skills": spec.skills,
    }
    return {
        key: value
        for key, value in candidate.items()
        if value is not None and key in accepted
    }


def _agent_file_stem(agent_name: str) -> str:
    """A colon is legal in a POSIX filename but unproven in the CLI's own
    agent-name grammar (plan Approach) — replaced with `__` for both the
    materialized file's stem and its `name:` field, so `--agent <stem>`
    (using this same stem) finds a definition whose declared name matches."""
    return agent_name.replace(":", "__")


def materialize_agent_dir(spec: RunSpec, run_dir: Path | str) -> Path:
    """Write `<run_dir>/agents/.claude/agents/<stem>.md` — the materialized
    carrier of last resort for a definition the `--agents` JSON schema
    cannot fully express (`dispatch_mode` returning `"materialized"`).

    Every non-`None` INHERIT-only field on `spec` is emitted as real
    frontmatter (`agents.frontmatter.dump_frontmatter`), including the ones
    the JSON schema rejects (`hooks`, `mcpServers`) — the whole point of
    this carrier is that the child's own agent loader parses the full
    frontmatter directly, unconstrained by that schema. The body is
    `spec.prompt`, verbatim. `run_dir` need not exist beforehand.

    `description` is always written, even though the definition might not
    have set one: a live probe against the real CLI (v2.1.277, R5's own
    substitute-execution run) found that `--agent <stem>` reports `<stem>
    not found` for a materialized file with no `description:` key at all —
    the CLI's own agent loader silently drops a description-less file from
    discovery. `spec.description` (threaded through by `resolve()` from
    `AgentDefinition.description`) is used when the definition actually set
    one; the generic synthesized filler below is only a fallback for the
    genuinely-description-less case, to keep the materialized carrier
    dispatchable rather than to stand in for real prose.
    """
    from ..agents.frontmatter import dump_frontmatter

    run_dir = Path(run_dir)
    stem = _agent_file_stem(spec.agent_name or "agent")
    agents_dir = run_dir / "agents" / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest = agents_dir / f"{stem}.md"

    fields: dict[str, Any] = {
        "name": stem,
        "description": spec.description
        or f"Materialized lib-python-harness agent dispatch ({stem})",
    }
    for value, key in (
        (spec.model, "model"),
        (spec.permission_mode, "permissionMode"),
        (spec.tools, "tools"),
        (spec.disallowed_tools, "disallowedTools"),
        (spec.skills, "skills"),
        (spec.max_turns, "maxTurns"),
        (spec.hooks, "hooks"),
        (spec.mcp_servers, "mcpServers"),
        (spec.omit_claude_md, "omitClaudeMd"),
    ):
        if value is not None:
            fields[key] = value

    dest.write_text(dump_frontmatter(fields, _agent_body(spec)))
    return dest


def _profile_argv(spec: RunSpec) -> list[str]:
    """`ISOLATION_ARGV[spec.isolation]` with this run's own values
    substituted into the profile's value slots (byte-identical to the fixed
    tokens when the run sets none):

    - `--setting-sources`: `spec.setting_sources` joined by `,` when set
      (an empty list -> the flag with an empty operand, as CLEAN does);
      else `user,local` when `omit_claude_md` (INHERIT only); else the
      profile default.
    - `--tools` (CLEAN's slot): `spec.tools` as a `,`-joined allowlist when
      set, else the profile default (`""`).
    """
    tokens = list(ISOLATION_ARGV[spec.isolation])
    for i, token in enumerate(tokens[:-1]):
        if token == _SETTING_SOURCES_FLAG:
            if spec.setting_sources is not None:
                tokens[i + 1] = ",".join(spec.setting_sources)
            elif spec.isolation is Isolation.INHERIT and spec.omit_claude_md:
                tokens[i + 1] = "user,local"
        elif token == _TOOLS_FLAG and spec.tools is not None:
            tokens[i + 1] = ",".join(_split_tools(spec.tools) or [])
    return tokens


def _resolve_and_validate_cwd(spec: RunSpec) -> Path:
    if spec.isolation is Isolation.INHERIT:
        return _resolve_inherit_cwd(spec)
    return _resolve_clean_cwd(spec)


def _resolve_inherit_cwd(spec: RunSpec) -> Path:
    """`Isolation.INHERIT`'s whole point is running inside the parent's own
    repo, so none of `CLEAN`'s git-ancestor/emptiness checks apply here —
    only "does this path exist and is it a directory" (unlike `CLEAN`,
    `cwd=None` is not defaulted to a fresh temp dir: INHERIT without an
    explicit cwd has no parent cwd to inherit)."""
    if spec.cwd is None:
        raise UnsafeCwdError(
            "Isolation.INHERIT requires an explicit cwd (the parent session's "
            "own working directory) — unlike Isolation.CLEAN, there is no "
            "fresh-temp-dir default to fall back to"
        )
    cwd = Path(spec.cwd)
    if not cwd.exists():
        raise UnsafeCwdError(f"{cwd} does not exist")
    if not cwd.is_dir():
        raise UnsafeCwdError(f"{cwd} is not a directory")
    if spec.memory is False:
        # Auto-memory is keyed by cwd: a directory `claude` has never run in
        # has no memory tree to load. The original cwd stays reachable via
        # `--add-dir` (see `_build_inherit_plan`).
        return Path(tempfile.mkdtemp(prefix="lib-python-harness-cwd-"))
    return cwd


def _scrub_env() -> dict[str, str]:
    return scrub_env(SCRUBBED_ENV, (_SCRUBBED_ENV_PREFIX,))


# #42 pending-background-launch walk (`ClaudeCliProvider.parse_events`): the
# three text shapes that can resolve a `run_in_background: true` launch L
# (its tool_use id) to, or past, its background task id T.
#
# - `_BACKGROUND_ACK`: T is not carried anywhere on the launch's own
#   `tool_use` block — the *only* place it appears is the ack `tool_result`
#   text the CLI writes back for that same launch (verified live, plan
#   Premises verified). If the ack text does not match this pattern, T stays
#   unknown for that launch and only a `<tool-use-id>` notification (below)
#   can still resolve it — never a poll, since a poll is keyed by T.
# - `_POLL_RESULT`: a `TaskOutput` poll's result text carries both T and its
#   `<status>` in the *same* tool_result content string, so matching them as
#   one pair (not two independent substring searches) never mispairs ids
#   across two polls that happen to land in the same event (plan-critic
#   round 3 note 2).
# - `_NOTIFICATION_TOOL_USE_ID`: a `<task-notification>` names the launch id
#   L directly, so it resolves a launch even when the ack was unparseable.
_BACKGROUND_ACK = re.compile(r"running in background with ID: (\w+)")
_POLL_RESULT = re.compile(r"<task_id>(\w+)</task_id>\s*<status>(\w+)</status>")
_NOTIFICATION_TOOL_USE_ID = re.compile(r"<tool-use-id>(\w+)</tool-use-id>")


class ClaudeCliProvider:
    """Builds and parses the `claude` CLI's command line, for either
    isolation profile."""

    name = "claude"
    binary_argv = ["claude"]

    def build_launch_plan(
        self,
        spec: RunSpec,
        *,
        session_id: str,
        run_dir: Path,
        accepted_keys: Iterable[str] | None = None,
    ) -> LaunchPlan:
        """`accepted_keys` is the same injectable override `dispatch_mode`
        takes — defaulting to the real, probe-verified `AGENT_JSON_KEYS` in
        production, but overridable so a test can pin the payload/
        materialized branch deterministically without depending on
        whatever the production set happens to be today. Only consulted for
        `Isolation.INHERIT` runs that set `agent_name`; ignored otherwise.
        """
        check_spec_values(
            spec,
            provider=self.name,
            aliases=_MODEL_ALIASES,
            families=_MODEL_FAMILIES,
            efforts=_EFFORT_VALUES,
        )
        if spec.isolation is Isolation.INHERIT:
            return self._build_inherit_plan(
                spec, session_id=session_id, run_dir=run_dir, accepted_keys=accepted_keys
            )
        return self._build_clean_plan(spec, session_id=session_id, run_dir=run_dir)

    def _build_clean_plan(
        self, spec: RunSpec, *, session_id: str, run_dir: Path
    ) -> LaunchPlan:
        cwd = _resolve_and_validate_cwd(spec)

        argv: list[str] = ["-p", "--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        # Both emissions are gated on fields no plain CLEAN caller sets
        # (they are filled only by a `.seretos/harness.yml` override).
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]

        argv += _profile_argv(spec)
        if spec.disallowed_tools is not None:
            argv += ["--disallowedTools", ",".join(_split_tools(spec.disallowed_tools) or [])]

        if spec.mcp_servers:
            # CLEAN always carries --strict-mcp-config, so the named set is
            # exactly the reachable set.
            argv += ["--mcp-config", json.dumps({"mcpServers": spec.mcp_servers})]

        argv += ["--system-prompt", spec.system_prompt or ""]
        argv += ["--session-id", session_id]

        if spec.json_schema is not None:
            argv += ["--json-schema", json.dumps(spec.json_schema)]

        env = _scrub_env()

        return LaunchPlan(argv=argv, cwd=str(cwd), env=env, stdin=spec.prompt)

    def _build_inherit_plan(
        self,
        spec: RunSpec,
        *,
        session_id: str,
        run_dir: Path,
        accepted_keys: Iterable[str] | None,
    ) -> LaunchPlan:
        cwd = _resolve_and_validate_cwd(spec)

        argv: list[str] = ["-p", "--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]

        argv += _profile_argv(spec)
        if spec.strict_mcp:
            # The child's MCP set was computed (config-driven run): without
            # this, a server absent from --mcp-config would still be
            # reachable through inherited user/project settings.
            argv += ["--strict-mcp-config"]
        if spec.tools is not None:
            argv += ["--tools", ",".join(_split_tools(spec.tools) or [])]
        if spec.disallowed_tools is not None:
            argv += ["--disallowedTools", ",".join(_split_tools(spec.disallowed_tools) or [])]
        argv += ["--output-format", "stream-json", "--verbose"]

        if spec.mcp_servers:
            # `--mcp-config`'s JSON schema wants a full MCP-config-file
            # shape (`{"mcpServers": {...}}`), not the bare `{"<name>":
            # {...}}` mapping the agent-definition frontmatter's own
            # `mcpServers:` key uses (live-verified, R5 probe round: the
            # bare shape produces "Invalid MCP configuration" from the real
            # CLI). `spec.mcp_servers` itself stays the bare mapping
            # (that is what `materialize_agent_dir`'s `mcpServers:` field
            # and `--agents`' rejected-key check both expect) — only this
            # flag's own JSON value gets the wrapper.
            argv += ["--mcp-config", json.dumps({"mcpServers": spec.mcp_servers})]

        argv += ["--session-id", session_id]

        if spec.json_schema is not None:
            argv += ["--json-schema", json.dumps(spec.json_schema)]

        if spec.memory is False:
            argv += ["--add-dir", str(Path(spec.cwd))]

        if spec.agent_name:
            keys = AGENT_JSON_KEYS if accepted_keys is None else set(accepted_keys)
            mode = dispatch_mode(spec, keys)
            if mode == "payload":
                payload = _build_agent_payload(spec, keys)
                argv += ["--agents", json.dumps({spec.agent_name: payload})]
                argv += ["--agent", spec.agent_name]
            else:
                materialize_agent_dir(spec, run_dir)
                argv += ["--add-dir", str(Path(run_dir) / "agents")]
                argv += ["--agent", _agent_file_stem(spec.agent_name)]

        env = _scrub_env()

        return LaunchPlan(argv=argv, cwd=str(cwd), env=env, stdin=spec.prompt)

    def build_resume_plan(
        self,
        *,
        provider_argv: list[str],
        session_id: str,
        cwd: str | Path | None,
        prompt: str,
    ) -> LaunchPlan:
        """Plan a follow-up turn on a finished session, replaying the origin
        run's flags.

        `provider_argv` is the origin's recorded argv without the binary. It
        is copied verbatim — derived, never rebuilt, so every isolation flag
        the origin ran with survives — except that any `--session-id <id>` or
        `--resume <id>` pair (the latter when the origin was itself a resume)
        is dropped and `--resume <session_id>` appended. The run starts
        in the origin's `cwd`; if that directory no longer exists a fresh
        temp directory is used (measured live: `claude --resume` finds the
        session from a foreign cwd). `_resolve_and_validate_cwd` is never
        called: the CLEAN "cwd must be empty" check must not re-trip on files
        `claude` itself wrote there.
        """
        argv: list[str] = []
        skip = False
        for token in provider_argv:
            if skip:
                skip = False
                continue
            if token in ("--session-id", "--resume"):
                skip = True
                continue
            argv.append(token)
        argv += ["--resume", session_id]

        if cwd is not None and Path(cwd).is_dir():
            run_cwd = str(cwd)
        else:
            run_cwd = tempfile.mkdtemp(prefix="lib-python-harness-cwd-")
        return LaunchPlan(argv=argv, cwd=run_cwd, env=_scrub_env(), stdin=prompt)

    def describe_last_activity(self, lines: Iterable[str]) -> str | None:
        """Short label for the newest recognizable event of a (possibly
        still growing) stream: `tool_use:<name>` for the last `tool_use`
        block of the newest `assistant` event, else `text`, `tool_result` or
        `init`. Malformed/unknown lines are skipped; `None` when nothing is
        recognizable. Never raises."""
        for line in reversed([ln.strip() for ln in lines]):
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            label = self._activity_label(event)
            if label is not None:
                return label
        return None

    def has_terminal_event(self, lines: Iterable[str]) -> bool:
        """`True` iff some line in `lines` parses to a dict with
        `type == "result"` — the post-completion grace-kill's scoping probe
        (#55): `Harness` looks this up with `getattr`, like
        `describe_last_activity`, so a provider without it (Codex, Mistral,
        an injected fake) is never grace-killed. Torn/garbage lines are
        skipped, never raised on — the same tolerance `describe_last_activity`
        applies to a still-growing stream."""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return True
        return False

    @staticmethod
    def _content_blocks(event: Any) -> list[dict]:
        """The dict blocks of an event's `message.content` list — the shape
        both `_activity_label` (the newest-event liveness label) and
        `parse_events`'s pending-background-launch walk read. A plain-string
        `message.content` (the `<task-notification>` shape parse_events also
        has to see) is not a block list and yields `[]` here; a caller that
        needs that string reads `message.content` directly."""
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []

    @staticmethod
    def _tool_result_texts(block: dict) -> list[str]:
        """A `tool_result` block's own text, for substring/regex matching:
        its `content` when that is a plain string, or each list item's own
        `text` when `content` is a list of text items (plan Text
        extraction)."""
        content = block.get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            return [
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
        return []

    @staticmethod
    def _resolve_pending_by_task(pending: dict[str, str | None], task_id: str) -> None:
        """Drop every still-pending launch currently mapped to `task_id` —
        shared by the `TaskStop` and non-"running"-poll resolution signals,
        both of which resolve by T rather than by L."""
        for launch_id in [lid for lid, mapped in pending.items() if mapped == task_id]:
            del pending[launch_id]

    @staticmethod
    def _activity_label(event: Any) -> str | None:
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "system":
            return "init" if event.get("subtype") == "init" else None
        if kind not in ("assistant", "user"):
            return None
        blocks = ClaudeCliProvider._content_blocks(event)
        if kind == "assistant":
            tools = [b for b in blocks if b.get("type") == "tool_use"]
            if tools:
                return f"tool_use:{tools[-1].get('name', '?')}"
            if any(b.get("type") == "text" for b in blocks):
                return "text"
            return None
        return "tool_result" if any(b.get("type") == "tool_result" for b in blocks) else None

    def parse_events(self, lines: Iterable[str]) -> RunResult:
        """One pass in stream order. Alongside the terminal `result` event,
        tracks `pending`: every `run_in_background: true` Bash launch (its
        `tool_use` id L, mapped to its background task id T once the ack
        parses) that no later event has resolved by one of the three signals
        `_BACKGROUND_ACK`/`_POLL_RESULT`/`_NOTIFICATION_TOOL_USE_ID` module
        docstring names — an intervening "still running" poll, or any other
        tool call, resolves nothing (plan Approach; round-2 misread::M1).
        `abandoned_background_tasks` is `tuple(pending)` at the end, in
        launch order (dicts preserve insertion order; a resolved launch's
        `del` never reorders what remains).
        """
        terminal: dict | None = None
        pending: dict[str, str | None] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            kind = event.get("type")
            if kind == "result":
                terminal = event
                continue
            if kind == "assistant":
                for block in self._content_blocks(event):
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    tool_input = block.get("input")
                    tool_input = tool_input if isinstance(tool_input, dict) else {}
                    if name == "Bash" and tool_input.get("run_in_background"):
                        launch_id = block.get("id")
                        if launch_id is not None:
                            pending[launch_id] = None
                    elif name == "TaskStop":
                        task_id = tool_input.get("task_id")
                        if task_id is not None:
                            self._resolve_pending_by_task(pending, task_id)
            elif kind == "user":
                for block in self._content_blocks(event):
                    if block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id")
                    for text in self._tool_result_texts(block):
                        if tool_use_id in pending and pending[tool_use_id] is None:
                            ack = _BACKGROUND_ACK.search(text)
                            if ack:
                                pending[tool_use_id] = ack.group(1)
                        for match in _POLL_RESULT.finditer(text):
                            task_id, status = match.group(1), match.group(2)
                            if status != "running":
                                self._resolve_pending_by_task(pending, task_id)
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    for match in _NOTIFICATION_TOOL_USE_ID.finditer(content):
                        pending.pop(match.group(1), None)

        if terminal is None:
            raise ValueError(
                "stream-json output ended without a terminal 'result' event "
                "(truncated stream) — cannot be treated as a successful run"
            )

        return RunResult(
            text=terminal.get("result", ""),
            is_error=bool(terminal.get("is_error", False)),
            subtype=terminal.get("subtype"),
            structured_output=terminal.get("structured_output"),
            usage=terminal.get("usage") or {},
            cost=terminal.get("total_cost_usd"),
            session_id=terminal.get("session_id"),
            abandoned_background_tasks=tuple(pending),
        )
