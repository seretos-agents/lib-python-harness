"""R4 (ticket #55) -- resume works on the same session after a grace-kill.

requires_claude: needs the installed `claude` CLI + subscription auth.
Excluded from the default `python -m pytest` run; run explicitly with
`python -m pytest -m requires_claude tests/test_grace_kill_live.py -q -s`.

The offline tests (`tests/test_harness_observe.py`) prove the grace-kill
mechanics against the fake CLI; this live test verifies the one premise the
fake can't stand in for (plan "Premises verified"): that the real `claude`
CLI's transcript is complete at/shortly after `result`, so a session that
gets grace-killed can still be resumed for a real follow-up turn that reads
back what the origin turn said.

The spy wraps the real `_wait_or_kill` (not a no-op) so the child is
actually reclaimed rather than left lingering for the retry loop's next
attempt.
"""
from __future__ import annotations

import uuid

import pytest

import lib_python_harness.harness as harness_module
from lib_python_harness import Harness, Isolation, RunSpec
from lib_python_harness.runtime.lifecycle import RunState
from lib_python_harness.runtime.process import _wait_or_kill as _real_wait_or_kill

pytestmark = pytest.mark.requires_claude

RESUME_PROMPT = (
    "What codeword did I ask you to remember? Answer with just the codeword, "
    "nothing else."
)


def test_resume_after_grace_kill_keeps_the_session(tmp_path, monkeypatch):
    codeword = f"CODEWORD-{uuid.uuid4().hex[:8]}"

    kills: list[tuple] = []

    def spy(*args, **kwargs):
        kills.append((args, kwargs))
        return _real_wait_or_kill(*args, **kwargs)

    monkeypatch.setattr(harness_module, "_wait_or_kill", spy, raising=False)
    monkeypatch.setattr(harness_module, "_FINALIZE_GRACE_S", 0.0)

    harness = Harness()
    origin = None
    for attempt in range(3):
        kills.clear()
        started = harness.start(
            RunSpec(
                prompt=(
                    f"Remember this codeword: {codeword}. Reply with exactly OK."
                ),
                isolation=Isolation.CLEAN,
                model="haiku",
                artifacts_dir=tmp_path / f"artifacts-{attempt}",
            )
        )
        origin = harness.wait(started.run_id, timeout=120, poll_interval=0.02)
        print(
            f"\n[grace-kill] attempt={attempt} state={origin.state} "
            f"kills={len(kills)} duration_s={origin.duration_s}"
        )
        if kills:
            break
    else:
        pytest.fail(
            "the grace-kill spy (_wait_or_kill) was never called across 3 "
            f"attempts; last origin state={origin.state if origin else None} "
            "-- the fix is not wired into wait() yet"
        )

    assert origin.state == RunState.COMPLETED

    result = harness.resume(origin.run_id, RESUME_PROMPT)

    print(f"[grace-kill] resume reply: {result.text!r}")
    assert result.state == RunState.COMPLETED
    assert result.session_id == origin.session_id
    assert codeword in result.text
