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


def _skip_events(data_root):
    path = data_root / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if row.get("type") == "usage_ledger_compaction_skipped"
    ]


def test_a_policy_abort_records_its_typed_reason_once_per_cause(data_root, monkeypatch):
    """An abort leaves the ledger byte-identical and the pass runs again later,
    so the reason has to outlive the pass: one typed event per process per
    cause, which is what lets the ledger's size tripwire be diagnosed instead
    of just observed."""
    ua.reserve_attempt(ua.AttemptRequest(  # in-flight only: nothing folds
        model="openai/gpt-5.2", provider="openai", reservation_usd=1.0,
        drive_root=data_root, task_id="open", root_task_id="root", source="test",
    ))

    assert _compact(data_root) is None
    assert _compact(data_root) is None  # same cause again: still ONE row
    assert [row["reason"] for row in _skip_events(data_root)] == ["nothing foldable"]
    assert _skip_events(data_root)[0]["ts"]

    for cost in _DRIFT_COSTS:
        _settle(data_root, cost=cost, cost_final=True)
    _rewrite_one_group_row(
        monkeypatch,
        lambda row: row.update({"cost_usd": format(Decimal(row["cost_usd"]) + Decimal("0.01"), "f")}),
    )

    assert _compact(data_root) is None
    assert _compact(data_root) is None
    # A NEW cause is never hidden behind the one already told.
    assert [row["reason"] for row in _skip_events(data_root)] == [
        "nothing foldable", "decimal money totals mismatch",
    ]


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


# --- The growth guard after a COMMITTED pass ---------------------------------
#
# The trigger is a size threshold and the residue that cannot fold (group rows,
# retained idempotent and review-attributed rows) only ever grows, so a
# compacted ledger settles just under the threshold and stays there. While a
# success cleared the memo, that left nothing between the threshold and the
# pass: every reservation rewrote the whole monetary authority under the held
# lock and copied the entire live file into a new, never-collected archive
# segment to save a few kilobytes. Measured on the owner's live-ledger copy
# before this fix: 200 production chains on the folded 7.87 MB ledger ran 7
# full passes and grew the archive from 77.8 MB to 125.8 MB while the live
# file gained 114 KB — a ninth of the 1 MB the guard asks for.


def _trigger(data_root):
    with ua._locked(data_root) as heartbeat:
        return uc.maybe_compact_usage_ledger_locked(data_root, heartbeat=heartbeat)


def _folds(data_root):
    return sum(1 for row in _ledger_rows(data_root) if row.get("kind") == "usage_baseline")


def test_a_committed_fold_arms_the_same_growth_guard_as_an_abort(data_root, monkeypatch):
    """A pass that COMMITTED must throttle the next one exactly like a pass that
    aborted: the memo means "the size this process last ran a pass on", not
    "the size the last unprofitable pass saw"."""
    _fixtures._seed_mixed_ledger(data_root)
    monkeypatch.setattr("ouroboros.config.USAGE_LEDGER_COMPACT_BYTES", 1)
    monkeypatch.setattr("ouroboros.config.USAGE_LEDGER_COMPACT_RETRY_GROWTH_BYTES", 1_000_000)
    uc._COMPACT_ATTEMPTS.clear()
    entered = []
    original = uc.compact_usage_ledger_locked
    monkeypatch.setattr(uc, "compact_usage_ledger_locked",
                        lambda root, **kwargs: (entered.append(1), original(root, **kwargs))[1])

    assert _trigger(data_root) is True  # first pass: above threshold, no memo yet
    assert _folds(data_root) == 1
    compacted_bytes = (data_root / ua.LEDGER_REL).read_bytes()

    # Still far above the threshold and freshly foldable rows keep arriving, so
    # the threshold alone would re-enter the pass on every settle.
    for index in range(3):
        _settle(data_root, cost=0.25, cost_final=True, task_id="after-%d" % index)
        assert _trigger(data_root) is False
    assert len(entered) == 1, "the pass ran again below the growth threshold"
    # Declined, not aborted: the pass was never entered, so it recorded no
    # typed skip reason and the fold it already committed still stands.
    assert _skip_events(data_root) == []
    assert (data_root / ua.LEDGER_REL).read_bytes().startswith(compacted_bytes)

    # Real growth past the threshold releases it, and the new pass folds again.
    monkeypatch.setattr("ouroboros.config.USAGE_LEDGER_COMPACT_RETRY_GROWTH_BYTES", 1)
    assert _trigger(data_root) is True
    assert len(entered) == 2
    assert _folds(data_root) == 1  # the header is replaced, never accumulated
    assert int(_ledger_rows(data_root)[0]["compaction_epoch"]) == 2


def test_repeated_chains_cost_at_most_one_pass_per_growth_window(data_root, monkeypatch):
    """The refuters' driver, in miniature: chains that keep the ledger above the
    trigger may not buy one full rewrite each. The bound is the guard's own
    arithmetic — a pass, then one more per RETRY_GROWTH_BYTES of real growth —
    not a magic number."""
    _fixtures._seed_mixed_ledger(data_root)
    growth = 4_096
    monkeypatch.setattr("ouroboros.config.USAGE_LEDGER_COMPACT_BYTES", 1)
    monkeypatch.setattr("ouroboros.config.USAGE_LEDGER_COMPACT_RETRY_GROWTH_BYTES", growth)
    uc._COMPACT_ATTEMPTS.clear()
    passes = []
    original = uc.compact_usage_ledger_locked

    def counting(root, **kwargs):
        receipt = original(root, **kwargs)
        passes.append(receipt is not None)
        return receipt

    monkeypatch.setattr(uc, "compact_usage_ledger_locked", counting)

    path = data_root / ua.LEDGER_REL
    added = 0
    for index in range(30):
        before = path.stat().st_size
        _settle(data_root, cost=0.5, cost_final=True, task_id="chain-%d" % index)
        added += max(0, path.stat().st_size - before)  # a pass shrinks it: count appends only
        assert _trigger(data_root) is not None
    assert added > 0
    assert len(passes) <= 1 + added // growth, (
        "%d passes for %d appended bytes at a %d-byte guard" % (len(passes), added, growth)
    )
    assert all(passes), "a pass that aborts would prove nothing about the guard"
