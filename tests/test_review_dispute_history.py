"""Both parts of the existing review brief preserve supplied dispute facts."""
from pathlib import Path

import pytest

from ouroboros.tools.review_helpers import review_history_with_obligations
from tests.test_review_change_end_to_end import staged_body as _staged_body

staged_body = _staged_body


def _rounds():
    return [
        {"attempt": 1, "subject": {"diff_sha": "old-diff"}, "verdict": "FAIL",
         "critical": [{"item": "parser", "reason": "Original objection", "recommendation": "Use a database"}],
         "author_disposition": {"decision": "reject", "rationale": "The bounded file is the accepted scope."},
         "reviewer_outputs": [{"slot_id": "old-seat", "raw_text": "I disagree because atomic writes need a proof."}]},
        {"attempt": 2, "subject": {"diff_sha": "middle-diff"}, "verdict": "PASS",
         "advisory": [{"item": "naming", "reason": "Check the spelling"}]},
        {"attempt": 3, "subject": {"diff_sha": "new-diff"}, "verdict": "FAIL",
         "critical": [{"item": "parser", "reason": "Consider a database again"}],
         "review_rebuttal": "Now multiple writers are required; reconsider the earlier scope."},
    ]


@pytest.mark.parametrize("route,retrieves", [("api_chat", False), ("api_chat", True), ("agent_session", True)])
def test_public_packet_and_two_part_brief_keep_all_supplied_arguments(staged_body, tmp_path, monkeypatch, route, retrieves):
    from ouroboros import reviewer_window, capability_evidence
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.review_admission import build_two_part_brief
    from ouroboros.tools.review_subject import ReviewSubjectSpec, freeze_subject

    # Every capability answer is local; this test assembles inputs and dispatches nothing.
    monkeypatch.setattr(reviewer_window, "reviewer_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(capability_evidence, "probe", lambda *a, **k: None)
    root = Path(staged_body["repo"])
    ctx = ToolContext(repo_dir=root, drive_root=tmp_path / "data")
    frozen = freeze_subject(ctx, ReviewSubjectSpec(root_kind="system_repo", root=str(root), kind="index",
                                                 governance_root=str(root), surface="change", layer="body"))
    history = _rounds()
    coupling = [{"verdict": "FAIL", "blocked": True, "summary": "The coupling rationale stays visible.",
                 "critical_findings": [{"item": "contract", "reason": "Reader mismatch"}],
                 "author_disposition": {"decision": "reject", "rationale": "The reader is migrated together."}}]
    brief = build_two_part_brief(
        frozen, {"slot_id": "fresh", "model": "fake/reviewer", "route": route, "retrieves": retrieves},
        drive_root=ctx.drive_root, task_id="test-task", review_history=history, coupling_history=coupling,
        owner_words="Owner chose file storage for this scope; that approval excludes a database service.")
    text = brief["system"]
    for value in ("The bounded file is the accepted scope.", "I disagree because atomic writes need a proof.",
                  "Use a database", "Check the spelling", "Now multiple writers are required", "old-diff", "new-diff",
                  "approval excludes a database service"):
        assert value in text
    assert brief["parts"] == (["change", "coupling"] if retrieves else ["change"])
    if retrieves:
        assert "The reader is migrated together." in text
        assert "The coupling rationale stays visible." in text
    assert "An author's rejection is not reviewer agreement" in text


def test_missing_durable_obligations_are_an_explicit_gap_with_available_history(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "advisory_review.json").write_text("{broken", encoding="utf-8")
    text = review_history_with_obligations(_rounds(), drive_root=tmp_path, repo_root=tmp_path / "repo")
    assert "REVIEW_HISTORY_SOURCE_UNAVAILABLE" in text
    assert "The bounded file is the accepted scope." in text
    assert "I disagree because atomic writes need a proof." in text


def test_obligation_reason_is_complete_in_shared_history(tmp_path):
    from ouroboros.review_state import AdvisoryReviewState, ObligationItem, make_repo_key, save_state

    reason = "Exact reason and rejected alternative. " * 150 + "DECISIVE_REASON_END"
    state = AdvisoryReviewState()
    state.open_obligations.append(ObligationItem(
        obligation_id="ob-1", item="contract", severity="critical", reason=reason,
        source_attempt_ts="2026-10-09T00:00:00Z", source_attempt_msg="repair", status="still_open",
        repo_key=make_repo_key(tmp_path)))
    save_state(tmp_path, state)
    text = review_history_with_obligations([], drive_root=tmp_path, repo_root=tmp_path)
    assert reason in text and "OMISSION NOTE" not in text
