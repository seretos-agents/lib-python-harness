"""Driving tests for package 13 — observing a run from a process that did not
start it (`Harness.wait_for`, `Harness.list_runs`, live progress in `poll`).

Real two-process tests: `tests/fixtures/start_run.py` is the *starter* (it
`start()`s a `fake_claude.py` child and exits, or `stop()`s it), and the test
process is the foreign *observer* with a fresh `Harness(FileRunStore(dir))`.
None of them is POSIX-only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import lib_python_harness.harness as harness_module
from lib_python_harness import (
    FileRunStore,
    Harness,
    HarnessError,
    InMemoryRunStore,
    Isolation,
    RunSpec,
    RunState,
)
from lib_python_harness.harness import _FINALIZE_GRACE_S
from lib_python_harness.runtime.process import _capture_start_time, _pid_status, _wait_or_kill

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
START_RUN = FIXTURES / "start_run.py"
SRC = Path(__file__).resolve().parent.parent / "src"

PROMPT = "SECRET-PROMPT-TEXT-4711"


def _starter_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return env


def _launch_starter(artifacts, *starter_args, fake_args=()):
    """Spawn the starter; return (starter_proc, run_id) once it printed the id."""
    cmd = [sys.executable, str(START_RUN), str(artifacts), *starter_args]
    if fake_args:
        cmd += ["--", *fake_args]
    proc = subprocess.Popen(
        cmd, env=_starter_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    line = proc.stdout.readline()
    if not line:
        raise AssertionError(f"starter printed no run_id; stderr:\n{proc.stderr.read()}")
    return proc, json.loads(line)["run_id"]


def _start_and_let_starter_exit(artifacts, *starter_args, fake_args=()):
    proc, run_id = _launch_starter(artifacts, *starter_args, fake_args=fake_args)
    assert proc.wait(timeout=30) == 0, proc.stderr.read()
    return run_id


def _observer(artifacts) -> Harness:
    return Harness(store=FileRunStore(artifacts))


# -- R1 ----------------------------------------------------------------------


def test_wait_for_returns_completed_from_a_foreign_process(tmp_path):
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "3"])
    observer = _observer(tmp_path)

    began = time.monotonic()
    result = observer.wait_for(run_id, timeout=30)
    elapsed = time.monotonic() - began

    assert result.state is RunState.COMPLETED
    assert result.text == "OK"
    assert result.usage
    assert result.timed_out is False
    assert elapsed >= 1.0, "wait_for returned before the child could have finished"
    assert FileRunStore(tmp_path).get(run_id)["state"] is RunState.COMPLETED


def test_wait_for_unknown_run_id_raises_harness_error(tmp_path):
    with pytest.raises(HarnessError):
        _observer(tmp_path).wait_for("no-such-run", timeout=1)


def test_wait_for_on_a_terminal_record_returns_immediately():
    store = InMemoryRunStore()
    store.put(
        "run-1",
        {"run_id": "run-1", "state": RunState.COMPLETED, "text": "done", "usage": {"a": 1}},
    )
    began = time.monotonic()
    result = Harness(store=store).wait_for("run-1", timeout=30)
    assert time.monotonic() - began < 2.0
    assert result.state is RunState.COMPLETED
    assert result.text == "done"
    assert result.timed_out is False


# -- R2 ----------------------------------------------------------------------


def test_wait_for_timeout_leaves_the_run_running(tmp_path):
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "4"])
    observer = _observer(tmp_path)

    began = time.monotonic()
    first = observer.wait_for(run_id, timeout=1)
    elapsed = time.monotonic() - began

    assert first.timed_out is True
    assert first.state is RunState.RUNNING
    assert 0.8 <= elapsed < 3.5
    record = FileRunStore(tmp_path).get(run_id)
    assert record["state"] is RunState.RUNNING
    assert _pid_status(record["pid"], record["start_time"]) is True, "wait_for killed the child"
    assert observer.poll(run_id).state is RunState.RUNNING

    second = observer.wait_for(run_id, timeout=30)
    assert second.state is RunState.COMPLETED
    assert second.timed_out is False
    assert second.text == "OK"


# -- R3 ----------------------------------------------------------------------


def test_wait_for_reports_cancelled_when_another_process_stops_the_run(tmp_path):
    starter, run_id = _launch_starter(
        tmp_path, "--stop-after", "1", fake_args=["--sleep", "10"]
    )
    try:
        result = _observer(tmp_path).wait_for(run_id, timeout=30)
    finally:
        starter.wait(timeout=60)

    assert result.state is RunState.CANCELLED
    record = FileRunStore(tmp_path).get(run_id)
    assert record["state"] is RunState.CANCELLED
    assert Path(record["provenance_path"]).exists()


# -- R4 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake_args", [["--no-result"], ["--no-result", "--exit-code", "2"]]
)
def test_wait_for_reports_failed_without_a_terminal_event(tmp_path, fake_args):
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "1", *fake_args])

    result = _observer(tmp_path).wait_for(run_id, timeout=30)

    assert result.state is RunState.FAILED
    record = FileRunStore(tmp_path).get(run_id)
    assert record["state"] is RunState.FAILED
    assert record["exit_code"] is None  # unknowable cross-process
    assert Path(record["provenance_path"]).exists()


def _dead_run_record(tmp_path, events_text: str) -> dict:
    """A RUNNING record for a process that already exited, with a hand-written
    events log (the shape of a run whose starter is gone)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    start_time = _capture_start_time(child.pid)
    child.wait(timeout=30)
    run_dir = tmp_path / "torn-run"
    run_dir.mkdir()
    events = run_dir / "events.jsonl"
    events.write_bytes(events_text.encode())
    return {
        "run_id": "torn-run",
        "session_id": "00000000-0000-4000-8000-000000000001",
        "state": RunState.RUNNING,
        "run_dir": run_dir,
        "cwd": tmp_path,
        "events_path": events,
        "stderr_path": run_dir / "stderr.txt",
        "provider": "claude",
        "model": "haiku",
        "argv": ["claude"],
        "binary_argv": [sys.executable, str(FAKE_CLAUDE)],
        "cleanup_paths": [],
        "pid": child.pid,
        "start_time": start_time,
        "created_at": time.time() - 2,
    }


_INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s"})
_RESULT = json.dumps(
    {
        "type": "result", "subtype": "success", "is_error": False, "result": "OK",
        "session_id": "s", "usage": {"input_tokens": 1, "output_tokens": 1},
    }
)


def test_wait_for_torn_trailing_line_without_result_is_failed(tmp_path):
    record = _dead_run_record(tmp_path, _INIT + "\n" + '{"type": "res')
    store = FileRunStore(tmp_path)
    store.put("torn-run", record)

    result = Harness(store=store).wait_for("torn-run", timeout=15)

    assert result.state is RunState.FAILED


def test_wait_for_complete_result_plus_torn_trailing_line_is_completed(tmp_path):
    record = _dead_run_record(tmp_path, _INIT + "\n" + _RESULT + "\n" + '{"type": "assi')
    store = FileRunStore(tmp_path)
    store.put("torn-run", record)

    result = Harness(store=store).wait_for("torn-run", timeout=15)

    assert result.state is RunState.COMPLETED
    assert result.text == "OK"


# -- R6 ----------------------------------------------------------------------


def test_finalize_does_not_overwrite_a_terminal_record(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    events = run_dir / "events.jsonl"
    events.write_text(_INIT + "\n" + _RESULT + "\n")
    stale = {
        "run_id": "run-x",
        "session_id": "s",
        "state": RunState.RUNNING,
        "run_dir": run_dir,
        "cwd": tmp_path,
        "events_path": events,
        "provider": "claude",
        "argv": ["claude"],
        "binary_argv": [sys.executable, str(FAKE_CLAUDE)],
        "cleanup_paths": [],
        "started_at": time.monotonic(),
        "created_at": time.time(),
    }
    store = InMemoryRunStore()
    harness = Harness(store=store)
    # Another process cancelled the run after this Harness read its copy.
    store.put("run-x", {**stale, "state": RunState.CANCELLED})

    harness._finalize("run-x", dict(stale), SimpleNamespace(returncode=0))

    assert store.get("run-x")["state"] is RunState.CANCELLED
    assert harness.poll("run-x").state is RunState.CANCELLED


# -- R7 ----------------------------------------------------------------------


def test_list_runs_sees_runs_started_by_another_process(tmp_path):
    began = time.time() - 1.0  # tolerate coarse wall-clock granularity
    labelled = _start_and_let_starter_exit(
        tmp_path, "--label", "nightly", "--prompt", PROMPT, fake_args=["--sleep", "1"]
    )
    plain = _start_and_let_starter_exit(
        tmp_path, "--prompt", PROMPT, fake_args=["--sleep", "1"]
    )

    observer = _observer(tmp_path)
    listed = observer.list_runs()
    assert [s.run_id for s in listed] == [labelled, plain], "sorted oldest first"
    summaries = {s.run_id: s for s in listed}

    assert set(summaries) == {labelled, plain}
    assert summaries[labelled].label == "nightly"
    assert summaries[plain].label is None
    for run_id, s in summaries.items():
        stored = json.loads((tmp_path / run_id / "record.json").read_text())
        assert isinstance(s.state, RunState)
        # values come from the foreign record, not from a placeholder/clock
        assert s.model == stored["model"] == "haiku"
        assert s.cwd == Path(stored["cwd"])
        assert s.created_at == stored["created_at"]
        assert began <= s.created_at <= time.time()
    assert summaries[labelled].created_at < summaries[plain].created_at

    for run_id in (labelled, plain):
        raw = (tmp_path / run_id / "record.json").read_text()
        assert PROMPT not in raw
        assert PROMPT not in repr(summaries[run_id])
        observer.wait_for(run_id, timeout=30)


# -- R8 ----------------------------------------------------------------------


def _harness_with_fake(store, *fake_args) -> Harness:
    return Harness(
        store=store, claude_argv=[sys.executable, str(FAKE_CLAUDE), *fake_args]
    )


def _spec(tmp_path) -> RunSpec:
    return RunSpec(
        prompt="Reply with exactly OK",
        isolation=Isolation.CLEAN,
        model="haiku",
        artifacts_dir=tmp_path,
    )


def test_poll_reports_growing_progress_while_running(tmp_path):
    harness = _harness_with_fake(
        InMemoryRunStore(), "--ticks", "6", "--tick-interval", "0.3"
    )
    run_id = harness.start(_spec(tmp_path)).run_id

    deadline = time.monotonic() + 15
    first = harness.poll(run_id)
    while first.event_count < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
        first = harness.poll(run_id)
    assert first.state is RunState.RUNNING
    assert first.event_count >= 1
    assert isinstance(first.last_event_at, float)

    second = harness.poll(run_id)
    while second.event_count <= first.event_count and time.monotonic() < deadline:
        time.sleep(0.05)
        second = harness.poll(run_id)

    assert second.state is RunState.RUNNING
    assert second.event_count > first.event_count
    # would fail if last_event_at were constant (e.g. the start time)
    assert second.last_event_at > first.last_event_at

    final = harness.wait(run_id, timeout=30)
    assert final.state is RunState.COMPLETED
    assert final.text == "OK"
    assert final.event_count == 0  # terminal results carry no progress
    assert final.last_event_at is None


def test_poll_does_not_count_a_torn_trailing_line(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_bytes((_INIT + "\n" + _INIT + "\n" + '{"type": "assi').encode())
    store = InMemoryRunStore()
    pid = os.getpid()
    store.put(
        "live",
        {
            "run_id": "live",
            "state": RunState.RUNNING,
            "events_path": events,
            "run_dir": tmp_path,
            "provider": "claude",
            "pid": pid,
            "start_time": _capture_start_time(pid),
            "created_at": time.time(),
        },
    )

    result = Harness(store=store).poll("live")

    assert result.state is RunState.RUNNING
    assert result.event_count == 2


def test_poll_last_event_at_is_the_events_file_mtime_not_the_poll_time(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_bytes((_INIT + "\n").encode())
    old = time.time() - 3600
    os.utime(events, (old, old))
    store = InMemoryRunStore()
    pid = os.getpid()
    store.put(
        "live",
        {
            "run_id": "live",
            "state": RunState.RUNNING,
            "events_path": events,
            "run_dir": tmp_path,
            "provider": "claude",
            "pid": pid,
            "start_time": _capture_start_time(pid),
            "created_at": time.time(),
        },
    )

    result = Harness(store=store).poll("live")

    assert result.last_event_at == pytest.approx(old, abs=2.0)


# -- R9 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake_args, expected",
    [(["--no-result"], RunState.FAILED), ([], RunState.COMPLETED)],
)
def test_list_runs_reconciles_an_orphaned_run(tmp_path, fake_args, expected):
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=fake_args)
    observer = _observer(tmp_path)

    deadline = time.monotonic() + 30
    state = None
    while time.monotonic() < deadline:
        (summary,) = observer.list_runs()
        state = summary.state
        if state is not RunState.RUNNING:
            break
        time.sleep(0.2)

    assert state is expected
    assert FileRunStore(tmp_path).get(run_id)["state"] is expected


def test_poll_reconciles_an_orphaned_run(tmp_path):
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--no-result"])
    observer = _observer(tmp_path)

    deadline = time.monotonic() + 30
    result = observer.poll(run_id)
    while result.state is RunState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.2)
        result = observer.poll(run_id)

    assert result.state is RunState.FAILED


