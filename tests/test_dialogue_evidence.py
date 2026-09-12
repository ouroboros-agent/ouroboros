"""Full room evidence retains conversations beyond consolidation and rotation."""
from __future__ import annotations

from ouroboros.memory import Memory
from ouroboros.project_dialogue import build_owner_message_ref, project_recent_dialogue
from ouroboros.projects_registry import bind_task_to_project, create_project
from ouroboros.utils import append_jsonl, atomic_write_json


def test_full_room_includes_rotated_consolidated_dialogue_and_child_lineage(tmp_path):
    from ouroboros.dialogue_evidence import read_room_source

    project = create_project(tmp_path, "room", name="Our discussion")
    ref = build_owner_message_ref(chat_id=1, client_message_id="origin", ts="2026-09-01T00:00:00Z", text="Original choice")
    bind_task_to_project(tmp_path, "parent", project["id"], origin={"ref": ref, "text": "Original choice"})
    for index, (direction, text) in enumerate([("in", "I need a plan"), ("out", "Option A saves time; option B keeps flexibility")]):
        append_jsonl(tmp_path / "archive" / f"chat_2026090{index + 1}.jsonl",
                     {"ts": f"2026-09-0{index + 1}T01:00:00Z", "chat_id": project["chat_id"], "direction": direction, "text": text})
    live = tmp_path / "logs" / "chat.jsonl"
    append_jsonl(live, {"ts": "2026-09-03T01:00:00Z", "chat_id": 1, "direction": "out", "text": "Child explanation", "task_id": "child", "root_task_id": "parent"})
    atomic_write_json(tmp_path / "memory" / "dialogue_meta.json", {"consolidated_chat_lines": 100000})
    # Full reader ignores the consolidation cursor's bounded recent window.
    memory = Memory(tmp_path)
    recent, _, _ = project_recent_dialogue(memory, project["chat_id"], 10**9)
    source = read_room_source(tmp_path, project["chat_id"])
    assert "Original choice" in source["text"] and "I need a plan" in source["text"]
    assert "Option A saves time; option B keeps flexibility" in source["text"]
    assert "Child explanation" in source["text"]
    assert source["coverage"]["chat"]["snapshot_stable"]
    assert len(source["rows"]) > len(recent)


def test_quiz_provenance_mailbox_and_attachment_names_are_complete(tmp_path):
    from ouroboros.dialogue_evidence import read_room_source
    from ouroboros.owner_mailbox import write_task_message

    quiz = {"quiz_id": "q", "question": "Which approach?", "options": [{"label": "Fast", "detail": "Less flexible", "recommended": True}, {"label": "Flexible"}], "state": "open"}
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "out", "chat_id": 1, "type": "quiz", "quiz": quiz, "task_id": "root", "ts": "2026-09-01T00:00:00Z"})
    winning = {"quiz_id": "q", "question": "Which approach?", "options": ["Fast", "Flexible"], "option_details": ["Less flexible", ""], "recommended_index": 0, "answered_index": 1, "comment": "  exact words  ", "asked_at": "2026-09-01T00:00:00Z", "answered_at": "2026-09-01T00:01:00Z", "state": "answered"}
    answer = {"direction": "system", "source": "owner_quiz_answer", "chat_id": 1, "type": "quiz_answer", "quiz": winning, "task_id": "root", "client_message_id": "quiz_answer:root:q", "ts": winning["answered_at"]}
    append_jsonl(tmp_path / "logs" / "chat.jsonl", answer)
    append_jsonl(tmp_path / "logs" / "chat.jsonl", answer)
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "in", "chat_id": 1, "text": "See attachment", "filename": "notes.pdf", "file_base64": "MUST_NOT_ATTACH_BINARY"})
    append_jsonl(tmp_path / "logs" / "progress.jsonl", {"chat_id": 1, "task_id": "root", "content": "Panel completed, collection pending", "status": "pending"})
    write_task_message(tmp_path, "Peer suggestion only", task_id="root", source_task_id="parent", provenance="peer_via_ancestor", relayed_from_task_id="peer")
    source = read_room_source(tmp_path, 1, task_id="root")
    assert source["text"].count('"type": "quiz_answer"') == 1
    assert '"author": "Owner"' in source["text"]
    for text in ("Less flexible", "recommended", "answered_index", "  exact words  ", "notes.pdf", "Panel completed, collection pending", "relayed by ancestor parent", "Peer suggestion only"):
        assert text in source["text"]
    assert "MUST_NOT_ATTACH_BINARY" not in source["text"]


def test_unknown_room_is_an_explicit_gap_and_chat_selectors_use_redaction(tmp_path):
    from ouroboros.dialogue_evidence import chat_evidence_reader, read_room_source
    from ouroboros.tools.plan_evidence import resolve_evidence

    assert read_room_source(tmp_path, 999) is None
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "out", "chat_id": 1, "text": "First explanation"})
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "in", "chat_id": 1, "text": "OPENAI_API_KEY=sk-" + "x" * 48})
    manifest = resolve_evidence(["chat:999", "chat:1::lines=2-3"], active_root=tmp_path, allowed_roots=[], resolve_chat=chat_evidence_reader(tmp_path))
    assert {"locator": "chat:999", "reason": "chat_not_found"} in manifest["omissions"]
    [row] = manifest["attached"]
    assert row["kind"] == "chat" and row["selection_bytes"] > 0
    assert row["attached_bytes"] == len(row["text"].encode())
    assert "x" * 48 not in row["text"]


def test_explicit_hidden_room_keeps_its_address_and_never_becomes_main(tmp_path):
    from ouroboros.dialogue_evidence import read_room_source
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "out", "chat_id": 0, "text": "Headless discussion"})
    append_jsonl(tmp_path / "logs" / "chat.jsonl", {"direction": "in", "chat_id": 1, "text": "Main conversation"})
    assert "Headless discussion" in read_room_source(tmp_path, 0)["text"]
    assert "Main conversation" not in read_room_source(tmp_path, 0)["text"]
    assert "Headless discussion" not in read_room_source(tmp_path, 1)["text"]
