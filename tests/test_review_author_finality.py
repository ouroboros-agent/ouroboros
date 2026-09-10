import json
import inspect

import pytest


def test_commit_attempt_does_not_infer_author_finish_from_success():
    from ouroboros.tools import commit_gate

    source = inspect.getsource(commit_gate._record_commit_attempt)
    assert 'author_disposition = _req("author_disposition", None)' in source
    assert 'if author_disposition is None and status == "succeeded"' not in source
    assert 'disposition="accepted"' not in source


def test_author_disposition_is_hash_bound_and_rejects_malformed():
    from ouroboros.review_records import (
        build_author_disposition,
        validate_author_disposition,
    )

    record = build_author_disposition(
        disposition="rejected",
        rationale="The remaining note is outside this task.",
        subject_hash="abc123",
        reviewer_signal="REVISE_PLAN",
        enforcement="advisory",
    )
    assert record["subject_hash"] == "abc123"
    assert validate_author_disposition(record, subject_hash="abc123") == record
    assert validate_author_disposition(record, subject_hash="stale") is None
    with pytest.raises(ValueError, match="rationale"):
        build_author_disposition(
            disposition="accepted", rationale="", subject_hash="abc123",
        )


def test_plan_author_finish_is_projected_without_closing_blocking_gate():
    from ouroboros.task_results import _validated_plan_review_state

    state = {
        "schema_version": 2,
        "series_id": "s",
        "cycles_paid": 1,
        "need_evidence_seen": [],
        "current_attempt": {"fingerprint": "a" * 64, "status": "open", "reason": ""},
        "waves": [{
            "request_fingerprint": "a" * 64,
            "aggregate": "REVISE_PLAN",
            "closed": False,
            "spec": {"goal": "g"},
            "findings": [{"finding_id": "f", "class": "blocking"}],
            "dispositions": [],
            "author_disposition": {
                "disposition": "rejected",
                "rationale": "The reviewer request is out of scope.",
                "subject_hash": "a" * 64,
                "reviewer_signal": "REVISE_PLAN",
                "enforcement": "advisory",
                "recorded_at": "2026-01-01T00:00:00Z",
                "source": "author",
            },
        }],
    }
    loaded = _validated_plan_review_state(state)
    assert loaded["waves"][0]["author_disposition"]["disposition"] == "rejected"
    assert loaded["waves"][0]["closed"] is False
    bad = json.loads(json.dumps(state))
    bad["waves"][0]["author_disposition"]["subject_hash"] = "b" * 64
    with pytest.raises(ValueError, match="author_disposition"):
        _validated_plan_review_state(bad)


def test_skill_review_state_round_trips_current_hash_author_finish(tmp_path):
    from ouroboros.skill_loader import SkillReviewState, load_review_state, save_review_state

    state = SkillReviewState(
        status="blockers",
        content_hash="c" * 64,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory"}],
        author_disposition={
            "disposition": "partial",
            "rationale": "The advisory finding is understood and accepted for this revision.",
            "subject_hash": "c" * 64,
            "reviewer_signal": "blockers",
            "enforcement": "advisory",
            "recorded_at": "2026-01-01T00:00:00Z",
            "source": "author",
        },
    )
    save_review_state(tmp_path, "demo", state)
    loaded = load_review_state(tmp_path, "demo")
    assert loaded.author_disposition["subject_hash"] == "c" * 64
    assert loaded.to_dict()["author_disposition"]["disposition"] == "partial"


def test_advisory_acceptance_author_finish_skips_improvement_capsule(monkeypatch, tmp_path):
    import ouroboros.loop as loop_mod
    import ouroboros.review_substrate as substrate
    from ouroboros.loop_acceptance_review import _apply_task_acceptance_result

    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "advisory")
    monkeypatch.setattr(loop_mod, "_end_task_acceptance_fence", lambda *_a, **_k: True)
    monkeypatch.setattr(loop_mod, "_mark_root_acceptance_checkpoint", lambda *_a, **_k: None)
    tool_ctx = type("Ctx", (), {
        "_task_acceptance_reviewed": False,
        "_task_acceptance_improvement_passes": 0,
        "_task_acceptance_seen_bindings": {},
    })()
    ctx = loop_mod._TaskAcceptanceContext(
        tools=type("Tools", (), {"_ctx": tool_ctx})(),
        content="done", task_id="t", task_type="task",
        llm_trace={"tool_calls": [], "acceptance_decision": {
            "agent_disposition": "rejected",
            "agent_rationale": "The reviewer suggestion is outside scope.",
        }},
        drive_root=None, messages=[], emit_progress=lambda *_a, **_k: None,
        mode="required", subtree_statuses=[], budget_profile={"max_improvement_passes": 3},
        passes_done=0,
    )
    result = substrate.ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=[{"slot_id": "s0", "signal": "FAIL", "parsed": {
            "verdict": "FAIL", "outcome_tier": "best_effort",
            "completion_coach": "make a change",
        }}],
        parsed_findings=[], aggregate_signal="FAIL",
    )
    assert _apply_task_acceptance_result(ctx, result, record_run=False) is False
    decision = ctx.llm_trace["acceptance_decision"]
    assert decision["reason"] == "author_finish"
    assert decision["status"] == "finalized_unaccepted"
    assert decision["author_disposition"]["disposition"] == "rejected"
    assert decision["author_disposition"]["subject_hash"]
    assert tool_ctx._task_acceptance_reviewed is True


