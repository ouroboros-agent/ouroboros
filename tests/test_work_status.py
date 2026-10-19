"""One work, one status: the pure classification behind /api/state ``works`` (gateway/work_status.py)."""

from __future__ import annotations

import pytest

from ouroboros.gateway import work_status as ws
from ouroboros.gateway import work_projection as wp


def row(task_id, phase="working", **extra):
    return {"activity_id": task_id, "phase": phase, "kind": extra.pop("kind", "managed_task"),
            "chat_id": extra.pop("chat_id", 42), "task_attempt": 1, "started_at": extra.pop("started_at", 100.0), **extra}


# ---- one member ---------------------------------------------------------------------------------------

def test_working_member_names_its_exact_call():
    state = ws.classify_member(row("t", model_activity={"phase": "awaiting_response"}))
    assert (state["activity"], state["call"], state["release"]) == ("working", "sending", "self")
    assert ws.classify_member(row("t", "thinking"))["call"] == "thinking"
    assert ws.classify_member(row("t", "preparing", kind="direct_chat"))["call"] == "preparing"


def test_writer_held_continue_waits_and_is_never_the_owners_turn():
    held = row("c", "budget_paused", pause_cause="unknown", can_resume=False)
    state = ws.classify_member(held, {"hold": {"reason": "continuation_writer_unsettled"}})
    assert state["activity"] == "waiting"
    assert [entry["code"] for entry in state["reasons"]] == ["earlier_work_unconfirmed"]
    assert state["turn"] == [] and state["release"] == "self"
    # The same parked row without the writer hold is a pause, not a wait.
    assert ws.classify_member(held, {"hold": {}})["activity"] == "paused"
    # A hold the owner's selection already released explains nothing.
    assert ws.classify_member(held, {"hold": {"reason": "continuation_writer_unsettled", "selected": True}})[
        "activity"] == "paused"


@pytest.mark.parametrize("fence, paused_by, release", [
    ({"requested_by": "owner"}, "owner", "owner"),
    ({"requested_by": "owner", "actor": "mind", "actor_task_id": "m1"}, "mind", "mind"),
    ({"requested_by": "server_shutdown"}, "host", "owner"),
])
def test_owner_pause_names_who_paused_from_the_recorded_actor(fence, paused_by, release):
    state = ws.classify_member(row("t", "budget_paused", pause_cause="owner"), {"fence": fence})
    assert (state["activity"], state["paused_by"], state["release"]) == ("paused", paused_by, release)
    assert state["reasons"][0]["code"] == f"paused_by_{paused_by}"
    assert state["turn"] == []


@pytest.mark.parametrize("cause, hold, code", [
    ("restart", {"reason": "saved_work_hold"}, "saved_on_stop"),
    ("restart", {"reason": "owner_restart_hold"}, "after_restart"),
    ("budget", {}, "spending_limit"),
    ("authority", {}, "technical_pause"),
    ("sleep", {"reason": "panic_hold"}, "after_panic"),
])
def test_owner_required_resume_is_the_owners_turn_only_when_the_census_says_so(cause, hold, code):
    parked = row("t", "budget_paused", pause_cause=cause, can_resume=True)
    state = ws.classify_member(parked, {"hold": hold})
    assert state["activity"] == "paused" and state["reasons"][0]["code"] == code
    assert state["turn"] == [{"kind": "action", "task_id": "t", "code": code}] and state["release"] == "owner"
    quiet = ws.classify_member(dict(parked, can_resume=False), {"hold": hold})
    assert quiet["turn"] == []


def test_cold_sleep_waits_and_pausing_is_still_work():
    assert ws.classify_member(row("t", "budget_paused", pause_cause="sleep"))["activity"] == "waiting"
    pausing = ws.classify_member(row("t", "budget_pausing", pause_cause="owner"), {"fence": {"requested_by": "owner"}})
    assert (pausing["activity"], pausing["transition"]) == ("working", "pausing")


def test_unread_pause_cause_stays_raw_and_releases_nothing():
    state = ws.classify_member(row("t", "budget_paused", pause_cause="unknown"), {"hold": {"reason": "restore_refused:x"}})
    assert state["reasons"][0]["code"] == "restore_failed" and state["release"] == "none"
    raw = ws.classify_member(row("t", "budget_paused", pause_cause="mystery"), {"hold": {}})
    assert raw["reasons"][0]["code"] == "held" and raw["reasons"][0]["text"] == "Held: mystery"


