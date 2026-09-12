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


def test_a_healed_debt_never_leaves_a_warning_headline_without_a_cause() -> None:
    """Headline and Reason describe the same record.

    The overlay stamps BOTH the top-level reason code and an objective warning,
    and the refresh may not rewrite either. Suppressing the code once the debt
    heals therefore produced a row headed "Done with warnings" that stated no
    cause at all, which is a worse lie than the stale code it removed: the
    warning is still what the record holds. Built through the real overlay, not
    a hand-written axes dict.
    """
    from ouroboros.outcomes import custody_debt_axes
    from ouroboros.project_dialogue import OUTCOME_PHASE_HEADLINE, outcome_phase

    axes = custody_debt_axes({"lifecycle": {"status": "completed"},
                              "execution": {"status": "ok", "reason_code": ""}})
    for debt in ([], ["run-a1"]):
        row = _row(outcome_axes=axes, delegated_runs_unreconciled=debt)
        assert OUTCOME_PHASE_HEADLINE[outcome_phase(row, {})] == "Done with warnings"
        assert _completion_verdict(row, {}) == (
            f"Reason: {WARN_DELEGATED_CUSTODY_UNRECONCILED}."
        )

    # A row whose axes healed too reads as clean, so it states nothing: that is
    # the false Reason line this rule removes, and it stays removed.
    clean = _row(outcome_axes={"execution": {"status": "ok"}},
                 delegated_runs_unreconciled=[])
    assert OUTCOME_PHASE_HEADLINE[outcome_phase(clean, {})] == "Done"
    assert _completion_verdict(clean, {}) == ""

    # An execution cause still wins over the healed debt.
    railed = _row(outcome_axes=custody_debt_axes(
        {"execution": {"status": "failed", "reason_code": "provider_unavailable"}}),
        delegated_runs_unreconciled=[])
    assert _completion_verdict(railed, {}) == "Reason: provider_unavailable."
