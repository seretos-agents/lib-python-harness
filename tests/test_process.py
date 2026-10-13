"""Offline coverage for R4 — the spawn/stop path, run against a local subprocess.

Ported behaviour from lib_python_worktree's core/process_lifecycle.py
(``_spawn_detached``, ``_send_graceful_signal``, ``_force_kill``,
``_reap_until_gone``, ``_wait_or_kill``, ``_capture_start_time``), POSIX tests
plus native-Windows tests (R6: PATHEXT shim spawn, whole-tree kill). Unlike that reference, stdin carries the run's
prompt text (piped, not DEVNULL) and stdout/stderr are split into two
separate files (events JSONL vs. stderr.txt) rather than one combined log.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from lib_python_harness.runtime.process import (
    _spawn_detached,
    _send_graceful_signal,
    _force_kill,
    _reap_until_gone,
    _wait_or_kill,
    _capture_start_time,
    _pid_status,
    _raw_alive,
)

# Per-test platform marks (was a module-wide POSIX skip). The POSIX tests
# below are unchanged; they use `signal.SIGTERM`-ignoring children and
# `_capture_start_time` (no `psutil` in the `test` extra on windows-latest).
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX signal semantics (SIGTERM-ignoring child, start-time via /proc)",
)
WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="native Windows process control (PATHEXT shims, taskkill)"
)


def _spawn(tmp_path, code):
    return _spawn_detached(
        argv=[sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=None,
        stdin_text="",
        events_path=tmp_path / "events.jsonl",
        stderr_path=tmp_path / "stderr.txt",
    )


@POSIX_ONLY
def test_graceful_exit_on_sigterm(tmp_path):
    proc = _spawn(tmp_path, "import time; time.sleep(30)")
    start_time = _capture_start_time(proc.pid)
    assert start_time is not None

    delivered = _send_graceful_signal(proc.pid)
    assert delivered

    proc.wait(timeout=5)
    assert proc.returncode is not None
    _reap_until_gone(proc.pid)
    assert _pid_status(proc.pid, start_time) is False


@POSIX_ONLY
def test_sigterm_ignoring_child_escalates_to_kill(tmp_path):
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    proc = _spawn(tmp_path, code)
    start_time = _capture_start_time(proc.pid)

    began = time.monotonic()
    _wait_or_kill(proc.pid, timeout=1.0, expected_start_time=start_time)
    elapsed = time.monotonic() - began

    assert elapsed < 5.0
    assert _pid_status(proc.pid, start_time) is False


@POSIX_ONLY
def test_timeout_zero_forces_immediate_kill(tmp_path):
    proc = _spawn(tmp_path, "import time; time.sleep(30)")
    start_time = _capture_start_time(proc.pid)

    began = time.monotonic()
    _wait_or_kill(proc.pid, timeout=0, expected_start_time=start_time)
    elapsed = time.monotonic() - began

    assert elapsed < 2.0
    assert _pid_status(proc.pid, start_time) is False


@POSIX_ONLY
def test_pid_status_reports_mismatched_identity_as_not_alive(tmp_path):
    proc = _spawn(tmp_path, "import time; time.sleep(30)")
    real_start_time = _capture_start_time(proc.pid)

    assert _pid_status(proc.pid, real_start_time) is True
    # A start time that does not match the live process's own must never
    # read as "alive" — this is the tri-state identity check stop() relies
    # on before ever signalling a possibly-recycled pid.
    assert _pid_status(proc.pid, real_start_time - 999) is False

    _force_kill(proc.pid)
    _reap_until_gone(proc.pid)


# -- native Windows (R6) -----------------------------------------------------


def _tasklist_alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace",
    ).stdout
    return f'"{pid}"' in out


def _taskkill_tree(pid: int) -> None:  # test-owned cleanup, independent of the code under test
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def _wait_for(path, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared")


@pytest.fixture
def cmd_shim(tmp_path, monkeypatch):
    """A `probe.cmd` reachable only through PATHEXT, whose grandchild python
    records its pid and sleeps -- the shape of the npm `codex.cmd` shim."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    pidfile = tmp_path / "grandchild.pid"
    script = shim_dir / "grandchild.py"
    script.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    (shim_dir / "probe.cmd").write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "{script}" "{pidfile}"\r\n',
        newline="",
    )
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
    return pidfile


@WINDOWS_ONLY
def test_pathext_shim_can_be_spawned(tmp_path, cmd_shim):
    proc = _spawn_detached(
        argv=["probe"], cwd=str(tmp_path), env=None, stdin_text="",
        events_path=tmp_path / "events.jsonl", stderr_path=tmp_path / "stderr.txt",
    )
    try:
        grandchild = _wait_for(cmd_shim)
        assert proc.poll() is None
        assert _tasklist_alive(grandchild)
    finally:
        _taskkill_tree(proc.pid)