def test_owner_question_is_a_wait_on_the_owner_and_a_gap_is_unconfirmed():
    asked = row("t", owner_wait={"quiz_id": "q1", "owner_wait_state": "waiting", "quiz_state": "open"})
    state = ws.classify_member(asked)
    assert state["activity"] == "waiting" and state["turn"] == [{"kind": "question", "task_id": "t", "quiz_id": "q1"}]
    answered = row("t", owner_wait={"quiz_id": "q1", "owner_wait_state": "resumed", "quiz_state": "answered"})
    assert ws.classify_member(answered)["activity"] == "working"
    gap = ws.classify_member(row("t", required_question_unavailable=True))
    assert gap["confirmed"] is False and gap["activity"] == "unknown"


def test_model_wait_is_the_owners_turn_only_when_it_requires_him():
    waits = {"w1": {"state": "waiting", "task_attempt": 1, "requires_owner_action": True, "revision": 2}}
    state = ws.classify_member(row("t", model_waits=waits))
    assert state["activity"] == "waiting" and state["turn"][0]["wait_id"] == "w1"
    calm = ws.classify_member(row("t", model_waits={"w1": {**waits["w1"], "requires_owner_action": False}}))
    assert calm["activity"] == "waiting" and calm["turn"] == [] and calm["release"] == "self"
    other_attempt = ws.classify_member(row("t", model_waits={"w1": {**waits["w1"], "task_attempt": 2}}))
    assert other_attempt["activity"] == "working"


def test_project_hold_queue_finalizing_and_unknown():
    held = ws.classify_member(row("t", "queued", project_admission_hold={"label": "Waiting for Project verification"}))
    assert held["activity"] == "waiting" and held["reasons"][0]["text"] == "Waiting for Project verification"
    assert ws.classify_member(row("t", "queued"))["activity"] == "queued"
    final = ws.classify_member(row("t", "finalizing"))
    assert (final["activity"], final["transition"]) == ("working", "finalizing")
    unknown = ws.classify_member(row("t", "unknown"))
    assert unknown["activity"] == "unknown" and unknown["confirmed"] is False


# ---- one work -----------------------------------------------------------------------------------------

def _helpers(count, root="p"):
    return [ws.classify_member(row(f"h{index}", parent_task_id=root, root_task_id=root)) for index in range(count)]


def test_held_continue_with_running_helpers_is_working_and_says_why():
    current = ws.classify_member(row("c", "budget_paused", pause_cause="unknown"),
                                 {"hold": {"reason": "continuation_writer_unsettled"}})
    members = [current, *_helpers(3)]
    work = ws.roll_up_work("p", members, episode={"task_id": "c", "state": current}, name="Release 7.7", room=42)
    assert work["activity"] == "working" and work["release"] == "self" and work["turn"] == []
    assert work["because"][0]["code"] == "earlier_attempt_finishing" and work["because"][0]["count"] == 3
    assert work["because"][0]["text"] == "Continue starts when 3 helpers of the earlier attempt finish"
    assert work["episode"] == {"task_id": "c", "live": True} and work["members"]["working"] == 3
    assert sorted(work["members"]["ids"]) == ["c", "h0", "h1", "h2"]
    one = ws.roll_up_work("p", [current, *_helpers(1)], episode={"task_id": "c", "state": current})
    assert one["because"][0]["text"] == "Continue starts when the helper of the earlier attempt finishes"


def test_held_continue_waiting_on_external_runs_or_on_nothing_visible():
    current = ws.classify_member(row("c", "budget_paused", pause_cause="unknown"),
                                 {"hold": {"reason": "continuation_writer_unsettled"}})
    runs = ws.roll_up_work("p", [current], episode={"task_id": "c", "state": current}, delegated=2)
    assert runs["activity"] == "working" and runs["because"][0]["code"] == "earlier_runs_finishing"
    alone = ws.roll_up_work("p", [current], episode={"task_id": "c", "state": current})
    assert alone["activity"] == "waiting" and alone["because"][0]["code"] == "earlier_work_unconfirmed"
    assert alone["release"] == "self"


def test_helpers_of_an_ended_attempt_keep_the_work_working_with_its_outcome():
    work = ws.roll_up_work("p", _helpers(2), episode={"task_id": "p"}, outcome="failed")
    assert work["activity"] == "working" and work["outcome"] == "failed"
    assert work["because"] == [ws.reason("helpers_running", count=2, ids=["h0", "h1"])]
    assert work["episode"]["live"] is False


