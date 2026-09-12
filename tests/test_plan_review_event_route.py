"""``plan_task``'s event route (owner batch 2, Q2=A): a fresh dispatch returns at
the dispatch barrier, the reviewer workers settle into process-local custody, the
last settlement writes ONE system frame into the task mailbox, and a later $0
collection closes the wave and pays its cycle exactly once.

Two levels: the custody drain loop itself (``run_custodied_review_slots`` with a
held worker) and the whole engine through the REAL review substrate with a
blocking executor (no ``run_review_request`` stub, no custody stub).
"""

from __future__ import annotations

import pathlib
import threading
import time
from types import SimpleNamespace


from tests.test_plan_review_engine import CLEAN, DECK_SPEC, _call, _control, _state
from tests.test_plan_review_engine import harness as _engine_harness

harness = _engine_harness  # noqa: F811 - pytest fixture re-export


def _mailbox_entries(drive, task_id):
    from ouroboros.owner_mailbox import drain_owner_entries

    return drain_owner_entries(pathlib.Path(drive), task_id, set())


def _custody_kwargs(tmp_path, *, surface, retry_key, slots, run_slot, ctx):
    from ouroboros.review_substrate import ReviewActorRecord, ReviewRequest
    from ouroboros.usage_accounting import UsageScope

    request = ReviewRequest(
        surface=surface, goal="review", task_id="event-route", retry_key=retry_key,
        reconciliation_identity={"subject_hash": "f" * 64},
    )

    def error_actor(slot, error, operation_id="", operation_state="settled"):
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model, status="error", error=error,
            operation_id=operation_id, operation_state=operation_state,
            late_result_pending=operation_state == "in_flight",
        )

    return request, dict(
        request=request, slots=slots, usage_ctx=ctx, task_id=request.task_id,
        usage_meta={}, review_usage_scope=UsageScope(drive_root=tmp_path),
        run_slot=run_slot, error_actor=error_actor,
    )


def _held_worker(slots, results, release, entered):
    from ouroboros.review_substrate import ReviewActorRecord

    calls = []

    def run_slot(slot, operation_id, _retry_state, _deadline, _checkpoint):
        calls.append(slot.slot_id)
        entered[slot.slot_id].set()
        assert release[slot.slot_id].wait(10), "test did not release the review worker"
        row = results.get(slot.slot_id) or {}
        return ReviewActorRecord(
            slot_id=slot.slot_id, model=slot.model,
            status=row.get("status", "ok"), raw_text=row.get("raw_text", CLEAN),
            error=row.get("error", ""), operation_id=operation_id,
            operation_state=row.get("operation_state", "settled"),
        )

    return calls, run_slot


def test_drain_deadline_releases_pending_dispatch_rows_and_the_last_settlement_writes_one_frame(tmp_path):
    import ouroboros.review_custody as custody
    from ouroboros.review_substrate import ReviewSlot

    slots = [ReviewSlot(slot_id="s1", model="m/a", timeout_sec=30.0),
             ReviewSlot(slot_id="s2", model="m/b", timeout_sec=30.0)]
    release = {s.slot_id: threading.Event() for s in slots}
    entered = {s.slot_id: threading.Event() for s in slots}
    calls, run_slot = _held_worker(slots, {}, release, entered)
    progress = []
    ctx = SimpleNamespace(drive_root=tmp_path, emit_progress_fn=progress.append)
    request, kwargs = _custody_kwargs(
        tmp_path, surface="plan_review", retry_key="plan_review:" + "f" * 64 + ":1",
        slots=slots, run_slot=run_slot, ctx=ctx)
    request.drain_deadline = time.monotonic()
    try:
        first = custody.run_custodied_review_slots(**kwargs)
        assert {a.operation_state for a in first} == {"pending_dispatch"}
        assert all(a.late_result_pending for a in first)
        assert all(a.error.startswith("Pending dispatch;") for a in first)
        assert all(entered[s.slot_id].wait(10) for s in slots)
        # Released, never timed out: the later settlement is not a ``late`` result.
        assert all(not e.timed_out and e.released_early for e in custody._ACTIVE.values())
        assert _mailbox_entries(tmp_path, request.task_id) == []
        release["s1"].set()
        deadline = time.time() + 10
        while len(progress) < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert len(progress) == 1 and "reviewer slot s1 settled (ok)" in progress[0]
        assert _mailbox_entries(tmp_path, request.task_id) == []  # one slot still running
        release["s2"].set()
        while len(_mailbox_entries(tmp_path, request.task_id)) < 1 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        for event in release.values():
            event.set()
    frames = _mailbox_entries(tmp_path, request.task_id)
    assert len(frames) == 1
    assert frames[0]["provenance"] == "system" and frames[0]["kind"] == "task_message"
    assert "Plan review wave ffffffff: 2 released reviewer slot(s) settled (2 ok, 0 failed)" in frames[0]["text"]
    assert "not yet collected" in frames[0]["text"]
    assert not custody._RELEASED_WAVES
    # Collection: the same cycle replays both settled actors with no second send.
    request.drain_deadline = time.monotonic()
    collected = custody.run_custodied_review_slots(**kwargs)
    assert [a.status for a in collected] == ["ok", "ok"]
    assert calls == ["s1", "s2"]
    assert len(_mailbox_entries(tmp_path, request.task_id)) == 1


