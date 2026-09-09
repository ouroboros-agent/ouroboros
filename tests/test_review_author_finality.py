import json

import pytest


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
