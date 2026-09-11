"""Exact-cycle reconciliation regressions for plan review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.test_plan_review_engine import (
    CLEAN,
    DECK_SPEC,
    _call,
    _control,
    _finding,
    harness as _engine_harness,
    _patch_health,
    _state,
)

# Explicitly re-export the fixture. pytest 8.x does not reliably discover a
# fixture from a test module via ``pytest_plugins`` when the provider is also
# collected, while pytest 9.x happens to do so.
harness = _engine_harness  # noqa: F811 - pytest fixture re-export


def _install_two_turn_substrate(monkeypatch, calls, *, pending_ids=None, texts=None):
    import ouroboros.review_custody as review_custody
    import ouroboros.review_substrate as review_substrate

    pending_ids = set(pending_ids or [])
    texts = dict(texts or {})

    def substrate(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append((request.retry_key, [slot.slot_id for slot in slots]))
        first = len(calls) == 1
        actors = []
        for slot in slots:
            pending = first and (not pending_ids or slot.slot_id in pending_ids)
            actors.append({
                "slot_id": slot.slot_id, "model": slot.model,
                "status": "error" if pending else "ok",
                "raw_text": "" if pending else texts.get(slot.slot_id, CLEAN),
                "error": "logical wait expired" if pending else "",
                "usage": {"resolved_model": slot.model},
                "prompt_ref": {}, "response_ref": {},
                "operation_id": f"op-{slot.slot_id}",
                "operation_state": "in_flight" if pending else "late_settled",
                "late_result_pending": pending,
            })
        return SimpleNamespace(actors=actors)

    monkeypatch.setattr(review_substrate, "run_review_request", substrate)
    monkeypatch.setattr(review_custody, "review_retry_custody_available", lambda **_kwargs: True)


def test_partial_quorum_stays_open_while_one_paid_slot_is_in_flight(harness, monkeypatch):
    """A 2/3 parseable quorum cannot close over a live paid reviewer worker."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    calls = []
    _install_two_turn_substrate(monkeypatch, calls, pending_ids={"s3"})
    ctx = harness.make_ctx()

    first = _call(ctx)
    state = _state(harness)
    wave = state["waves"][-1]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert wave["aggregate"] == "DEGRADED"
    assert wave["closed"] is False and wave["custody_pending"] is True
    assert "review_late_result_pending" in wave["reasons"]
    assert "Closed: proceed" not in first

    second = _call(ctx)
    assert _control(second) == {"outcome": "GREEN", "closed": True}