def test_finalizing_answer_and_paused_work():
    final = ws.classify_member(row("t", "finalizing", status="done"))
    work = ws.roll_up_work("t", [final], episode={"task_id": "t", "state": final}, outcome="done")
    assert (work["activity"], work["transition"], work["outcome"]) == ("working", "finalizing", "done")
    paused = ws.classify_member(row("t", "budget_paused", pause_cause="owner"), {"fence": {"requested_by": "owner"}})
    work = ws.roll_up_work("t", [paused], episode={"task_id": "t", "state": paused})
    assert (work["activity"], work["paused_by"], work["release"]) == ("paused", "owner", "owner")


def test_any_members_turn_is_the_works_turn_and_unknown_never_erases_known_work():
    asked = ws.classify_member(row("h", owner_wait={"quiz_id": "q", "owner_wait_state": "waiting"},
                                   parent_task_id="t", root_task_id="t"))
    lead = ws.classify_member(row("t"))
    work = ws.roll_up_work("t", [lead, asked], episode={"task_id": "t", "state": lead})
    assert work["activity"] == "working" and work["turn"][0]["kind"] == "question" and work["release"] == "owner"
    unknown = ws.classify_member(row("u", "unknown", parent_task_id="t", root_task_id="t"))
    work = ws.roll_up_work("t", [lead, unknown], episode={"task_id": "t", "state": lead})
    assert work["activity"] == "working" and work["confirmed"] is False


def test_structure_is_the_same_for_one_and_eighteen_helpers():
    current = ws.classify_member(row("c", "budget_paused", pause_cause="unknown"),
                                 {"hold": {"reason": "continuation_writer_unsettled"}})
    small = ws.roll_up_work("p", [current, *_helpers(1)], episode={"task_id": "c", "state": current})
    large = ws.roll_up_work("p", [current, *_helpers(18)], episode={"task_id": "c", "state": current})
    assert set(small) == set(large) and small["activity"] == large["activity"] == "working"
    assert [entry["code"] for entry in small["because"]] == [entry["code"] for entry in large["because"]]


# ---- one room -----------------------------------------------------------------------------------------

def _work(activity, *, transition="none", turn=(), confirmed=True, work_id="w"):
    return {"work_id": work_id, "activity": activity, "transition": transition, "call": "", "turn": list(turn),
            "confirmed": confirmed}


def test_room_takes_the_most_active_work_and_amber_follows_the_turn():
    room = ws.roll_up_room([_work("waiting", turn=[{"kind": "question", "task_id": "a"}], work_id="a"),
                            _work("working", work_id="b")])
    assert (room["activity"], room["mark"], room["amber"]) == ("working", "moving", True)
    assert ws.roll_up_room([_work("queued")])["mark"] == "still"
    assert ws.roll_up_room([_work("paused")])["mark"] == "dot" and not ws.roll_up_room([_work("paused")])["amber"]


def test_room_finalizing_only_says_finalizing_and_empty_rooms_are_idle_or_unknown():
    assert ws.roll_up_room([_work("working", transition="finalizing")])["transition"] == "finalizing"
    mixed = ws.roll_up_room([_work("working", transition="finalizing", work_id="a"), _work("working", work_id="b")])
    assert mixed["transition"] == "none"
    assert ws.roll_up_room([], complete=True)["activity"] == "idle"
    assert ws.roll_up_room([], complete=False)["mark"] == "ring"
    unconfirmed = ws.roll_up_room([_work("paused", confirmed=False)])
    assert unconfirmed["confirmed"] is False and unconfirmed["mark"] == "ring"


# ---- the server adapter ------------------------------------------------------------------------------

_REAL_OPEN_RUNS = wp._open_runs  # the custody reader itself, before a fixture fakes it

@pytest.fixture
def stored(monkeypatch):
    results = {}
    monkeypatch.setattr(wp._Observations, "result", lambda self, task_id: results.get(task_id))
    monkeypatch.setattr(wp, "_open_runs", lambda drive_root: {})
    monkeypatch.setattr(wp, "_inflight_upkeep", lambda drive_root, known: [])
    return results