# -- #42 R2: observer-process finalization applies the same rule ------------


def test_observer_finalizes_abandoned_background_task_as_failed(tmp_path):
    """A background task never reported finished must fail the run even
    when the finalizing `Harness` is a foreign observer process reading only
    the persisted record after a JSON round-trip (`FileRunStore`) -- not
    just the starter's own in-process `Harness` (R1, test_harness_offline.py).
    """
    run_id = _start_and_let_starter_exit(
        tmp_path,
        fake_args=["--background-task", "toolu_bg", "--reply", "Waiting..."],
    )
    observer = _observer(tmp_path)

    result = observer.wait_for(run_id, timeout=30)

    assert result.state is RunState.FAILED
    assert result.abandoned_background_tasks == ("toolu_bg",)
    record = FileRunStore(tmp_path).get(run_id)
    assert record["state"] is RunState.FAILED


# -- package 25: wait() is cross-process and never cancels --------------------

WAIT_RUN = FIXTURES / "wait_run.py"


def _run_waiter(artifacts, run_id, *extra):
    return subprocess.Popen(
        [sys.executable, str(WAIT_RUN), str(artifacts), run_id, *extra],
        env=_starter_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _waiter_json(proc):
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def test_wait_from_a_second_process_matches_the_starter(tmp_path):
    starter, run_id = _launch_starter(
        tmp_path, "--wait", fake_args=["--sleep", "3", "--reply", "SHARED"]
    )
    try:
        waiter = _waiter_json(_run_waiter(tmp_path, run_id))
        starter_out, starter_err = starter.communicate(timeout=60)
    finally:
        if starter.poll() is None:
            starter.kill()
    assert starter.returncode == 0, starter_err
    starter_result = json.loads(starter_out.strip().splitlines()[-1])

    assert waiter["state"] == "COMPLETED"
    assert waiter["text"] == "SHARED"
    assert waiter["timed_out"] is False
    assert waiter["elapsed"] >= 1.0, "foreign wait returned before the child could finish"
    assert waiter["text"] == starter_result["text"]
    assert waiter["usage"] == starter_result["usage"]
    assert waiter["usage"] == {"input_tokens": 10, "output_tokens": 2}  # the fake CLI's own usage


def test_wait_on_unknown_run_id_raises_harness_error(tmp_path):
    with pytest.raises(HarnessError):
        _observer(tmp_path).wait("no-such-run", timeout=1)


def test_wait_on_a_terminal_record_returns_at_once():
    store = InMemoryRunStore()
    store.put("run-1", {"run_id": "run-1", "state": RunState.COMPLETED, "text": "done"})
    began = time.monotonic()
    result = Harness(store=store).wait("run-1", timeout=30)
    assert time.monotonic() - began < 2.0
    assert result.state is RunState.COMPLETED
    assert result.text == "done"


def test_two_parallel_waiters_do_not_block_each_other(tmp_path):
    run_a = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "3", "--reply", "A"])
    run_b = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "3", "--reply", "B"])

    began = time.monotonic()
    waiter_a = _run_waiter(tmp_path, run_a)
    waiter_b = _run_waiter(tmp_path, run_b)
    out_a = _waiter_json(waiter_a)
    out_b = _waiter_json(waiter_b)
    elapsed = time.monotonic() - began

    assert out_a["state"] == out_b["state"] == "COMPLETED"
    assert out_a["text"] == "A"
    assert out_b["text"] == "B"
    # Children run concurrently (3 s each). A foreign waiter finalizes a gone
    # process only after _FINALIZE_GRACE_S (3 s), so one waiter takes ~6.5 s;
    # serialised waiting would take about twice that (>= 12 s).
    assert elapsed < 10.0, f"parallel waiters took {elapsed:.1f}s"


