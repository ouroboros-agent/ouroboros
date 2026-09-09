"""Startup recovers saved child work before materialization or source cleanup."""

import json
import threading
from types import SimpleNamespace

import pytest

from ouroboros import headless, observability, server_maintenance as maintenance
from ouroboros.task_results import load_task_result, write_task_result


@pytest.fixture
def roots(tmp_path, monkeypatch):
    from ouroboros import config, post_task_checkpoint
    from supervisor import queue, state, workers, active_activity

    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(maintenance, "DATA_DIR", root)
    monkeypatch.setattr(config, "DATA_DIR", root)
    monkeypatch.setattr(queue, "DRIVE_ROOT", root)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", root / "state/queue_snapshot.json")
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "PENDING", queue.PENDING)
    monkeypatch.setattr(workers, "RUNNING", queue.RUNNING)
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "DRIVE_ROOT", root)
    monkeypatch.setattr(state, "DRIVE_ROOT", root)
    monkeypatch.setattr(state, "STATE_PATH", root / "state/state.json")
    monkeypatch.setattr(post_task_checkpoint, "POST_TASK_SYNTHESIS_INFLIGHT", {})
    registry = active_activity.DirectActivityRegistry()
    monkeypatch.setattr(active_activity, "get_direct_activity_registry", lambda: registry)
    monkeypatch.setenv("OUROBOROS_GC_RETENTION_DAYS", "0")
    return root, tmp_path / "repo"


def _terminal(root, task_id="saved", *, family="headless", phase="completed"):
    if family == "headless":
        child = headless.prepare_task_drive(root, task_id, "empty")
    else:
        child = root / "task_drives" / task_id
        child.mkdir(parents=True)
    ref = observability.persist_call(child, task_id=task_id, call_id="response", call_type="llm_response",
                                     payload={"answer": "full retained answer"})["manifest_ref"]
    write_task_result(child, task_id, "completed", result="full retained answer", artifact_status="ready",
                      trace_refs={"response": ref}, memory_mode="empty", drive_root=str(child),
                      root_phase_checkpoint={"post_task_synthesis": phase})
    write_task_result(root, task_id, "completed", child_drive_root=str(child),
                      root_phase_checkpoint={"post_task_synthesis": phase})
    return child


def _recovery(root, repo):
    return maintenance._run_startup_task_recovery(root, repo, skip_live_data=False, prior_worker_pids=set())


@pytest.mark.parametrize("family", ["headless", "task_drives"])
def test_saved_body_and_sources_precede_orphan_reader_and_actual_prune(roots, monkeypatch, family):
    root, repo = roots
    child = _terminal(root, family=family)
    seen = []
    def orphan_reader(_root, *, exclude_task_ids):
        row = load_task_result(root, "saved", strict=True)
        assert row["result"] == "full retained answer"
        manifest = observability.read_call_manifest_ref(root, row["trace_refs"]["response"], task_id="saved")
        assert observability.read_blob_ref(root, manifest["full_payload_ref"])["answer"] == row["result"]
        seen.append("orphan")
    monkeypatch.setattr("ouroboros.task_status.reconcile_orphaned_running_tasks", orphan_reader)
    monkeypatch.setattr("ouroboros.agent_task_pipeline.recover_pending_root_post_task_synthesis",
                        lambda *a, **k: seen.append("synthesis"))
    report = _recovery(root, repo)
    assert report["recovered"] == ["saved"] and report["unresolved"] == []
    assert seen == ["orphan", "synthesis"]
    with monkeypatch.context() as saved:
        saved.setattr(headless, "prepare_terminal_task_files", lambda *a: pytest.fail("already saved task recopied"))
        assert _recovery(root, repo)["recovered"] == []
    row = load_task_result(root, "saved")
    monkeypatch.setattr("ouroboros.retention.age_cutoff", lambda *a, **k: 4_000_000_000)
    maintenance._startup_prune_sweeps()
    assert not child.exists()
    manifest = observability.read_call_manifest_ref(root, row["trace_refs"]["response"], task_id="saved")
    assert observability.read_blob_ref(root, manifest["full_payload_ref"])["answer"] == "full retained answer"


