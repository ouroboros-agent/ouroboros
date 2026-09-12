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


def test_a_healed_debt_is_never_resurrected_by_its_own_frozen_warning() -> None:
    """Once the debt list is empty the code is gone, whatever the axes still say.

    The overlay stamps BOTH the top-level reason code and an objective warning,
    and the refresh may rewrite neither. That frozen warning is what keeps the
    headline at "Done with warnings" after the debt heals, and it is NOT licence
    to restore the code beside it: a Reason line naming a debt the same record
    shows as empty is exactly the false statement this rule removes. The current
    execution reason speaks when there is one; otherwise the row states no cause
    and leaves the headline to the axis that owns it. Built through the real
    overlay, not a hand-written axes dict.
    """
    from ouroboros.outcomes import custody_debt_axes
    from ouroboros.project_dialogue import OUTCOME_PHASE_HEADLINE, outcome_phase

    axes = custody_debt_axes({"lifecycle": {"status": "completed"},
                              "execution": {"status": "ok", "reason_code": ""}})
    healed = _row(outcome_axes=axes, delegated_runs_unreconciled=[])
    # The frozen warning still heads the row; the healed debt says nothing.
    assert OUTCOME_PHASE_HEADLINE[outcome_phase(healed, {})] == "Done with warnings"
    assert _completion_verdict(healed, {}) == ""

    # While the debt is real the row names it, headline and cause agreeing.
    owed = _row(outcome_axes=axes, delegated_runs_unreconciled=["run-a1"])
    assert OUTCOME_PHASE_HEADLINE[outcome_phase(owed, {})] == "Done with warnings"
    assert _completion_verdict(owed, {}) == f"Reason: {WARN_DELEGATED_CUSTODY_UNRECONCILED}."

    # A row whose axes healed too reads as clean and also states nothing.
    clean = _row(outcome_axes={"execution": {"status": "ok"}},
                 delegated_runs_unreconciled=[])
    assert OUTCOME_PHASE_HEADLINE[outcome_phase(clean, {})] == "Done"
    assert _completion_verdict(clean, {}) == ""

    # A current execution cause is what a healed row renders when it has one.
    railed = _row(outcome_axes=custody_debt_axes(
        {"execution": {"status": "failed", "reason_code": "provider_unavailable"}}),
        delegated_runs_unreconciled=[])
    assert _completion_verdict(railed, {}) == "Reason: provider_unavailable."


def test_the_event_and_the_row_render_one_reason_line_over_the_shared_fixture() -> None:
    """S1: one debt rule on every surface, from one source.

    ``web/tests/fixtures/outcome_phase_parity.json`` is the twin fixture the
    browser reads in ``web/tests/reason_detail.test.js``: every case that
    declares a Reason line is asserted there against ``taskReasonDetail`` and
    here against ``_completion_verdict``, so a rule that lives on only one
    surface fails on both sides of the boundary. The same record is asserted in
    both lifecycle positions, because a live ``task_done`` event and the durable
    row reach this renderer through different arguments and must never disagree:
    the custody warning is named while the record's own debt list is non-empty,
    the current execution cause stands once it is empty, and a record carrying
    no list states nothing about the debt at all.
    """
    import json
    import pathlib

    fixture = (pathlib.Path(__file__).resolve().parents[1]
               / "web" / "tests" / "fixtures" / "outcome_phase_parity.json")
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    asserted = [case for case in cases if case.get("acceptance_clause")]
    assert len(asserted) >= 5, "the fixture lost its Reason-line cases"
    for case in asserted:
        record, clause = case["record"], case["acceptance_clause"]
        assert _completion_verdict(record, {}) == clause, case["name"]
        assert _completion_verdict({}, record) == clause, case["name"]