def test_typed_zero_refusal_settled_after_release_replays_at_collection(tmp_path):
    import ouroboros.review_custody as custody
    from ouroboros.review_substrate import ReviewSlot

    slots = [ReviewSlot(slot_id="s1", model="m/a", timeout_sec=30.0)]
    release = {"s1": threading.Event()}
    entered = {"s1": threading.Event()}
    calls, run_slot = _held_worker(
        slots, {"s1": {"status": "not_dispatched", "raw_text": "",
                       "error": "Owner deadline exhausted before physical review dispatch",
                       "operation_state": "not_dispatched"}}, release, entered)
    ctx = SimpleNamespace(drive_root=tmp_path, emit_progress_fn=lambda _m: None)
    request, kwargs = _custody_kwargs(
        tmp_path, surface="plan_review", retry_key="plan_review:" + "e" * 64 + ":1",
        slots=slots, run_slot=run_slot, ctx=ctx)
    request.drain_deadline = time.monotonic()
    try:
        [first] = custody.run_custodied_review_slots(**kwargs)
        assert first.operation_state == "pending_dispatch"
        assert entered["s1"].wait(10)
        release["s1"].set()
        deadline = time.time() + 10
        while not _mailbox_entries(tmp_path, request.task_id) and time.time() < deadline:
            time.sleep(0.01)
    finally:
        release["s1"].set()
    [collected] = custody.run_custodied_review_slots(**kwargs)
    assert collected.operation_state == "not_dispatched"  # typed $0, never custody_lost
    assert calls == ["s1"]


def test_requests_without_a_drain_deadline_and_other_surfaces_are_untouched(tmp_path):
    import ouroboros.review_custody as custody
    from ouroboros.review_substrate import ReviewSlot

    slots = [ReviewSlot(slot_id="s1", model="m/a", timeout_sec=30.0)]
    release = {"s1": threading.Event()}
    entered = {"s1": threading.Event()}
    release["s1"].set()
    calls, run_slot = _held_worker(slots, {}, release, entered)
    progress = []
    ctx = SimpleNamespace(drive_root=tmp_path, emit_progress_fn=progress.append)
    request, kwargs = _custody_kwargs(
        tmp_path, surface="plan_review", retry_key="plan_review:" + "a" * 64 + ":1",
        slots=slots, run_slot=run_slot, ctx=ctx)
    assert request.drain_deadline is None
    [actor] = custody.run_custodied_review_slots(**kwargs)  # waits for the worker as before
    assert actor.status == "ok" and actor.operation_state == "settled"
    assert not custody._RELEASED_WAVES and progress == []
    assert _mailbox_entries(tmp_path, request.task_id) == []
    # A non-plan surface released at a drain deadline gets pending rows but no frame.
    triad, triad_kwargs = _custody_kwargs(
        tmp_path, surface="multi_model_review", retry_key="commit_review:" + "b" * 64,
        slots=slots, run_slot=run_slot, ctx=ctx)
    triad.drain_deadline = time.monotonic()
    release["s1"].clear()
    try:
        [pending] = custody.run_custodied_review_slots(**triad_kwargs)
        assert pending.operation_state == "pending_dispatch"
    finally:
        release["s1"].set()
    deadline = time.time() + 5
    while custody._RELEASED_WAVES and time.time() < deadline:
        time.sleep(0.01)
    assert _mailbox_entries(tmp_path, request.task_id) == [] and progress == []


