"""#59 R5 -- a Popen-less observer finalizes a real finished `claude -p` run.

requires_claude: needs the installed `claude` CLI + subscription auth.
Excluded from the default run; run explicitly with
`python -m pytest -m requires_claude tests/test_finalize_gone_live.py -q -s`.

Harness A starts a CLEAN haiku run and keeps its Popen; Harness B, on the same
store, polls `wait(run_id, 0)` like the agent-harness Stop hook does.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lib_python_harness import FileRunStore, Harness, Isolation, RunSpec
from lib_python_harness.harness import _FINALIZE_GRACE_S
from lib_python_harness.runtime.lifecycle import RunState

pytestmark = pytest.mark.requires_claude


def _result_duration_s(events_path: Path):
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result" and "duration_ms" in event:
            return event["duration_ms"] / 1000
    return None


def test_observer_zero_waits_finalize_a_finished_real_run(tmp_path):
    store = FileRunStore(tmp_path)
    starter = Harness(store=store)  # keeps the Popen alive for the whole test
    observer = Harness(store=FileRunStore(tmp_path))
    run_id = starter.start(
        RunSpec(
            prompt="Reply with exactly OK",
            isolation=Isolation.CLEAN,
            model="haiku",
            artifacts_dir=tmp_path,
        )
    ).run_id

    deadline = time.monotonic() + 120
    result = observer.wait(run_id, 0)
    while result.state is RunState.RUNNING and time.monotonic() < deadline:
        began = time.monotonic()
        result = observer.wait(run_id, 0)
        assert time.monotonic() - began < _FINALIZE_GRACE_S + 2, "a wait(run_id, 0) call was slow"
        time.sleep(0.5)

    assert result.state is RunState.COMPLETED, result
    record = store.get(run_id)
    real_s = _result_duration_s(Path(record["events_path"]))
    assert real_s is not None, "no result event with duration_ms"
    provenance = json.loads(Path(record["provenance_path"]).read_text(encoding="utf-8"))
    assert abs(record["duration_s"] - real_s) < 5
    assert abs(provenance["duration_s"] - real_s) < 5
