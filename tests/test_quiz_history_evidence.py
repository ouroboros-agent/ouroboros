"""Accepted quiz answers remain exact room evidence after lifecycle/mailbox GC."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ouroboros.gateway.task_decision import answer_decision
from ouroboros.memory import Memory
from ouroboros.owner_mailbox import cleanup_task_mailbox, drain_owner_entries
from ouroboros.owner_quiz import quiz_states, record_asked
from supervisor import message_bus, queue, state


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # Explicitly bind process-global roots before any production helper runs.
    for module, names in (
        (state, ("DRIVE_ROOT", "STATE_PATH", "STATE_LAST_GOOD_PATH", "STATE_LOCK_PATH")),
        (queue, ("DRIVE_ROOT", "QUEUE_SNAPSHOT_PATH")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    state.init(tmp_path)
    queue.init(tmp_path)
    assert state.DRIVE_ROOT == queue.DRIVE_ROOT == tmp_path
    assert state.STATE_PATH == tmp_path / "state" / "state.json"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    bridge = message_bus.LocalChatBridge()
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "_BRIDGE", bridge)
    task = {"id": "task-quiz", "chat_id": 1, "drive_root": str(tmp_path)}
    monkeypatch.setattr(queue, "RUNNING", {task["id"]: {"task": task}})
    monkeypatch.setattr(queue, "PENDING", [])
    return SimpleNamespace(root=tmp_path, bridge=bridge, frames=frames, task=task)


def _ask(runtime, quiz_id, *, chat_id=1):
    block = record_asked(
        runtime.root, runtime.task["id"], quiz_id=quiz_id,
        question=f"Choose for {quiz_id}?", options=["First", "Second"],
        option_details=["First benefit and cost", "Second benefit and cost"],
        recommended_index=1, stake="Delivery time", assumption="Research meanwhile",
    )
    ok, detail = runtime.bridge.send_quiz(
        chat_id, quiz_id, block["question"],
        [{"label": "First", "detail": block["option_details"][0]},
         {"label": "Second", "detail": block["option_details"][1], "recommended": True}],
        stake=block["stake"], assumption=block["assumption"], task_id=runtime.task["id"],
    )
    assert ok, detail


def _answer(runtime, quiz_id, *, request_id, index=0, comment=""):
    body = {"request_id": request_id,
            "decision_id": f"quiz:{runtime.task['id']}:{quiz_id}", "comment": comment}
    if index is not None:
        body["option_index"] = index
    return asyncio.run(answer_decision(runtime.root, body))


def _facts(runtime):
    rows, coverage = Memory(runtime.root).read_chat_generations()
    assert coverage["snapshot_stable"] and not coverage["gaps"]
    return [row for row in rows if row.get("type") == "quiz_answer"]


def test_answers_survive_eighteen_quizzes_mailbox_gc_and_rotation(runtime):
    for index in range(18):
        quiz_id = f"q{index:02}"
        _ask(runtime, quiz_id)
        selected = 0 if index % 2 == 0 else None
        assert _answer(runtime, quiz_id, request_id=f"answer-{index}", index=selected,
                       comment=f"  Verbatim choice {index}\nsecond line  ")[0] == 200
        if index == 8:
            state.rotate_jsonl_log_if_needed(runtime.root, "chat.jsonl", "chat", max_bytes=1)
    assert len(quiz_states(runtime.root, runtime.task["id"])) == 16
    assert "q00" not in quiz_states(runtime.root, runtime.task["id"])
    cleanup_task_mailbox(runtime.root, runtime.task["id"])
    state.rotate_jsonl_log_if_needed(runtime.root, "chat.jsonl", "chat", max_bytes=1)
    assert not drain_owner_entries(runtime.root, runtime.task["id"], include_acknowledged=True)
    facts = _facts(runtime)
    assert len(facts) == 18
    for index, row in enumerate(facts):
        quiz = row["quiz"]
        assert row["source"] == "owner_quiz_answer"
        assert row["client_message_id"] == f"quiz_answer:task-quiz:q{index:02}"
        assert row["ts"] == quiz["answered_at"] and quiz["asked_at"] <= quiz["answered_at"]
        assert quiz["question"] == f"Choose for q{index:02}?"
        assert quiz["options"] == ["First", "Second"]
        assert quiz["option_details"] == ["First benefit and cost", "Second benefit and cost"]
        assert quiz["recommended_index"] == 1
        assert quiz["comment"] == f"  Verbatim choice {index}\nsecond line  "
        assert quiz["request_id"] == f"answer-{index}"
        if index % 2:
            assert "answered_index" not in quiz and "rejected all offered options" in row["text"]
        else:
            assert quiz["answered_index"] == 0 and "chose option 1: First" in row["text"]
    assert len([frame for frame in runtime.frames if frame.get("type") == "quiz"]) == 18
    assert len([frame for frame in runtime.frames if frame.get("type") == "quiz_state"]) == 18
    assert not [frame for frame in runtime.frames if frame.get("type") == "chat"]


@pytest.mark.parametrize("initial_index", [0, None])
def test_same_request_retry_after_rotation_preserves_winning_source(runtime, initial_index):
    _ask(runtime, "retry")
    assert _answer(runtime, "retry", request_id="one", index=initial_index,
                   comment="  Winning words  ")[0] == 200
    state.rotate_jsonl_log_if_needed(runtime.root, "chat.jsonl", "chat", max_bytes=1)
    before = _facts(runtime)
    status, result = _answer(runtime, "retry", request_id="one", index=1, comment="Changed retry")
    assert status == 200 and result["duplicate"] is True
    assert _facts(runtime) == before
    assert len(before) == 1 and before[0]["quiz"]["comment"] == "  Winning words  "
    assert before[0]["quiz"].get("answered_index") == initial_index


def test_competing_answers_preserve_only_the_recorded_winner(runtime):
    _ask(runtime, "race")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda index: _answer(
            runtime, "race", request_id=f"request-{index}", index=index,
            comment=f"Owner choice {index}",
        ), [0, 1]))
    assert sorted(status for status, _ in outcomes) == [200, 409]
    winner = quiz_states(runtime.root, runtime.task["id"])["race"]
    [fact] = _facts(runtime)
    assert fact["quiz"] == winner
    assert fact["quiz"]["comment"] == f"Owner choice {winner['answered_index']}"


def test_retry_heals_history_write_failure_from_the_winning_block(runtime, monkeypatch):
    _ask(runtime, "heal")
    real = message_bus.log_chat

    def fail_answer(*args, **kwargs):
        if kwargs.get("record_type") == "quiz_answer":
            raise OSError("simulated history write failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(message_bus, "log_chat", fail_answer)
    status, result = _answer(runtime, "heal", request_id="same", index=0, comment="Accepted")
    assert status == 503 and result["reason_code"] == "quiz_history_write_failed"
    assert quiz_states(runtime.root, runtime.task["id"])["heal"]["answered_index"] == 0
    assert not _facts(runtime)
    monkeypatch.setattr(message_bus, "log_chat", real)
    status, result = _answer(runtime, "heal", request_id="same", index=1, comment="Retry payload")
    assert status == 200 and result["duplicate"] is True
    [fact] = _facts(runtime)
    assert fact["quiz"]["answered_index"] == 0 and fact["quiz"]["comment"] == "Accepted"
    [mailbox] = drain_owner_entries(runtime.root, runtime.task["id"], include_acknowledged=True)
    assert mailbox["text"] == fact["text"]


def test_answer_history_uses_canonical_root_and_project_binding(runtime):
    from ouroboros.projects_registry import bind_task_to_project, create_project

    project = create_project(runtime.root, "quiz-room", name="Quiz room")
    bind_task_to_project(runtime.root, runtime.task["id"], project["id"], origin={"absent": "system"})
    child = runtime.root / "child-drive"
    runtime.task["drive_root"] = str(child)
    _ask(runtime, "project", chat_id=project["chat_id"])
    assert _answer(runtime, "project", request_id="project-answer", comment="Keep the room")[0] == 200
    [fact] = _facts(runtime)
    assert fact["chat_id"] == project["chat_id"]
    assert not (child / "logs" / "chat.jsonl").exists()
    [mailbox] = drain_owner_entries(child, runtime.task["id"], include_acknowledged=True)
    assert mailbox["text"] == fact["text"]


def test_history_recovers_evicted_answer_without_an_extra_bubble(runtime):
    import json
    from ouroboros.gateway.history import make_chat_history_endpoint

    for index in range(18):
        qid = f"history-{index}"
        _ask(runtime, qid)
        assert _answer(runtime, qid, request_id=qid, index=1, comment=f"choice-{index}")[0] == 200
    cleanup_task_mailbox(runtime.root, runtime.task["id"])
    response = asyncio.run(make_chat_history_endpoint(runtime.root)(
        SimpleNamespace(query_params={"n_human": "100", "thread": "1"})))
    messages = json.loads(response.body)["messages"]
    cards = [row for row in messages if row.get("msg_type") == "quiz"]
    assert len(cards) == 18
    assert not [row for row in messages if row.get("system_type") == "quiz_answer"]
    first = next(row["quiz"] for row in cards if row["quiz"]["quiz_id"] == "history-0")
    assert first["state"] == "answered" and first["answered_index"] == 1
    assert first["comment"] == "choice-0" and first["options"][1]["recommended"] is True


def test_recent_room_and_history_share_parent_root_and_origin_membership(runtime):
    from ouroboros.gateway.history import _make_thread_filter
    from ouroboros.project_dialogue import project_recent_dialogue
    from ouroboros.projects_registry import bind_task_to_project, create_project

    project = create_project(runtime.root, "membership", name="Membership")
    bind_task_to_project(runtime.root, "parent", project["id"], origin={"absent": "system"})
    for field in ("parent_task_id", "root_task_id"):
        message_bus.log_chat("out", 1, 0, field, task_id="child-" + field,
                             message_meta={field: "parent"}, drive_root=runtime.root)
    rows, coverage, _origins = project_recent_dialogue(Memory(runtime.root), project["chat_id"], 20)
    assert {row["text"] for row in rows} == {"parent_task_id", "root_task_id"}
    project_filter = _make_thread_filter(project["chat_id"], {project["chat_id"]}, [], {"parent": project["chat_id"]})
    main_filter = _make_thread_filter(1, {project["chat_id"]}, [], {"parent": project["chat_id"]})
    assert all(project_filter(row["chat_id"], row) and not main_filter(row["chat_id"], row) for row in rows)
