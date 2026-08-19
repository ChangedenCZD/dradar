import json
from pathlib import Path

from dradar.runner import aggregate_codex_session_usage


def _session(path: Path, session_id: str, role: str, usages: list[dict],
             parent: str | None = None, inherited: dict | None = None,
             history_mode: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "exec"
    if role == "subagent":
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    session_meta = {
        "id": session_id, "thread_source": role, "source": source,
    }
    if history_mode is not None:
        session_meta["history_mode"] = history_mode
    events = [{"type": "session_meta", "payload": session_meta}]
    if inherited is not None:
        events += [
            {"type": "session_meta", "payload": {
                "id": parent, "thread_source": "user", "source": "exec",
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started",
            }},
            {"type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "total_token_usage": inherited},
            }, "timestamp": "2026-08-17T00:59:58Z"},
        ]
    events += [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
    ]
    events += [{"type": "event_msg", "timestamp": (
        f"2026-08-17T01:00:{index:02d}Z"
    ), "payload": {
        "type": "token_count", "info": {"total_token_usage": usage},
    }} for index, usage in enumerate(usages)]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _usage(input_tokens, cached, output, reasoning=0):
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output, "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output}


def test_aggregates_final_root_and_subagent_counters(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions" / "2026" / "07" / "19"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(20, 10, 2), _usage(100, 60, 10, 4),
    ])
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(150, 80, 15, 7),
    ], parent="root-1", inherited=_usage(100, 60, 10, 4))

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["agent_session_count"] == 2
    assert usage["root_session_count"] == 1
    assert usage["subagent_session_count"] == 1
    assert usage["n_input_tokens"] == 150
    assert usage["n_cache_tokens"] == 80
    assert usage["n_output_tokens"] == 15
    assert usage["n_reasoning_output_tokens"] == 7
    assert usage["sessions"][1]["parent_session_id"] == "root-1"
    assert usage["timed_usage_complete"] is True
    assert len(usage["token_usage_events"]) == 3


def test_duplicate_session_id_uses_largest_cumulative_record(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "old.jsonl", "root-1", "user", [_usage(10, 5, 1)])
    _session(sessions / "new.jsonl", "root-1", "user", [_usage(30, 20, 4)])

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["session_file_count"] == 2
    assert usage["agent_session_count"] == 1
    assert usage["n_input_tokens"] == 30
    assert usage["n_output_tokens"] == 4


def test_paginated_subagent_counters_start_at_zero(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10, 4),
    ], history_mode="paginated")
    # Codex 0.148+ retains parent metadata/task boundaries in paginated child
    # files but omits inherited token counters.  The child's cumulative values
    # are standalone and must be added once, not rejected or subtracted.
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(50, 20, 5, 3),
    ], parent="root-1", inherited=None, history_mode="paginated")
    child_events = [json.loads(line) for line in (
        sessions / "child.jsonl").read_text().splitlines()]
    child_events.insert(1, {"type": "session_meta", "payload": {
        "id": "root-1", "thread_source": "user", "source": "exec",
        "history_mode": "paginated",
    }})
    child_events.insert(2, {"type": "event_msg", "payload": {
        "type": "task_started",
    }})
    (sessions / "child.jsonl").write_text(
        "\n".join(json.dumps(event) for event in child_events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["n_input_tokens"] == 150
    assert usage["n_cache_tokens"] == 80
    assert usage["n_output_tokens"] == 15
    assert usage["n_reasoning_output_tokens"] == 7
    assert usage["timed_usage_complete"] is True
    assert len(usage["token_usage_events"]) == 2


def test_unknown_child_history_without_baseline_remains_incomplete(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10),
    ])
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(50, 20, 5),
    ], parent="root-1")
    child_events = [json.loads(line) for line in (
        sessions / "child.jsonl").read_text().splitlines()]
    child_events.insert(1, {"type": "session_meta", "payload": {
        "id": "root-1", "thread_source": "user", "source": "exec",
    }})
    child_events.insert(2, {"type": "event_msg", "payload": {
        "type": "task_started",
    }})
    (sessions / "child.jsonl").write_text(
        "\n".join(json.dumps(event) for event in child_events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["n_input_tokens"] == 100


def test_paginated_marker_with_inherited_counter_is_not_double_counted(
    tmp_path: Path,
):
    sessions = tmp_path / "agent" / "sessions"
    root_usage = _usage(100, 60, 10)
    _session(sessions / "root.jsonl", "root-1", "user", [root_usage])
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(150, 80, 15),
    ], parent="root-1", inherited=root_usage, history_mode="paginated")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["n_input_tokens"] == 100


def test_missing_child_token_count_marks_aggregate_incomplete(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [_usage(30, 20, 4)])
    child = sessions / "child.jsonl"
    child.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "child-1", "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "root-1"}}},
    }}) + "\n" + json.dumps({"type": "turn_context", "payload": {
        "model": "gpt-5.6-terra"}}) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["agent_session_count"] == 2
    assert usage["subagent_session_count"] == 1
    assert usage["n_input_tokens"] == 30


def test_no_sessions_returns_none(tmp_path: Path):
    assert aggregate_codex_session_usage(tmp_path) is None