def test_a_held_continue_with_helpers_of_its_failed_attempt_is_one_working_work(stored, tmp_path):
    stored["p"] = {"task_id": "p", "status": "failed", "suggested_name": "Ouroboros 7.7.0 — finish the saved release",
                   "title": "НОВОЕ ПРЯМОЕ решение"}
    owners = [row("c", "budget_paused", pause_cause="unknown", can_resume=False),
              *[row(f"h{index}", parent_task_id="p", root_task_id="p") for index in range(3)]]
    raw = {"c": {"_budget_pause_hold": {"reason": "continuation_writer_unsettled"},
                 "title": "Continue: НОВОЕ ПРЯМОЕ решение", "continuation": {"predecessor_task_id": "p"}}}
    snap = wp.works_snapshot(tmp_path, owners, raw)
    assert snap["works_complete"] is True and len(snap["works"]) == 1
    work = snap["works"][0]
    assert work["work_id"] == "p" and work["name"] == "Ouroboros 7.7.0 — finish the saved release"
    assert work["activity"] == "working" and work["because"][0]["count"] == 3
    assert work["room"] == 42
    assert [(entry["action"], entry["target"]) for entry in work["controls"]] == [
        ("stop", "c"), ("stop", "p"), ("stop_earlier", "p")]
    room = snap["rooms"]["42"]
    assert room["activity"] == "working" and room["amber"] is False


def test_every_live_owner_row_is_a_member_of_exactly_one_work(stored, tmp_path):
    stored["p"] = {"task_id": "p"}
    owners = [row("p", "working"), row("x", "queued", chat_id=1), row("d", "preparing", kind="direct_chat", chat_id=1),
              row("h", parent_task_id="p", root_task_id="p"), row("c2", "queued"),
              row("c3", "budget_paused", pause_cause="owner")]
    raw = {"c2": {"continuation": {"predecessor_task_id": "p"}}, "c3": {"continuation": {"work_id": "p"}}}
    snap = wp.works_snapshot(tmp_path, owners, raw)
    seen = [member for work in snap["works"] for member in work["members"]["ids"]]
    assert sorted(seen) == sorted(item["activity_id"] for item in owners)
    by_id = {work["work_id"]: work for work in snap["works"]}
    assert sorted(by_id["p"]["members"]["ids"]) == ["c2", "c3", "h", "p"]


def test_legacy_chain_of_three_episodes_groups_under_its_first_root(stored, tmp_path):
    stored.update({"r": {"task_id": "r", "suggested_name": "First"},
                   "e2": {"task_id": "e2", "metadata": {"continuation": {"predecessor_task_id": "r"}}}})
    owners = [row("e3", "queued"), row("h", parent_task_id="r", root_task_id="r")]
    raw = {"e3": {"continuation": {"predecessor_task_id": "e2"}}}
    snap = wp.works_snapshot(tmp_path, owners, raw)
    assert [work["work_id"] for work in snap["works"]] == ["r"] and snap["works"][0]["name"] == "First"


def test_an_unreadable_episode_record_is_a_disclosed_gap(stored, tmp_path):
    owners = [row("e3", "queued")]
    snap = wp.works_snapshot(tmp_path, owners, {"e3": {"continuation": {"predecessor_task_id": "gone"}}})
    assert snap["works"][0]["work_id"] == "e3"
    assert snap["works_gaps"] == [{"reason": "episode_record_unavailable", "task_id": "gone"}]


def test_a_failing_source_never_reads_as_an_empty_room(monkeypatch, tmp_path):
    monkeypatch.setattr(wp, "_snapshot", lambda *args, **kwargs: 1 / 0)
    snap = wp.works_snapshot(tmp_path, [row("t")], {})
    assert snap["works_complete"] is False and snap["works_gaps"][0]["reason"] == "work_projection_unavailable"


