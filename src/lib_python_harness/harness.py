"""The façade: `Harness` (start/poll/stop/cleanup/run) plus the module-level
`run(spec)` convenience the acceptance criterion's literal `run(RunSpec(...))`
resolves to.

Named `harness.py`, not `run.py`, on purpose: a submodule literally called
`lib_python_harness.run` would collide with (shadow, or be shadowed by) the
exported callable `lib_python_harness.run` — see `tests/test_public_api.py::
test_run_resolves_to_a_function_not_the_facade_module`.

`Harness` owns two things a `Provider` does not: the run's *lifecycle*
(`RunState` + `_TRANSITIONS`, enforced by `runtime.lifecycle.transition`) and
its *artifacts* — `provenance.json`, `events.jsonl` and `stderr.txt` always
land on real disk under `<artifacts_dir>/<run_id>/` regardless of which
`RunStore` is used for the run *record* (default: `InMemoryRunStore`, so a
plain `Harness()` call never leaves a stray `record.json` around, while
still writing a real provenance file — see the plan-critic note this
resolves: the acceptance call `run(RunSpec(prompt=..., isolation=...,
model=...))` supplies no `artifacts_dir`, so the harness falls back to a
`Harness`-owned temp directory it creates lazily, distinct from the
consumer-owned `artifacts_dir` case `cleanup()` never deletes).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import HarnessError, RunIdentityUnverifiedError, UnsupportedByProvider
from .providers.base import Provider, RunResult, RunSpec
from .providers.claude_cli import ClaudeCliProvider
from .providers.codex_cli import CodexCliProvider
from .providers.mistral_cli import MistralCliProvider
from .runtime.lifecycle import RunState, transition
from .runtime.process import (
    _capture_start_time,
    _force_kill,
    _pid_status,
    _reap_until_gone,
    _send_graceful_signal,
    _spawn_detached,
    _wait_or_kill,
    resolve_executable,
)
from .runtime.store import InMemoryRunStore, RunStore

DEFAULT_STOP_TIMEOUT = 10.0

_LIVE_STATES = (RunState.CREATED, RunState.RUNNING)
_TERMINAL_STATES = (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)

# Two uses, same value, same idea -- give a run that looks done a short
# window before `wait()` acts on it unilaterally:
# 1. How long `wait_for` lets a vanished process's record stay RUNNING before
#    it finalizes the run itself: a `stop()` in the starter's process kills
#    the pid first and writes CANCELLED a moment later, and there is no
#    cross-process lock to close that race, so the observer waits it out
#    instead of writing FAILED over a cancel in flight. The window is
#    measured from a durable anchor (`_last_sign_of_life`: created_at,
#    events/stderr mtimes, `stop_requested_at`), not a per-call timer, so
#    repeated `wait(run_id, 0)` calls from any process converge.
# 2. (#55) How long a provider's child gets to exit on its own after its
#    terminal `result` event has been written before `wait()` grace-kills it
#    (`_kill_lingering`, via `_wait_or_kill` which itself waits up to this
#    same value after the graceful signal) -- a clean `claude -p` child that
#    keeps running past its own terminal event must not hold `wait()`
#    hostage. The two grace windows are independent and can both apply on
#    the grace-kill path (signal -> up to `_FINALIZE_GRACE_S` for the
#    graceful exit -> force kill), so the worst-case bound there is close to
#    `2 * _FINALIZE_GRACE_S`, not one grace period.
_FINALIZE_GRACE_S = 3.0


@dataclass(frozen=True)
class RunSummary:
    """One row of `Harness.list_runs()`: the narrow, prompt-free view of a
    run record."""

    run_id: str
    state: RunState
    model: str | None
    cwd: Path | None
    created_at: float | None
    label: str | None = None

# Serialises `resume()`'s scan-then-insert across EVERY `Harness` in this
# process, not per instance: two instances on one store must not both pass the
# same-session liveness guard. Cross-process exclusion is not provided (a
# shared `FileRunStore` has no cross-process lock).
_SESSION_LOCK = threading.Lock()

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

# name -> provider class: what `RunSpec.provider` selects. One instance is
# created per run (a provider may keep per-run state between
# `build_launch_plan` and `parse_events`).
PROVIDERS: dict[str, type] = {
    "claude": ClaudeCliProvider,
    "codex": CodexCliProvider,
    "mistral": MistralCliProvider,
}


class Harness:
    """Owns the run lifecycle for a `Provider`: `start`, `poll`, `stop`,
    `cleanup`, and `run` (= `start` + `wait`, one code path).
    """

    def __init__(
        self,
        store: RunStore | None = None,
        claude_argv: list[str] | None = None,
        provider: Provider | None = None,
    ) -> None:
        self.store: RunStore = store if store is not None else InMemoryRunStore()
        # Override for tests/alternate installs; `None` = the provider's own
        # `binary_argv`.
        self.claude_argv: list[str] | None = (
            list(claude_argv) if claude_argv is not None else None
        )
        # An injected instance is registered under its own `.name` and wins
        # for `spec.provider == provider.name`.
        self.provider = provider if provider is not None else ClaudeCliProvider()
        self._injected: dict[str, Provider] = (
            {provider.name: provider} if provider is not None else {}
        )
        self._run_providers: dict[str, Provider] = {}
        self._default_artifacts_dir: Path | None = None
        self._processes: dict[str, subprocess.Popen] = {}
        self._version_cache: dict[tuple[str, ...], str | None] = {}

    def _resolve_provider(self, spec: RunSpec) -> Provider:
        name = spec.provider
        if name in self._injected:
            return self._injected[name]
        cls = PROVIDERS.get(name)
        if cls is None:
            raise HarnessError(
                f"unknown provider {name!r}; known providers: "
                + ", ".join(sorted({*PROVIDERS, *self._injected}))
            )
        return cls()

    # -- artifacts ---------------------------------------------------------

    def _artifacts_base(self, spec: RunSpec) -> Path:
        if spec.artifacts_dir is not None:
            return Path(spec.artifacts_dir)
        if self._default_artifacts_dir is None:
            self._default_artifacts_dir = Path(
                tempfile.mkdtemp(prefix="lib-python-harness-runs-")
            )
        return self._default_artifacts_dir

    def _cli_version(self, binary_argv: list[str]) -> str | None:
        key = tuple(binary_argv)
        if key not in self._version_cache:
            text = ""
            try:
                proc = subprocess.run(
                    resolve_executable(list(binary_argv)) + ["--version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                text = (proc.stdout or "") + (proc.stderr or "")
            except (OSError, subprocess.TimeoutExpired):
                text = ""
            match = _VERSION_RE.search(text)
            self._version_cache[key] = match.group(0) if match else None
        return self._version_cache[key]

    # -- lifecycle -----------------------------------------------------

    def start(self, spec: RunSpec) -> RunResult:
        run_id = str(uuid_module.uuid4())
        session_id = str(uuid_module.uuid4())

        # Provider selection happens first, before any record or artifact is
        # written: an unknown name raises HarnessError.
        provider = self._resolve_provider(spec)
        run_dir = self._artifacts_base(spec) / run_id

        # Raises UnsafeCwdError / UnsupportedByProvider before anything is
        # ever recorded, if spec.cwd fails the CLEAN recipe or the provider
        # cannot honour a field.
        plan = provider.build_launch_plan(spec, session_id=session_id, run_dir=run_dir)
        return self._launch(
            provider,
            plan,
            run_id=run_id,
            session_id=session_id,
            run_dir=run_dir,
            prompt=spec.prompt,
            fields={
                "model": spec.model,
                "effort": spec.effort,
                "system_prompt_sha256": hashlib.sha256(
                    (spec.system_prompt or "").encode()
                ).hexdigest(),
                "allow_nonempty_cwd": spec.allow_nonempty_cwd,
                "label": spec.label,
            },
        )

    def _launch(
        self,
        provider: Provider,
        plan: Any,
        *,
        run_id: str,
        session_id: str,
        run_dir: Path,
        prompt: str,
        fields: dict[str, Any],
        exclusive_session: bool = False,
    ) -> RunResult:
        """Record, spawn and return the RUNNING result — the one code path
        under `start()` and `resume()`. With `exclusive_session`, the
        same-session liveness scan and the first `put()` share one
        process-wide critical section."""
        self._run_providers[run_id] = provider

        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.txt"

        record: dict[str, Any] = {
            "run_id": run_id,
            "session_id": session_id,
            "state": RunState.CREATED,
            "run_dir": run_dir,
            "cwd": Path(plan.cwd),
            "created_at": time.time(),
            "events_path": events_path,
            "stderr_path": stderr_path,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "provider": provider.name,
            **fields,
        }
        if exclusive_session:
            with _SESSION_LOCK:
                try:
                    self._require_session_idle(session_id)
                except HarnessError:
                    self._run_providers.pop(run_id, None)
                    raise
                self.store.put(run_id, record)
        else:
            self.store.put(run_id, record)

        binary_argv = list(self.claude_argv or provider.binary_argv)
        argv = binary_argv + plan.argv
        record["binary_argv"] = binary_argv
        # Names present in this process's own environment that did not make
        # it into plan.env — what the provider actually scrubbed for this run.
        scrubbed_env = sorted(set(os.environ) - set(plan.env))
        record["argv"] = argv
        record["cleanup_paths"] = list(plan.cleanup_paths)
        record["scrubbed_env"] = scrubbed_env
        self.store.put(run_id, record)

        started_at = time.monotonic()
        try:
            proc = _spawn_detached(
                argv=argv,
                cwd=plan.cwd,
                env=plan.env,
                stdin_text=plan.stdin,
                events_path=events_path,
                stderr_path=stderr_path,
            )
        except Exception:
            self._remove_cleanup_paths(record)
            record["state"] = transition(record["state"], RunState.FAILED)
            duration_s = time.monotonic() - started_at
            record["exit_code"] = None
            record["duration_s"] = duration_s
            record["provenance_path"] = self._write_provenance(
                record, exit_code=None, duration_s=duration_s
            )
            self.store.put(run_id, record)
            raise

        pid = proc.pid
        start_time = _capture_start_time(pid)
        self._processes[run_id] = proc

        record["state"] = transition(record["state"], RunState.RUNNING)
        record.update(
            {
                "pid": pid,
                "start_time": start_time,
            }
        )
        self.store.put(run_id, record)

        return self._record_to_result(record)

    def resume(
        self, run_id: str, prompt: str, *, timeout: float | None = None
    ) -> RunResult:
        """Send a follow-up `prompt` to the finished run `run_id`, blocking
        until the new run ends: exactly `start_resume()` followed by `wait()`
        on the new run (one code path). Use `start_resume()` to not block.

        `timeout` has `wait()` semantics: an expired timeout ends the waiting,
        not the new run, which stays `RUNNING` (`timed_out=True`).

        A resume is a NEW run: new `run_id`, the origin's `session_id`, and
        `resumed_from` = the origin's `run_id`; the origin record is left
        untouched. The origin's argv is replayed by the provider (isolation
        flags included), not rebuilt, and runs in the origin's cwd (a fresh
        temp directory if it no longer exists).

        Raises `HarnessError` for an unknown run, a `CREATED`/`RUNNING`
        origin (resume is terminal-only) or a session that already has a live
        run; `UnsupportedByProvider` when the origin's provider cannot resume.
        A `CANCELLED` origin resumes mechanically, but only the transcript
        the CLI actually wrote exists, so the answer may rest on a partial
        turn. Same-session exclusion holds across every `Harness` in this
        process; it is NOT enforced across processes.
        """
        started = self.start_resume(run_id, prompt)
        return self.wait(started.run_id, timeout=timeout)

    def start_resume(self, run_id: str, prompt: str) -> RunResult:
        """Non-blocking `resume()`: send a follow-up `prompt` to the finished
        run `run_id` and return as soon as the follow-up child is spawned.

        The returned `RunResult` is the NEW run: new `run_id`, the origin's
        `session_id`, `state == RUNNING` and `resumed_from` = the origin's
        `run_id` (in the record). Unlike `resume()` it does not wait for the
        run to end; afterwards use `poll` / `wait_for` / `stop` on the new
        `run_id` (`wait` works too on the same `Harness` instance). The
        origin record is left untouched. Validation, errors, isolation-flag
        replay, cwd handling and same-session exclusion are exactly those of
        `resume()`.
        """
        origin = self._require_record(run_id)
        state = origin["state"]
        if state not in _TERMINAL_STATES:
            raise HarnessError(
                f"cannot resume run {run_id} in state {state.name}: "
                "resume is only allowed for a COMPLETED, FAILED or CANCELLED run"
            )

        provider = self._resolve_recorded_provider(origin)
        builder = getattr(provider, "build_resume_plan", None)
        if builder is None:
            raise UnsupportedByProvider(
                f"provider {provider.name!r} does not support resume "
                "(it has no build_resume_plan)"
            )

        session_id = origin["session_id"]
        # Early, lock-free refusal so nothing is built for a doomed resume;
        # `_launch(exclusive_session=True)` repeats it atomically with the put.
        self._require_session_idle(session_id)
        binary_argv = origin.get("binary_argv") or []
        provider_argv = list(origin["argv"])[len(binary_argv):]
        plan = builder(
            provider_argv=provider_argv,
            session_id=session_id,
            cwd=origin.get("cwd"),
            prompt=prompt,
        )

        new_run_id = str(uuid_module.uuid4())
        run_dir = Path(origin["run_dir"]).parent / new_run_id
        started = self._launch(
            provider,
            plan,
            run_id=new_run_id,
            session_id=session_id,
            run_dir=run_dir,
            prompt=prompt,
            fields={
                "model": origin.get("model"),
                "effort": origin.get("effort"),
                "system_prompt_sha256": origin.get("system_prompt_sha256"),
                "allow_nonempty_cwd": origin.get("allow_nonempty_cwd", False),
                "resumed_from": run_id,
                "label": origin.get("label"),
            },
            exclusive_session=True,
        )
        return started

    def _resolve_recorded_provider(self, record: dict[str, Any]) -> Provider:
        name = record.get("provider") or "claude"
        if name in self._injected:
            return self._injected[name]
        cls = PROVIDERS.get(name)
        if cls is None:
            raise UnsupportedByProvider(
                f"provider {name!r} of this run is unknown; cannot resume it"
            )
        return cls()

    def _require_session_idle(self, session_id: str) -> None:
        for other in self.store.list():
            if other.get("session_id") == session_id and other.get("state") in _LIVE_STATES:
                raise HarnessError(
                    f"cannot resume session {session_id}: run {other.get('run_id')} "
                    f"is still {other['state'].name}"
                )

    def poll(self, run_id: str) -> RunResult:
        record = self._require_record(run_id)
        if record["state"] == RunState.RUNNING:
            if self._finalize_if_ended(run_id, record) or self._finalize_if_gone(
                run_id, record
            ):
                record = self._require_record(run_id)
        return self._record_to_result(record)

    def wait_for(
        self,
        run_id: str,
        timeout: float | None = None,
        poll_interval: float = 0.25,
    ) -> RunResult:
        """Alias of `wait()` (same semantics, same code path)."""
        return self.wait(run_id, timeout=timeout, poll_interval=poll_interval)

    def list_runs(self) -> list[RunSummary]:
        """Every run in the store, oldest first, as `RunSummary` rows. Runs
        still recorded `RUNNING` whose process is gone are reconciled to
        their end state first (same rule as `poll`)."""
        summaries: list[RunSummary] = []
        for record in self.store.list():
            run_id = record.get("run_id")
            if record.get("state") == RunState.RUNNING and run_id is not None:
                if self._finalize_if_ended(run_id, record) or self._finalize_if_gone(
                    run_id, record
                ):
                    record = self.store.get(run_id) or record
            cwd = record.get("cwd")
            summaries.append(
                RunSummary(
                    run_id=run_id,
                    state=record.get("state"),
                    model=record.get("model"),
                    cwd=Path(cwd) if cwd else None,
                    created_at=record.get("created_at"),
                    label=record.get("label"),
                )
            )
        summaries.sort(key=lambda s: s.created_at or 0.0)
        return summaries

    def wait(
        self,
        run_id: str,
        timeout: float | None = None,
        poll_interval: float = 0.25,
    ) -> RunResult:
        """Block until run `run_id` reaches a terminal state, whichever
        process started it, and return its result.

        A time limit ends the WAITING, never the run: an expired `timeout`
        returns the still-`RUNNING` result with `timed_out=True` (nothing is
        signalled; continue with `wait(run_id)`). Only an explicit `stop()`
        ever yields `CANCELLED`. `timeout=None` waits indefinitely. A run
        whose process vanished without finalizing is finalized here once it
        has been gone for a short grace period (so a `stop()` running in the
        starter's process wins and is reported `CANCELLED`); with no exit
        code known, the terminal event alone decides `COMPLETED` vs `FAILED`.
        A provider child that has already written its terminal event but
        keeps the OS process alive (#55) is likewise grace-killed after a
        short period and finalized from that event alone, whichever process
        is waiting — except when the pid's identity cannot be verified (no
        `psutil`/`start_time` and this process never held the `Popen`
        itself), in which case `wait()` never signals it and just keeps
        waiting as it always did. Raises `HarnessError` for an unknown
        `run_id`.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            record = self._require_record(run_id)
            if record["state"] == RunState.RUNNING:
                if self._finalize_if_ended(run_id, record):
                    continue
                if self._is_gone(run_id, record):
                    # Durable anchor (file mtimes + record fields), so every
                    # call and every process measures the same grace window.
                    if time.time() - self._last_sign_of_life(record) >= _FINALIZE_GRACE_S:
                        self._finalize(run_id, record, None)
                        continue
                else:
                    written_at = self._result_written_at(run_id, record)
                    if (
                        written_at is not None
                        and time.time() - written_at >= _FINALIZE_GRACE_S
                        and self._kill_lingering(run_id, record)
                    ):
                        self._finalize(run_id, record, None)
                        continue
            if record["state"] in _TERMINAL_STATES:
                return self._record_to_result(record)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return replace(self._record_to_result(record), timed_out=True)
            delay = poll_interval if remaining is None else min(poll_interval, remaining)
            time.sleep(max(delay, 0.0))

    def run(self, spec: RunSpec) -> RunResult:
        started = self.start(spec)
        return self.wait(started.run_id, timeout=spec.timeout)

    def stop(self, run_id: str, timeout: float = DEFAULT_STOP_TIMEOUT) -> RunResult:
        record = self._require_record(run_id)

        # No terminal-state early return: this is the first thing that can
        # observably happen. A stop() on an already-COMPLETED/FAILED/
        # CANCELLED run raises right here, before any signal is ever sent —
        # see tests/test_harness_offline.py::test_stop_on_completed_run_raises,
        # which asserts nothing reaches the (patched) signal sender.
        transition(record["state"], RunState.CANCELLED)

        pid = record.get("pid")
        start_time = record.get("start_time")
        proc = self._processes.get(run_id)
        signalled_live = False

        if pid is not None:
            status = _pid_status(pid, start_time)
            if status is None:
                if proc is None:
                    raise RunIdentityUnverifiedError(
                        f"cannot verify identity of pid {pid} for run {run_id}; "
                        "refusing to signal a possibly-recycled pid"
                    )
                status = True  # this process still holds the child's Popen

            def still_alive() -> bool:
                current = _pid_status(pid, start_time)
                if current is None:  # cannot verify identity (no psutil)
                    return proc.poll() is None if proc is not None else False
                return current

            if status:
                signalled_live = True
                # Durable marker: an observer in another process measures its
                # gone-grace from this kill request, not from the child's
                # last (possibly old) write.
                record["stop_requested_at"] = time.time()
                self.store.put(run_id, record)
                delivered = _send_graceful_signal(pid)
                if delivered:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if not still_alive():
                            break
                        time.sleep(0.05)
                if still_alive():
                    _force_kill(pid)
                _reap_until_gone(pid)

        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            if proc.returncode is not None:
                record["exit_code"] = proc.returncode

        ended_at = self._ended_at(
            run_id, record, time.time() if signalled_live else None
        )
        duration_s = max(0.0, ended_at - record.get("created_at", ended_at))
        record["duration_s"] = duration_s
        record["state"] = transition(record["state"], RunState.CANCELLED)
        record["provenance_path"] = self._write_provenance(
            record, exit_code=record.get("exit_code"), duration_s=duration_s
        )
        self._remove_cleanup_paths(record)
        self.store.put(run_id, record)
        self._processes.pop(run_id, None)
        self._run_providers.pop(run_id, None)
        return self._record_to_result(record)

    def cleanup(self, run_id: str, remove_cwd: bool = False) -> None:
        """Drop the run's record from the store and stop tracking its
        process handle. Never touches the CLI transcript. Removes the run's
        cwd only when `remove_cwd=True` (default `False`): resume does not
        need the original cwd (see `docs/run-lifecycle.md`), but a cwd may
        hold files the caller cares about, so the safer default is to leave
        it. The artifacts dir (`provenance.json`, `events.jsonl`,
        `stderr.txt`) is never deleted by the library.
        """
        record = self._require_record(run_id)
        self._processes.pop(run_id, None)
        self._run_providers.pop(run_id, None)
        self._remove_cleanup_paths(record)
        if remove_cwd:
            cwd = record.get("cwd")
            if cwd:
                shutil.rmtree(Path(cwd), ignore_errors=True)
        self.store.remove(run_id)

    # -- internals -----------------------------------------------------

    @staticmethod
    def _remove_cleanup_paths(record: dict[str, Any]) -> None:
        """Delete the per-run private paths the provider asked to have
        removed (e.g. a scrubbed CODEX_HOME). Idempotent, never raises."""
        for path in record.get("cleanup_paths") or ():
            shutil.rmtree(path, ignore_errors=True)

    def _require_record(self, run_id: str) -> dict[str, Any]:
        record = self.store.get(run_id)
        if record is None:
            raise HarnessError(f"unknown run_id: {run_id}")
        return record

    def _resolve_transcript_path(self, session_id: str) -> Path | None:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(config_dir) if config_dir else Path.home() / ".claude"
        matches = sorted(base.glob(f"projects/*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def _write_provenance(
        self, record: dict[str, Any], *, exit_code: int | None, duration_s: float
    ) -> Path:
        """Write `provenance.json` for `record` and return its path. The one
        writer for all three terminal-state routes (`_finalize`'s COMPLETED/
        FAILED, `stop()`'s CANCELLED, and `start()`'s spawn-failure FAILED) —
        every field below is already on the record by the time any of the
        three call it; `exit_code`/`duration_s` are parameters because the
        three routes compute them differently (a spawned-but-never-ran
        process has no exit code at all, hence `None`, not a missing field).
        """
        cli_version = self._cli_version(record.get("binary_argv") or ["claude"])
        provenance = {
            "flags": record["argv"],
            "cwd": str(record["cwd"]),
            "model": record.get("model"),
            "effort": record.get("effort"),
            "prompt_sha256": record.get("prompt_sha256"),
            "system_prompt_sha256": record.get("system_prompt_sha256"),
            "provider": record.get("provider"),
            "cli_version": cli_version,
            # alias of `cli_version`, kept for slice-1 consumers
            "claude_version": cli_version,
            "exit_code": exit_code,
            "duration_s": duration_s,
            "scrubbed_env": record.get("scrubbed_env", []),
            "allow_nonempty_cwd": record.get("allow_nonempty_cwd", False),
            # the fact only - never the contents/paths of the private home
            "scrubbed_home": bool(record.get("cleanup_paths")),
            "session_id": record.get("session_id"),
            "resumed_from": record.get("resumed_from"),
        }
        provenance_path = Path(record["run_dir"]) / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, indent=2))
        return provenance_path

    def _finalize_if_ended(self, run_id: str, record: dict[str, Any]) -> bool:
        """Finalize a RUNNING run whose `Popen` this process holds and which
        has exited. Returns whether it finalized."""
        proc = self._processes.get(run_id)
        if proc is not None and proc.poll() is not None:
            self._finalize(run_id, record, proc)
            return True
        return False

    def _is_gone(self, run_id: str, record: dict[str, Any]) -> bool:
        """A RUNNING run this process holds no `Popen` for whose recorded
        `pid`+`start_time` no longer names a live process."""
        if run_id in self._processes:
            return False
        pid = record.get("pid")
        if pid is None:
            return False
        return _pid_status(pid, record.get("start_time")) is False

    def _finalize_if_gone(self, run_id: str, record: dict[str, Any]) -> bool:
        """Finalize an orphaned RUNNING run (process gone, no `Popen` here)
        without an exit code. Returns whether it finalized."""
        if self._is_gone(run_id, record):
            self._finalize(run_id, record, None)
            return True
        return False

    def _last_sign_of_life(self, record: dict[str, Any]) -> float:
        """The latest durable sign the run was alive: the max of
        `created_at`, the events/stderr files' mtimes and the record's
        `stop_requested_at`. Depends only on files and record fields, never
        on in-memory state, so every call and process agrees. Never raises."""
        candidates: list[float] = []
        created = record.get("created_at")
        if isinstance(created, (int, float)):
            candidates.append(float(created))
        stop_at = record.get("stop_requested_at")
        if isinstance(stop_at, (int, float)):
            candidates.append(float(stop_at))
        for key in ("events_path", "stderr_path"):
            path = record.get(key)
            if path is None:
                continue
            try:
                candidates.append(Path(path).stat().st_mtime)
            except Exception:
                pass
        return max(candidates) if candidates else time.time()

    def _ended_at(
        self, run_id: str, record: dict[str, Any], observed_exit: float | None
    ) -> float:
        """When the run ended, for `duration_s`: the terminal-event time, else
        an exit this process observed itself, else the last durable sign of
        life."""
        written_at = self._result_written_at(run_id, record)
        if written_at is not None:
            return written_at
        if observed_exit is not None:
            return observed_exit
        return self._last_sign_of_life(record)

    def _result_written_at(self, run_id: str, record: dict[str, Any]) -> float | None:
        """When the run's terminal `result` event was written (#55), or
        `None` when it hasn't been yet, its provider has no
        `has_terminal_event` probe (looked up with `getattr`, like
        `_describe_activity` — Codex/Mistral/an injected fake without the
        method are never grace-killed), or anything about reading the events
        file goes wrong.

        The events file's own mtime is the anchor, not `time.time()` at
        probe time: `result` is the child's last write, so the mtime already
        *is* the moment it landed (a later write only delays the kill, it
        never advances it). Anchoring there rather than to a loop-local
        variable means repeated short `wait()` calls, a fresh observer
        process, and a late-arriving waiter all measure the same grace
        window from the same point (critic F1)."""
        try:
            provider = self._run_providers.get(run_id)
            if provider is None:
                provider = self._resolve_recorded_provider(record)
            probe = getattr(provider, "has_terminal_event", None)
            if probe is None:
                return None
            events_path = record.get("events_path")
            if events_path is None:
                return None
            events_path = Path(events_path)
            if not probe(self._read_event_lines(events_path)):
                return None
            return events_path.stat().st_mtime
        except Exception:
            return None

    def _kill_lingering(self, run_id: str, record: dict[str, Any]) -> bool:
        """Grace-kill the run's child once its terminal event has aged past
        `_FINALIZE_GRACE_S` (#55). Verifies pid identity first — an
        unverifiable pid (`_pid_status` returns `None`) is never signalled
        unless this process still holds the child's own `Popen`, exactly
        `stop()`'s rule; a pid already confirmed gone is left to the
        ended/gone paths. Otherwise reuses `_wait_or_kill` for the real
        signal -> bounded wait -> force-kill -> reap sequence. Returns
        whether it killed (i.e. the caller may now finalize from the
        terminal event alone)."""
        pid = record.get("pid")
        if pid is None:
            return False
        proc = self._processes.get(run_id)
        start_time = record.get("start_time")
        status = _pid_status(pid, start_time)
        if status is None:
            if proc is None:
                return False  # cannot verify identity; never signal
            status = True  # this process still holds the child's Popen
        if not status:
            return False  # already gone; the ended/gone paths handle it
        _wait_or_kill(pid, _FINALIZE_GRACE_S, start_time)
        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        return True

    @staticmethod
    def _read_event_lines(events_path: Path) -> list[str]:
        """Non-empty lines of `events.jsonl`, minus a half-written trailing
        line (no newline and not valid JSON) that a live child is still
        writing or that a crash tore off."""
        try:
            text = Path(events_path).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = text.split("\n")
        last = lines.pop()  # text after the final newline ("" when it ends in one)
        out = [ln for ln in lines if ln.strip()]
        if last.strip():
            try:
                json.loads(last)
            except ValueError:
                pass
            else:
                out.append(last)
        return out

    def _finalize(
        self, run_id: str, record: dict[str, Any], proc: subprocess.Popen | None
    ) -> None:
        """Move a RUNNING record to its end state. `proc` is `None` when this
        process did not start the run: then there is no exit code and the
        terminal event alone decides COMPLETED vs FAILED. Idempotent: a record
        that is already terminal in the store is left as it is."""
        if self._already_terminal(run_id):
            return
        events_path = Path(record["events_path"])
        exit_code = proc.returncode if proc is not None else None
        ended_at = self._ended_at(
            run_id, record, time.time() if proc is not None else None
        )
        duration_s = max(0.0, ended_at - record.get("created_at", ended_at))

        lines = self._read_event_lines(events_path)

        parse_error: Exception | None = None
        parsed: RunResult | None = None
        try:
            provider = self._run_providers.get(run_id)
            if provider is None:
                try:
                    provider = self._resolve_recorded_provider(record)
                except Exception:
                    provider = self.provider
            parsed = provider.parse_events(lines)
        except Exception as exc:  # missing terminal event => FAILED
            parse_error = exc

        session_id = record["session_id"]
        transcript_path = self._resolve_transcript_path(session_id)

        provenance_path = self._write_provenance(
            record, exit_code=exit_code, duration_s=duration_s
        )

        self._remove_cleanup_paths(record)
        failed = (
            parse_error is not None
            or (proc is not None and exit_code != 0)
            or bool(parsed and parsed.abandoned_background_tasks)
        )
        new_state = RunState.FAILED if failed else RunState.COMPLETED
        record["state"] = transition(record["state"], new_state)
        record["exit_code"] = exit_code
        record["duration_s"] = duration_s
        record["provenance_path"] = provenance_path
        record["transcript_path"] = transcript_path
        if parsed is not None:
            record["text"] = parsed.text
            record["is_error"] = parsed.is_error
            record["subtype"] = parsed.subtype
            record["structured_output"] = parsed.structured_output
            record["usage"] = parsed.usage
            record["cost"] = parsed.cost
            record["abandoned_background_tasks"] = list(parsed.abandoned_background_tasks)
            if parsed.session_id:
                record["session_id"] = parsed.session_id

        # Another process may have finalized (e.g. cancelled) the run while we
        # parsed: keep its end state rather than overwrite it. No cross-process
        # lock exists, so this narrows the window rather than closing it.
        if not self._already_terminal(run_id):
            self.store.put(run_id, record)
        self._processes.pop(run_id, None)
        self._run_providers.pop(run_id, None)

    def _already_terminal(self, run_id: str) -> bool:
        stored = self.store.get(run_id)
        return stored is not None and stored.get("state") in _TERMINAL_STATES

    def _describe_activity(self, record: dict[str, Any], lines: list[str]) -> str | None:
        """Provider-derived label of the run's newest activity; `None` when
        the provider has no `describe_last_activity`, the provider is unknown,
        or anything goes wrong (a liveness sign must never raise)."""
        try:
            provider = self._run_providers.get(record.get("run_id"))
            if provider is None:
                provider = self._resolve_recorded_provider(record)
            describe = getattr(provider, "describe_last_activity", None)
            return describe(lines) if describe is not None else None
        except Exception:
            return None

    def _record_to_result(self, record: dict[str, Any]) -> RunResult:
        event_count = 0
        last_event_at: float | None = None
        last_activity: str | None = None
        duration_s = record.get("duration_s")
        events_path = record.get("events_path")
        if record.get("state") == RunState.RUNNING:
            duration_s = time.time() - record.get("created_at", time.time())
            if events_path:
                path = Path(events_path)
                try:
                    last_event_at = path.stat().st_mtime
                    lines = self._read_event_lines(path)
                    event_count = len(lines)
                    last_activity = self._describe_activity(record, lines)
                except OSError:
                    pass
        return RunResult(
            run_id=record.get("run_id"),
            session_id=record.get("session_id"),
            text=record.get("text", ""),
            is_error=record.get("is_error", False),
            subtype=record.get("subtype"),
            structured_output=record.get("structured_output"),
            usage=record.get("usage", {}),
            cost=record.get("cost"),
            transcript_path=record.get("transcript_path"),
            state=record.get("state"),
            duration_s=duration_s,
            event_count=event_count,
            last_event_at=last_event_at,
            last_activity=last_activity,
            abandoned_background_tasks=tuple(record.get("abandoned_background_tasks") or ()),
        )


def run(spec: RunSpec) -> RunResult:
    """Module-level convenience: `Harness().run(spec)`. The acceptance
    criterion's literal surface form `run(RunSpec(...))` resolves here — a
    plain function, not `lib_python_harness.harness` the module (see
    `tests/test_public_api.py::test_run_resolves_to_a_function_not_the_facade_module`).
    """
    return Harness().run(spec)
