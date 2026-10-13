"""Process control: spawn, signal, reap. Ported from
`lib-python-worktree/core/process_lifecycle.py` (`_spawn_detached`,
`_send_graceful_signal`, `_force_kill`, `_reap_until_gone`, `_wait_or_kill`,
`_capture_start_time`), copied not imported. POSIX is the reference
behaviour; native Windows gets a minimal `os.name == "nt"` branch at each
point where a POSIX primitive does not exist or is destructive (no
handle-scan machinery, no new dependency): `shutil.which` for PATHEXT shims
(the npm `codex.cmd`), `CREATE_NEW_PROCESS_GROUP`, a non-destructive liveness
check (never `os.kill(pid, 0)` — on Windows that is `TerminateProcess`), and
`taskkill /T /F` so a shim's grandchild dies with it. A run's whole process
tree (also descendants orphaned by an exited parent) is closed by
`_close_tree`: a named Windows Job Object assigned at spawn, the process group
on POSIX.

Unlike that reference: stdin carries the run's prompt text (via a backing
file, never a blocking pipe write — see `_spawn_detached`), and
stdout/stderr are split into two separate files (events JSONL vs.
`stderr.txt`) rather than one combined log.

PID identity is tri-state (`_pid_status` returns `True`/`False`/`None`):
a two-valued answer would have to collapse "cannot tell" onto either
"signal it" (risking a recycled pid) or "never signal" (breaking cancel).
`None` means "cannot verify" — `Harness.stop()` only acts on it while this
process still holds the child's own `Popen` object; otherwise it raises
`RunIdentityUnverifiedError` rather than guess.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path


_IS_WINDOWS = os.name == "nt"


def resolve_executable(argv: list[str]) -> list[str]:
    """On Windows, resolve `argv[0]` through `PATH` + `PATHEXT` (CreateProcess
    does not apply `PATHEXT`, so a bare `codex` cannot start the npm
    `codex.cmd` shim); unchanged elsewhere or when nothing is found."""
    if _IS_WINDOWS and argv:
        found = shutil.which(argv[0])
        if found:
            return [found, *argv[1:]]
    return list(argv)


def _spawn_detached(
    *,
    argv: list[str],
    cwd: str,
    env: dict[str, str] | None,
    stdin_text: str,
    events_path: Path,
    stderr_path: Path,
    tree_name: str | None = None,
) -> subprocess.Popen:
    """Spawn `argv`, stdin from `stdin_text`, stdout to `events_path`,
    stderr to `stderr_path`. POSIX `start_new_session=True` (Windows:
    `CREATE_NEW_PROCESS_GROUP`) so the child survives this process's own
    controlling terminal/process group.

    The prompt is written to a small sidecar file and handed to the child
    as a real stdin file object (not a `PIPE` we then write to) — a `PIPE`
    write from the parent can deadlock against a child that has not yet
    started reading, for no benefit here since stdout/stderr are already
    going straight to files rather than pipes we'd need to drain.

    `tree_name` (Windows only; POSIX needs nothing, `start_new_session` makes
    the pgid the pid) puts the child into a named Job Object so `_close_tree`
    can later reach the whole tree, including descendants whose parent has
    already exited (which `taskkill /T` cannot). Best effort.
    """
    events_path = Path(events_path)
    stderr_path = Path(stderr_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    stdin_path = events_path.parent / ".stdin"
    stdin_path.write_text(stdin_text or "")

    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        argv = resolve_executable(argv)

    with open(stdin_path, "rb") as stdin_file, open(events_path, "wb") as events_file, open(
        stderr_path, "wb"
    ) as stderr_file:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin_file,
            stdout=events_file,
            stderr=stderr_file,
            **kwargs,
        )
    if _IS_WINDOWS and tree_name:
        _assign_job(proc.pid, tree_name)
    return proc


_JOBS: dict[str, object] = {}  # tree_name -> job handle opened by this process
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_JOB_OBJECT_TERMINATE = 0x0008


def _job_name(tree_name: str) -> str:
    return "Local\\lib-python-harness-" + tree_name


def _job_kernel32():
    from ctypes import wintypes

    k = _kernel32()
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.OpenJobObjectW.restype = wintypes.HANDLE
    k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return k


def _assign_job(pid: int, tree_name: str) -> None:
    """Create the named job and assign `pid` to it. No `KILL_ON_JOB_CLOSE`:
    runs outlive the starting process. Never raises; on failure `taskkill`
    stays the fallback."""
    try:
        k = _job_kernel32()
        job = k.CreateJobObjectW(None, _job_name(tree_name))
        if not job:
            return
        proc = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        ok = bool(proc) and k.AssignProcessToJobObject(job, proc)
        if proc:
            k.CloseHandle(proc)
        if ok:
            _JOBS[tree_name] = job
        else:
            k.CloseHandle(job)
    except Exception:
        pass


def _close_tree(pid: int | None, tree_name: str | None, *, kill: bool) -> None:
    """Kill (`kill=True`) the run's whole process tree and/or release the
    tree handle. Windows: the named job (own handle, else opened by name --
    works from another process while any member lives). POSIX: `killpg` on the
    run's group (pgid == pid via `start_new_session`). Never raises."""
    try:
        if _IS_WINDOWS:
            if not tree_name:
                return
            k = _job_kernel32()
            job = _JOBS.pop(tree_name, None)
            if not job and kill:
                job = k.OpenJobObjectW(_JOB_OBJECT_TERMINATE, False, _job_name(tree_name))
            if job:
                if kill:
                    k.TerminateJobObject(job, 1)
                k.CloseHandle(job)
        elif kill and pid is not None and pid != os.getpgrp():
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def _capture_start_time(pid: int) -> float | None:
    """Best-effort process start time (epoch seconds), or `None` if it
    cannot be determined. Prefers `psutil` if already importable (no new
    runtime dependency is added by this library); falls back to
    `/proc/<pid>/stat` on Linux and `GetProcessTimes` (ctypes) on Windows.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore

    if psutil is not None:
        try:
            return psutil.Process(pid).create_time()
        except Exception:
            return None

    if _IS_WINDOWS:
        return _windows_start_time(pid)

    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        after = data.rsplit(")", 1)[1].split()
        starttime_ticks = int(after[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as fh:
            uptime_seconds = float(fh.read().split()[0])
        boot_time = time.time() - uptime_seconds
        return boot_time + starttime_ticks / clk_tck
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError, OSError):
        return None


def _raw_alive(pid: int) -> bool:
    if _IS_WINDOWS:
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # An exited-but-unreaped child (zombie) still answers `kill(pid, 0)`.
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """`True` when `/proc/<pid>/stat` reports state `Z`; `False` when it says
    anything else or `/proc` cannot be read (then `kill(pid, 0)` stands)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        return data.rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _kernel32():
    """`kernel32` with the signatures the process helpers below rely on."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _windows_alive(pid: int) -> bool:
    """Non-destructive liveness check: `OpenProcess` +
    `GetExitCodeProcess == STILL_ACTIVE`. Never `os.kill(pid, 0)`."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED (5): exists but not queryable -> alive;
        # anything else (e.g. ERROR_INVALID_PARAMETER 87) -> gone.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_start_time(pid: int) -> float | None:
    """Process creation time (epoch seconds) via `GetProcessTimes`, or `None`
    when the process cannot be opened. No `psutil` needed."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return (ticks - 116444736000000000) / 1e7  # 100 ns since 1601 -> epoch s
    finally:
        kernel32.CloseHandle(handle)


def _pid_status(pid: int, expected_start_time: float | None) -> bool | None:
    """Tri-state identity check: `True` (alive and it's the same process),
    `False` (gone, or a different process now holds this pid), `None`
    (cannot tell — e.g. no start time recorded or obtainable).

    A matching start time alone is not "alive": a pid kept in existence by an
    open handle (Windows) or an unreaped zombie (POSIX) still reports its
    start time, so liveness is decided by `_raw_alive` as the last step.
    """
    if expected_start_time is None:
        return None

    current = _capture_start_time(pid)
    if current is None:
        return None if _raw_alive(pid) else False

    if abs(current - expected_start_time) > 1.0:
        return False
    return _raw_alive(pid)


def _send_graceful_signal(pid: int) -> bool:
    """SIGTERM. Returns `True` if delivered, `False` if the pid was already
    gone. On Windows there is no graceful signal to a console-less process
    tree, so it returns `False` and callers go straight to `_force_kill`."""
    if _IS_WINDOWS:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _force_kill(pid: int) -> None:
    """SIGKILL, swallowing "already gone". On Windows: `taskkill /T /F`, so
    the whole tree (an npm shim's grandchild) dies, not just the top pid."""
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _reap_until_gone(pid: int, timeout: float = 5.0) -> None:
    """Poll until `pid` is no longer a live/zombie child of this process,
    or `timeout` elapses. Safe to call after the process has already been
    reaped (e.g. via `Popen.wait()`) — `os.waitpid` raising
    `ChildProcessError` just means there is nothing left to reap.
    """
    deadline = time.monotonic() + timeout
    if _IS_WINDOWS:
        while time.monotonic() < deadline and _raw_alive(pid):
            time.sleep(0.05)
        return
    while time.monotonic() < deadline:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped_pid == pid:
            return
        if not _raw_alive(pid):
            return
        time.sleep(0.05)


def _still_alive(pid: int, expected_start_time: float | None) -> bool:
    """`_pid_status` collapsed for the kill path: an unverifiable identity
    (`None`, e.g. Windows without `psutil`) counts as alive while the pid is
    live, so the force-kill is not silently skipped."""
    status = _pid_status(pid, expected_start_time)
    if status is None:
        return _raw_alive(pid)
    return status


def _wait_or_kill(pid: int, timeout: float, expected_start_time: float | None) -> None:
    """Graceful signal -> bounded wait -> force kill -> reap, identity-checked.

    `timeout=0` skips straight to the force-kill branch (no waiting), which
    is "immediate kill" in effect while still going through the same
    graceful-signal-first code path.
    """
    if _pid_status(pid, expected_start_time) is False:
        return

    delivered = _send_graceful_signal(pid)

    if delivered:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _still_alive(pid, expected_start_time):
                break
            time.sleep(0.05)

    if _still_alive(pid, expected_start_time):
        _force_kill(pid)

    _reap_until_gone(pid)