def test_running_result_carries_liveness_signs(tmp_path):
    harness = _harness_with_fake(
        InMemoryRunStore(), "--tool-ticks", "6", "--tick-interval", "0.3"
    )
    run_id = harness.start(_spec(tmp_path)).run_id

    deadline = time.monotonic() + 15
    first = harness.poll(run_id)
    while first.event_count < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
        first = harness.poll(run_id)
    assert first.state is RunState.RUNNING
    assert isinstance(first.last_event_at, float)
    assert first.duration_s is not None and first.duration_s > 0
    assert first.last_activity == "tool_use:Bash"

    time.sleep(0.4)
    timed_out = harness.wait(run_id, timeout=0.2)
    assert timed_out.timed_out is True
    assert timed_out.state is RunState.RUNNING
    assert timed_out.event_count >= first.event_count
    assert timed_out.duration_s > first.duration_s  # grows while alive
    assert timed_out.last_activity == "tool_use:Bash"

    final = harness.wait(run_id, timeout=30)
    assert final.state is RunState.COMPLETED
    assert final.event_count == 0
    assert final.last_event_at is None


def test_running_result_last_activity_is_none_for_an_unrecognizable_stream(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_bytes(b'{"type": "mystery"}\nnot json at all\n')
    store = InMemoryRunStore()
    pid = os.getpid()
    store.put(
        "live",
        {
            "run_id": "live",
            "state": RunState.RUNNING,
            "events_path": events,
            "run_dir": tmp_path,
            "provider": "claude",
            "pid": pid,
            "start_time": _capture_start_time(pid),
            "created_at": time.time() - 5,
        },
    )

    result = Harness(store=store).poll("live")

    assert result.state is RunState.RUNNING
    assert result.last_activity is None
    assert result.duration_s is not None and 5 <= result.duration_s < 60


# -- #55: post-completion grace-kill in wait() -------------------------------
#
# A CLEAN `claude -p` child that has already written its own terminal
# `result` event but keeps the OS process alive must not hold `wait()`
# hostage. `--linger <s>` (tests/fixtures/fake_claude.py) reproduces exactly
# that shape: the terminal event lands on disk, then the child sleeps well
# past the point wait() should have returned.

_GRACE_BOUND = 2 * _FINALIZE_GRACE_S + 5  # generous bound: covers one grace period plus slack


def test_wait_grace_kills_a_child_lingering_after_its_result(tmp_path):
    """R1 driving test: the starter's own `wait()` (this process holds the
    `Popen`) returns COMPLETED promptly once the terminal event is on disk,
    without waiting for the OS process itself to exit."""
    store = InMemoryRunStore()
    harness = _harness_with_fake(store, "--linger", "60")
    run_id = harness.start(_spec(tmp_path)).run_id

    began = time.monotonic()
    result = harness.wait(run_id, timeout=30)
    elapsed = time.monotonic() - began

    assert result.state is RunState.COMPLETED
    assert result.text == "OK"
    assert result.timed_out is False
    assert elapsed < _GRACE_BOUND, f"wait() took {elapsed:.1f}s, expected a prompt grace-kill"

    record = store.get(run_id)
    assert _pid_status(record["pid"], record["start_time"]) is False, "child was not killed"


def test_wait_grace_kills_a_lingering_child_with_an_abandoned_background_task(tmp_path):
    """R1 additional coverage (a): the grace-kill finalizes from the terminal
    event alone, so an abandoned background task still fails the run exactly
    as it would for a child that exited normally."""
    store = InMemoryRunStore()
    harness = _harness_with_fake(store, "--background-task", "bg1", "--linger", "60")
    run_id = harness.start(_spec(tmp_path)).run_id

    result = harness.wait(run_id, timeout=30)

    assert result.state is RunState.FAILED
    assert result.abandoned_background_tasks == ("bg1",)


def test_repeated_short_waits_still_grace_kill_a_lingering_child(tmp_path):
    """R1 additional coverage (b) -- F1 guard: the grace anchor must persist
    across repeated wait() calls (it lives on the events file's mtime, not on
    a loop-local variable). Each call here times out (1.0s) well before the
    grace period (3.0s) elapses, so a per-call timer would never fire and
    this would spin to its deadline still RUNNING -- this test is RED against
    that shape of implementation too, not just against today's no-hook code."""
    store = InMemoryRunStore()
    harness = _harness_with_fake(store, "--linger", "60")
    run_id = harness.start(_spec(tmp_path)).run_id

    deadline = time.monotonic() + _GRACE_BOUND
    result = harness.wait(run_id, timeout=1.0)
    while result.timed_out and time.monotonic() < deadline:
        result = harness.wait(run_id, timeout=1.0)

    assert result.timed_out is False
    assert result.state is RunState.COMPLETED


def test_observer_wait_grace_kills_a_lingering_foreign_child(tmp_path):
    """R2 driving test: a cross-process waiter (no `Popen`, only the
    recorded pid+start_time) grace-kills a verified-identity pid too."""
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--linger", "60"])
    observer = _observer(tmp_path)

    began = time.monotonic()
    result = observer.wait(run_id, timeout=30)
    elapsed = time.monotonic() - began

    assert result.state is RunState.COMPLETED
    assert result.text == "OK"
    assert result.timed_out is False
    assert elapsed < _GRACE_BOUND, f"wait() took {elapsed:.1f}s, expected a prompt grace-kill"

    record = FileRunStore(tmp_path).get(run_id)
    assert _pid_status(record["pid"], record["start_time"]) is False, "child was not killed"


def test_late_observer_kills_without_a_fresh_grace(tmp_path):
    """R2 additional coverage (a): the grace anchor is the events file's
    mtime (when `result` was written), not the moment this particular
    wait() call began -- an observer that shows up well after the grace
    period has already elapsed kills (and finalizes) on its very first,
    short wait()."""
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--linger", "60"])
    time.sleep(_FINALIZE_GRACE_S + 1)

    observer = _observer(tmp_path)
    result = observer.wait(run_id, timeout=1.5)

    assert result.state is RunState.COMPLETED
    assert result.timed_out is False


def test_observer_wait_never_signals_an_unverifiable_pid(tmp_path, monkeypatch):
    """R2 additional coverage (b): with no held `Popen` (foreign observer)
    and no verifiable `start_time`, `wait()` must never signal the pid --
    already true of today's code (there is no grace-kill hook at all yet),
    pinned here as the contract `_kill_lingering` must keep once the hook
    exists."""
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--linger", "60"])
    store = FileRunStore(tmp_path)
    record = store.get(run_id)
    pid = record["pid"]
    real_start_time = record["start_time"]
    record["start_time"] = None
    store.put(run_id, record)

    calls = []
    monkeypatch.setattr(
        harness_module,
        "_wait_or_kill",
        lambda *a, **k: calls.append((a, k)),
        raising=False,
    )

    try:
        observer = _observer(tmp_path)
        result = observer.wait(run_id, timeout=_FINALIZE_GRACE_S + 2)

        assert result.timed_out is True
        assert result.state is RunState.RUNNING
        assert calls == [], "an unverifiable pid must never be signalled"
        assert _pid_status(pid, real_start_time) is True, "child must still be alive, untouched"
    finally:
        _wait_or_kill(pid, 5.0, real_start_time)


# -- #59: finalize gone runs promptly across wait() calls and processes ------
#
# `wait(run_id, 0)` from a process without the `Popen` (the agent-harness
# Stop hook) must finalize a run whose child exited, and `duration_s` must
# be the run's own length, not "when someone looked".


def _wait_until_pid_gone(store_dir, run_id, deadline_s: float = 30.0) -> None:
    record = FileRunStore(store_dir).get(run_id)
    deadline = time.monotonic() + deadline_s
    while _pid_status(record["pid"], record.get("start_time")) is not False:
        assert time.monotonic() < deadline, "child never exited"
        time.sleep(0.1)


def _poll_zero_waits(observer, run_id, budget_s: float):
    """Loop wait(run_id, 0) until non-RUNNING or budget elapsed; each call must be short."""
    deadline = time.monotonic() + budget_s
    while True:
        began = time.monotonic()
        result = observer.wait(run_id, 0)
        assert time.monotonic() - began < 1.0, "a wait(run_id, 0) call was slow"
        if result.state is not RunState.RUNNING or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


@pytest.mark.parametrize(
    "fake_args,expected",
    [
        pytest.param(["--sleep", "1"], RunState.COMPLETED, id="completed"),
        pytest.param(["--sleep", "1", "--no-result"], RunState.FAILED, id="failed"),
    ],
)
def test_repeated_zero_waits_finalize_an_exited_foreign_run(tmp_path, fake_args, expected):
    """R1 driving test: repeated wait(run_id, 0) from a Popen-less observer
    finalizes an exited run within the grace period plus a little."""
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=fake_args)
    _wait_until_pid_gone(tmp_path, run_id)
    observer = _observer(tmp_path)

    result = _poll_zero_waits(observer, run_id, _FINALIZE_GRACE_S + 2)

    assert result.state is expected, f"still {result.state} after grace + 2s of wait(run_id, 0)"
    if expected is RunState.COMPLETED:
        assert result.text == "OK"
    else:
        assert FileRunStore(tmp_path).get(run_id)["exit_code"] is None


