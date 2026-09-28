#!/usr/bin/env python3
"""Canned stream-json emitter standing in for the real `claude` binary (R7).

Used by ``tests/test_harness_offline.py::test_provenance_fields_from_spawned_run``
so that test can drive a full spawn/parse/persist cycle through the real
``Harness``/``ClaudeCliProvider`` code paths without a live CLI or auth.

Ignores its argv except ``--version`` (answered the way the real CLI would,
so the harness's own `claude --version` provenance step has something real
to parse), ``--session-id`` (echoed back into the emitted events, the way
the real CLI would echo the session it was told to use), ``--tools`` (#37
R0/R2: echoed, split on `,`, into the ``init`` event's own ``tools`` key --
a stand-in default set stands in for it when the flag is absent, the way the
real CLI's entrypoint default does) and ``--sleep <seconds>`` (R3/R4: used by
``tests/test_harness_offline.py::test_stop_cancels_running_child`` to keep a
real child alive long enough for `Harness.stop()` to have something to
signal and kill — the plain mode below exits the instant it emits its
terminal event, so nothing would be left to cancel). Reads and discards
stdin (the prompt), then emits a fixed stream-json event sequence ending in
a terminal ``result`` event, mimicking
``claude -p --output-format stream-json --verbose``.

``--linger <seconds>`` (#55): sleep AFTER the terminal ``result`` event has
already been printed (and flushed), instead of before it -- reproduces a
clean `claude` CLI child that keeps its OS process alive for a while past
having already written its own terminal event. The opposite end of
``--sleep`` (which delays the child from writing `result` at all): here the
event is on disk, timestamped by the moment `events.jsonl`'s mtime updates,
before the process itself lingers.
"""
from __future__ import annotations

import json
import sys
import time

FAKE_VERSION = "0.0.1 (Claude Code)"

# #37 R0: a plain `claude -p --model haiku --output-format stream-json
# --verbose` session (no --tools), probed 2026-09-22 against installed
# `claude` v2.1.278 (.adev/37-2/r0/probe1.jsonl), carries a 116-entry `tools`
# key on its own `init` event -- the entrypoint's default session set. This
# stand-in is not that full list, only a representative subset: the six
# ordinary tools an offline test still wants to see, plus every one of the
# ticket's seven forbidden names -- the three the ticket calls
# directly-callable (`ListAgents`, `ReportFindings`, `ScheduleWakeup`) and
# the four deferred families (`Cron*`, `Task*`, `RemoteTrigger`,
# `PushNotification`), all of which R0's probe confirmed present in the real
# plain list (answer to R0 (c): all seven observed, not just the three
# direct ones). A second probe with `--tools Read,Glob` showed the flag
# narrows this list to exactly `Read`/`Glob` plus other MCP-server tool
# names never part of the harness's own default set -- none of the seven
# forbidden names survive it (R0 (b)/(d)).
FAKE_DEFAULT_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "ListAgents",
    "ReportFindings",
    "ScheduleWakeup",
    "CronCreate",
    "CronDelete",
    "CronList",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "RemoteTrigger",
    "PushNotification",
]


def _init_tools(argv: list[str]) -> list[str]:
    """The `init` event's own `tools` list: the `--tools` operand split on
    `,` when the flag is present (mirrors `providers.claude_cli._split_tools`
    -- empty/whitespace-only entries drop out, so a CLEAN run's always-present
    but often-empty `--tools ""` yields `[]`), else `FAKE_DEFAULT_TOOLS`
    (R0's plain-probe stand-in) -- the fixture's only channel for observing
    what a spawned run's own session-level tool allowlist actually was."""
    if "--tools" in argv:
        idx = argv.index("--tools")
        operand = argv[idx + 1] if idx + 1 < len(argv) else ""
        return [item.strip() for item in operand.split(",") if item.strip()]
    return list(FAKE_DEFAULT_TOOLS)