class _HeldExecutor:
    """A plan-review api_chat executor whose sends block until the test releases them."""

    def __init__(self):
        self.execute_calls = 0
        self.release = threading.Event()

    def restore_custody(self, _state):
        return None

    def set_pending_invocation_checkpoint(self, _checkpoint):
        return None

    def prompt_payload(self):
        return {"messages": []}

    def prompt_chars(self):
        return 0

    def execute(self):
        from ouroboros.review_execution import ReviewAttemptResult

        self.execute_calls += 1
        assert self.release.wait(20), "test did not release the reviewer send"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "physical_attempt_state": "settled"}
        return ReviewAttemptResult(message={"content": CLEAN}, usage=usage, raw_text=CLEAN)

    def failure_custody(self):
        return {}


def _install_real_substrate(monkeypatch):
    """No run_review_request stub and no custody stub: the REAL drain loop runs."""
    executor = _HeldExecutor()
    monkeypatch.setattr("ouroboros.review_substrate._review_route_executor",
                        lambda *_a, **_k: executor)
    return executor


def _wait_until(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)
    return predicate()


def test_fresh_dispatch_returns_at_the_barrier_and_the_resubmitted_envelope_collects_once(harness, monkeypatch):
    executor = _install_real_substrate(monkeypatch)
    ctx = harness.make_ctx()
    try:
        first = _call(ctx)
        assert _control(first) == {"outcome": "DEGRADED", "closed": False}
        state = _state(harness)
        wave = state["waves"][-1]
        assert wave["custody_pending"] is True and wave["paid"] is False
        assert state["cycles_paid"] == 0  # paid iff dispatched: nothing proven yet
        assert {a["operation_state"] for a in wave["actors"]} == {"pending_dispatch"}
        assert "REVIEW CUSTODY PENDING" in first
        assert _wait_until(lambda: executor.execute_calls == 3)  # every worker reached its send
        assert _mailbox_entries(harness.drive, "task-1") == []
        executor.release.set()
        assert _wait_until(lambda: len(_mailbox_entries(harness.drive, "task-1")) == 1)
    finally:
        executor.release.set()
    [frame] = _mailbox_entries(harness.drive, "task-1")
    assert frame["provenance"] == "system"
    assert frame["text"].startswith(f"Plan review wave {wave['request_fingerprint'][:8]}: 3 released")
    # The frame reaches the model as a system task message, never as an owner directive.
    from ouroboros.loop_round_limits import _drain_incoming_messages
    import queue

    messages, owner_ctx = [], SimpleNamespace()
    _drain_incoming_messages(messages, queue.Queue(), harness.drive, "task-1", None, set(), owner_ctx=owner_ctx)
    assert messages and messages[0]["content"].startswith("[System task message]\nPlan review wave")
    assert getattr(owner_ctx, "_owner_directives", []) == []
    # The identical envelope is the existing resume path: it collects the settled
    # slots (one cycle, no second send) and closes the wave.
    second = _call(ctx)
    assert _control(second) == {"outcome": "GREEN", "closed": True}
    state = _state(harness)
    assert state["cycles_paid"] == 1 and state["waves"][-1]["paid"] is True
    assert executor.execute_calls == 3
    assert any("reviewer slot s1 settled (ok)" in line for line in harness.progress)


