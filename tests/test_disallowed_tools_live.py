"""R4 (#53) — the real `claude` CLI actually refuses the denied tool, on the
one run shape where the deny was previously dropped entirely: `Isolation.
CLEAN` with no `agent_name`. `_build_clean_plan` never reads `agent_name`,
`dispatch_mode` or `disallowed_tools` (plan Approach's "Effect on
enforcement"), so before this ticket a CLEAN run's `disallowed_tools` was
silent — this is the one live probe that proves the new top-level
`--disallowedTools` flag is real enforcement, not only an argv-visibility
change.

requires_claude: needs the installed `claude` CLI + subscription auth.
Excluded from the default `python -m pytest` run (see `pyproject.toml`'s
`addopts`, which deselects `requires_claude`); run explicitly with this
module's own substitute-execution command, per the plan:

    python -m pytest -m requires_claude -k disallowed -v

Bypasses `resolve()`/`AgentDefinition` entirely and drives
`Harness(store=InMemoryRunStore()).run(RunSpec(...))` directly, reading the
run's own `events.jsonl` back off the store — the same minimal-dependency
shape `tests/test_tools_scope_live.py` uses.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from lib_python_harness import Harness, Isolation, RunSpec
from lib_python_harness.runtime.store import InMemoryRunStore

pytestmark = pytest.mark.requires_claude


def _init_event_tools(events_path):
    for line in Path(events_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("tools")
    raise AssertionError(f"no init event found in {events_path}")


def _non_error_tool_result_texts(events_path):
    """Every `tool_result` block's own text, across the whole stream, that
    the CLI did *not* itself flag as an error (`is_error` truthy on the
    block) — a permission-denied refusal is exactly the shape that sets
    `is_error`, so a real refusal must never show up here even though the
    model's own attempted Bash call (with the nonce in its `input`, not a
    `tool_result`) still appears earlier in the stream."""
    texts: list[str] = []
    for line in Path(events_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") != "user":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                continue
            block_content = block.get("content")
            if isinstance(block_content, str):
                texts.append(block_content)
            elif isinstance(block_content, list):
                for item in block_content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        texts.append(item["text"])
    return texts


def _run_clean(*, disallowed_tools):
    nonce = f"NONCE-{uuid.uuid4().hex}"
    spec = RunSpec(
        prompt=(
            f"Use the Bash tool to run: echo {nonce} -- do not answer from "
            "memory or context, actually invoke the tool -- and report its "
            "exact output."
        ),
        isolation=Isolation.CLEAN,
        model="haiku",
        tools="Bash,Read",
        disallowed_tools=disallowed_tools,
        permission_mode="bypassPermissions",
    )
    harness = Harness(store=InMemoryRunStore())
    result = harness.run(spec)
    record = harness.store.get(result.run_id)
    return nonce, record["events_path"]


def test_disallowed_tools_flag_refuses_denied_tool_live():
    # Deny arm: --disallowedTools Bash must both narrow the init tool list
    # and stop the model's Bash call from ever producing a real result.
    denied_nonce, denied_events_path = _run_clean(disallowed_tools="Bash")

    denied_tools = _init_event_tools(denied_events_path)
    assert "Read" in denied_tools
    assert "Bash" not in denied_tools

    denied_texts = _non_error_tool_result_texts(denied_events_path)
    assert not any(denied_nonce in text for text in denied_texts), (
        "the nonce leaked through a non-error tool_result -- Bash ran "
        "despite the deny"
    )

    # Control arm: the same spec without disallowed_tools set. Proves the
    # narrowing above is caused by the flag, not by the model simply
    # refusing to use Bash on its own or by --tools already excluding it.
    control_tools = _init_event_tools(_run_clean(disallowed_tools=None)[1])
    assert "Bash" in control_tools