def test_live_open_synthesis_and_pending_owner_wait_survive_while_dead_child_heals(roots, monkeypatch):
    from ouroboros import post_task_checkpoint
    from supervisor import queue

    root, repo = roots
    child = _terminal(root, "dead", phase="running")
    waiting_child = _terminal(root, "waiting", phase="running")
    queue.PENDING.append({"id": "waiting", "_owner_wait_resume": {"wait_id": "owner-question"}})
    live = write_task_result(root, "live", "completed", result="answer already delivered",
                             root_phase_checkpoint={"post_task_synthesis": "running"})
    post_task_checkpoint.POST_TASK_SYNTHESIS_INFLIGHT[(str(root.resolve()), "live")] = SimpleNamespace(closed=False)
    waiting_bytes = (waiting_child / "task_results/waiting.json").read_bytes()
    reader = []
    monkeypatch.setattr("ouroboros.task_status.load_effective_task_result",
                        lambda root, tid: reader.append(tid) or pytest.fail("terminal rows need no orphan materialization"))
    report = _recovery(root, repo)
    assert report["protected"] == ["live", "waiting"]
    assert report["recovered"] == ["dead"]
    assert load_task_result(root, "live") == live
    assert (waiting_child / "task_results/waiting.json").read_bytes() == waiting_bytes
    assert load_task_result(root, "waiting")["root_phase_checkpoint"]["post_task_synthesis"] == "running"
    assert load_task_result(root, "dead")["root_phase_checkpoint"]["post_task_synthesis"] == "degraded"
    assert load_task_result(root, "dead")["result"] == "full retained answer"
    assert child.exists() and reader == []


def test_orphan_exclusion_filters_before_effective_materialization(roots, monkeypatch):
    from ouroboros.task_status import reconcile_orphaned_running_tasks
    root, _ = roots
    for tid in ["live", "dead"]:
        write_task_result(root, tid, "running", result="original")
    read = []
    def effective(root, tid):
        read.append(tid)
        return {"task_id": tid, "status": "failed", "result": "proven orphan"}
    monkeypatch.setattr("ouroboros.task_status.load_effective_task_result", effective)
    assert reconcile_orphaned_running_tasks(root, exclude_task_ids={"live"}) == 1
    assert read == ["dead"]
    assert load_task_result(root, "live")["status"] == "running"
    assert load_task_result(root, "dead")["status"] == "failed"


def test_startup_recovery_skips_symlinked_external_task_drive(roots):
    root, repo = roots
    external = root.parent / "external-task-drive"
    external.mkdir()
    write_task_result(external, "evil", "completed", result="outside data")
    drives = root / "task_drives"
    drives.mkdir()
    (drives / "evil").symlink_to(external, target_is_directory=True)
    report = _recovery(root, repo)
    assert report["recovered"] == []
    assert report["unresolved"] == []
    assert not (root / "task_results" / "evil.json").exists()


@pytest.mark.parametrize("family", ["headless", "task_drives"])
@pytest.mark.parametrize("status", ["failed", "cancelled", "rejected_duplicate"])
@pytest.mark.parametrize("child_status", [None, "running"])
def test_host_terminal_without_child_terminal_does_not_disable_retention(
    roots, monkeypatch, family, status, child_status,
):
    root, repo = roots
    task_id = "host-stopped"
    child = (headless.prepare_task_drive(root, task_id, "empty") if family == "headless"
             else root / "task_drives" / task_id)
    child.mkdir(parents=True, exist_ok=True)
    if child_status:
        write_task_result(child, task_id, child_status, result="unfinished work")
    stored = write_task_result(root, task_id, status, result="Host ended this execution",
                               child_drive_root=str(child))
    # Repeated boots do not turn confirmed absence of a child terminal into
    # a permanent save obligation that blocks unrelated startup retention.
    for _ in range(2):
        report = _recovery(root, repo)
        assert report["unresolved"] == report["errors"] == report["protected"] == []
        assert load_task_result(root, task_id, strict=True) == stored
    monkeypatch.setattr("ouroboros.retention.age_cutoff", lambda *a, **k: 4_000_000_000)
    maintenance._startup_prune_sweeps(preserve_task_sources=bool(
        report["unresolved"] or report["protected"] or report["errors"]))
    assert not child.exists()
    assert load_task_result(root, task_id, strict=True) == stored


def test_no_provider_unrestored_wait_is_preserved_but_other_saved_work_recovers(roots, monkeypatch):
    root, repo = roots
    _terminal(root, "dead")
    waiting = _terminal(root, "waiting", phase="running")
    (root / "state/queue_snapshot.json").write_text(json.dumps({"pending": [{"task": {
        "id": "waiting", "_owner_wait_resume": {"wait_id": "saved-question"}}}]}))
    before = load_task_result(root, "waiting")
    report = _recovery(root, repo)
    assert report["protected"] == ["waiting"] and report["recovered"] == ["dead"]
    assert load_task_result(root, "waiting") == before and waiting.exists()