def test_started_delegated_runs_of_an_ended_root_are_its_live_work(stored, monkeypatch, tmp_path):
    """Through the real custody store: started runs under an ended root are live members that its Stop reaches,
    a settled run custody still holds is not, and the stored ISO start becomes epoch seconds."""
    from datetime import datetime, timezone

    from ouroboros import delegate_custody as custody

    monkeypatch.setattr(wp, "_open_runs", _REAL_OPEN_RUNS)
    stored["p"] = {"task_id": "p", "status": "done", "chat_id": 42, "started_at": "2026-10-17T11:00:00Z"}
    stored["q"] = {"task_id": "q", "status": "done", "chat_id": 43, "started_at": "2026-10-17T10:00:00Z"}
    for run_id in ("run-1", "run-2"):
        assert custody.emit(tmp_path, custody.STARTED, {"run_id": run_id, "task_id": "p-h", "root_task_id": "p"})
    assert custody.emit(tmp_path, custody.STARTED, {"run_id": "kept", "task_id": "q", "root_task_id": "q",
                                                     "project_id": "x", "project_owned": True})
    assert custody.emit(tmp_path, custody.SETTLED, {"run_id": "kept", "task_id": "q", "state": "succeeded"})
    snap = wp.works_snapshot(tmp_path, [], {})
    assert snap["works_complete"] is True, snap["works_gaps"]
    [work] = snap["works"]
    assert work["work_id"] == "p" and work["activity"] == "working" and work["members"]["delegated"] == 2
    assert work["because"][0]["code"] == "delegated_running"
    assert work["since"] == datetime(2026, 10, 17, 11, tzinfo=timezone.utc).timestamp()
    # The cascade Stop cancels a settled tree's open runs (tests/test_settled_tree_run_stop.py drives the door).
    assert work["controls"] == [{"action": "stop", "target": "p", "scope": "cascade"}]


def test_working_helpers_say_the_reason_while_the_work_still_counts_its_runs(stored, monkeypatch, tmp_path):
    stored["p"] = {"task_id": "p", "status": "failed", "chat_id": 42}
    monkeypatch.setattr(wp, "_open_runs", lambda drive_root: {"p": ["run-1", "run-2"]})
    snap = wp.works_snapshot(tmp_path, [row("h0", parent_task_id="p", root_task_id="p")], {})
    [work] = snap["works"]
    assert work["because"] == [ws.reason("helpers_running", count=1, ids=["h0"])]
    assert work["members"]["delegated"] == 2 and work["members"]["working"] == 1


# ---- controls -----------------------------------------------------------------------------------------

def _actions(controls):
    return [(entry["action"], entry["target"]) for entry in controls]


def test_held_continue_offers_stop_and_stop_earlier_never_resume_or_pause():
    held_row = row("c", "budget_paused", pause_cause="unknown", can_resume=False)
    current = ws.classify_member(held_row, {"hold": {"reason": "continuation_writer_unsettled"}})
    controls = ws.controls_for(current, held_row, earlier_live=["p"], live_roots=["p"])
    assert _actions(controls) == [("stop", "c"), ("stop", "p"), ("stop_earlier", "p")]
    assert not {"resume", "pause"} & {entry["action"] for entry in controls}


@pytest.mark.parametrize("kind", ["managed_task", "direct_chat"])
def test_a_live_root_offers_wrap_up_hurry_pause_and_stop_managed_or_direct(kind):
    working = row("t", kind=kind)
    controls = ws.controls_for(ws.classify_member(working), working)
    assert [entry["action"] for entry in controls] == ["wrap_up", "hurry", "pause", "stop"]
    held = row("c", "budget_paused", kind=kind, pause_cause="unknown", can_resume=False)
    state = ws.classify_member(held, {"hold": {"reason": "continuation_writer_unsettled"}})
    assert [entry["action"] for entry in ws.controls_for(state, held)] == ["stop"], "held: only Stop"


@pytest.mark.parametrize("kind", ["managed_task", "direct_chat"])
def test_post_answer_upkeep_keeps_its_controls_and_a_settling_pause_keeps_its_pause(kind):
    finishing = row("f", "finalizing", kind=kind)
    assert [entry["action"] for entry in ws.controls_for(ws.classify_member(finishing), finishing)] == [
        "wrap_up", "hurry", "pause", "stop"]
    pausing = row("f", "budget_pausing", kind=kind, pause_cause="owner")
    assert [entry["action"] for entry in ws.controls_for(ws.classify_member(pausing), pausing)] == ["pause", "stop"]
    paused = row("f", "budget_paused", kind=kind, pause_cause="owner", can_resume=True)
    assert "pause" not in [entry["action"] for entry in ws.controls_for(ws.classify_member(paused), paused)]


def test_a_live_retry_is_the_current_episode_of_its_original(stored, tmp_path):
    """A timeout retry carries its original's identity: it is that work's live episode, at its own id."""
    stored["held"] = {"task_id": "held", "status": "interrupted", "chat_id": 1}
    retry = row("t-retry", "budget_paused", pause_cause="owner", can_resume=True, original_task_id="held")
    snap = wp.works_snapshot(tmp_path, [retry], {"t-retry": {"_budget_pause": {"reason": "owner"}}})
    [work] = snap["works"]
    assert work["work_id"] == "held" and work["episode"] == {"task_id": "t-retry", "live": True}
    assert work["activity"] == "paused" and work["room"] == 42
    actions = [(entry["action"], entry["target"]) for entry in work["controls"]]
    assert ("resume", "t-retry") in actions
    # Stop ends the whole work: the retry, then its original's tree (a helper may still name that root).
    assert [target for action, target in actions if action == "stop"] == ["t-retry", "held"]
    assert not any(action == "stop_earlier" for action, _ in actions)


