"""Standing findings follow the REAL predecessor across same-fingerprint re-dispatches and
pending supersessions: the wave a dispatch judged against stays reachable by its exact
artifact (``previous_wave_artifact``), and a seat still pending when its wave was superseded
owes its answer to the wave before (``plan_review_artifacts.standing_findings_lineage``)."""

from __future__ import annotations

import json

from tests.test_plan_review_engine import CLEAN, _call, _control, _finding, _patch_health, _state
from tests.test_plan_review_engine import harness as _engine_harness
from tests.test_plan_review_epoch import _effort_aware_builder
from tests.test_plan_review_reconciliation import _collect, _install_barrier_substrate

harness = _engine_harness  # noqa: F811 - pytest fixture re-export


def _objection(fid, summary):
    return json.dumps([_finding(fid, "blocking", breaks="claim_1", summary=summary)])


def _carried(h):
    return sorted(f["finding_id"] for f in _state(h)["waves"][-1]["findings"] if f.get("carried_absent_answer"))


def test_a_returning_fingerprint_keeps_the_predecessor_it_was_judged_against(harness, monkeypatch):
    """A: s2 objects. B (prose changed, same spec): s1 objects, s2 retires. Back to A's prose
    with a changed order: the new wave replaces old A in the hot index but was judged
    against B, so a silent s1 carries B's objection and s2's retired A finding never
    returns; the wave stays REVIEW_REQUIRED."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "6")
    _patch_health(monkeypatch, lambda slots: {})
    _effort_aware_builder(harness, monkeypatch)
    sub = harness.install({"s1": CLEAN, "s2": _objection("a1", "Friday is impossible"), "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    sub.answers = {"s1": _objection("b1", "No rollback step"), "s2": CLEAN, "s3": CLEAN}
    assert _control(_call(ctx, plan="Draft each slide first, outline after.")) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    calls = []
    _install_barrier_substrate(monkeypatch, calls, refused={"s1"})
    assert _control(_call(ctx, reviewer_effort="max")) == {"outcome": "DEGRADED", "closed": False}
    wave = _state(harness)["waves"][-1]
    assert wave["previous_wave_artifact"] and wave["previous_fingerprint"] != wave["request_fingerprint"]
    settled = _collect(ctx, wave["request_fingerprint"])
    assert _control(settled) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert _carried(harness) == ["s1:b1"]


def test_a_seat_pending_through_a_superseded_wave_still_owes_its_earlier_objection(harness, monkeypatch):
    """A: s1 objects. B (prose changed): s1 never answers before B is superseded. C (prose
    changed again): s1 is refused at $0. C's immediate predecessor B holds no answer from
    s1, so the lineage walks to A and carries s1's objection; a clean s1 in C retires it."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "6")
    _patch_health(monkeypatch, lambda slots: {})
    _effort_aware_builder(harness, monkeypatch)
    harness.install({"s1": _objection("n1", "Friday is impossible"), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    _install_barrier_substrate(monkeypatch, [], still_pending={"s1"})
    assert _control(_call(ctx, plan="Draft each slide first, outline after.")) == {"outcome": "DEGRADED", "closed": False}
    b_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_collect(ctx, b_fp)) == {"outcome": "DEGRADED", "closed": False}
    assert _carried(harness) == []  # s1 is merely awaited on B: nothing is carried there
    _install_barrier_substrate(monkeypatch, [], refused={"s1"}, pending_waves={b_fp})
    assert _control(_call(ctx, plan="Rehearse, then draft, then outline.")) == {"outcome": "DEGRADED", "closed": False}
    c_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_collect(ctx, c_fp)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert _carried(harness) == ["s1:n1"]
    s1 = next(a for a in _state(harness)["waves"][-1]["actors"] if a["slot_id"] == "s1")
    assert s1["operation_state"] == "not_dispatched" and s1["carried_findings"] == 1
    # Positive path: an actual clean answer from s1 on a further prose revision retires it.
    _install_barrier_substrate(monkeypatch, [], pending_waves={b_fp})
    assert _control(_call(ctx, plan="Outline, draft, rehearse, ship.")) == {"outcome": "DEGRADED", "closed": False}
    assert _control(_collect(ctx, _state(harness)["waves"][-1]["request_fingerprint"])) == {"outcome": "GREEN", "closed": True}
    assert _carried(harness) == []