def main() -> int:
    argv = sys.argv[1:]

    if "--version" in argv:
        print(FAKE_VERSION)
        return 0

    # Drain stdin (the prompt) the same way the real CLI would.
    stdin_text = sys.stdin.read()

    session_id = "00000000-0000-4000-8000-000000000000"
    for i, token in enumerate(argv):
        if token in ("--session-id", "--resume") and i + 1 < len(argv):
            session_id = argv[i + 1]

    # Emitted first, same as the plain mode below, so a caller that lets a
    # --sleep run finish (instead of killing it) still gets a real init
    # event before the sleep — and, if never killed, the same terminal
    # event sequence afterwards.
    init_event = {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "tools": _init_tools(argv),
    }
    print(json.dumps(init_event), flush=True)

    # --background-task <id>: right after init and before any
    # --ticks/--tool-ticks output, emits a Bash tool_use launched with
    # run_in_background: true (id = <id>) plus its ack tool_result naming a
    # fixed background task id "bg1" (#42 R1/R2/R3 -- the ticket's repro is a
    # backgrounded tool_use whose completion notification never lands before
    # the run's own turn ends). Two optional flags, each checked after the
    # launch so their fixed ordering matches the plan's fixture spec:
    #   --background-poll <status>: a TaskOutput poll naming task "bg1" and
    #     the given <status> (e.g. "running", "completed", "failed").
    #   --background-notify: a <task-notification> naming task "bg1" and
    #     tool-use-id <id> (the launch's own id), status "completed".
    if "--background-task" in argv:
        launch_id = argv[argv.index("--background-task") + 1]
        task_id = "bg1"
        print(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": launch_id,
                                "name": "Bash",
                                "input": {
                                    "command": "sleep 75",
                                    "run_in_background": True,
                                },
                            }
                        ]
                    },
                }
            ),
            flush=True,
        )
        print(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": launch_id,
                                "content": (
                                    f"Command running in background with ID: "
                                    f"{task_id}. Use TaskOutput to read its output."
                                ),
                            }
                        ]
                    },
                }
            ),
            flush=True,
        )

        if "--background-poll" in argv:
            status = argv[argv.index("--background-poll") + 1]
            poll_id = f"toolu_poll_{task_id}"
            print(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": poll_id,
                                    "name": "TaskOutput",
                                    "input": {"task_id": task_id},
                                }
                            ]
                        },
                    }
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": poll_id,
                                    "content": (
                                        f"<task_id>{task_id}</task_id>\n"
                                        f"<status>{status}</status>"
                                    ),
                                }
                            ]
                        },
                    }
                ),
                flush=True,
            )

        if "--background-notify" in argv:
            print(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                "<task-notification>\n"
                                f"<task-id>{task_id}</task-id>\n"
                                f"<tool-use-id>{launch_id}</tool-use-id>\n"
                                "<status>completed</status>\n"
                                "</task-notification>"
                            )
                        },
                    }
                ),
                flush=True,
            )

    # --ticks <n> / --tick-interval <s>: emit n assistant events, spaced
    # tick-interval apart, before the terminal event -- a run whose events
    # log visibly grows while it is alive (live-progress tests).
    if "--ticks" in argv:
        ticks = int(argv[argv.index("--ticks") + 1])
        interval = 0.3
        if "--tick-interval" in argv:
            interval = float(argv[argv.index("--tick-interval") + 1])
        for n in range(ticks):
            time.sleep(interval)
            print(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": f"tick {n}"}]},
                    }
                ),
                flush=True,
            )

    # --tool-ticks <n>: emit n assistant events whose content is a `tool_use`
    # block (name "Bash"), spaced --tick-interval apart (default 0.3 s).
    if "--tool-ticks" in argv:
        tool_ticks = int(argv[argv.index("--tool-ticks") + 1])
        interval = 0.3
        if "--tick-interval" in argv:
            interval = float(argv[argv.index("--tick-interval") + 1])
        for n in range(tool_ticks):
            time.sleep(interval)
            print(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"toolu_{n}",
                                    "name": "Bash",
                                    "input": {"command": "ls"},
                                }
                            ]
                        },
                    }
                ),
                flush=True,
            )

    if "--sleep" in argv:
        idx = argv.index("--sleep")
        seconds = float(argv[idx + 1]) if idx + 1 < len(argv) else 5.0
        time.sleep(seconds)

    # --reply <text>: the final answer (default "OK").
    reply = "OK"
    if "--reply" in argv:
        reply = argv[argv.index("--reply") + 1]
    # --echo-stdin: reply with the drained stdin text verbatim, so a test can
    # observe what the child actually received as its user message.
    if "--echo-stdin" in argv:
        reply = stdin_text
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": reply}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": reply,
            "session_id": session_id,
            "duration_ms": 42,
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "total_cost_usd": 0.0001,
        },
    ]
    # --no-result: end without a terminal `result` event (a truncated
    # stream). --exit-code <n>: the process exit code (default 0).
    exit_code = 0
    if "--exit-code" in argv:
        exit_code = int(argv[argv.index("--exit-code") + 1])
    if "--no-result" in argv:
        return exit_code
    for event in events:
        print(json.dumps(event), flush=True)
    if "--linger" in argv:
        idx = argv.index("--linger")
        linger_seconds = float(argv[idx + 1]) if idx + 1 < len(argv) else 5.0
        time.sleep(linger_seconds)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