def test_zero_waits_finalize_while_the_starter_is_alive_and_holds_its_popen(tmp_path):
    """R1 variant (plan-critic F3): the starter stays alive (its Popen unreaped)
    while a separate observer Harness polls wait(run_id, 0)."""
    starter, run_id = _launch_starter(
        tmp_path, "--stop-after", "60", fake_args=["--sleep", "1"]
    )
    try:
        time.sleep(3.0)  # child has written its result and exited by now
        result = _poll_zero_waits(_observer(tmp_path), run_id, _FINALIZE_GRACE_S + 2)
        assert result.state is RunState.COMPLETED
        assert result.text == "OK"
    finally:
        starter.kill()
        starter.wait(timeout=30)


def _assert_duration_in(value, lo, hi, what):
    assert value is not None and lo <= value < hi, f"{what} duration_s={value}"


def test_late_observer_duration_is_the_runs_own_not_finalization_time(tmp_path):
    """R2 driving test: duration_s ends at the run's end, not at finalization."""
    run_id = _start_and_let_starter_exit(tmp_path, fake_args=["--sleep", "1"])
    _wait_until_pid_gone(tmp_path, run_id)
    time.sleep(_FINALIZE_GRACE_S + 3)

    result = _observer(tmp_path).wait(run_id, timeout=30)

    assert result.state is RunState.COMPLETED
    _assert_duration_in(result.duration_s, 0.9, 4.0, "result")
    record = FileRunStore(tmp_path).get(run_id)
    _assert_duration_in(record["duration_s"], 0.9, 4.0, "record")
    provenance = json.loads(Path(record["provenance_path"]).read_text(encoding="utf-8"))
    _assert_duration_in(provenance["duration_s"], 0.9, 4.0, "provenance")


