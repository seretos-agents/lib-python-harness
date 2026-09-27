"""R2 — the clean flag set is in the command line and cannot be dropped silently.

The literal flag list below is written independently of the production
``CLEAN_ARGV_FLAGS`` constant on purpose (plan Mechanism balance: "a second,
independently written flag literal ... a test asserting argv against the
constant the builder consumed is vacuous and could never catch the silent
drop the criterion names").
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from lib_python_harness.agents.frontmatter import parse_frontmatter
from lib_python_harness.providers.base import Isolation, RunSpec
from lib_python_harness.providers.claude_cli import ClaudeCliProvider, SCRUBBED_ENV
from lib_python_harness.errors import UnsafeCwdError

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Independently written — deliberate duplication of CLEAN_ARGV_FLAGS, not an
# import of it. Every flag/value pair the ticket's isolation recipe names.
# `-p` and `--system-prompt` are in the ticket's canonical clean set too (see
# plan Approach) — omitted here originally, which meant a silent drop of
# either flag by the builder would pass this check undetected.
EXPECTED_ALWAYS_PRESENT_FLAGS = [
    "-p",
    "--setting-sources",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools",
    "--output-format",
    "--verbose",
    "--system-prompt",
]


def _build_plan(tmp_path, **spec_overrides):
    kwargs = dict(
        prompt="Reply with exactly OK",
        isolation=Isolation.CLEAN,
        model="haiku",
        cwd=tmp_path / "cwd",
    )
    kwargs.update(spec_overrides)
    (kwargs["cwd"]).mkdir(exist_ok=True)
    spec = RunSpec(**kwargs)
    provider = ClaudeCliProvider()
    run_dir = tmp_path / "run"
    return provider.build_launch_plan(spec, session_id=str(uuid.uuid4()), run_dir=run_dir)


def _inherit_plan(tmp_path, run_dir=None, **spec_overrides):
    """Ticket #53 R1's own INHERIT builder — deliberately independent of
    `tests/test_resolve_inherit.py`'s `_inherit_plan` (same shape, but this
    module must not depend on another test module's internals)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    kwargs = dict(
        prompt="Reply with exactly OK",
        isolation=Isolation.INHERIT,
        model="haiku",
        cwd=repo,
    )
    kwargs.update(spec_overrides)
    spec = RunSpec(**kwargs)
    provider = ClaudeCliProvider()
    if run_dir is None:
        run_dir = tmp_path / "run"
    return provider.build_launch_plan(spec, session_id=str(uuid.uuid4()), run_dir=run_dir)


def test_argv_contains_canonical_clean_set(tmp_path, monkeypatch):
    # Plant every SCRUBBED_ENV name into the parent environment with a
    # nonce value *before* building the plan. Without this, on a runner
    # where these vars are simply unset already, a builder that performs
    # zero scrubbing would still pass the loop below — see F1. Planting a
    # concrete value first means "absent from plan.env" can only be true
    # because the builder actually removed it.
    nonce = "leaked-nonce-should-be-scrubbed"
    for name in SCRUBBED_ENV:
        monkeypatch.setenv(name, nonce)

    plan = _build_plan(tmp_path)
    argv = plan.argv

    for flag in EXPECTED_ALWAYS_PRESENT_FLAGS:
        assert flag in argv, f"missing required isolation flag {flag}"

    assert "--bare" not in argv

    setting_sources_idx = argv.index("--setting-sources")
    assert argv[setting_sources_idx + 1] == ""

    tools_idx = argv.index("--tools")
    assert argv[tools_idx + 1] == ""

    output_format_idx = argv.index("--output-format")
    assert argv[output_format_idx + 1] == "stream-json"

    assert "--session-id" in argv
    session_id_idx = argv.index("--session-id")
    assert UUID4_RE.match(argv[session_id_idx + 1]), "session id is not a uuid4"

    # prompt travels on stdin, never on argv
    assert "Reply with exactly OK" not in argv
    assert plan.stdin == "Reply with exactly OK"

    for name in SCRUBBED_ENV:
        assert name not in plan.env, f"{name} survived env scrub"
    assert nonce not in plan.env.values(), "scrubbed value leaked under a different key"


