"""Reconciliation of delegated runs by the supervisor generation's sweeps.

Started as the D11 slice of the reference theme split of
``tests/test_delegated_subagent_transport.py`` (v7 WIP 9f691656,
``tests/test_delegated_reconciliation.py``): the two tests here bind the
``ouroboros.server_maintenance`` owner the server composition split created, and
re-homing them is the byte-debt pressure valve — the giant shrinks, the pin
gains its family. The F2.1 delegation split then brought the rest of the
reference theme here (orphan-sweep predicate, absent-run closure, release
points) — this file now owns the full reconciliation theme.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from ouroboros.config import CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION
from ouroboros.gateways import claudexor as cx

from tests._delegated_transport_shared import (  # noqa: F401  (autouse fixture applies on import)
    _LiveRunStub,
    _event_types,
    _health_invariants,
    _owned_gateway_uses_each_test_transport,
    _waiting,
    _write_attempt,
)


@pytest.fixture
def startup_owners(tmp_path, monkeypatch):
    from ouroboros import post_task_checkpoint, server_maintenance
    from supervisor import active_activity, queue, workers

    monkeypatch.setattr(server_maintenance, "DATA_DIR", tmp_path)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(post_task_checkpoint, "POST_TASK_SYNTHESIS_INFLIGHT", {})
    registry = active_activity.DirectActivityRegistry()
    monkeypatch.setattr(active_activity, "_DIRECT_ACTIVITY_REGISTRY", registry)
    return queue, workers, post_task_checkpoint, registry


def test_the_startup_sweep_reconciles_delegated_runs_too(monkeypatch, startup_owners):
    """A fresh generation has no surviving queue, direct or post-task owners.

    Startup must reconcile with that actual empty set, without inheriting another
    test's queue. An in-process revival with live owners is covered separately.
    """
    import ouroboros.server_maintenance as sm
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc

    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes", lambda root, **kw: [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.setdefault("live", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(sm, "_installed_skill_names", lambda: None)
    sm._startup_custody_sweep()
    assert seen["live"] == set(), "an empty live set is the point: nothing survived the restart"


def test_startup_revival_keeps_actual_current_owners(tmp_path, monkeypatch, startup_owners):
    from ouroboros import delegate_custody as dc, process_custody as pc, server_maintenance as sm

    queue, workers, post_task, registry = startup_owners
    queue.RUNNING["queue-live"] = {"task": {"id": "queue-live"}}
    workers.WORKERS[7] = SimpleNamespace(busy_task_id="worker-live")
    registry.register("native-live", 1)
    post_task.POST_TASK_SYNTHESIS_INFLIGHT[(str(tmp_path.resolve()), "post-live")] = None
    expected = {"queue-live", "worker-live", "native-live", "post-live"}
    for task_id in expected:
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=f"run-{task_id}", task_id=task_id, route_id="r", model="m",
            project_id="p", project_owned=False, root_task_id=task_id, ledger_root=str(tmp_path),
        ))
    seen = {}
    transport = _LiveRunStub()
    real_reconcile = dc.reconcile_orphaned_runs
    def reconcile(root, **kwargs):
        seen["delegated"] = kwargs["running_task_ids"]
        kwargs["gateway_factory"] = lambda: transport
        outcomes = real_reconcile(root, **kwargs)
        seen["outcomes"] = outcomes
        return outcomes
    monkeypatch.setattr(dc, "reconcile_orphaned_runs", reconcile)
    monkeypatch.setattr(pc, "reap_orphaned_processes",
                        lambda root, **kw: seen.__setitem__("processes", kw["running_task_ids"]) or [])
    monkeypatch.setattr(sm, "_installed_skill_names", lambda: None)
    sm._startup_custody_sweep()
    assert seen["processes"] == seen["delegated"] == expected
    assert seen["outcomes"] == []
    assert transport.cancels == []
    assert {row.run_id for row in dc.open_runs(tmp_path)} == {f"run-{task_id}" for task_id in expected}


def test_both_custody_surfaces_see_the_same_live_task_set(tmp_path, monkeypatch, startup_owners):
    """The periodic sweep must hand the delegated reconciler the SAME live task set the
    process reaper gets. Two copies of "is the owner still running" is exactly how one
    custody surface ends up reaping while its twin does not."""
    import time
    import threading

    import ouroboros.server_maintenance as sm
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc
    import supervisor.queue as queue
    import supervisor.task_lifecycle as lifecycle

    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    lock = threading.Lock()
    monkeypatch.setattr(sm, "_CANCEL_INTENT_SWEEP_LOCK", lock)
    monkeypatch.setattr(sm, "_LAST_CANCEL_INTENT_SWEEP", [0.0])
    entered, release = threading.Event(), threading.Event()
    threads = []
    def tracked_thread(**kwargs):
        thread = threading.Thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(sm, "threading", SimpleNamespace(Thread=tracked_thread))
    real_sweep = lifecycle.sweep_cancel_intents
    def delayed_sweep():
        entered.set()
        assert release.wait(5), "test must release its maintenance work"
        return real_sweep()
    monkeypatch.setattr(lifecycle, "sweep_cancel_intents", delayed_sweep)
    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes",
                        lambda root, **kw: seen.__setitem__("processes", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.__setitem__("delegated", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(sm, "_installed_skill_names", lambda: None)
    # Replace RUNNING wholesale, never append: queue globals are rebound by
    # init_queue_refs across the suite without restore (the upstream test
    # convention), so assuming the dict is empty here is cross-test fragile.
    monkeypatch.setattr(queue, "RUNNING", {"t-live": {}})
    from supervisor.active_activity import get_direct_activity_registry

    get_direct_activity_registry().register("native-live", 1)
    try:
        sm._periodic_supervisor_maintenance([0.0], [time.time()])
        assert seen["processes"] == seen["delegated"] == {"t-live", "native-live"}, seen
        assert entered.wait(2) and len(threads) == 1
        assert threads[0].name == "terminal-maintenance" and threads[0].is_alive()
    finally:
        # The actual maintenance owner finishes before monkeypatch restores its
        # root, lock and dependent functions, including when an assertion fails.
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
    assert not lock.locked(), "the real maintenance finally released its latch"


def test_an_orphaned_delegated_run_is_reconciled_when_its_owner_is_gone(tmp_path, monkeypatch):
    """The predicate is the one `process_custody.reap_orphaned_processes` already owns:
    the owning task is no longer in the supervisor's live set. A delegated run has no
    pid, so the process reaper cannot see it — but it is still spending quota and still
    writing to a workspace.

    The floor is INVERTED (owner B1-A): a live orphan is cancelled only behind a
    DELIBERATE owner terminal. An owner that died of the provider, one whose result
    is unreadable, and one whose result was never written all leave the run live
    (``left_live``, no cancel POST); the next sweep settles a spared run once the
    daemon reports it terminal."""
    import ouroboros.delegate_custody as dc
    from ouroboros.outcomes import infra_failed_axes
    from ouroboros.task_results import write_task_result

    live = _LiveRunStub(run_id="run-orphan")
    finished = _LiveRunStub(run_id="run-done")
    finished.get_run = lambda rid: {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.0}}

    for run_id, task in (("run-orphan", "t-gone"), ("run-done", "t-also-gone"),
                         ("run-spared", "t-provider-died"), ("run-garbled", "t-garbled"),
                         ("run-silent", "t-never-wrote")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=run_id, task_id=task, route_id="r", model="m",
            project_id="p", project_owned=False, root_task_id=task, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-alive", task_id="t-running", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-running", ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    # The owner verdicts: a deliberate completion, a provider death, an unreadable row.
    write_task_result(tmp_path, "t-gone", "completed", result="verdict")
    write_task_result(tmp_path, "t-provider-died", "failed", reason_code="provider_unavailable",
                      outcome_axes=infra_failed_axes("provider_unavailable"))
    (tmp_path / "task_results").mkdir(exist_ok=True)
    (tmp_path / "task_results" / "t-garbled.json").write_text("{not json", encoding="utf-8")
    terminal_ids = {"run-done"}

    class _Router(_LiveRunStub):
        def get_run(self, rid, **_kw):
            return (finished if rid in terminal_ids else live).get_run(rid)
        def cancel_run(self, rid, reason=""):
            return live.cancel_run(rid, reason)

    outcomes = dc.reconcile_orphaned_runs(tmp_path, {"t-running"}, gateway_factory=_Router)
    dc._CUSTODY.clear()
    by_run = {row["run_id"]: row for row in outcomes}
    assert set(by_run) == {"run-orphan", "run-done", "run-spared", "run-garbled", "run-silent"}, \
        "a live owner's run must be left alone"
    assert by_run["run-orphan"]["action"] == "cancelled"
    assert live.cancels == [("run-orphan", "owner_task_gone")], "only the deliberate terminal cancels"
    assert by_run["run-done"]["action"] == "settle_attempted" and by_run["run-done"]["settled"] is True
    for spared in ("run-spared", "run-garbled", "run-silent"):
        assert by_run[spared]["action"] == "left_live" and by_run[spared]["state"] == "running", spared
    # run-orphan stays open too: the stub answers "running" to the verify read, so its
    # cancel is merely REQUESTED (the pre-existing receipt vocabulary), not settled.
    assert {row.run_id for row in dc.open_runs(tmp_path)} == {
        "run-alive", "run-orphan", "run-spared", "run-garbled", "run-silent"}

    # The spared run settles itself on the sweep that finds it terminal.
    terminal_ids.add("run-spared")
    again = {row["run_id"]: row for row in dc.reconcile_orphaned_runs(tmp_path, {"t-running"}, gateway_factory=_Router)}
    dc._CUSTODY.clear()
    assert again["run-spared"]["action"] == "settle_attempted" and again["run-spared"]["settled"] is True
    assert again["run-silent"]["action"] == "left_live"
    # The requested-not-confirmed cancel is re-asked each sweep (unchanged); no spared
    # run was ever asked.
    assert live.cancels == [("run-orphan", "owner_task_gone")] * 2

    # Unknown liveness reconciles nothing: never mass-cancel on missing information.
    live.cancels.clear()
    assert dc.reconcile_orphaned_runs(tmp_path, None, gateway_factory=_Router) == []
    assert live.cancels == []
    dc._CUSTODY.clear()


def test_what_the_daemon_says_is_absent_is_closed_not_faulted_forever(tmp_path):
    """One root cause at two surfaces: a 404 is the daemon ANSWERING that the thing is not
    there, and both were read as "we could not find out".

    A run the daemon does not have was treated exactly like an unreachable daemon, so it
    was never settled, stayed in `open_runs`, and was re-faulted on EVERY pass — a
    permanent CRITICAL health invariant that no cancel or settlement could ever clear. Its
    sibling: a registration the daemon does not have kept `project_owned` true, so a
    terminal run could never finish settling and was reconciled forever."""
    import ouroboros.delegate_custody as dc

    class _NoSuchRun(_LiveRunStub):
        def get_run(self, rid, **_kw):
            raise cx.ClaudexorUnavailable("run_not_found", "no such run", status_code=404)

    class _NoSuchProject(_LiveRunStub):
        def get_run(self, rid, **_kw):
            return {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def remove_project(self, pid):
            raise cx.ClaudexorUnavailable("project_not_found", "no such project", status_code=404)

    dc._CUSTODY.clear()
    for run_id, task in (("run-gone", "t-gone"), ("run-owns-a-dead-project", "t-also-gone")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=run_id, task_id=task, route_id="r", model="m", project_id="prj-ours",
            project_owned=run_id.endswith("project"), root_task_id=task, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    class _Router(_LiveRunStub):
        def get_run(self, rid, **_kw):
            return (_NoSuchProject() if rid.endswith("project") else _NoSuchRun()).get_run(rid)
        def remove_project(self, pid): _NoSuchProject().remove_project(pid)

    passes = []
    for _ in range(3):
        passes.append(dc.reconcile_orphaned_runs(tmp_path, {"t-live"}, gateway_factory=_Router))
        dc._CUSTODY.clear()
    assert [row["action"] for row in passes[0]] == ["absent", "settle_attempted"], passes[0]
    assert passes[1] == [] and passes[2] == [], "a closed run must not be reconciled again"
    assert dc.open_runs(tmp_path) == [], "neither run may stay open"
    assert dc.open_containment_faults(tmp_path) == []
    assert "DELEGATED RUN MAY STILL BE LIVE" not in _health_invariants(tmp_path)
    types = _event_types(tmp_path)
    assert "delegate_run_containment_fault" not in types, "absence is not a containment fault"
    assert "delegate_run_project_retire_failed" not in types, "absence IS discharge"
    # An absent run is CLOSED, not settled: no ledger row is invented for a run the daemon
    # cannot even describe.
    assert "delegate_run_closed_absent" in types
    rows = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    ledgered = [r for r in rows if r.get("type") == "delegate_run_ledger_recorded"]
    assert [r["run_id"] for r in ledgered] == ["run-owns-a-dead-project"], ledgered

    # A daemon that is merely UNREACHABLE still faults: absence and ignorance stay apart.
    class _Deaf(_LiveRunStub):
        def get_run(self, rid, **_kw):
            raise cx.ClaudexorUnavailable("daemon_unreachable", "connection refused")

    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-unknown", task_id="t-gone", route_id="r", model="m", project_id="p",
        project_owned=False, root_task_id="t-gone", ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    assert [row["action"] for row
            in dc.reconcile_orphaned_runs(tmp_path, {"t-live"}, gateway_factory=_Deaf)] == ["unreadable"]
    dc._CUSTODY.clear()
    assert [f["run_id"] for f in dc.open_containment_faults(tmp_path)] == ["run-unknown"]


def test_a_terminalizing_parent_releases_the_run_it_still_holds(tmp_path):
    """The in-process twin of reconciliation. A parent that finishes while its delegated
    run is still going used to leave it mutating until the next 10-minute sweep; the
    loop's own resource-release point now settles or cancels it like any held resource.
    A task that delegated nothing must pay nothing for this.

    Inverted floor (B1-A): the release cancels only behind the parent's DELIBERATE
    durable terminal. Without one (the ordinary loop-exit shape: the result is written
    after this point) the run is left live and disclosed as open for the next sweep."""
    import ouroboros.delegate_custody as dc
    from ouroboros.task_results import load_task_result, write_task_result

    live = _LiveRunStub(run_id="run-held")
    dc._CUSTODY.clear()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-held", task_id="t-parent", route_id="r",
        model="m", project_id="p", project_owned=False, ledger_root=str(tmp_path),
    ))
    assert dc.release_task_runs(tmp_path, "t-someone-else", gateway_factory=lambda: live) == []
    assert live.cancels == [], "another task's run is not this task's to release"

    unwritten = dc.release_task_runs(tmp_path, "t-parent", gateway_factory=lambda: live)
    dc._CUSTODY.clear()
    assert [row["action"] for row in unwritten] == ["left_live"]
    assert live.cancels == [], "no verdict yet: the run outlives the loop exit"
    assert load_task_result(tmp_path, "t-parent")["delegated_runs_unreconciled"] == ["run-held"]

    write_task_result(tmp_path, "t-parent", "completed", result="done on purpose")
    outcomes = dc.release_task_runs(tmp_path, "t-parent", gateway_factory=lambda: live)
    dc._CUSTODY.clear()
    assert [row["action"] for row in outcomes] == ["cancelled"]
    assert live.cancels == [("run-held", "owner_task_gone")]


def test_the_loops_own_release_point_reaches_the_delegated_reconciler(tmp_path, monkeypatch):
    """`release_task_runs` only helps if something CALLS it. The test beside this one drives
    the function directly, so it passed with the loop's wiring deleted — and the loop is
    the ordinary path: without it a terminalized parent leaves its run mutating until the
    next ten-minute sweep. The release must also read the CANONICAL root, not the child
    drive the subagent runs on, or it looks for custody where none was written."""
    from types import SimpleNamespace

    import ouroboros.delegate_custody as dc
    import ouroboros.loop as loop

    released = []
    monkeypatch.setattr(dc, "release_task_runs",
                        lambda root, task_id, **kw: released.append((str(root), task_id)) or [])
    canonical = tmp_path / "canonical"
    inner = SimpleNamespace(drive_root=tmp_path / "child",
                            task_metadata={"budget_drive_root": str(canonical)})
    loop._cleanup_loop_resources(None, loop._LoopExitContext(
        tools=SimpleNamespace(_ctx=inner), drive_root=tmp_path, task_id="t-parent",
        event_queue=None, drive_logs=tmp_path / "logs", accumulated_usage={}, llm_trace={}))
    assert released == [(str(canonical), "t-parent")], released


def test_a_breach_whose_cancel_was_never_verified_is_not_reported_as_cancelled(
    tmp_path, monkeypatch
):
    """A containment BREACH stops the run through the one verified cancel path, and the
    sentence the agent reads comes from that cancel's typed outcome.

    The ad-hoc cancel this replaced swallowed every exception into a log line and then
    said "The run was cancelled. Do not retry it" unconditionally — so a daemon that
    REFUSED the cancel, or that could not be reached to confirm it, left an overpowered
    run mutating a workspace while the agent was told it had stopped. That is exactly
    what `record_containment_fault`'s own contract forbids: an incident must surface as
    a critical health invariant, "never as a reassuring string in a tool result".
    """
    from ouroboros.gateways import claudexor as gw
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    import ouroboros.delegate_custody as dc

    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)

    class _RefusingStub:
        engine_version = CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION

        def handshake(self, **_kw): return {}

        def get_run(self, rid, **_kw):
            # Still RUNNING: the cancel changed nothing the daemon will confirm.
            return {"lastSeq": 7, "summary": {
                "state": "running", "effectiveAccess": "workspace_write",
                "runDir": str(run_dir),
            }}

        def cancel_run(self, rid, reason=""):
            raise ClaudexorUnavailable("control_refused", "daemon refused the cancel")

        def remove_project(self, pid): pass
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _RefusingStub())
    _write_attempt(run_dir, isolated=False, home_dir=str(home))

    out = _waiting(tmp_path, monkeypatch)

    assert out["status"] == "refused" and out["reason"] == "home_isolation_not_applied", out
    # The typed outcome rides out with the refusal instead of a comforting sentence.
    assert out["cancel_outcome"] == dc.CANCEL_CONTAINMENT_FAULT, out
    assert "CONTAINMENT FAULT" in out["detail"], out["detail"]
    assert "MAY STILL BE LIVE" in out["detail"], out["detail"]
    assert "The run was cancelled." not in out["detail"], out["detail"]


def test_reconciliation_default_transport_is_the_ensured_owned_daemon(tmp_path, monkeypatch):
    """Regression (v6.89.0): the startup sweep reaps the previous generation's owned
    daemon and THEN reconciled through a bare discovery-only gateway — which always
    found the corpse it had just made, so every restart's reconciliation silently
    no-opped and open runs stayed unsettled until the next delegate_start. With real
    work to reconcile, the default transport must be the ENSURE path (which also
    adopts a staged runtime update the old always-running daemon never could)."""
    from ouroboros import delegate_custody as dc

    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-orphan", task_id="t-gone", route_id="r", model="m",
        ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    ensured = []

    class _EnsuredGateway:
        def handshake(self): return {}
        def get_run(self, run_id, timeout_sec=None):
            return {"state": "cancelled", "summary": {"state": "cancelled"}}
        def cancel_run(self, run_id): return {}
        def close(self): pass

    def _fake_ensure():
        ensured.append(True)
        return _EnsuredGateway()

    monkeypatch.setattr(
        "ouroboros.claudexor_daemon.ensure_owned_gateway", _fake_ensure)
    dc.reconcile_orphaned_runs(tmp_path, set())
    assert ensured, "the default gateway factory must go through ensure_owned_gateway"

    # And with NOTHING to reconcile the daemon is never started at all: the empty
    # early-return keeps the ordinary idle restart free of a daemon spawn.
    ensured.clear()
    empty = tmp_path / "empty-drive"
    empty.mkdir()
    assert dc.reconcile_orphaned_runs(empty, set()) == []
    assert not ensured


def test_the_kill_boundary_states_its_own_verdict_before_it_writes_it(tmp_path):
    """An owner cancellation still cancels the paid run, at the kill boundary.

    The A4 ordering audits custody BEFORE the terminal write, so the durable
    result the inverted floor reads does not exist yet and the run would have
    survived until the next periodic sweep (up to ten minutes of paid work).
    The killing caller already knows the verdict, so it states it. A host bound
    that has no verdict passes nothing and still spares the run (owner B1-A).
    """
    import ouroboros.delegate_custody as dc

    def _started(run_id: str, task_id: str) -> None:
        dc._CUSTODY.clear()
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=run_id, task_id=task_id, route_id="r", model="m",
            project_id="p", project_owned=False, root_task_id=task_id,
            ledger_root=str(tmp_path)))
        dc._CUSTODY.clear()

    _started("run-owner-cancelled", "t-owner-cancel")
    deliberate = _LiveRunStub(run_id="run-owner-cancelled")
    outcomes = dc.reconcile_task_runs(
        tmp_path, "t-owner-cancel", gateway_factory=lambda: deliberate,
        deliberate_terminal="cancelled",
    )
    assert [row["action"] for row in outcomes] == ["cancelled"]
    assert deliberate.cancels == [("run-owner-cancelled", "owner_task_gone")]

    _started("run-no-verdict", "t-no-verdict")
    spared = _LiveRunStub(run_id="run-no-verdict")
    outcomes = dc.reconcile_task_runs(
        tmp_path, "t-no-verdict", gateway_factory=lambda: spared,
    )
    assert [row["action"] for row in outcomes] == ["left_live"]
    assert spared.cancels == []
    dc._CUSTODY.clear()


def test_the_supervisor_kill_path_carries_the_cancellation_verdict(tmp_path, monkeypatch):
    """End to end over the supervisor seam the finder probed: the cancel kill
    path passes its own terminal into the audit, a reap passes nothing."""
    import types

    import ouroboros.delegate_terminal as delegate_terminal
    from supervisor.cancel_publication import _audit_delegated_runs_on_kill

    seen: list = []
    monkeypatch.setattr(
        delegate_terminal, "terminal_reconcile_task",
        lambda root, tid, **kw: seen.append(kw.get("deliberate_terminal", "")) or {
            "task_id": tid, "trigger": kw.get("trigger", ""), "outcomes": [],
            "unreconciled": [], "audit_status": "ok",
        },
    )
    q = types.SimpleNamespace(DRIVE_ROOT=str(tmp_path))

    _audit_delegated_runs_on_kill(q, "t1", deliberate_terminal="cancelled")
    _audit_delegated_runs_on_kill(q, "t1", trigger="reaper_deadline_exceeded")

    assert seen == ["cancelled", ""]