def test_skill_author_finish_uses_existing_review_without_dispatch_same_hash(
    monkeypatch, tmp_path,
):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state
    from ouroboros.tool_access_types import ResolvedResourceBinding
    from ouroboros.tools import skill_exec as skill_exec_mod
    from tests.test_skill_exec import _build_skill, _make_ctx

    skills_root = tmp_path / "skills"
    skill_dir = _build_skill(skills_root, "demo")
    ctx = _make_ctx(tmp_path)
    binding = ResolvedResourceBinding(
        profile="self_modification", root="skill_payload", operation="review",
        base_path=skill_dir, target_path=skill_dir, source="test",
        skill_name="demo", state_drive_root=tmp_path,
    )
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(skill_exec_mod, "build_resolved_resource_binding", lambda *a, **k: binding)
    monkeypatch.setattr(skill_exec_mod, "_skill_tool_preflight", lambda *a, **k: "")
    prior_hash = compute_content_hash(skill_dir)
    save_review_state(tmp_path, "demo", SkillReviewState(
        status="blockers", content_hash=prior_hash,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "reason": "review finding"}],
        raw_actor_records=[{"slot_id": "s0", "status": "ok"}],
    ))
    monkeypatch.setattr(
        skill_exec_mod, "run_skill_review_lifecycle_blocking",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("author finish dispatched a new panel")),
        raising=False,
    )
    out = skill_exec_mod._handle_review_skill(
        ctx, skill="demo", _resolved_binding=binding,
        author_disposition="rejected", author_rationale="The finding is outside this task.",
    )
    assert "Author finish recorded" in out
    assert "review finding" in out
    loaded = __import__("ouroboros.skill_loader", fromlist=["load_review_state"]).load_review_state(tmp_path, "demo")
    assert loaded.author_disposition["subject_hash"] == prior_hash
    assert loaded.status == "blockers"


def test_skill_author_finish_binds_changed_hash_after_preflight_without_panel(
    monkeypatch, tmp_path,
):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, load_review_state, save_review_state
    from ouroboros.tool_access_types import ResolvedResourceBinding
    from ouroboros.tools import skill_exec as skill_exec_mod
    from tests.test_skill_exec import _build_skill, _make_ctx

    skills_root = tmp_path / "skills"
    skill_dir = _build_skill(skills_root, "demo", script_body="print('old')\n")
    ctx = _make_ctx(tmp_path)
    binding = ResolvedResourceBinding(
        profile="self_modification", root="skill_payload", operation="review",
        base_path=skill_dir, target_path=skill_dir, source="test",
        skill_name="demo", state_drive_root=tmp_path,
    )
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(skill_exec_mod, "build_resolved_resource_binding", lambda *a, **k: binding)
    monkeypatch.setattr(skill_exec_mod, "_skill_tool_preflight", lambda *a, **k: "")
    old_hash = compute_content_hash(skill_dir)
    save_review_state(tmp_path, "demo", SkillReviewState(
        status="blockers", content_hash=old_hash,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "reason": "old finding"}],
        raw_actor_records=[{"slot_id": "s0", "status": "ok"}],
    ))
    (skill_dir / "scripts" / "hello.py").write_text("print('fixed')\n", encoding="utf-8")
    monkeypatch.setattr(
        skill_exec_mod, "run_skill_review_lifecycle_blocking",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("changed-hash finish dispatched a panel")),
        raising=False,
    )
    out = skill_exec_mod._handle_review_skill(
        ctx, skill="demo", _resolved_binding=binding,
        author_disposition="partial", author_rationale="The fix addresses the actionable part.",
    )
    current_hash = compute_content_hash(skill_dir)
    loaded = load_review_state(tmp_path, "demo")
    assert current_hash != old_hash
    assert loaded.content_hash == current_hash
    assert loaded.reviewed_content_hash == old_hash
    assert loaded.author_disposition["subject_hash"] == current_hash
    assert loaded.status == "blockers"
    assert "raw reviewer findings" in out


def test_ordinary_advisory_commit_does_not_invent_author_finish(tmp_path):
    from types import SimpleNamespace
    from ouroboros.review_state import load_state
    from ouroboros.tools.commit_gate import _record_commit_attempt

    ctx = SimpleNamespace(
        drive_root=tmp_path,
        repo_dir=tmp_path,
        task_id="",
        _review_advisory=["advisory finding"],
        _current_review_attempt_number=0,
    )
    _record_commit_attempt(ctx, commit_message="ordinary advisory", status="succeeded")
    attempts = load_state(tmp_path).attempts
    assert attempts
    assert attempts[-1].author_disposition == {}