def test_grace_killed_run_duration_ends_at_its_result(tmp_path):
    """R3 driving test: on the lingering branch duration_s is the terminal-event
    time, not the time of the grace-kill."""
    store = InMemoryRunStore()
    harness = _harness_with_fake(store, "--linger", "60")
    run_id = harness.start(_spec(tmp_path)).run_id

    result = harness.wait(run_id, timeout=30)

    assert result.state is RunState.COMPLETED
    assert result.duration_s is not None and result.duration_s < _FINALIZE_GRACE_S
    record = store.get(run_id)
    provenance = json.loads(Path(record["provenance_path"]).read_text(encoding="utf-8"))
    assert provenance["duration_s"] < _FINALIZE_GRACE_S


def test_observer_reports_cancelled_for_a_quiet_run_stopped_late(tmp_path):
    """R4 guard: a stop() long after the run's last output still wins over the
    observer's gone-finalization (pins the durable stop marker)."""
    starter, run_id = _launch_starter(
        tmp_path, "--stop-after", "5", fake_args=["--sleep", "30"]
    )
    try:
        result = _observer(tmp_path).wait_for(run_id, timeout=30)
    finally:
        starter.wait(timeout=60)

    assert result.state is RunState.CANCELLED
    assert FileRunStore(tmp_path).get(run_id)["state"] is RunState.CANCELLED
