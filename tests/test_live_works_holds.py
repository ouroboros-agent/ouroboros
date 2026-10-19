"""Ouroboros sees what holds its rooms: queue holds, an owner Pause by its recorded actor, and live work whose
own root has ended (helpers, retained runs) — beside the unchanged tell_task address book."""
from ouroboros.live_works import live_works_present
from ouroboros.peer_roster import independent_roots, render_roster_note, unlisted_live_work
from ouroboros.task_results import write_task_result
from ouroboros.utils import utc_now_iso
from tests._shared import write_queue_document


def _snapshot(root, *, running=(), pending=()):
    stamp = utc_now_iso()
    write_queue_document(root / "state/queue_snapshot.json", {"ts": stamp, "running": list(running), "pending": list(pending)})
    write_queue_document(root / "state/direct_roots.json", {"ts": stamp, "incomplete": False, "roots": []})


def test_a_writer_held_continue_reads_as_held_for_the_earlier_attempt(tmp_path):
    write_task_result(tmp_path, "c", "scheduled", title="Continue: Release")
    held = {"reason": "continuation_writer_unsettled", "held_at": "2026-10-17T11:00:00Z", "selected": False}
    _snapshot(tmp_path, pending=[{"id": "c", "attempt": 1, "task": {
        "id": "c", "chat_id": 42, "_budget_pause_hold": held,
        "metadata": {"continuation": {"predecessor_task_id": "p"}}}}])
    row = independent_roots(tmp_path)["roots"][0]
    assert row["waiting"] == [{"kind": "hold", "reason": "earlier_work_unconfirmed",
                               "text": "Continue starts when the earlier attempt's work is confirmed finished",
                               "since": "2026-10-17T11:00:00Z", "tasks": ["p"]}]
    assert "held (reason=earlier_work_unconfirmed" in render_roster_note(independent_roots(tmp_path))
    # A hold the owner already selected explains nothing any more.
    _snapshot(tmp_path, pending=[{"id": "c", "attempt": 1, "task": {
        "id": "c", "chat_id": 42, "_budget_pause_hold": {**held, "selected": True}}}])
    assert "waiting" not in independent_roots(tmp_path)["roots"][0]


def test_an_owner_pause_names_the_actor_the_fence_recorded(tmp_path):
    write_task_result(tmp_path, "t", "running", title="Work", owner_pause={
        "state": "paused", "requested_by": "owner", "actor": "mind", "actor_task_id": "m1",
        "paused_at": "2026-10-13T12:50:00Z", "fence_id": "f1"})
    _snapshot(tmp_path, running=[{"id": "t", "attempt": 1, "task": {"id": "t", "chat_id": 1}}])
    waits = independent_roots(tmp_path)["roots"][0]["waiting"]
    assert {"kind": "owner_pause", "state": "paused", "by": "mind", "since": "2026-10-13T12:50:00Z"} in waits
    write_task_result(tmp_path, "t", "running", owner_pause={"state": "released", "requested_by": "owner",
                                                            "fence_id": "f1"})
    assert "waiting" not in independent_roots(tmp_path)["roots"][0]


def test_live_work_without_a_listed_root_is_visible_but_never_addressable(tmp_path, monkeypatch):
    write_task_result(tmp_path, "p", "failed", suggested_name="Release 7.7", chat_id=42)
    _snapshot(tmp_path, running=[{"id": f"h{index}", "task": {"id": f"h{index}", "parent_task_id": "p",
                                                               "root_task_id": "p"}} for index in range(3)])
    rows = unlisted_live_work(tmp_path, set())
    assert rows == [{"work_id": "p", "title": "Release 7.7", "room": {"chat_id": 42, "project_id": ""},
                     "root_status": "failed", "helpers_running": 3, "helpers_waiting": 0, "external_runs": 0}]
    assert unlisted_live_work(tmp_path, {"p"}) == []
    present = live_works_present(tmp_path)
    assert present["works"] == [] and present["other_live_work"][0]["work_id"] == "p"
    assert "ordinary hygiene" in present["note"]
    # The address book is unchanged: the ended root is not a tell_task target.
    assert independent_roots(tmp_path)["roots"] == []


def test_runs_still_working_under_a_settled_root_count_as_its_live_work(tmp_path):
    """Through the real custody store: started runs count; a settled run custody still holds does not."""
    from ouroboros import delegate_custody as custody

    write_task_result(tmp_path, "p", "done", title="Done work")
    write_task_result(tmp_path, "q", "done", title="Done earlier")
    _snapshot(tmp_path)
    assert custody.emit(tmp_path, custody.STARTED, {"run_id": "r1", "task_id": "p-h", "root_task_id": "p"})
    assert custody.emit(tmp_path, custody.STARTED, {"run_id": "r2", "task_id": "p"})
    assert custody.emit(tmp_path, custody.STARTED, {"run_id": "kept", "task_id": "q", "project_id": "x",
                                                     "project_owned": True})
    assert custody.emit(tmp_path, custody.SETTLED, {"run_id": "kept", "task_id": "q", "state": "succeeded"})
    rows = unlisted_live_work(tmp_path, set())
    assert [(row["work_id"], row["external_runs"], row["helpers_running"]) for row in rows] == [("p", 2, 0)]