def test_barrier_wave_replaces_a_stale_paid_predecessor_and_pays_only_at_collection(tmp_path):
    """The D2 narrowing (an unpaid wave never replaces a paid predecessor) does not
    swallow the barrier wave of a re-dispatched stale DEGRADED envelope."""
    from ouroboros.task_results import load_plan_review_state, record_plan_review_wave
    from tests.test_plan_review import _wave

    fp = "c" * 64
    record_plan_review_wave(tmp_path, "t", {**_wave(fp, aggregate="DEGRADED"), "actors": []})
    barrier = {**_wave(fp, aggregate="DEGRADED"), "cycle_index": 2, "paid": False, "custody_pending": True,
               "actors": [{"slot_id": "s1", "operation_state": "pending_dispatch"}]}
    record_plan_review_wave(tmp_path, "t", barrier)
    state = load_plan_review_state(tmp_path, "t")
    assert state["cycles_paid"] == 1 and state["waves"][-1]["custody_pending"] is True
    collected = {**barrier, "paid": True, "custody_pending": False, "aggregate": "GREEN", "closed": True,
                 "actors": [{"slot_id": "s1", "operation_state": "settled", "physical_attempt_state": "settled"}]}
    record_plan_review_wave(tmp_path, "t", collected)
    state = load_plan_review_state(tmp_path, "t")
    assert state["cycles_paid"] == 2 and state["waves"][-1]["closed"] is True


def test_in_flight_panels_count_toward_the_cycle_cap_at_dispatch(harness, monkeypatch):
    """Fix cycle 1, F1: a dispatched panel commits its cycle at the barrier. Under
    OUROBOROS_REVIEW_MAX_CYCLES=1 a revised envelope submitted while the first panel is
    still in flight buys no second panel: it is refused with the typed cap state."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    executor = _install_real_substrate(monkeypatch)
    ctx = harness.make_ctx()
    try:
        first = _call(ctx)
        assert _control(first) == {"outcome": "DEGRADED", "closed": False}
        assert _wait_until(lambda: executor.execute_calls == 3)
        second = _call(ctx, spec={**DECK_SPEC, "in_scope": ["a 6-slide deck"]})
        assert second.startswith("⚠️ PLAN_REVIEW_CYCLES_EXHAUSTED: 1 of 1 paid plan-review cycles are spent")
        assert "REVIEW CUSTODY PENDING" in second  # the committed in-flight wave is the live obligation
        third = _call(ctx, spec={**DECK_SPEC, "in_scope": ["a 7-slide deck"]})
        assert executor.execute_calls == 3, "no panel beyond the cap was dispatched"
        assert third.startswith("⚠️ PLAN_REVIEW_CYCLES_EXHAUSTED")
        assert _state(harness)["cycles_paid"] == 0  # committed, not yet proven paid
        assert any(line.startswith("📐 plan_task: PLAN_REVIEW_CYCLES_EXHAUSTED") for line in harness.progress)
    finally:
        executor.release.set()
    assert _wait_until(lambda: len(_mailbox_entries(harness.drive, "task-1")) == 1)


def test_the_barrier_records_no_failed_last_execution_for_running_slots(harness, monkeypatch, tmp_path):
    """Fix cycle 1, F3: a slot released at the dispatch barrier is running, not failed.
    The last-execution projection (Settings and the capabilities digest) is written when
    the slot settles, never at the barrier with an error status."""
    from ouroboros import reviewer_slot_config

    monkeypatch.setattr(reviewer_slot_config, "_last_execution_path", lambda: tmp_path / "last.json")
    executor = _install_real_substrate(monkeypatch)
    ctx = harness.make_ctx()
    try:
        _call(ctx)
        assert {a["operation_state"] for a in _state(harness)["waves"][-1]["actors"]} == {"pending_dispatch"}
        last = reviewer_slot_config.reviewer_slot_last_executions()
        assert not [sid for sid, row in last.items() if row.get("status") == "error"], last
        assert _wait_until(lambda: executor.execute_calls == 3)
        executor.release.set()
        assert _wait_until(lambda: len(_mailbox_entries(harness.drive, "task-1")) == 1)
    finally:
        executor.release.set()
    assert _control(_call(ctx)) == {"outcome": "GREEN", "closed": True}  # the collection
    last = reviewer_slot_config.reviewer_slot_last_executions()
    assert {sid: row["status"] for sid, row in last.items()} == {"s1": "ok", "s2": "ok", "s3": "ok"}