def test_a_retry_and_the_helpers_it_started_are_one_work(stored, tmp_path):
    """Helpers that name the retry as their root still belong to the original's work: one row, the retry live."""
    stored["held"] = {"task_id": "held", "status": "interrupted", "chat_id": 42}
    stored["t-retry"] = {"task_id": "t-retry", "status": "running", "chat_id": 42, "original_task_id": "held"}
    retry = row("t-retry", original_task_id="held")
    helper = row("h", parent_task_id="t-retry", root_task_id="t-retry")
    snap = wp.works_snapshot(tmp_path, [retry, helper], {"t-retry": {"original_task_id": "held"}})
    [work] = snap["works"]
    assert work["work_id"] == "held" and work["episode"] == {"task_id": "t-retry", "live": True}
    assert work["members"]["ids"] == ["t-retry", "h"] and work["activity"] == "working"
    assert not any(entry["action"] == "stop_earlier" for entry in work["controls"])


def test_a_held_continue_offers_stop_earlier_only_where_the_stop_door_reaches(stored, tmp_path):
    """A writer the queue does not list (an unbound start, a handoff, a process) is a wait the Stop door cannot end
    (it answers 404 for a settled tree with no live row): no dead control. A live helper of that attempt can."""
    stored["p"] = {"task_id": "p", "status": "failed", "chat_id": 42}
    held = row("c", "budget_paused", pause_cause="unknown", can_resume=False)
    raw = {"c": {"_budget_pause_hold": {"reason": "continuation_writer_unsettled"},
                 "continuation": {"predecessor_task_id": "p"}}}
    [alone] = wp.works_snapshot(tmp_path, [held], raw)["works"]
    assert [(entry["action"], entry["target"]) for entry in alone["controls"]] == [("stop", "c")]
    helper = row("h0", parent_task_id="p", root_task_id="p")
    [helped] = wp.works_snapshot(tmp_path, [held, helper], raw)["works"]
    assert [(entry["action"], entry["target"]) for entry in helped["controls"]] == [
        ("stop", "c"), ("stop", "p"), ("stop_earlier", "p")]


@pytest.mark.parametrize("cause, raw, resumable", [
    ("owner", {"fence": {"requested_by": "owner"}}, True),
    ("owner", {"fence": {"requested_by": "owner", "actor": "mind"}}, True),
    ("restart", {"hold": {"reason": "saved_work_hold"}}, True),
    ("budget", {}, True),
    ("unknown", {"hold": {"reason": "restore_refused:x"}}, False),
    ("mystery", {}, False),
])
def test_resume_is_offered_only_where_the_resume_door_releases_the_pause(cause, raw, resumable):
    parked = row("t", "budget_paused", pause_cause=cause, can_resume=True)
    actions = {entry["action"] for entry in ws.controls_for(ws.classify_member(parked, raw), parked)}
    assert ("resume" in actions) is resumable and "stop" in actions and "pause" not in actions


def test_an_ended_episode_with_running_helpers_offers_only_stop_on_its_tree():
    controls = ws.controls_for(None, None, earlier_live=[], live_roots=["p"])
    assert _actions(controls) == [("stop", "p")]


def test_the_works_turn_and_mains_attention_share_one_rule():
    from ouroboros.gateway.main_decisions import owner_actions

    rows = [
        row("a", "budget_paused", pause_cause="budget", can_resume=True),
        row("b", "budget_paused", pause_cause="owner", can_resume=False),
        row("c", model_waits={"w": {"state": "waiting", "task_attempt": 1, "requires_owner_action": True, "revision": 1}}),
        row("d", model_waits={"w": {"state": "waiting", "task_attempt": 1, "requires_owner_action": True}}),  # no revision
        row("e", "budget_paused", pause_cause="restart", can_resume=False),
    ]
    for item in rows:
        waits, resume = owner_actions(item)
        owner_turns = [turn for turn in ws.classify_member(item)["turn"] if turn["kind"] == "action"]
        assert bool(owner_turns) is bool(waits or resume), item["activity_id"]
