"""Standing findings follow the REAL predecessor across same-fingerprint re-dispatches and
pending supersessions: the wave a dispatch judged against stays reachable by its exact
artifact (``previous_wave_artifact``), and a seat still pending when its wave was superseded
owes its answer to the wave before (``plan_review_artifacts.standing_findings_lineage``)."""

from __future__ import annotations

import json
import pathlib

import pytest

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
    _install_barrier_substrate(monkeypatch, [], refused={"s1"}, pending_by_wave={b_fp: {"s1"}})
    assert _control(_call(ctx, plan="Rehearse, then draft, then outline.")) == {"outcome": "DEGRADED", "closed": False}
    c = _state(harness)["waves"][-1]
    b = next(w for w in _state(harness)["waves"] if w["request_fingerprint"] == b_fp)
    assert b["paid"] is True and c["previous_fingerprint"] == b_fp and c["previous_wave_artifact"]
    assert _control(_collect(ctx, c["request_fingerprint"])) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert _carried(harness) == ["s1:n1"]
    s1 = next(a for a in _state(harness)["waves"][-1]["actors"] if a["slot_id"] == "s1")
    assert s1["operation_state"] == "not_dispatched" and s1["carried_findings"] == 1
    # Positive path: an actual clean answer from s1 on a further prose revision retires it.
    _install_barrier_substrate(monkeypatch, [], pending_by_wave={b_fp: {"s1"}})
    assert _control(_call(ctx, plan="Outline, draft, rehearse, ship.")) == {"outcome": "DEGRADED", "closed": False}
    assert _control(_collect(ctx, _state(harness)["waves"][-1]["request_fingerprint"])) == {"outcome": "GREEN", "closed": True}
    assert _carried(harness) == []


# ------------------------------------------------------------------ the walk itself (hot index, no artifacts)

from ouroboros.tools import plan_spec  # noqa: E402
from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable, standing_findings_lineage  # noqa: E402
from tests.test_plan_review_engine import DECK_SPEC  # noqa: E402

SPEC = plan_spec.normalize_spec(DECK_SPEC)[0]
OBJECTION = {"finding_id": "s1:n1", "id": "n1", "class": "blocking", "slot": "s1", "breaks": "claim_1", "summary": "Friday"}


def _hot(fp, cycle, *, actors, findings=(), previous="", closed=False, spec=None, **extra):
    """A hot-index wave shaped like the writer leaves it (inline spec, no artifact)."""
    return {"request_fingerprint": fp, "cycle_index": cycle, "spec": dict(spec or SPEC), "closed": closed, "paid": True,
            "aggregate": "GREEN" if closed else ("REVIEW_REQUIRED" if findings else "DEGRADED"),
            "findings": list(findings), "dispositions": [], "previous_fingerprint": previous, "actors": list(actors), **extra}


def _ok(sid):
    return {"slot_id": sid, "ok": True, "operation_state": "settled"}


def _pend(sid):
    return {"slot_id": sid, "ok": False, "operation_state": "pending_dispatch", "late_result_pending": True}


def _walk(waves, previous):
    return standing_findings_lineage(pathlib.Path("/nonexistent"), "task", {"waves": waves}, previous, SPEC, "blocking")


def test_the_walk_follows_any_length_of_pending_history():
    waves = [_hot("a" * 64, 1, actors=[_ok("s1"), _ok("s2")], findings=[OBJECTION])]
    for n in range(2, 13):  # eleven paid revisions in which s1 never answered
        waves.append(_hot(chr(ord("a") + n) * 64, n, actors=[_pend("s1"), _ok("s2")], previous=waves[-1]["request_fingerprint"]))
    assert _walk(waves, waves[-1]) == {"s1": [OBJECTION]}


def test_a_newer_real_answer_ends_the_obligation_and_an_older_wave_never_re_adds_it():
    a = _hot("a" * 64, 1, actors=[_ok("s1"), _ok("s2")], findings=[OBJECTION])
    b = _hot("b" * 64, 2, actors=[_pend("s1"), _pend("s2")], previous=a["request_fingerprint"])
    c = _hot("c" * 64, 3, actors=[_ok("s1"), _pend("s2")], previous=b["request_fingerprint"])  # s1 retired it here
    assert _walk([a, b, c], c) == {}
    still = _hot("c" * 64, 3, actors=[_pend("s1"), _pend("s2")], previous=b["request_fingerprint"])
    assert _walk([a, b, still], still) == {"s1": [OBJECTION]}


def test_a_changed_spec_on_the_immediate_predecessor_ends_the_walk():
    other = dict(SPEC, in_scope=list(SPEC["in_scope"]) + ["a 6-slide deck"])
    a = _hot("a" * 64, 1, actors=[_ok("s1")], findings=[OBJECTION])
    b = _hot("b" * 64, 2, actors=[_pend("s1")], previous=a["request_fingerprint"], spec=other)
    assert _walk([a, b], b) == {}
    same = _hot("b" * 64, 2, actors=[_pend("s1")], previous=a["request_fingerprint"])
    assert _walk([a, same], same) == {"s1": [OBJECTION]}


def test_history_that_cannot_be_read_is_never_an_empty_obligation():
    a = _hot("a" * 64, 1, actors=[_ok("s1")], findings=[OBJECTION], compact=True, wave_artifact={})
    b = _hot("b" * 64, 2, actors=[_pend("s1")], previous=a["request_fingerprint"])
    with pytest.raises(PlanReviewSourceUnavailable):  # a compact entry with no readable artifact
        _walk([a, b], b)
    missing = _hot("b" * 64, 2, actors=[_pend("s1")], previous="f" * 64)
    with pytest.raises(PlanReviewSourceUnavailable):  # a predecessor that left the index
        _walk([missing], missing)
    broken = _hot("b" * 64, 2, actors=[_pend("s1")], previous_wave_artifact={"root": "artifact_store", "path": "nope.json"})
    with pytest.raises(PlanReviewSourceUnavailable):  # an unreadable pointer
        _walk([broken], broken)
    looped = _hot("b" * 64, 2, actors=[_pend("s1")], previous="b" * 64)
    with pytest.raises(PlanReviewSourceUnavailable):  # a wave naming itself
        _walk([looped], looped)
    # Quiet side: a seat that was pending in the very first wave owes nothing (nobody objected).
    first = _hot("a" * 64, 1, actors=[_pend("s1")])
    assert _walk([first], first) == {}