def test_expired_deadline_still_reconciles_existing_paid_wave(harness, monkeypatch):
    """An owner deadline must not strand a reviewer cycle already in flight."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    calls = []
    _install_two_turn_substrate(monkeypatch, calls, pending_ids={"s3"})
    ctx = harness.make_ctx()
    ctx.task_metadata["deadline_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=2000)
    ).isoformat()

    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert _state(harness)["waves"][-1]["custody_pending"] is True

    # The second envelope arrives after the task's logical deadline. It must
    # rejoin the exact paid cycle, not return a fresh-deadline skip forever.
    ctx.task_metadata["deadline_at"] = "2000-01-01T00:00:00+00:00"
    second = _call(ctx)

    assert _control(second) == {"outcome": "GREEN", "closed": True}
    assert calls == [calls[0], calls[0]]
    wave = _state(harness)["waves"][-1]
    assert wave["custody_pending"] is False and wave["closed"] is True


def test_resume_keeps_original_dispatched_set_when_skipped_lane_heals(harness, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    health_calls = []

    def health(_slots):
        health_calls.append(1)
        if len(health_calls) > 1:
            raise AssertionError("in-flight reconciliation re-probed live health")
        return {"s1": {"failure_code": "credential_pool_exhausted", "reset_at": ""}}

    _patch_health(monkeypatch, health)
    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    out = _call(ctx)

    assert _control(out) == {"outcome": "GREEN", "closed": True}
    assert calls == [(calls[0][0], ["s2", "s3"]), (calls[0][0], ["s2", "s3"])]
    assert health_calls == [1]
    state = _state(harness)
    assert state["cycles_paid"] == 1 and state["waves"][-1]["cycle_index"] == 1
    frozen = {row["slot_id"]: row for row in state["waves"][-1]["actors"]}["s1"]
    assert frozen["failure_code"] == "credential_pool_exhausted" and frozen["cost"] == 0.0


def test_resume_keeps_dispatched_lane_when_live_health_worsens(harness, monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    health_state, health_calls = {"evidence": {}}, []

    def health(_slots):
        health_calls.append(1)
        if len(health_calls) > 1:
            raise AssertionError("in-flight reconciliation re-probed worsened health")
        return dict(health_state["evidence"])

    _patch_health(monkeypatch, health)
    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    health_state["evidence"] = {
        "s1": {"failure_code": "subscription_window_exhausted",
               "reset_at": "2030-01-01T00:00:00+00:00"},
    }
    out = _call(ctx)

    assert _control(out) == {"outcome": "GREEN", "closed": True}
    assert calls == [
        (calls[0][0], ["s1", "s2", "s3"]),
        (calls[0][0], ["s1", "s2", "s3"]),
    ]
    assert health_calls == [1]
    assert _state(harness)["cycles_paid"] == 1


def test_in_flight_wave_defers_need_evidence_until_terminal_reconciliation(
    harness, monkeypatch,
):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    requested = json.dumps([_finding(
        "e1", "need_evidence", locator="notes.md", summary="read the notes",
    )])
    calls = []
    _install_two_turn_substrate(
        monkeypatch, calls, pending_ids={"s2", "s3"}, texts={"s1": requested},
    )
    ctx = harness.make_ctx()
    _call(ctx)
    first_state = _state(harness)
    first_fp = first_state["waves"][-1]["request_fingerprint"]
    assert first_state["need_evidence_seen"] == []

    out = _call(ctx)
    state = _state(harness)
    assert _control(out) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert [key for key, _slots in calls] == [calls[0][0], calls[0][0]]
    assert state["waves"][-1]["request_fingerprint"] == first_fp
    assert state["waves"][-1]["cycle_index"] == 1 and state["cycles_paid"] == 1
    assert state["need_evidence_seen"] == ["notes.md"]


@pytest.mark.parametrize(
    ("actors", "custody_pending"),
    [
        (["CORRUPT-ROW"], True),
        ([{
            "slot_id": "s1", "operation_id": "op-s1",
            "operation_state": "settled", "late_result_pending": False,
            "status": "error", "error": "unknown custody",
            "usage": {"physical_attempt_state": "future_state"},
        }], False),
    ],
)
def test_malformed_paid_wave_cannot_bypass_resume_validation(
    harness, monkeypatch, actors, custody_pending,
):
    """Malformed exact custody must not fall through to a fresh paid cycle."""
    from ouroboros.tools import plan_review as plan_review_tool

    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert len(calls) == 1

    materialize = plan_review_tool._authority_wave

    def malformed_authority(*args, **kwargs):
        wave = dict(materialize(*args, **kwargs))
        wave.update({
            "actors": actors, "paid": True,
            "custody_pending": custody_pending,
            "aggregate": "DEGRADED", "closed": False, "health_epoch": [],
        })
        return wave

    monkeypatch.setattr(plan_review_tool, "_authority_wave", malformed_authority)
    second = _call(ctx)

    assert len(calls) == 1
    assert second.startswith("ERROR: PLAN_REVIEW_CUSTODY_INVALID:")
    assert "Refusing" in second


def test_contradictory_positive_capture_reenters_custody_instead_of_fresh_cycle(
    harness, monkeypatch,
):
    """A synthetic $0 label cannot erase a positive physical-attempt fact."""
    import ouroboros.review_custody as review_custody
    from ouroboros.tools import plan_review as plan_review_tool

    calls = []
    _install_two_turn_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert len(calls) == 1

    materialize = plan_review_tool._authority_wave

    def contradictory_authority(*args, **kwargs):
        wave = dict(materialize(*args, **kwargs))
        wave.update({
            "actors": [{
                "slot_id": "s1", "operation_id": "op-s1",
                "operation_state": "not_dispatched", "late_result_pending": False,
                "status": "not_dispatched", "error": "synthetic refusal",
                "usage": {
                    "physical_attempt_state": "unresolved",
                    "provider_status_code": 503,
                },
            }, {
                "slot_id": "s2", "operation_id": "op-s2-free",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "frozen $0 refusal",
            }, {
                "slot_id": "s3", "operation_id": "op-s3-free",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "frozen $0 refusal",
            }],
            "paid": True, "custody_pending": False,
            "aggregate": "DEGRADED", "closed": False, "health_epoch": [],
        })
        return wave

    monkeypatch.setattr(plan_review_tool, "_authority_wave", contradictory_authority)
    monkeypatch.setattr(
        review_custody, "review_retry_custody_available", lambda **_kwargs: False,
    )
    second = _call(ctx)

    assert len(calls) == 1
    assert _control(second) == {"outcome": "DEGRADED", "closed": False}
    assert "process-local custody is unavailable" in second


def test_resume_excludes_synthetic_not_dispatched_operation_ids(tmp_path):
    """A pre-dispatch $0 row has an operation id, but is not a callable lane."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    slots = [
        SimpleNamespace(slot_id="s1", model="model/one"),
        SimpleNamespace(slot_id="s2", model="model/two"),
    ]
    result = in_flight_resume_inputs(
        {
            "actors": [
                {
                    "slot_id": "s1", "operation_id": "op-paid",
                    "operation_state": "in_flight", "late_result_pending": True,
                    "status": "error", "error": "still running",
                },
                {
                    "slot_id": "s2", "operation_id": "op-free",
                    "operation_state": "not_dispatched", "status": "not_dispatched",
                    "error": "budget admission refused",
                },
            ],
        },
        {}, tmp_path, "mixed-resume", slots,
    )

    assert result["dispatched_slot_ids"] == ["s1"]
    assert [row["slot_id"] for row in result["frozen_rows"]] == ["s2"]


