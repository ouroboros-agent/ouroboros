"""The Reason line names the cause the record actually holds (owner item, spam B).

``_apply_terminal_custody_outcome`` stamps ``delegated_custody_unreconciled`` as
the row's reason_code while a delegated run is still unreconciled. The debt then
heals from the WRITE side, and ``docs/ARCHITECTURE.md`` (the stored
``delegated_runs_unreconciled`` projection is healed only from the write side)
forbids that refresh rewriting reason_code - so the stored code outlives the
fact and the owner keeps reading about a debt the same record shows as empty.
Nine of fourteen terminal rows in the audited window did exactly that, and one
of them repeated it twenty-one minutes later through the project summary, which
shares this renderer.

New module rather than tests/test_terminal_truth_projection.py: that file is 986
lines and would cross the 1000-line target here.
"""

from __future__ import annotations

from ouroboros.outcomes import WARN_DELEGATED_CUSTODY_UNRECONCILED
from ouroboros.project_dialogue import _completion_verdict


def _row(**fields) -> dict:
    row = {"status": "completed", "reason_code": WARN_DELEGATED_CUSTODY_UNRECONCILED}
    row.update(fields)
    return row


def test_a_healed_debt_yields_the_execution_reason_instead() -> None:
    healed = _row(
        delegated_runs_unreconciled=[],
        outcome_axes={"execution": {"status": "degraded", "reason_code": "tool_failure"}},
    )
    assert _completion_verdict(healed, {}) == "Reason: tool_failure."


def test_an_open_debt_is_still_named() -> None:
    open_debt = _row(
        delegated_runs_unreconciled=["run-a1"],
        outcome_axes={"execution": {"status": "ok"}},
    )
    assert _completion_verdict(open_debt, {}) == (
        f"Reason: {WARN_DELEGATED_CUSTODY_UNRECONCILED}."
    )


def test_both_real_are_stated_in_one_line() -> None:
    both = _row(
        delegated_runs_unreconciled=["run-a1", "run-b2"],
        outcome_axes={"execution": {"status": "failed", "reason_code": "provider_unavailable"}},
    )
    assert _completion_verdict(both, {}) == (
        f"Reason: provider_unavailable ({WARN_DELEGATED_CUSTODY_UNRECONCILED})."
    )


def test_a_healed_debt_with_no_execution_cause_states_nothing() -> None:
    """The row earned no cause of its own and no longer owes anything: inventing
    a Reason line here is exactly the false statement this closes."""
    silent = _row(delegated_runs_unreconciled=[], outcome_axes={"execution": {"status": "ok"}})
    assert _completion_verdict(silent, {}) == ""


def test_the_debt_may_arrive_on_the_event_instead_of_the_result() -> None:
    """Both lifecycle writers share this renderer; the live event carries the
    same fields the stored result does."""
    event = {
        "reason_code": WARN_DELEGATED_CUSTODY_UNRECONCILED,
        "delegated_runs_unreconciled": ["run-a1"],
        "outcome_axes": {"execution": {"status": "ok"}},
    }
    assert _completion_verdict({}, event) == f"Reason: {WARN_DELEGATED_CUSTODY_UNRECONCILED}."


def test_every_other_reason_code_passes_through_untouched() -> None:
    plain = {"status": "failed", "reason_code": "provider_unavailable"}
    assert _completion_verdict(plain, {}) == "Reason: provider_unavailable."
    assert _completion_verdict({"status": "completed"}, {}) == ""
