"""Memory authoring tools bind exact sources and preserve capability classes."""
import json

import pytest
from types import SimpleNamespace

from ouroboros.chronicle_store import ChronicleStore, source_row_id
from ouroboros.tools.chronicle import _chronicle_write, _memory_mark, _memory_read


def context(tmp_path):
    return SimpleNamespace(drive_root=tmp_path / "child", budget_drive_root=str(tmp_path),
                           task_metadata={"chat_id": 1}, current_chat_id=1, task_id="author",
                           _accumulated_usage={"_observed_route": {"provider": "actual", "model": "served"}})


def test_authored_episode_has_host_actor_and_no_invented_coverage(tmp_path):
    ctx = context(tmp_path)
    result = json.loads(_chronicle_write(ctx, text="I chose to investigate"))
    assert result["author"]["task_id"] == "author"
    assert result["author"]["route"] == ctx._accumulated_usage["_observed_route"]
    assert result["room_id"] == "1"
    assert result["metadata"]["coverage"] == "authored_without_source_range"
    assert result["metadata"]["task_ids"] == ["author"]
    assert ChronicleStore(tmp_path).scan_state() == {}
    assert not (ctx.drive_root / "memory" / "chronicle").exists()


def test_raw_room_read_retains_exact_page_and_write_binds_same_rows(tmp_path):
    ctx = context(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [{"chat_id": 7, "text": "owner first", "direction": "in", "ts": "2026-09-30T00:00:00Z"},
            {"chat_id": 2, "text": "another room", "direction": "in", "ts": "2026-09-30T00:00:01Z"},
            {"chat_id": 7, "text": "answer\nexact", "direction": "out", "ts": "2026-09-30T00:00:02Z"}]
    (logs / "chat.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    result = json.loads(_memory_read(ctx, room_id="7", raw_room=True, start=1, end=2))
    assert result["rows"] == [rows[2]]
    assert result["range"] == {"start": 1, "end": 2, "total": 2, "unit": "matching_rows"}
    assert result["page_complete"] is False
    assert result["source_row_ids"] == [source_row_id(rows[2])]
    episode = json.loads(_chronicle_write(ctx, text="I answered", source_ref=result["source_ref"]))
    assert episode["metadata"]["source_row_ids"] == result["source_row_ids"]
    assert episode["metadata"]["source_range"] == result["range"]
    assert episode["metadata"]["source_span"] == {
        "start": "2026-09-30T00:00:02+00:00", "end": "2026-09-30T00:00:02+00:00", "incomplete": False}
    mark = json.loads(_memory_mark(ctx, text="Remember exact answer", source_ref=result["source_ref"], quote="answer\nexact"))
    assert mark["quote"] == "answer\nexact"
    assert json.loads(_memory_mark(ctx, text="Bad", source_ref=result["source_ref"], quote="invented"))["error"]


def test_revision_read_and_rejection_keep_original(tmp_path):
    ctx = context(tmp_path)
    episode = json.loads(_chronicle_write(ctx, text="Original"))
    store = ChronicleStore(tmp_path)
    correction = store.revise(episode["id"], "Helper's correction", {"kind": "helper"})
    before = json.loads(_memory_read(ctx, node_id=episode["id"]))
    assert before["original"]["text"] == "Original"
    assert before["current"]["current_text"] == "Helper's correction"
    decision = json.loads(_chronicle_write(ctx, revision_id=correction["id"], decision="reject", reason="Source says otherwise"))
    assert decision["accepted"] is False
    after = json.loads(_memory_read(ctx, node_id=episode["id"]))
    assert after["current"]["current_text"] == "Original"
    assert after["current"]["revisions"][-1]["decision"]["reason"] == "Source says otherwise"


def test_mark_release_and_pagination_are_explicit(tmp_path):
    ctx = context(tmp_path)
    first = json.loads(_chronicle_write(ctx, text="first"))
    json.loads(_chronicle_write(ctx, text="second"))
    mark = json.loads(_memory_mark(ctx, text="Important", node_id=first["id"], scope="global", quote="first"))
    page = json.loads(_memory_read(ctx, limit=1))
    assert page["has_more"] is True
    assert len(page["records"]) == 1
    assert page["active_marks"][0]["id"] == mark["id"]
    next_page = json.loads(_memory_read(ctx, limit=1, after_seq=page["next_after_seq"]))
    assert next_page["records"][0]["text"] == "second"
    assert json.loads(_memory_mark(ctx, release_id=mark["id"]))["error"]
    json.loads(_memory_mark(ctx, release_id=mark["id"], reason="No longer current"))
    assert ChronicleStore(tmp_path).active_marks("1") == []


def test_catalog_and_readonly_capability_parity():
    from ouroboros.tools.knowledge import get_tools
    from ouroboros.tool_capabilities import (COGNITIVE_MEMORY_TOOL_NAMES, LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
                                             ACTING_SUBAGENT_TOOL_NAMES, UNTRUNCATED_TOOL_RESULTS)
    entries = {e.name: e for e in get_tools()}
    assert {"chronicle_write", "memory_read", "memory_mark"} <= entries.keys()
    assert {"chronicle_write", "memory_read", "memory_mark"} <= COGNITIVE_MEMORY_TOOL_NAMES
    assert "memory_read" in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert not {"chronicle_write", "memory_mark"} & LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert ("chronicle_write" in ACTING_SUBAGENT_TOOL_NAMES) == ("knowledge_write" in ACTING_SUBAGENT_TOOL_NAMES)
    assert "memory_read" in UNTRUNCATED_TOOL_RESULTS
    for name in ("chronicle_write", "memory_mark"):
        assert "author" not in entries[name].schema["parameters"]["properties"]


def test_mind_can_change_mark_view_without_releasing_or_erasing_quote(tmp_path):
    ctx = context(tmp_path)
    episode = json.loads(_chronicle_write(ctx, text="verbatim source"))
    mark = json.loads(_memory_mark(ctx, text="The decision remains important", node_id=episode["id"], quote="verbatim source"))
    assert json.loads(_memory_mark(ctx, mark_id=mark["id"], visibility="meaning"))["error"]
    json.loads(_memory_mark(ctx, mark_id=mark["id"], visibility="meaning", reason="Need space for this task"))
    projected = ChronicleStore(tmp_path).active_marks("1")[0]
    assert projected["visibility"] == "meaning"
    assert projected["quote"] == "verbatim source"
    assert projected["text"] == "The decision remains important"
    assert projected["view_decision"]["author"]["task_id"] == "author"
    ChronicleStore(tmp_path).index_path.unlink()
    assert ChronicleStore(tmp_path).active_marks("1")[0]["visibility"] == "meaning"
    json.loads(_memory_mark(ctx, mark_id=mark["id"], visibility="full", reason="Return to the exact words"))
    assert ChronicleStore(tmp_path).active_marks("1")[0]["visibility"] == "full"
    assert ChronicleStore(tmp_path).get(mark["id"])["quote"] == "verbatim source"


def test_readonly_registry_exposes_read_and_denies_actual_memory_writes(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext, ToolRegistry
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_chat_id=1,
        task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False)))
    assert registry.get_schema_by_name("memory_read") is not None
    for name in ("chronicle_write", "memory_mark"):
        assert registry.get_schema_by_name(name) is None
        assert "LOCAL_READONLY_SUBAGENT_BLOCKED" in registry.execute(name, {"text": "must not land"})
    assert not (tmp_path / "memory" / "chronicle" / "records.jsonl").exists()


