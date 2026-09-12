"""v6.37.0 guard (C4.1): the in-task ensure_project_scope affordance — create/attach
a named Ouroboros project and scope the CURRENT running task into it, instead of the
cyber-racing fallback (bare `mkdir ~/Desktop`). Idempotent for the same project,
refuses to re-scope to a different one, rejects subagents."""

import logging
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _hermetic_bindings(tmp_path, monkeypatch):
    """R13: the guard reads the durable bindings at the canonical data dir, so no
    test in this module may reach the live root."""
    import ouroboros.config as cfg

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    return tmp_path


def _ctx(**kw):
    base = dict(project_id="", task_metadata={}, task_contract={}, task_id="t1", event_queue=None, pending_events=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_creates_named_project_and_scopes_current_task():
    from ouroboros.tools.control import _ensure_project_scope
    from ouroboros.project_facts import project_id_from_display_name

    ctx = _ctx()
    out = _ensure_project_scope(ctx, project_name="Cyber Racing")
    assert out.startswith("OK")
    expected_pid = project_id_from_display_name("Cyber Racing")
    # the rest of THIS task is scoped immediately (journal/knowledge work now)
    assert ctx.project_id == expected_pid
    # a durable ensure_project_scope event is emitted for the supervisor
    evs = [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]
    assert len(evs) == 1
    assert evs[0]["task_id"] == "t1"
    assert evs[0]["project_id"] == expected_pid
    assert evs[0]["project_name"] == "Cyber Racing"


def test_idempotent_same_project_and_refuses_different():
    from ouroboros.tools.control import _ensure_project_scope
    from ouroboros.project_facts import project_id_from_display_name

    pid = project_id_from_display_name("Cyber Racing")
    ctx = _ctx(project_id=pid)
    out = _ensure_project_scope(ctx, project_name="Cyber Racing")
    assert "already scoped" in out
    assert not [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]

    ctx2 = _ctx(project_id="other-project")
    out2 = _ensure_project_scope(ctx2, project_name="Cyber Racing")
    assert "cannot be re-scoped" in out2
    assert ctx2.project_id == "other-project"  # scope NOT changed


def test_bound_task_renames_its_project_instead_of_creating_a_second(tmp_path):
    """B4=A: the durable binding is the one truth. A task already bound to a
    project that asks to be scoped to a differently named one keeps its project
    and carries the requested name to it - the empty second project that split
    token-observatory off token-atlas is exactly what this refuses.

    The event keeps the REQUESTED id: the supervisor handler reads the same
    binding and owns the rename turn. Rewriting the id here made that branch
    unreachable, so no rename ever happened while this text claimed one had."""
    from ouroboros.projects_registry import bind_task_to_project, list_projects
    from ouroboros.tools.control import _ensure_project_scope

    bind_task_to_project(tmp_path, "t1", "token-atlas", 5150, origin={"absent": "system"})
    ctx = _ctx()
    out = _ensure_project_scope(ctx, project_name="Token Observatory")

    assert "token-atlas" in out and "no second project" in out
    assert "Token Observatory" in out and "rename" in out
    assert ctx.project_id == "token-atlas"
    evs = [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]
    assert len(evs) == 1
    assert evs[0]["project_id"] == "token-observatory"
    assert evs[0]["project_name"] == "Token Observatory"
    assert [p["id"] for p in list_projects(tmp_path)] == ["token-atlas"]


def test_bound_task_scope_text_claims_no_rename_when_the_name_is_unchanged(tmp_path):
    """The text must be TRUE: a request that names the project it is already called
    says nothing about a rename."""
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from ouroboros.tools.control import _ensure_project_scope

    # The bound project already carries the requested display name, while the id the
    # name derives to is a different one.
    create_project(tmp_path, "token-atlas", name="Token Observatory")
    bind_task_to_project(tmp_path, "t1", "token-atlas", 5150, origin={"absent": "system"})

    out = _ensure_project_scope(_ctx(), project_name="Token Observatory")

    assert "no second project" in out
    assert "rename" not in out


def test_bound_task_rename_reaches_the_registry_through_the_real_handler(tmp_path, monkeypatch):
    """The two halves joined: real tool -> real event -> real supervisor handler.
    Each half was green on its own while the rename never happened in production."""
    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from ouroboros.projects_registry import bind_task_to_project, create_project, get_project
    from ouroboros.tools.control import _ensure_project_scope
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    project = create_project(tmp_path, "token-atlas", name="Token Atlas")
    bind_task_to_project(tmp_path, "t1", "token-atlas", project["chat_id"], origin={"absent": "system"})
    broadcasts: list = []
    announced: list = []
    monkeypatch.setattr(mb, "get_bridge",
                        lambda: SimpleNamespace(broadcast=lambda payload: broadcasts.append(payload)))
    monkeypatch.setattr(workers, "_announce_created_project",
                        lambda *a, **kw: announced.append(True))

    ctx = _ctx()
    out = _ensure_project_scope(ctx, project_name="Token Observatory")
    event = [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"][0]
    running = {"t1": {"task": {"id": "t1", "project_id": "token-atlas"}}}
    workers.ensure_project_scope(event, SimpleNamespace(RUNNING=running, PENDING=[]))

    assert get_project(tmp_path, "token-atlas")["name"] == "Token Observatory"
    assert [p["id"] for p in reg.list_projects(tmp_path)] == ["token-atlas"]
    assert broadcasts == [] and announced == []
    assert running["t1"]["task"]["project_id"] == "token-atlas"
    assert "rename" in out  # the text was true


def test_binding_outranks_a_stale_ctx_scope_and_stays_idempotent(tmp_path):
    from ouroboros.projects_registry import bind_task_to_project
    from ouroboros.tools.control import _ensure_project_scope

    bind_task_to_project(tmp_path, "t1", "token-atlas", 5150, origin={"absent": "system"})
    ctx = _ctx(project_id="stale-scope")
    out = _ensure_project_scope(ctx, project_id="token-atlas")

    assert "already scoped" in out
    assert ctx.project_id == "token-atlas"
    assert not [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]


def test_unreadable_bindings_are_disclosed_and_do_not_block(tmp_path, caplog):
    """Proportionality: an unreadable store reads as "no binding" (the behaviour
    before this seam existed) and says so once; only a READABLE binding refuses."""
    from ouroboros.project_facts import project_id_from_display_name
    from ouroboros.tools.control import _ensure_project_scope

    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "project_task_bindings.json").write_text("{ not json", encoding="utf-8")

    ctx = _ctx()
    with caplog.at_level(logging.WARNING):
        out = _ensure_project_scope(ctx, project_name="Cyber Racing")

    assert out.startswith("OK")
    assert ctx.project_id == project_id_from_display_name("Cyber Racing")
    assert "project_binding_unreadable" in caplog.text


def test_rejects_subagent_and_requires_an_arg():
    from ouroboros.tools.control import _ensure_project_scope

    # delegation_role lives on task_metadata / contract lineage, not a ctx attr
    out = _ensure_project_scope(_ctx(task_metadata={"delegation_role": "subagent"}), project_name="X")
    assert "subagents" in out.lower()
    out_lineage = _ensure_project_scope(
        _ctx(task_contract={"lineage": {"delegation_role": "subagent"}}), project_name="X"
    )
    assert "subagents" in out_lineage.lower()

    out2 = _ensure_project_scope(_ctx())
    assert "TOOL_ARG_ERROR" in out2


def test_supervisor_handler_creates_binds_updates_running_and_broadcasts(monkeypatch):
    """C4.1 supervisor side (review F1/F2): the handler must create_project,
    bind the task, UPDATE the RUNNING map's task project_id (so the project lease
    counts it as a lane occupant), and broadcast projects_changed."""
    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from supervisor import workers

    calls = {"create": None, "bind": None, "touch": None, "broadcast": None}
    # Hermetic (R13): the handler now reads the durable binding first, and
    # workers.DRIVE_ROOT here is the LIVE data root.
    monkeypatch.setattr(reg, "project_id_for_task", lambda dr, tid, **kw: "")
    monkeypatch.setattr(reg, "create_project", lambda dr, pid, **kw: calls.__setitem__("create", (pid, kw)) or {"id": pid, "chat_id": 7})
    monkeypatch.setattr(
        reg,
        "bind_task_to_project",
        lambda dr, tid, pid, chat=None, *, origin: calls.__setitem__("bind", (tid, pid, chat, origin)),
    )
    monkeypatch.setattr(reg, "touch_project", lambda dr, pid: calls.__setitem__("touch", pid))

    class _Bridge:
        def broadcast(self, payload):
            calls["broadcast"] = payload

    monkeypatch.setattr(mb, "get_bridge", lambda: _Bridge())

    running = {"t1": {"task": {"id": "t1"}}}
    ctx = SimpleNamespace(RUNNING=running)
    workers.ensure_project_scope({"task_id": "t1", "project_id": "cyber-racing", "project_name": "Cyber Racing"}, ctx)

    assert calls["create"][0] == "cyber-racing"
    # An in-flight self-scope with no chat-born origin binds with the typed reason.
    assert calls["bind"] == ("t1", "cyber-racing", 7, {"absent": "mid_task_no_origin"})
    assert running["t1"]["task"]["project_id"] == "cyber-racing"  # F1: lease lane occupancy
    assert calls["broadcast"] == {"type": "projects_changed", "project_id": "cyber-racing", "chat_id": 7}


def test_supervisor_handler_refuses_before_side_effects_when_bound_elsewhere(monkeypatch, tmp_path):
    """B4=A: create precedes bind, so a task bound elsewhere used to leave a project
    row, a lease mark, a broadcast and a chat announcement behind before the immutable
    bind refused. The refusal is now first, and the requested name is carried to the
    project the task actually belongs to."""
    import json

    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    calls = {"create": None, "bind": None, "touch": None, "broadcast": None, "rename": None,
             "announce": None}
    monkeypatch.setattr(reg, "project_id_for_task", lambda dr, tid, **kw: "token-atlas")
    monkeypatch.setattr(reg, "create_project", lambda *a, **kw: calls.__setitem__("create", a))
    monkeypatch.setattr(reg, "bind_task_to_project", lambda *a, **kw: calls.__setitem__("bind", a))
    monkeypatch.setattr(reg, "touch_project", lambda *a, **kw: calls.__setitem__("touch", a))
    monkeypatch.setattr(reg, "update_project",
                        lambda dr, pid, **kw: calls.__setitem__("rename", (pid, kw)))
    monkeypatch.setattr(mb, "get_bridge", lambda: calls.__setitem__("broadcast", True))
    monkeypatch.setattr(workers, "_announce_created_project",
                        lambda *a, **kw: calls.__setitem__("announce", True))

    running = {"t1": {"task": {"id": "t1", "project_id": "token-atlas"}}}
    workers.ensure_project_scope(
        {"task_id": "t1", "project_id": "token-observatory", "project_name": "Token Observatory"},
        SimpleNamespace(RUNNING=running),
    )

    assert calls["create"] is None and calls["bind"] is None and calls["touch"] is None
    assert calls["broadcast"] is None and calls["announce"] is None
    assert calls["rename"] == ("token-atlas", {"name": "Token Observatory"})
    assert running["t1"]["task"]["project_id"] == "token-atlas"
    rows = [json.loads(line) for line in
            (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[-1]["type"] == "project_binding_failed"
    assert rows[-1]["reason"] == "project_scope_conflict"
    assert rows[-1]["project_id"] == "token-observatory"


def test_supervisor_handler_stops_when_the_bind_raises(monkeypatch, tmp_path):
    """A refused bind must not be followed by the lease mark, the broadcast and the
    announcement: the task is not in that project, so nothing may say it is."""
    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    calls = {"broadcast": None, "announce": None}

    def _raise(*_a, **_kw):
        raise ValueError("project binding is immutable")

    monkeypatch.setattr(reg, "project_id_for_task", lambda dr, tid, **kw: "")
    monkeypatch.setattr(reg, "create_project", lambda dr, pid, **kw: {"id": pid, "chat_id": 7})
    monkeypatch.setattr(reg, "touch_project", lambda *a, **kw: None)
    monkeypatch.setattr(reg, "bind_task_to_project", _raise)
    monkeypatch.setattr(mb, "get_bridge", lambda: calls.__setitem__("broadcast", True))
    monkeypatch.setattr(workers, "_announce_created_project",
                        lambda *a, **kw: calls.__setitem__("announce", True))

    running = {"t1": {"task": {"id": "t1", "project_id": ""}}}
    workers.ensure_project_scope(
        {"task_id": "t1", "project_id": "cyber-racing", "project_name": "Cyber Racing"},
        SimpleNamespace(RUNNING=running),
    )

    assert running["t1"]["task"]["project_id"] == ""
    assert calls["broadcast"] is None and calls["announce"] is None


def test_supervisor_handler_treats_an_unreadable_store_as_unbound(monkeypatch, tmp_path, caplog):
    """Proportionality (D6-6): an unreadable bindings file behaves like "no binding"
    and is disclosed once; it does not stop the conversion."""
    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "project_task_bindings.json").write_text("{ not json", encoding="utf-8")
    bound = {}
    monkeypatch.setattr(reg, "create_project", lambda dr, pid, **kw: {"id": pid, "chat_id": 7})
    monkeypatch.setattr(reg, "touch_project", lambda *a, **kw: None)
    monkeypatch.setattr(reg, "bind_task_to_project",
                        lambda dr, tid, pid, chat=None, *, origin: bound.update({"pid": pid}))
    monkeypatch.setattr(mb, "get_bridge", lambda: SimpleNamespace(broadcast=lambda payload: None))
    monkeypatch.setattr(workers, "_announce_created_project", lambda *a, **kw: None)

    running = {"t1": {"task": {"id": "t1", "project_id": ""}}}
    with caplog.at_level(logging.WARNING):
        workers.ensure_project_scope(
            {"task_id": "t1", "project_id": "cyber-racing", "project_name": "Cyber Racing"},
            SimpleNamespace(RUNNING=running),
        )

    assert bound == {"pid": "cyber-racing"}
    assert running["t1"]["task"]["project_id"] == "cyber-racing"
    assert "project_binding_unreadable" in caplog.text
