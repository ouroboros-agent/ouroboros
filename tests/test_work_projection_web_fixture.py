"""The browser reads the server's real work projection: one shared fixture for both sides.

``web/tests/fixtures/work_projection_cases.json`` holds the ``/api/state`` work fields
(``works``, ``rooms``, ``work_summary``, ``works_complete``, ``works_gaps``) that
``gateway/work_projection.works_snapshot`` builds for the rooms below, and every reason
sentence the server can send (a counted one at 1 and at 3). This test rebuilds both from the
server and fails on any difference, so the web tests (``web/tests/work_status.test.js`` and
the chat/room fixtures) never test a shape or a sentence the server does not send.
Regenerate after an intended projection change by running this module as a script from the
repository root (``python -m tests.test_work_projection_web_fixture``).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.gateway import work_status
from tests._work_projection_shared import work_fields

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "web" / "tests" / "fixtures" / "work_projection_cases.json"


def _row(task_id, phase="working", **extra):
    return {"activity_id": task_id, "phase": phase, "kind": extra.pop("kind", "managed_task"),
            "chat_id": extra.pop("chat_id", 42), "task_attempt": 1, "started_at": extra.pop("started_at", 100.0), **extra}


_HOLD = {"_budget_pause_hold": {"reason": "continuation_writer_unsettled"}}

# name -> (census owner rows, raw queue facts, stored task results, held delegated runs)
CASES = {
    # A held Continue waits for three helpers of its failed earlier attempt.
    "held_continue": (
        [_row("c", "budget_paused", pause_cause="unknown", can_resume=False),
         *[_row(f"h{index}", parent_task_id="p", root_task_id="p") for index in range(3)]],
        {"c": {**_HOLD, "title": "Continue: release", "continuation": {"predecessor_task_id": "p"}}},
        {"p": {"task_id": "p", "status": "failed", "suggested_name": "Ouroboros 7.7.0 — finish the saved release"}},
        {},
    ),
    "working_call": (
        [_row("w", model_activity={"phase": "awaiting_response"}, suggested_name="Compare the cited pages")],
        {"w": {"suggested_name": "Compare the cited pages"}}, {}, {},
    ),
    "direct_question": (
        [_row("d", kind="direct_chat", chat_id=1, client_message_id="cm-ask",
              owner_wait={"quiz_id": "q1", "owner_wait_state": "waiting", "quiz_state": "open"})],
        {}, {}, {},
    ),
    "paused_by_owner": (
        [_row("o", "budget_paused", pause_cause="owner", can_resume=False)],
        {"o": {"_budget_pause": {"reason": "owner", "paused_at": "2026-10-17T15:00:00Z"}}}, {}, {},
    ),
    "paused_by_mind": (
        [_row("m", "budget_paused", pause_cause="owner", can_resume=False)],
        {"m": {"_budget_pause": {"reason": "owner"}}}, {}, {},
    ),
    "spending_limit": (
        [_row("s", "budget_paused", pause_cause="budget", can_resume=True)],
        {"s": {"_budget_pause": {"reason": "budget"}}}, {}, {},
    ),
    "model_access": (
        [_row("a", model_waits={"w1": {"state": "waiting", "task_attempt": 1, "revision": 1,
                                       "requires_owner_action": True}})],
        {}, {}, {},
    ),
    "queued_and_pausing": (
        [_row("q", "queued", started_at=90.0), _row("z", "budget_pausing", pause_cause="owner")],
        {}, {}, {},
    ),
    "finalizing_failed": (
        [_row("f", "finalizing", status="failed", outcome_axes={"lifecycle": {"status": "failed"}})],
        {}, {}, {},
    ),
    "sleeping": (
        [_row("sl", "budget_paused", pause_cause="sleep")],
        {"sl": {"_budget_pause": {"reason": "sleep"}}}, {}, {},
    ),
    "unknown": ([_row("u", "unknown")], {}, {}, {}),
    "ended_with_runs": ([], {}, {"r": {"task_id": "r", "status": "done", "chat_id": 42}}, {"r": ["run-1", "run-2"]}),
}

# The owner fence records who paused: the owner, or Ouroboros itself (the mind's own Pause).
FENCES = {"o": {"requested_by": "owner"}, "m": {"requested_by": "owner", "actor": "mind", "actor_task_id": "mind-1"},
          "z": {"requested_by": "owner"}}


def project(name):
    """The ``/api/state`` work fields the server builds for one case."""
    owners, raw, stored, held = CASES[name]
    return work_fields(owners, raw, results=stored, fences=FENCES, held=held)


def reason_sentences():
    """Each reason code's English sentence as the server words it; a counted code at 1 and at 3."""
    return {code: ({"1": work_status.reason(code, count=1)["text"], "3": work_status.reason(code, count=3)["text"]}
                   if isinstance(phrase, tuple) else {"": work_status.reason(code)["text"]})
            for code, phrase in work_status.REASON_TEXT.items()}


def test_the_web_fixture_is_the_servers_projection():
    built = {name: project(name) for name in CASES}
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert sorted(recorded["cases"]) == sorted(built)
    for name in CASES:
        assert recorded["cases"][name] == built[name], name
    assert recorded["reasons"] == reason_sentences()


@pytest.mark.parametrize("name, activity", [
    ("held_continue", "working"), ("direct_question", "waiting"), ("paused_by_owner", "paused"),
    ("spending_limit", "paused"), ("queued_and_pausing", "working"), ("unknown", "unknown"),
])
def test_each_fixture_room_is_the_state_its_name_says(name, activity):
    """The fixture is meaningful, not merely equal: each room is the case the web tests read it as."""
    assert [room["activity"] for room in project(name)["rooms"].values()] == [activity]


if __name__ == "__main__":  # regenerate the fixture after an intended projection change
    FIXTURE.write_text(json.dumps({"_note": "Generated by tests/test_work_projection_web_fixture.py (run it as a script).",
                                   "cases": {name: project(name) for name in CASES},
                                   "reasons": reason_sentences()}, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