def test_resume_counts_positive_capture_despite_synthetic_not_dispatched(tmp_path):
    """Positive physical custody outranks contradictory synthetic $0 labels."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "not_dispatched", "status": "not_dispatched",
                "error": "synthetic refusal",
                "usage": {
                    "physical_attempt_state": "unresolved",
                    "provider_status_code": 503,
                },
            }],
        },
        {}, tmp_path, "contradictory-resume", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert result["dispatched_slot_ids"] == ["s1"]
    assert result["frozen_rows"] == []


def test_resume_rejects_non_object_rows_in_exact_paid_roster(tmp_path):
    """A corrupt durable roster must not lose rows during reconciliation."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "in_flight", "late_result_pending": True,
                "status": "error", "error": "still running",
            }, "CORRUPT-ROW"],
        },
        {}, tmp_path, "malformed-roster", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert "error" in result
    assert "drop rows" in result["error"]


def test_resume_rejects_unknown_physical_attempt_state(tmp_path):
    """Unknown custody facts cannot be inferred as a safe retry or refusal."""
    from ouroboros.tools.plan_review_artifacts import in_flight_resume_inputs

    result = in_flight_resume_inputs(
        {
            "actors": [{
                "slot_id": "s1", "operation_id": "op-paid",
                "operation_state": "settled", "status": "error",
                "error": "provider state unavailable",
                "usage": {"physical_attempt_state": "future_state"},
            }],
        },
        {}, tmp_path, "unknown-roster-state", [
            SimpleNamespace(slot_id="s1", model="model/one"),
        ],
    )

    assert "error" in result
    assert "unknown physical-attempt state" in result["error"]