def test_live_data_test_guard_precedes_every_recovery_read(roots, monkeypatch):
    root, repo = roots
    monkeypatch.setattr(maintenance, "_migrate_startup_cancel_latches", lambda *a: pytest.fail("live read"))
    assert maintenance._run_startup_task_recovery(root, repo, skip_live_data=True)["recovered"] == []


def test_failed_first_save_preserves_child_and_followup_attachment_through_prune(roots, monkeypatch):
    from ouroboros.artifacts import stage_task_attachments, task_artifact_dir_path
    from ouroboros.owner_mailbox import write_owner_message, _mailbox_path

    root, repo = roots
    child = _terminal(root)
    upload = root / "uploads" / ("a" * 32 + "_answer.txt")
    upload.parent.mkdir()
    upload.write_text("accepted follow-up file")
    manifest = stage_task_attachments(child, "saved", [str(upload)] * 26)
    assert len(manifest) == 26 and all(row["status"] == "staged" for row in manifest)
    assert write_owner_message(child, "use all attached material", "saved", attachment_manifest=manifest)
    early = load_task_result(root, "saved")
    called = []
    monkeypatch.setattr("ouroboros.task_status.reconcile_orphaned_running_tasks",
                        lambda *a, **k: called.append(k["exclude_task_ids"]))
    monkeypatch.setattr("ouroboros.agent_task_pipeline.recover_pending_root_post_task_synthesis",
                        lambda *a, **k: called.append(k["exclude_task_ids"]))
    with monkeypatch.context() as failure:
        failure.setattr(headless, "write_task_result", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        report = _recovery(root, repo)
    assert report["unresolved"] == ["saved"]
    assert called == [{"saved"}, {"saved"}]
    assert load_task_result(root, "saved") == early
    maintenance._startup_prune_sweeps(preserve_task_sources=True)
    assert child.exists() and _mailbox_path(child, "saved").exists()
    recovered = _recovery(root, repo)
    assert recovered["recovered"] == ["saved"]
    assert load_task_result(root, "saved")["child_ref_promotion"]["pending_refs"] == []
    for row in manifest:
        assert (task_artifact_dir_path(root, "saved") / row["relpath"]).read_text() == "accepted follow-up file"


@pytest.mark.parametrize("pids", [None, {777}])
def test_unknown_or_live_prior_worker_defers_without_sleep_or_capture(roots, monkeypatch, pids):
    root, repo = roots
    child = _terminal(root)
    monkeypatch.setattr("ouroboros.platform_layer.pid_is_alive", lambda pid: True)
    monkeypatch.setattr(headless, "prepare_terminal_task_files", lambda *a: pytest.fail("live owner capture"))
    monkeypatch.setattr("ouroboros.task_status.reconcile_orphaned_running_tasks", lambda *a, **k: pytest.fail("live materialization"))
    report = maintenance._run_startup_task_recovery(root, repo, skip_live_data=False, prior_worker_pids=pids)
    assert report["errors"] == ["prior_worker_ownership_unconfirmed"] and child.exists()


def test_worker_pid_evidence_is_captured_before_new_pool_overwrites_it(roots, monkeypatch):
    root, _ = roots
    path = root / "state/worker_pids.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"server_pid": 444, "workers": [{"pid": 555}]}))
    monkeypatch.setattr("ouroboros.process_custody._read_ledger_strict",
                        lambda root: (True, [{"pid": 666, "purpose": "worker:1"}, {"pid": 999, "purpose": "claudexor"}]))
    captured = maintenance._startup_worker_pids(root)
    path.write_text(json.dumps({"workers": [{"pid": 777}]}))
    assert captured == {444, 555, 666}
    path.write_text("{")
    assert maintenance._startup_worker_pids(root) is None


def test_missing_or_nonterminal_child_does_not_resume_model_work(roots, monkeypatch):
    root, repo = roots
    child = _terminal(root)
    (child / "task_results/saved.json").unlink()
    monkeypatch.setattr(headless, "prepare_terminal_task_files", lambda *a: pytest.fail("no terminal source"))
    excluded = []
    monkeypatch.setattr("ouroboros.task_status.reconcile_orphaned_running_tasks", lambda *a, **k: excluded.append(k["exclude_task_ids"]))
    monkeypatch.setattr("ouroboros.agent_task_pipeline.recover_pending_root_post_task_synthesis", lambda *a, **k: excluded.append(k["exclude_task_ids"]))
    assert _recovery(root, repo)["unresolved"] == ["saved"]
    write_task_result(child, "saved", "running", result="unfinished")
    assert _recovery(root, repo)["unresolved"] == ["saved"]
    assert excluded == [{"saved"}] * 4


@pytest.mark.parametrize("failure", [False, True])
def test_periodic_bulk_work_does_not_block_drain_or_duplicate_sweep(roots, monkeypatch, failure):
    root, _ = roots
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    clock = [100.0]
    calls = []
    monkeypatch.setattr(maintenance, "_CANCEL_INTENT_SWEEP_LOCK", lock)
    monkeypatch.setattr(maintenance, "_LAST_CANCEL_INTENT_SWEEP", [0.0])
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr("supervisor.task_lifecycle.sweep_cancel_intents", lambda: calls.append("cancel"))
    monkeypatch.setattr("supervisor.terminal_delivery.replay_pending_deliveries", lambda root: calls.append("delivery"))
    def bulk(root):
        calls.append("refs")
        entered.set()
        assert release.wait(3)
        done.set()
        if failure:
            raise OSError("copy failed")
    monkeypatch.setattr(observability, "retry_pending_child_ref_promotions", bulk)
    maintenance._periodic_supervisor_maintenance([100.0], [100.0])
    assert entered.wait(2)
    try:
        clock[0] = 125
        maintenance._periodic_supervisor_maintenance([125.0], [125.0])
        assert calls == ["cancel", "delivery", "refs"]  # drain returned while I/O is still held
    finally:
        release.set()
    assert done.wait(2) and lock.acquire(timeout=2)
    lock.release()
    maintenance._periodic_supervisor_maintenance([125.0], [125.0])
    assert lock.acquire(timeout=2)
    lock.release()
    assert calls == ["cancel", "delivery", "refs"] * 2


def test_thread_start_failure_releases_maintenance_latch(roots, monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(maintenance, "_CANCEL_INTENT_SWEEP_LOCK", lock)
    monkeypatch.setattr(maintenance, "_LAST_CANCEL_INTENT_SWEEP", [0.0])
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(time=lambda: 100.0))
    monkeypatch.setattr(maintenance, "threading", SimpleNamespace(Thread=lambda **k: (_ for _ in ()).throw(RuntimeError("thread unavailable"))))
    maintenance._periodic_supervisor_maintenance([100.0], [100.0])
    assert lock.acquire(blocking=False)
    lock.release()


def test_real_supervisor_orders_custody_recovery_before_prune(roots, monkeypatch, tmp_path):
    import server
    from tests.test_server_shutdown import _supervisor_harness
    from supervisor import queue, workers

    _supervisor_harness(monkeypatch, tmp_path, ["stop"])
    order = []
    monkeypatch.setattr(server, "_migrate_startup_cancel_latches", lambda root: order.append("migrate"))
    monkeypatch.setattr(server, "_startup_worker_pids", lambda root: order.append("capture-pids") or {777})
    monkeypatch.setattr(queue, "restore_pending_from_snapshot", lambda: order.append("restore") or 0)
    monkeypatch.setattr(workers, "kill_workers", lambda **k: order.append("kill"))
    monkeypatch.setattr(workers, "spawn_workers", lambda n: order.append("spawn"))
    monkeypatch.setattr(server, "_startup_custody_sweep", lambda: order.append("custody"))
    def recover(root, repo, **kw):
        assert kw["prior_worker_pids"] == {777}
        order.append("recover")
        return {"unresolved": ["saved"], "protected": [], "errors": []}
    monkeypatch.setattr(server, "_run_startup_task_recovery", recover)
    monkeypatch.setattr(server, "_startup_prune_sweeps", lambda **kw: order.append(("prune", kw["preserve_task_sources"])))
    server._run_supervisor({})
    assert server._supervisor_error is None
    assert order == ["migrate", "capture-pids", "restore", "kill", "spawn", "custody", "recover", ("prune", True)]


def test_supervisor_init_failure_keeps_boot_recovery_owner():
    import ast
    import inspect
    import server
    tree = ast.parse(inspect.getsource(server._run_supervisor))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)
             and node.func.id == "_run_startup_task_recovery"]
    assert len(calls) == 2
    assert any(any(isinstance(parent, ast.ExceptHandler) for parent in ast.walk(tree))
               for _ in calls)


def test_lifespan_does_not_race_recovery_against_provider_supervisor():
    import ast
    import inspect
    import textwrap
    import server

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.lifespan)))
    branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "not has_startup_ready_provider(settings)"]
    assert len(branches) == 1
    recovery = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "_run_startup_task_recovery"]
    assert len(recovery) == 1 and recovery[0] in list(ast.walk(branches[0]))
    assert "skip_live_data=pytest_default_real_data_dir" in ast.unparse(recovery[0])