def test_config_dir_passthrough_survives_env_scrub(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/fake-claude-config-dir")
    plan = _build_plan(tmp_path)
    assert plan.env.get("CLAUDE_CONFIG_DIR") == "/tmp/fake-claude-config-dir"


def test_effort_and_json_schema_appear_only_when_set(tmp_path):
    default_plan = _build_plan(tmp_path)
    assert "--effort" not in default_plan.argv
    assert "--json-schema" not in default_plan.argv

    set_plan = _build_plan(tmp_path, effort="high", json_schema={"type": "object"})
    assert "--effort" in set_plan.argv
    effort_idx = set_plan.argv.index("--effort")
    assert set_plan.argv[effort_idx + 1] == "high"
    assert "--json-schema" in set_plan.argv


def test_default_cwd_is_a_fresh_empty_non_repo_dir():
    spec = RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="haiku")
    provider = ClaudeCliProvider()
    plan = provider.build_launch_plan(
        spec, session_id=str(uuid.uuid4()), run_dir=Path("/tmp/irrelevant-run-dir")
    )
    cwd = Path(plan.cwd)
    assert cwd.exists() and cwd.is_dir()
    assert list(cwd.iterdir()) == []
    assert not (cwd / ".git").exists()


def test_cwd_inside_git_repo_raises_unsafe_cwd_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    spec = RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="haiku", cwd=repo)
    provider = ClaudeCliProvider()
    with pytest.raises(UnsafeCwdError):
        provider.build_launch_plan(spec, session_id=str(uuid.uuid4()), run_dir=tmp_path / "run")


def test_cwd_under_a_git_repo_ancestor_also_raises(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "nested" / "deeper"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    spec = RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="haiku", cwd=nested)
    provider = ClaudeCliProvider()
    with pytest.raises(UnsafeCwdError):
        provider.build_launch_plan(spec, session_id=str(uuid.uuid4()), run_dir=tmp_path / "run")


def test_nonempty_non_repo_cwd_raises_unless_opted_in(tmp_path):
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "canary.txt").write_text("nonce")

    spec = RunSpec(prompt="hi", isolation=Isolation.CLEAN, model="haiku", cwd=populated)
    provider = ClaudeCliProvider()
    with pytest.raises(UnsafeCwdError):
        provider.build_launch_plan(spec, session_id=str(uuid.uuid4()), run_dir=tmp_path / "run1")

    spec_opt_in = RunSpec(
        prompt="hi",
        isolation=Isolation.CLEAN,
        model="haiku",
        cwd=populated,
        allow_nonempty_cwd=True,
    )
    plan = provider.build_launch_plan(
        spec_opt_in, session_id=str(uuid.uuid4()), run_dir=tmp_path / "run2"
    )
    assert Path(plan.cwd) == populated


# -- #53 R1: --disallowedTools promoted to a top-level argv flag, mirroring
# --tools, for both Isolation.CLEAN and Isolation.INHERIT and both dispatch
# outcomes. -------------------------------------------------------------

# `_split_tools`-normalised: embedded whitespace-only/empty items are
# dropped, surviving items are comma-joined with no spaces. Deliberately not
# a single bare name — a builder that emitted the raw, unnormalised operand
# would still pass a test that only checked for one flag's presence.
_DISALLOWED_RAW = "Bash ,, WebFetch"
_DISALLOWED_SPLIT = "Bash,WebFetch"


def _assert_one_disallowed_flag(argv, expected_operand):
    assert argv.count("--disallowedTools") == 1, argv
    idx = argv.index("--disallowedTools")
    assert argv[idx + 1] == expected_operand


def _arm_clean_no_agent(tmp_path):
    plan = _build_plan(tmp_path, disallowed_tools=_DISALLOWED_RAW)
    _assert_one_disallowed_flag(plan.argv, _DISALLOWED_SPLIT)
    assert "--agents" not in plan.argv
    assert "--agent" not in plan.argv

    # A second, different value catches an operand hard-coded to the first
    # one ever tried.
    other_plan = _build_plan(tmp_path, disallowed_tools="Edit")
    _assert_one_disallowed_flag(other_plan.argv, "Edit")