def test_zero_send_route_refusal_does_not_spend_a_plan_cycle(harness, monkeypatch):
    """Callable configuration is not monetary proof when every slot refuses pre-send."""
    import ouroboros.review_substrate as review_substrate

    calls = []

    def zero_send(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append([slot.slot_id for slot in slots])
        return SimpleNamespace(actors=[{
            "slot_id": slot.slot_id,
            "model": slot.model,
            "status": "not_dispatched",
            "raw_text": "",
            "error": "agent session slot has no session task",
            "failure_code": "session_task_missing",
            "usage": {},
            "prompt_ref": {},
            "response_ref": {},
            "operation_id": f"op-{slot.slot_id}",
            "operation_state": "not_dispatched",
            "late_result_pending": False,
        } for slot in slots])

    monkeypatch.setattr(review_substrate, "run_review_request", zero_send)
    output = _call(harness.make_ctx())
    state = _state(harness)
    wave = state["waves"][-1]

    assert calls == [["s1", "s2", "s3"]]
    assert _control(output) == {"outcome": "DEGRADED", "closed": False}
    assert wave["paid"] is False
    assert state["cycles_paid"] == 0
    assert {row["failure_code"] for row in wave["actors"]} == {
        "session_task_missing",
    }


@pytest.mark.parametrize("row", [
    {"status": "error", "error": "substrate omitted the actor"},
    {"status": "error", "usage": {"physical_attempt_state": "settled"}},
    {"status": "error", "usage": {"physical_attempt_state": "future_state"}},
])
def test_missing_operation_identity_cannot_prove_a_free_wave(row):
    from ouroboros.tools.plan_review_artifacts import _row_has_physical_dispatch

    assert _row_has_physical_dispatch(row) is True


def test_explicit_zero_send_fact_is_the_only_missing_identity_free_proof():
    from ouroboros.tools.plan_review_artifacts import _row_has_physical_dispatch

    assert _row_has_physical_dispatch({
        "status": "not_dispatched", "operation_state": "not_dispatched",
    }) is False


def test_missing_substrate_actor_stays_paid_and_custody_lost(harness, monkeypatch):
    """A dropped actor is unknown physical custody, never a terminal free retry."""
    import ouroboros.review_substrate as review_substrate

    calls = []

    def no_actors(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append([slot.slot_id for slot in slots])
        return SimpleNamespace(actors=[])

    monkeypatch.setattr(review_substrate, "run_review_request", no_actors)
    first = _call(harness.make_ctx())
    state = _state(harness)
    wave = state["waves"][-1]

    assert calls == [["s1", "s2", "s3"]]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert wave["paid"] is True and wave["custody_pending"] is True
    assert state["cycles_paid"] == 1
    assert {row["failure_code"] for row in wave["actors"]} == {
        "review_custody_lost",
    }
    assert {row["operation_state"] for row in wave["actors"]} == {
        "custody_lost",
    }

    second = _call(harness.make_ctx())
    assert _control(second) == {"outcome": "DEGRADED", "closed": False}
    assert "Refusing a duplicate paid send" in second
    assert calls == [["s1", "s2", "s3"]]
    assert _state(harness)["cycles_paid"] == 1


# ------------------------------------------------------------- collection (P1-3)


def _install_barrier_substrate(monkeypatch, calls, *, texts=None, still_pending=()):
    """A substrate that honours the event route: a fresh dispatch released at its
    drain deadline returns ``pending_dispatch`` rows; a reconcile returns the settled
    rows (except ``still_pending`` slots, which are still running)."""
    import ouroboros.review_custody as review_custody
    import ouroboros.review_substrate as review_substrate

    texts = dict(texts or {})

    def substrate(request, *, slots, drive_root, llm, usage_ctx=None):
        calls.append({"retry_key": request.retry_key, "slots": [s.slot_id for s in slots],
                      "reconcile_only": request.reconcile_only, "drain": request.drain_deadline})
        fresh = request.drain_deadline is not None and not request.reconcile_only
        actors = []
        for slot in slots:
            pending = fresh or slot.slot_id in still_pending
            actors.append({
                "slot_id": slot.slot_id, "model": slot.model,
                "status": "error" if pending else "ok",
                "raw_text": "" if pending else texts.get(slot.slot_id, CLEAN),
                "error": "Pending dispatch; the physical review operation is in flight" if pending else "",
                "usage": {"resolved_model": slot.model, **({} if pending else {"physical_attempt_state": "settled"})},
                "prompt_ref": {}, "response_ref": {}, "operation_id": f"op-{slot.slot_id}",
                "operation_state": "pending_dispatch" if pending else "settled",
                "late_result_pending": pending,
            })
        return SimpleNamespace(actors=actors)

    monkeypatch.setattr(review_substrate, "run_review_request", substrate)
    monkeypatch.setattr(review_custody, "review_retry_custody_available", lambda **_kwargs: True)


def _collect(ctx, fingerprint, items=()):
    from ouroboros.tools import plan_review as pr

    return pr._handle_plan_task(ctx, review_disposition={"review_fingerprint": fingerprint, "items": list(items)})


def test_disposition_with_empty_items_collects_the_settled_wave_without_a_second_send(harness, monkeypatch):
    calls = []
    _install_barrier_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    first = _call(ctx)
    wave = _state(harness)["waves"][-1]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    assert wave["custody_pending"] is True and wave["paid"] is False
    assert calls[0]["drain"] is not None and calls[0]["reconcile_only"] is False

    collected = _collect(ctx, wave["request_fingerprint"])
    assert _control(collected) == {"outcome": "GREEN", "closed": True}
    state = _state(harness)
    assert state["cycles_paid"] == 1 and state["waves"][-1]["paid"] is True
    # ONE reconcile call: the same retry key, every released slot, no re-dispatch, no wait.
    assert [c["reconcile_only"] for c in calls] == [False, True]
    assert calls[1]["retry_key"] == calls[0]["retry_key"] and calls[1]["slots"] == ["s1", "s2", "s3"]
    assert calls[1]["drain"] is not None
    assert state["current_attempt"]["fingerprint"] == wave["request_fingerprint"]


def test_collection_never_waits_for_a_live_slot_and_stays_free(harness, monkeypatch):
    calls = []
    _install_barrier_substrate(monkeypatch, calls, still_pending={"s3"})
    ctx = harness.make_ctx()
    _call(ctx)
    fingerprint = _state(harness)["waves"][-1]["request_fingerprint"]
    peek = _collect(ctx, fingerprint)
    assert _control(peek) == {"outcome": "DEGRADED", "closed": False}
    wave = _state(harness)["waves"][-1]
    assert wave["custody_pending"] is True
    by_slot = {a["slot_id"]: a for a in wave["actors"]}
    assert by_slot["s1"]["ok"] and by_slot["s2"]["ok"]
    assert by_slot["s3"]["operation_state"] == "pending_dispatch"
    assert calls[-1]["drain"] is not None  # window 0: a peek, never a wait
    # Two settled physical rows prove dispatch: the cycle is paid now, once.
    assert _state(harness)["cycles_paid"] == 1
    # A disposition with items on a still-open wave is refused nothing: the peek
    # text is returned and the items wait for the wave to settle.
    again = _collect(ctx, fingerprint, items=[{"finding_id": "x", "decision": "accept", "rationale": "r"}])
    assert _control(again) == {"outcome": "DEGRADED", "closed": False}
    assert _state(harness)["cycles_paid"] == 1


def test_new_envelope_reconciles_the_in_flight_wave_before_superseding(harness, monkeypatch):
    calls = []
    _install_barrier_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    old = _state(harness)["waves"][-1]
    assert old["custody_pending"] is True
    second = _call(ctx, spec={**DECK_SPEC, "in_scope": ["a 6-slide deck"]})
    assert _control(second) == {"outcome": "DEGRADED", "closed": False}
    state = _state(harness)
    waves = {w["request_fingerprint"]: w for w in state["waves"]}
    # The old wave was collected (settled rows landed, closed GREEN, paid) BEFORE the
    # new envelope superseded it; the new wave is the current open attempt.
    assert waves[old["request_fingerprint"]]["closed"] is True
    assert waves[old["request_fingerprint"]]["paid"] is True
    new_fp = state["current_attempt"]["fingerprint"]
    assert new_fp != old["request_fingerprint"] and waves[new_fp]["custody_pending"] is True
    assert [c["reconcile_only"] for c in calls] == [False, True, False]
    assert calls[1]["retry_key"] == calls[0]["retry_key"]
    assert state["cycles_paid"] == 1  # the old wave's cycle; the new barrier wave is unpaid


def test_compaction_keeps_an_in_flight_wave_full(tmp_path):
    from ouroboros.task_results import _PLAN_REVIEW_FULL_WAVES, load_plan_review_state, record_plan_review_wave
    from tests.test_plan_review import _wave

    pending = {**_wave("a" * 64, aggregate="DEGRADED"), "paid": False, "custody_pending": True,
               "actors": [{"slot_id": "s1", "operation_state": "pending_dispatch"}]}
    record_plan_review_wave(tmp_path, "t", pending)
    for index in range(_PLAN_REVIEW_FULL_WAVES + 1):
        record_plan_review_wave(tmp_path, "t", _wave(f"{index:064x}", aggregate="GREEN", closed=True))
    waves = load_plan_review_state(tmp_path, "t")["waves"]
    first = waves[0]
    assert first["request_fingerprint"] == "a" * 64
    assert not first.get("compact") and first["custody_pending"] is True
    assert waves[1].get("compact") is True


def test_collect_binds_acceptance_claims_the_same_as_a_synchronous_close(harness, monkeypatch):
    from ouroboros.contracts.task_contract import effective_acceptance_claims
    from ouroboros.task_results import closed_plan_review_wave

    calls = []
    _install_barrier_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    _call(ctx)
    state = _state(harness)
    assert closed_plan_review_wave(state) is None
    assert effective_acceptance_claims({}, closed_plan_review_wave(state)) == ([], "")
    _collect(ctx, state["waves"][-1]["request_fingerprint"])
    claims, source = effective_acceptance_claims({}, closed_plan_review_wave(_state(harness)))
    assert source == "plan_review" and [c["claim"] for c in claims] == DECK_SPEC["acceptance_claims"]


def test_two_step_wave_emits_one_advisory_open_event_and_keeps_the_paid_identity(harness, monkeypatch):
    from ouroboros.loop_acceptance_review import acceptance_paid_identity

    harness.state["enforcement"] = "advisory"
    calls = []
    _install_barrier_substrate(monkeypatch, calls, still_pending={"s2"})
    ctx = harness.make_ctx()
    _call(ctx)
    fingerprint = _state(harness)["waves"][-1]["request_fingerprint"]
    _collect(ctx, fingerprint)
    rows = [json.loads(line) for line in
            (harness.drive / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    opens = [r for r in rows if r.get("type") == "plan_review_advisory_open" and r.get("fingerprint") == fingerprint]
    assert len(opens) == 1  # dispatch -> collect is ONE recorded-open state, deduplicated
    # Two plan_task receipts change the acceptance EVIDENCE revision, never the
    # paid identity a panel is bought under (candidate + dispositions only).
    trace = {"tool_calls": [{"plan_review_outcome": "DEGRADED"}, {"plan_review_outcome": "DEGRADED"}],
             "acceptance_obligations": []}
    assert acceptance_paid_identity("cand", trace) == acceptance_paid_identity("cand", {"tool_calls": [], "acceptance_obligations": []})
