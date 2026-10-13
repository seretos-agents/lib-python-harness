"""#61 -- a real `claude -p` child with a pending `run_in_background` launch
is not grace-killed by wait(); the deferred second `result` is returned.

requires_claude: needs the installed `claude` CLI + subscription auth.
Excluded from the default `python -m pytest` run; run explicitly with
`python -m pytest -m requires_claude tests/test_background_wait_live.py -q -s`.
"""
from __future__ import annotations

import json

import pytest

from lib_python_harness import Harness, Isolation, RunSpec
from lib_python_harness.runtime.lifecycle import RunState

pytestmark = pytest.mark.requires_claude

PROMPT = (
    "Use the Bash tool to run `sleep 90; echo SLEEP_DONE` with "
    "run_in_background set to true, then end your turn immediately. When you "
    "are notified that the command finished, reply with exactly the output "
    "it printed."
)


def test_wait_returns_the_deferred_result_of_a_background_command(tmp_path):
    harness = Harness()
    started = harness.start(
        RunSpec(
            prompt=PROMPT,
            isolation=Isolation.INHERIT,
            tools="Bash",
            permission_mode="bypassPermissions",
            artifacts_dir=tmp_path,
        )
    )

    result = harness.wait(started.run_id, timeout=600)

    assert result.state == RunState.COMPLETED
    assert "SLEEP_DONE" in result.text
    assert result.abandoned_background_tasks == ()
    events = harness.store.get(started.run_id)["events_path"]
    results = [
        line
        for line in open(events, encoding="utf-8")
        if line.strip() and json.loads(line).get("type") == "result"
    ]
    assert len(results) == 2