def _arm_clean_with_agent(tmp_path):
    # `agent_name` set on a CLEAN spec is otherwise inert (`_build_clean_plan`
    # never reads it) — this arm proves CLEAN ignores dispatch entirely, it
    # is not exercising a real CLEAN+agent feature.
    plan = _build_plan(tmp_path, agent_name="probe", disallowed_tools=_DISALLOWED_RAW)
    _assert_one_disallowed_flag(plan.argv, _DISALLOWED_SPLIT)
    assert "--agents" not in plan.argv
    assert "--agent" not in plan.argv


def _arm_clean_with_agent_and_mcp(tmp_path):
    plan = _build_plan(
        tmp_path,
        agent_name="probe",
        mcp_servers={"demo": {"command": "x"}},
        disallowed_tools=_DISALLOWED_RAW,
    )
    _assert_one_disallowed_flag(plan.argv, _DISALLOWED_SPLIT)
    assert "--agents" not in plan.argv
    assert "--agent" not in plan.argv


def _arm_inherit_payload(tmp_path):
    plan = _inherit_plan(tmp_path, agent_name="probe", disallowed_tools=_DISALLOWED_RAW)
    _assert_one_disallowed_flag(plan.argv, _DISALLOWED_SPLIT)
    # The existing --agents JSON carrier stays exactly as it was.
    assert "--agents" in plan.argv
    agents_json = json.loads(plan.argv[plan.argv.index("--agents") + 1])
    assert agents_json["probe"]["disallowedTools"] == ["Bash", "WebFetch"]


def _arm_inherit_materialized(tmp_path):
    run_dir = tmp_path / "run"
    plan = _inherit_plan(
        tmp_path,
        run_dir=run_dir,
        agent_name="probe",
        mcp_servers={"demo": {"command": "x"}},
        disallowed_tools=_DISALLOWED_RAW,
    )
    _assert_one_disallowed_flag(plan.argv, _DISALLOWED_SPLIT)
    assert "--agents" not in plan.argv
    # The existing materialized-frontmatter carrier stays exactly as it
    # was — verbatim scalar, not the top-level flag's normalised form.
    materialized_path = run_dir / "agents" / ".claude" / "agents" / "probe.md"
    fields, _body = parse_frontmatter(materialized_path.read_text())
    assert fields["disallowedTools"] == _DISALLOWED_RAW


_DISALLOWED_TOOLS_ARMS = {
    "clean-no-agent": _arm_clean_no_agent,
    "clean-with-agent": _arm_clean_with_agent,
    "clean-with-agent-and-mcp": _arm_clean_with_agent_and_mcp,
    "inherit-payload": _arm_inherit_payload,
    "inherit-materialized": _arm_inherit_materialized,
}


@pytest.mark.parametrize(
    "arm", _DISALLOWED_TOOLS_ARMS.values(), ids=_DISALLOWED_TOOLS_ARMS.keys()
)
def test_disallowed_tools_becomes_one_top_level_flag(tmp_path, arm):
    arm(tmp_path)


def test_disallowed_tools_none_emits_no_flag(tmp_path):
    # Passes before and after the change — `None` never carried a flag.
    plan = _build_plan(tmp_path)
    assert "--disallowedTools" not in plan.argv

    inherit_plan = _inherit_plan(tmp_path)
    assert "--disallowedTools" not in inherit_plan.argv


def test_disallowed_tools_empty_string_emits_flag_with_empty_operand(tmp_path):
    # Same `is not None` gate as --tools: "" still emits the flag.
    plan = _build_plan(tmp_path, disallowed_tools="")
    _assert_one_disallowed_flag(plan.argv, "")

    inherit_plan = _inherit_plan(tmp_path, disallowed_tools="")
    _assert_one_disallowed_flag(inherit_plan.argv, "")
