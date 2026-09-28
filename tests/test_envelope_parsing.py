"""R1 additional edge-case coverage — the same terminal-event parse driven
offline from tests/fixtures/result_envelope.jsonl: success, is_error +
subtype, structured_output present/absent, usage/cost, and a truncated
stream (no terminal `result` event) mapping to a failure outcome.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib_python_harness.providers.claude_cli import ClaudeCliProvider

FIXTURE = Path(__file__).parent / "fixtures" / "result_envelope.jsonl"


def _load_scenarios():
    scenarios = {}
    with FIXTURE.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            scenarios[obj["scenario"]] = [json.dumps(evt) for evt in obj["stream"]]
    return scenarios


SCENARIOS = _load_scenarios()


def test_success_envelope_parses_to_result_text():
    result = ClaudeCliProvider().parse_events(SCENARIOS["success"])
    assert result.text == "OK"
    assert result.is_error is False
    assert result.usage["input_tokens"] == 50
    assert result.cost == pytest.approx(0.001)


def test_is_error_envelope_carries_subtype():
    result = ClaudeCliProvider().parse_events(SCENARIOS["is_error_with_subtype"])
    assert result.is_error is True
    assert result.subtype == "error_max_turns"


def test_structured_output_present():
    result = ClaudeCliProvider().parse_events(SCENARIOS["structured_output_present"])
    assert result.structured_output == {"answer": 42}


def test_structured_output_absent_is_none():
    result = ClaudeCliProvider().parse_events(SCENARIOS["structured_output_absent"])
    assert result.structured_output is None


def test_truncated_stream_without_terminal_result_is_a_failure():
    # "A missing terminal event => FAILED with the raw tail" (plan Approach).
    # The exact exception type is an implementation detail of parse_events;
    # what matters here is that a truncated stream is never silently
    # accepted as a successful result.
    with pytest.raises(Exception):
        ClaudeCliProvider().parse_events(SCENARIOS["truncated_stream"])


# -- package 25: describe_last_activity (liveness sign on RUNNING results) ----


def _line(obj) -> str:
    return json.dumps(obj)


def _assistant(*blocks) -> str:
    return _line({"type": "assistant", "message": {"content": list(blocks)}})


_INIT_LINE = _line({"type": "system", "subtype": "init", "session_id": "s"})
_TOOL_USE = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
_TEXT = {"type": "text", "text": "hello"}


def test_describe_last_activity_names_the_last_tool_use():
    lines = [_INIT_LINE, _assistant(_TEXT), _assistant(_TOOL_USE)]
    assert ClaudeCliProvider().describe_last_activity(lines) == "tool_use:Bash"


def test_describe_last_activity_uses_the_last_tool_use_block_of_the_newest_event():
    other = {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}
    lines = [_assistant(_TOOL_USE), _assistant(_TEXT, _TOOL_USE, other)]
    assert ClaudeCliProvider().describe_last_activity(lines) == "tool_use:Read"


def test_describe_last_activity_text_only_stream():
    lines = [_INIT_LINE, _assistant(_TEXT)]
    assert ClaudeCliProvider().describe_last_activity(lines) == "text"


def test_describe_last_activity_init_only_stream():
    assert ClaudeCliProvider().describe_last_activity([_INIT_LINE]) == "init"


def test_describe_last_activity_skips_a_truncated_trailing_line():
    lines = [_assistant(_TOOL_USE), '{"type": "assist']
    assert ClaudeCliProvider().describe_last_activity(lines) == "tool_use:Bash"


def test_describe_last_activity_empty_or_unrecognizable_stream_is_none():
    provider = ClaudeCliProvider()
    assert provider.describe_last_activity([]) is None
    assert provider.describe_last_activity(["", "not json", _line({"type": "mystery"})]) is None


def test_describe_last_activity_is_scoped_to_the_newest_event():
    # A stale tool_use earlier in the stream must not outlive newer events.
    lines = [_assistant(_TOOL_USE), _assistant(_TEXT)]
    assert ClaudeCliProvider().describe_last_activity(lines) == "text"
    result = _line({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
    assert ClaudeCliProvider().describe_last_activity([_assistant(_TOOL_USE), result]) == "tool_result"


# -- #42 R1 additional edge-case coverage: abandoned_background_tasks -------
#
# `parse_events` driven directly with hand-built stream-json lines, covering
# the resolution rules the fixture-driven tests in test_harness_offline.py
# and test_harness_observe.py cannot isolate one at a time: which of the
# three signals (notification / non-running poll / TaskStop) resolves a
# pending launch, and what happens when the ack can't be parsed at all.


def _tool_use(tool_use_id, name, input_):
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": input_}


def _bg_launch(launch_id, ack_text=None, task_id="bg1"):
    """The two lines a real backgrounded launch always produces: the Bash
    tool_use with `run_in_background: true`, and its ack `tool_result`.
    `ack_text` overrides the ack content (for the "unparseable ack" cases);
    default is a parseable ack naming `task_id`.
    """
    if ack_text is None:
        ack_text = f"Command running in background with ID: {task_id}. ..."
    return [
        _assistant(
            _tool_use(launch_id, "Bash", {"command": "sleep 75", "run_in_background": True})
        ),
        _line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": launch_id, "content": ack_text}
                    ]
                },
            }
        ),
    ]


def _poll(task_id, status, poll_id="toolu_poll"):
    return [
        _assistant(_tool_use(poll_id, "TaskOutput", {"task_id": task_id})),
        _line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": poll_id,
                            "content": f"<task_id>{task_id}</task_id>\n<status>{status}</status>",
                        }
                    ]
                },
            }
        ),
    ]


def _task_stop(task_id, stop_id="toolu_stop"):
    return _assistant(_tool_use(stop_id, "TaskStop", {"task_id": task_id}))


def _notification(tool_use_id, task_id="bg1", status="completed"):
    text = (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<status>{status}</status>\n"
        "</task-notification>"
    )
    return _line({"type": "user", "message": {"content": text}})


_TERMINAL_RESULT = _line(
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s"}
)


def test_completed_poll_for_a_different_task_id_does_not_resolve():
    lines = (
        [_INIT_LINE]
        + _bg_launch("L1", task_id="bg1")
        + _poll("some-other-task", "completed")
        + [_TERMINAL_RESULT]
    )
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ("L1",)


def test_task_stop_for_the_task_id_resolves():
    lines = [_INIT_LINE] + _bg_launch("L1", task_id="bg1") + [_task_stop("bg1"), _TERMINAL_RESULT]
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ()


def test_poll_with_status_failed_resolves():
    lines = (
        [_INIT_LINE] + _bg_launch("L1", task_id="bg1") + _poll("bg1", "failed") + [_TERMINAL_RESULT]
    )
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ()


def test_unparseable_ack_plus_completed_poll_is_still_flagged():
    lines = (
        [_INIT_LINE]
        + _bg_launch("L1", ack_text="Started an asynchronous job.")
        + _poll("bg1", "completed")
        + [_TERMINAL_RESULT]
    )
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ("L1",)


def test_unparseable_ack_plus_notification_resolves():
    lines = (
        [_INIT_LINE]
        + _bg_launch("L1", ack_text="Started an asynchronous job.")
        + [_notification("L1"), _TERMINAL_RESULT]
    )
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ()


def test_two_launches_with_one_notified_give_only_the_other():
    lines = (
        [_INIT_LINE]
        + _bg_launch("L1", task_id="bg1")
        + _bg_launch("L2", task_id="bg2")
        + [_notification("L1", task_id="bg1"), _TERMINAL_RESULT]
    )
    result = ClaudeCliProvider().parse_events(lines)
    assert result.abandoned_background_tasks == ("L2",)


# -- #55 R3: has_terminal_event -- the grace-kill probe ----------------------
#
# `Harness` looks this method up with `getattr` (like `describe_last_activity`)
# to decide whether a provider's child is even eligible for the post-result
# grace-kill; a provider without it (codex, mistral, fakes injected directly)
# is never grace-killed. Driven directly against `ClaudeCliProvider`, offline:
# true iff some line parses to a dict with `type == "result"`, torn/garbage
# lines skipped rather than raising.


def test_has_terminal_event_detects_only_a_result_line():
    provider = ClaudeCliProvider()
    assert provider.has_terminal_event([_INIT_LINE, _TERMINAL_RESULT]) is True
    assert provider.has_terminal_event([_TERMINAL_RESULT]) is True
    assert provider.has_terminal_event([_INIT_LINE]) is False
    assert provider.has_terminal_event([]) is False
    # torn/garbage lines are skipped, not raised on
    assert provider.has_terminal_event(["not json at all", '{"type": "res']) is False
    assert provider.has_terminal_event(["not json at all", _TERMINAL_RESULT]) is True