@pytest.mark.parametrize("source_format", ["list", "chunks", "locators"])
def test_retained_source_formats_page_exact_rows_and_credit_only_that_page(tmp_path, monkeypatch, source_format):
    from ouroboros.chronicle_sources import capture_room, retain_room_source
    from ouroboros.consolidator import retain_memory_source
    from ouroboros.memory import Memory
    from ouroboros import chronicle_sources

    ctx = context(tmp_path)
    rows = [{"chat_id": 7, "text": "first original words", "task_id": "first"},
            {"chat_id": 7, "text": "second original words", "task_id": "second"}]
    chat = tmp_path / "logs/chat.jsonl"
    chat.parent.mkdir()
    chat.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if source_format == "list":
        ref = retain_memory_source(SimpleNamespace(drive_root=tmp_path, task_id=ctx.task_id),
                                  "episode", json.dumps(rows).encode("utf-8"), "json")
    else:
        memory = Memory(tmp_path)
        captured, coverage = capture_room(memory, "7", rendered_chars_budget=1 if source_format == "locators" else None)
        locators = coverage.pop("row_locators")
        if source_format == "locators":
            # Even a misleading stored path cannot redirect the source reader.
            for locator in locators:
                locator["path"] = str(tmp_path / "not-a-chat-source.json")
        ref = retain_room_source(memory, ctx.task_id, captured, locators, coverage)
    archive = tmp_path / "archive"
    archive.mkdir()
    chat.rename(archive / "chat_20260101T000000.jsonl")
    chat.write_text(json.dumps({"chat_id": 7, "text": "later uncaptured words"}) + "\n", encoding="utf-8")
    read_sizes = []
    original = chronicle_sources.JsonlChainSnapshot._read
    def counted(self, start, end):
        read_sizes.append(end - start)
        return original(self, start, end)
    monkeypatch.setattr(chronicle_sources.JsonlChainSnapshot, "_read", counted)
    page = json.loads(_memory_read(ctx, source_ref=ref, start=1, end=2))
    assert page["rows"] == rows[1:] and page["source_row_ids"] == [source_row_id(rows[1])]
    assert page["range"] == {"start": 1, "end": 2, "total": 2, "unit": "matching_rows"}
    assert page["range_complete"] and not page["page_complete"]
    assert page["source_ref"] != ref
    assert "first original words" not in json.dumps(page) and "later uncaptured words" not in json.dumps(page)
    if source_format == "locators":
        assert sum(read_sizes) == len((json.dumps(rows[1]) + "\n").encode("utf-8"))
    full = json.loads(_memory_read(ctx, source_ref=ref))
    assert full["rows"] == rows and full["page_complete"] and full["range_complete"]
    assert page["parent_source_ref"] == ref
    episode = json.loads(_chronicle_write(ctx, text="I recalled only the second source.", source_ref=page["source_ref"]))
    assert episode["metadata"]["source_row_ids"] == [source_row_id(rows[1])]
    assert "first" not in episode["metadata"]["task_ids"]
    assert episode["metadata"]["source_range"] == page["range"]
    (archive / "chat_20260101T000000.jsonl").unlink()
    assert json.loads(_memory_read(ctx, source_ref=page["source_ref"]))["rows"] == rows[1:]
    assert json.loads(_memory_mark(ctx, text="Exact page", room_id="7", source_ref=page["source_ref"],
                                   quote="second original words"))["quote"] == "second original words"