@WINDOWS_ONLY
def test_wait_or_kill_removes_shim_and_its_grandchild(tmp_path, cmd_shim):
    proc = _spawn_detached(
        argv=["probe"], cwd=str(tmp_path), env=None, stdin_text="",
        events_path=tmp_path / "events.jsonl", stderr_path=tmp_path / "stderr.txt",
    )
    try:
        grandchild = _wait_for(cmd_shim)
        start_time = _capture_start_time(proc.pid)

        _wait_or_kill(proc.pid, timeout=1.0, expected_start_time=start_time)

        assert not _tasklist_alive(proc.pid)
        assert not _tasklist_alive(grandchild)
    finally:
        _taskkill_tree(proc.pid)


@WINDOWS_ONLY
def test_raw_alive_reports_true_without_killing_the_process(tmp_path):
    # On Windows os.kill(pid, 0) is not a liveness probe: it can terminate.
    proc = _spawn(tmp_path, "import time; time.sleep(30)")
    try:
        assert _raw_alive(proc.pid) is True
        time.sleep(0.3)
        assert proc.poll() is None, "_raw_alive killed the process it probed"
        assert _tasklist_alive(proc.pid)
    finally:
        _taskkill_tree(proc.pid)


@WINDOWS_ONLY
def test_raw_alive_reports_false_for_a_finished_process(tmp_path):
    proc = _spawn(tmp_path, "pass")
    proc.wait(timeout=10)
    assert _raw_alive(proc.pid) is False


def test_start_time_and_identity_are_decidable_without_psutil(tmp_path):
    """Cross-process identity must be decidable on every platform: a real
    start time, `True` while the child lives, `False` once it exited -- even
    though this process still holds the child's `Popen`/handle (the shape of
    an MCP server that started the run while the observer waits elsewhere)."""
    proc = _spawn(tmp_path, "import time; time.sleep(1.5)")
    try:
        start_time = _capture_start_time(proc.pid)
        assert start_time is not None
        assert _pid_status(proc.pid, start_time) is True
        # identity: a different start time for the same live pid is not us
        assert _pid_status(proc.pid, start_time - 1000.0) is False

        proc.wait(timeout=30)  # `proc` (and its handle) stay referenced
        assert _pid_status(proc.pid, start_time) is False
    finally:
        if proc.poll() is None:
            proc.kill()


@POSIX_ONLY
def test_unreaped_zombie_child_reads_as_not_alive(tmp_path):
    proc = _spawn(tmp_path, "pass")
    start_time = _capture_start_time(proc.pid)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with open(f"/proc/{proc.pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("child never became a zombie")
        # exited but deliberately not reaped (no wait()/poll())
        assert _raw_alive(proc.pid) is False
        assert _pid_status(proc.pid, start_time) is False
    finally:
        proc.wait(timeout=10)


# -- #61: whole-tree close (_close_tree) ---------------------------------------
#
# The child spawns an intermediate that starts a long sleeper and exits at
# once -- the sleeper is an orphaned descendant that `taskkill /T` (parent-pid
# links) and a top-pid kill cannot reach. Job membership (Windows) / the
# process group (POSIX) does.

_INTERMEDIATE = "\n".join(
    [
        "import os, subprocess, sys",
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
        "open(sys.argv[1] + '.tmp', 'w').write(str(p.pid))",
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])",
    ]
)

_ORPHAN_PARENT = (
    "import subprocess, sys, time\n"
    f"subprocess.run([sys.executable, '-c', {_INTERMEDIATE!r}, sys.argv[1]], check=True,"
    " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "time.sleep(120)\n"
)


def _spawn_orphan_tree(tmp_path, tree_name):
    pidfile = tmp_path / "gpid"
    proc = _spawn_detached(
        argv=[sys.executable, "-c", _ORPHAN_PARENT, str(pidfile)],
        cwd=str(tmp_path),
        env=None,
        stdin_text="",
        events_path=tmp_path / "events.jsonl",
        stderr_path=tmp_path / "stderr.txt",
        tree_name=tree_name,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (
        pidfile.exists() and pidfile.read_text().strip()
    ):
        time.sleep(0.05)
    gpid = int(pidfile.read_text().strip())
    return proc, gpid, _capture_start_time(gpid)


def _hard_kill(*pids):
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 9)
        except OSError:
            pass


def test_close_tree_kill_reaches_an_orphaned_descendant(tmp_path):
    from lib_python_harness.runtime.process import _close_tree

    proc, gpid, gstart = _spawn_orphan_tree(tmp_path, f"t61-kill-{os.getpid()}")
    try:
        _close_tree(proc.pid, f"t61-kill-{os.getpid()}", kill=True)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and _pid_status(gpid, gstart) is not False:
            time.sleep(0.1)
        assert _pid_status(gpid, gstart) is False
    finally:
        _hard_kill(gpid, proc.pid)


def test_close_tree_without_kill_leaves_it(tmp_path):
    from lib_python_harness.runtime.process import _close_tree

    proc, gpid, gstart = _spawn_orphan_tree(tmp_path, f"t61-keep-{os.getpid()}")
    try:
        _close_tree(proc.pid, f"t61-keep-{os.getpid()}", kill=False)
        time.sleep(0.5)
        assert _pid_status(gpid, gstart) is not False
    finally:
        _hard_kill(gpid, proc.pid)
