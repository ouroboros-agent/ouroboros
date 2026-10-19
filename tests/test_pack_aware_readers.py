"""Readers that announce a retained source test its presence through the pack-aware check, not the
loose file alone: once the packer has moved a terminal task's checkpoint handles into its pack, a
Continue successor still reads its predecessor's exact sources and a wake still names a settled late
review's exact source. A handle that is truly gone is a read failure, never invented bytes."""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil

import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs
from ouroboros.artifacts import store_actor_source_bytes
from tests.test_evidence_packs import _handle_path, _result, root as root  # noqa: F401  (the shared fixture)


def test_a_successor_reads_its_predecessors_packed_sources_and_a_lost_one_fails_to_read(root):
    """The successor path (``main_context_authority.predecessor_notes_message``) reads the
    predecessor's retained sources through ``artifacts.read_actor_source_bytes``: once the packer
    has moved the terminal predecessor's handle into its pack, the same reader still returns the
    exact bytes; a handle that no pack ever held is a read failure, never invented bytes."""
    from ouroboros.artifacts import read_actor_source_bytes

    data = json.dumps({"decision": "keep the protocol"}).encode()
    ref = store_actor_source_bytes(root, "pred", category="context_checkpoints", source_id="plan-author-x",
                                   data=data, extension="json")
    _result(root, "pred", "failed")
    assert read_actor_source_bytes(root, "pred", ref) == data
    assert packer.run_pass(root)["packed"] == 1
    assert not _handle_path(root, "pred", ref).exists() and packs.handle_info(root, "pred", ref["path"])["loose"] is False
    assert read_actor_source_bytes(root, "pred", ref) == data  # a verified pack member: the same exact bytes
    lost = store_actor_source_bytes(root, "pred", category="context_checkpoints", source_id="plan-author-y",
                                    data=b'{"decision": "lost"}', extension="json")
    _handle_path(root, "pred", lost).unlink()  # gone before any pack held it
    with pytest.raises(OSError):  # a missing file, loose and retained alike: a typed read failure
        read_actor_source_bytes(root, "pred", lost)


def test_a_wake_names_the_exact_source_of_a_late_review_whose_handle_is_packed(tmp_path):
    from tests.test_consciousness_wake import T0, _iso, _late_panel, _lines, _wake, _write

    packs._READERS.clear()
    projection, _ref = _late_panel(tmp_path, "rv", pending=True)
    _write(tmp_path, "rv", status="completed", ts=_iso(T0 - 900), updated_at=_iso(T0 - 100), review_projection=projection)
    first = _wake(tmp_path, None, T0)
    projection, ref = _late_panel(tmp_path, "rv", settled_at=T0 + 60)
    _write(tmp_path, "rv", review_projection=projection)
    assert packer.run_pass(tmp_path)["packed"] == 1  # the settled task's handles move into its pack
    assert not _handle_path(tmp_path, "rv", ref).exists()
    line = _lines(_wake(tmp_path, first.boundary, T0 + 600))[0]
    assert "exact source {" in line and f"sha256 {ref['sha256']}" in line and "unavailable" not in line
    gone = _late_panel(tmp_path, "gone", settled_at=T0 + 60)
    _write(tmp_path, "gone", status="completed", ts=_iso(T0 - 900), updated_at=_iso(T0 - 100), review_projection=gone[0])
    _handle_path(tmp_path, "gone", gone[1]).unlink()  # no pack ever held it
    lines = _lines(_wake(tmp_path, None, T0 + 600))
    assert any("exact source unavailable" in text and "gone" in text for text in lines), lines


def test_a_review_opening_after_the_task_was_packed_gets_its_packet_back_beside_its_name(monkeypatch, tmp_path):
    """Both directions. The session reader is handed an absolute path. A re-review's byte-identical
    packet IS the handle the packer already moved into the pack (the write-once store writes nothing),
    so the path is written back from the pack's verified bytes before it is handed out, by
    ``retain_review_source`` and again by ``prepare_session_source``; a packet present loose is not
    rewritten; one neither loose nor packed stays a typed refusal, never invented."""
    from ouroboros.acceptance_retrieving import prepare_session_source, retain_review_source
    from ouroboros.review_execution import ReviewRouteUnavailable
    from ouroboros.task_results import write_task_result
    from tests.test_acceptance_delivery import _fake_session
    from tests.test_acceptance_source_first import _prepared_request

    _fake_session(monkeypatch)
    packs._READERS.clear()
    (tmp_path / "first").mkdir()
    request, slot, _author, canonical, _workspace = _prepared_request(tmp_path / "first")
    retain_review_source(request, slot.slot_id, canonical)
    delivery = request.slot_source_delivery[slot.slot_id]
    assert delivery["reader"] == "filesystem" and delivery["status"] == "paged"
    packet, ref = pathlib.Path(delivery["source_path"]), delivery["custody_source"]
    first = packet.stat()
    retain_review_source(request, slot.slot_id, canonical)  # present loose: write-once, not rewritten
    assert (packet.stat().st_ino, packet.stat().st_mtime_ns) == (first.st_ino, first.st_mtime_ns)
    write_task_result(canonical, request.task_id, "completed", result="done")  # the review closed, the task settled
    assert packer.run_pass(canonical)["packed"] == 1
    assert not packet.exists() and packs.handle_info(canonical, request.task_id, ref["path"])["loose"] is False
    (tmp_path / "again").mkdir()
    again, slot_again, _author2, _canonical2, _workspace2 = _prepared_request(tmp_path / "again")
    retain_review_source(again, slot_again.slot_id, canonical)  # a later review of the packed task
    delivery = again.slot_source_delivery[slot_again.slot_id]
    assert delivery["custody_source"] == ref and pathlib.Path(delivery["source_path"]) == packet
    assert packet.exists() and hashlib.sha256(packet.read_bytes()).hexdigest() == ref["sha256"]
    assert prepare_session_source(delivery, task_id=again.task_id) == str(packet)
    packet.unlink()  # packed again between retention and the session's start: handed out loose anew
    assert prepare_session_source(delivery, task_id=again.task_id) == str(packet) and packet.exists()
    packet.unlink()
    shutil.rmtree(packs.packs_dir(canonical))  # neither loose nor packed: a typed refusal
    packs._READERS.clear()
    with pytest.raises(ReviewRouteUnavailable, match="degraded_source_unreachable"):
        prepare_session_source(delivery, task_id=again.task_id)
    assert not packet.exists()