@pytest.mark.parametrize("damage", ["missing", "rewritten", "identity"])
def test_locator_source_gaps_do_not_claim_complete_or_unread_source_credit(tmp_path, damage):
    from ouroboros.chronicle_sources import capture_room, retain_room_source
    from ouroboros.memory import Memory

    ctx, memory = context(tmp_path), Memory(tmp_path)
    rows = [{"chat_id": 7, "text": "original A"}, {"chat_id": 7, "text": "original B"}]
    chat = tmp_path / "logs/chat.jsonl"
    chat.parent.mkdir()
    raw = "".join(json.dumps(row) + "\n" for row in rows)
    chat.write_text(raw, encoding="utf-8")
    captured, coverage = capture_room(memory, "7", rendered_chars_budget=1)
    locators = coverage.pop("row_locators")
    if damage == "identity":
        locators[0]["source_row_id"] = "not-this-row"
    ref = retain_room_source(memory, ctx.task_id, captured, locators, coverage)
    if damage == "missing":
        chat.unlink()
    elif damage == "rewritten":
        chat.write_text(raw.replace("original A", "rewrittenA"), encoding="utf-8")
    page = json.loads(_memory_read(ctx, source_ref=ref))
    assert page["rows"] == ([] if damage == "missing" else rows[1:])
    assert not page["page_complete"] and not page["range_complete"]
    assert page["missing"] and page["coverage"]["complete"] is False
    assert page["range"]["total"] == 2
    assert "rewrittenA" not in json.dumps(page)
    episode = json.loads(_chronicle_write(ctx, text="My source has an explicit gap.", source_ref=page["source_ref"]))
    assert episode["metadata"]["source_row_ids"] == [source_row_id(row) for row in page["rows"]]
    assert episode["metadata"]["source_coverage"]["complete"] is False
    reread = json.loads(_memory_read(ctx, source_ref=page["source_ref"]))
    assert reread["rows"] == page["rows"] and not reread["range_complete"]
