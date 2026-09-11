"""What the compaction pass COMPARES before it commits, and how it records a refusal.

Companion to ``tests/test_usage_compaction.py`` (the pass itself) and
``tests/test_usage_compaction_archive.py`` (the archive reader). Two pins live
here:

1. the self-check compares the readers' NON-MONEY projection and leaves money
   to the exact decimal totals — a float summary rounded at six places can
   round a per-row sum and an exact per-group sum to different last digits,
   which on the owner's live ledger aborted a CORRECT fold 240 times and held
   the file at 77.8 MB against an 8 MB trigger. Counts, weights, tokens and
   limits are still compared, and real money movement still aborts;
2. every policy abort leaves a typed, deduplicated
   ``usage_ledger_compaction_skipped`` event naming its reason, so the 20 MB
   health tripwire's cause is recoverable afterwards instead of living only in
   an INFO log line.
"""

from __future__ import annotations

import decimal
import json
from decimal import Decimal

from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as uc
from tests import fixtures_usage_compaction as _fixtures
from tests.fixtures_usage_compaction import _compact, _ledger_rows, _settle

data_root = _fixtures.data_root
data_root_any_tier = _fixtures.data_root_any_tier

# The two costs are VERBATIM from root 304db373 of the owner's live ledger:
# their exact sum 2.4675885 sits on the six-place rounding boundary, so the
# per-row float sum (2.4675884999999997) rounds down and the exact per-group
# sum rounds up. One of the 51 buckets that aborted the live fold.
_DRIFT_COSTS = (1.9542475, 0.513341)
_DRIFT_EXACT_SUM = Decimal("2.4675885")


def _final_rows(rows):
    finals = {}
    for row in rows:
        finals[str(row.get("attempt_id"))] = row
    return list(finals.values())


def _exact_money(rows):
    """Exact (settled cost, reservation bound) totals under a WIDE context."""
    with decimal.localcontext() as context:
        context.prec = 200
        cost = bound = Decimal(0)
        for row in _final_rows(rows):
            if str(row.get("kind") or "") == "usage_baseline":
                continue
            value = row.get("cost_usd")
            if value is not None and str(row.get("state") or "") == "settled":
                cost += Decimal(str(value))
            upper = row.get("reservation_upper_bound_usd")
            if upper is not None:
                bound += Decimal(str(upper))
    return cost, bound


def _rewrite_one_group_row(monkeypatch, mutate):
    """Let ``mutate`` edit the single group row of the built candidate."""
    real_build = uc._build_candidate

    def build(records, decimal_records, raw, beat):
        candidate, receipt = real_build(records, decimal_records, raw, beat)
        lines = candidate.decode("utf-8").splitlines()
        rebuilt = []
        for line in lines:
            row = json.loads(line)
            if str(row.get("kind") or "") == "usage_baseline_group":
                mutate(row)
                line = uc._dumps_row(row)
            rebuilt.append(line)
        candidate = ("\n".join(rebuilt) + "\n").encode("utf-8")
        receipt["compacted_size_bytes"] = len(candidate)
        return candidate, receipt

    monkeypatch.setattr(uc, "_build_candidate", build)


def test_one_ulp_float_rounding_drift_no_longer_aborts_the_fold(data_root):
    """The live defect, reduced: two settled rows whose float money rounds one
    unit away from their exact sum now FOLD. Money stays decimal-identical and
    the non-money view is untouched; the disclosed price is that the rounded
    float a reader displays may move by 1e-6 (a ten-thousandth of a cent)."""
    for cost in _DRIFT_COSTS:
        _settle(data_root, cost=cost, cost_final=True)

    before_rows = _ledger_rows(data_root)
    before = ua._summary(_final_rows(before_rows))
    receipt = _compact(data_root)

    assert receipt is not None  # pre-fix: _Abort("aggregation fingerprint mismatch")
    after_rows = _ledger_rows(data_root)
    groups = [row for row in after_rows if row.get("kind") == "usage_baseline_group"]
    assert len(groups) == 1
    assert Decimal(groups[0]["cost_usd"]) == _DRIFT_EXACT_SUM

    after = ua._summary(_final_rows(after_rows))
    # The drift this test exists for: real, one unit of the sixth place, and
    # in the FLOAT view only.
    assert before["settled_usd"] == 2.467588
    assert after["settled_usd"] == 2.467589
    assert _exact_money(after_rows) == _exact_money(before_rows)
    assert _exact_money(after_rows)[0] == _DRIFT_EXACT_SUM
    # Everything the fold could actually have lost is still identical.
    assert uc._render_fingerprint(_final_rows(after_rows)) == uc._render_fingerprint(
        _final_rows(before_rows)
    )
    assert (after["attempt_counts"], after["non_final_rows"], after["unknown_unmetered"]) == (
        before["attempt_counts"], before["non_final_rows"], before["unknown_unmetered"]
    )


def test_money_moved_in_the_candidate_still_aborts_on_exact_decimals(
    data_root, monkeypatch,
):
    """Money is no longer in the fingerprint, so the EXACT decimal totals are
    the guard that must catch a candidate whose dollars moved. A cent added to
    the group row leaves every non-money value identical and must still abort,
    byte-identically."""
    for cost in _DRIFT_COSTS:
        _settle(data_root, cost=cost, cost_final=True)
    before_bytes = (data_root / ua.LEDGER_REL).read_bytes()

    _rewrite_one_group_row(
        monkeypatch,
        lambda row: row.update({"cost_usd": format(Decimal(row["cost_usd"]) + Decimal("0.01"), "f")}),
    )

    assert _compact(data_root) is None
    assert (data_root / ua.LEDGER_REL).read_bytes() == before_bytes


def test_lost_folded_weight_still_aborts_on_the_non_money_fingerprint(
    data_root, monkeypatch,
):
    """The subtraction removed money and nothing else: a candidate that drops
    one folded attempt from the group weight changes only counts, and the
    fingerprint still refuses it."""
    for cost in _DRIFT_COSTS:
        _settle(data_root, cost=cost, cost_final=True)
    before_bytes = (data_root / ua.LEDGER_REL).read_bytes()

    _rewrite_one_group_row(
        monkeypatch,
        lambda row: row.update({"folded_attempt_count": int(row["folded_attempt_count"]) - 1}),
    )

    assert _compact(data_root) is None
    assert (data_root / ua.LEDGER_REL).read_bytes() == before_bytes


def test_fingerprint_carries_no_money_but_keeps_limits_and_counts(data_root):
    """The projection shape itself: no money key anywhere in it, while the
    per-root ``root_limit_usd`` and every count axis stay compared."""
    _settle(data_root, cost=1.25, cost_final=True, root_limit_usd=40.0)
    _settle(data_root, cost=2.5, cost_final=True, task_id="t2", root_limit_usd=50.0)
    fingerprint = uc._render_fingerprint(_final_rows(_ledger_rows(data_root)))

    buckets = [fingerprint["summary"], fingerprint["breakdown"]]
    buckets += [summary for summary, _limit in fingerprint["by_root"].values()]
    for grouped, unattributed in fingerprint["axes"].values():
        buckets += [*grouped.values(), unattributed]
    for bucket in buckets:
        assert not uc._FINGERPRINT_MONEY_KEYS & set(bucket)
        assert "attempt_counts" in bucket

    assert fingerprint["by_root"]["root"][1] == 40.0  # min known root limit
    assert fingerprint["summary"]["attempt_counts"] == {"settled": 2}
    assert fingerprint["breakdown"]["physical_calls"] == 2
