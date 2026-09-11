"""Collection side of ``plan_task``'s event route.

A fresh plan-review dispatch returns control at the dispatch barrier
(``ReviewRequest.drain_deadline``): the wave is recorded open with typed
``pending_dispatch`` rows and the reviewer workers keep running under
process-local custody (``ouroboros/review_custody.py``). This leaf owns what
happens next: the per-slot progress line and the ONE system frame the last
settlement writes into the task's mailbox (the mind wakes exactly as it does
for a child result — ``wait_task``/``wait_tasks`` return on
``owner_mailbox_pending``), and the $0 collection that closes or advances the
recorded wave through the existing ``review_disposition`` mode. Closing and
aggregating a wave stays with the collecting call (the sole wave writer);
nothing here polls, times, or writes wave state of its own.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict

log = logging.getLogger(__name__)


def announce_released_settlement(
    usage_ctx: Any, *, request: Any, task_id: str, slot: Any, actor: Any,
    settled_wave: Dict[str, str],
) -> None:
    """One progress line per settled released slot (derived from the terminal
    ``cognitive_operation`` fact the worker just emitted) and, when the LAST
    released slot of a plan-review wave settles, ONE system frame in the task's
    mailbox. ``settled_wave`` is ``{slot_id: status}`` for the whole released set
    when this settlement completed it, otherwise empty."""
    if str(getattr(request, "surface", "") or "") != "plan_review":
        return
    fingerprint = str((getattr(request, "reconciliation_identity", {}) or {}).get("subject_hash") or "")
    slot_id = str(getattr(slot, "slot_id", "") or "")
    try:
        emit = getattr(usage_ctx, "emit_progress_fn", None)
        if callable(emit):
            emit(f"📐 plan_task: reviewer slot {slot_id} settled ({actor.status}) for wave "
                 f"{fingerprint[:8] or '?'}" + ("; every released slot has settled" if settled_wave else ""))
    except Exception:
        log.debug("released-slot progress line failed", exc_info=True)
    if not settled_wave or usage_ctx is None or not getattr(usage_ctx, "drive_root", None):
        return
    ok = sum(1 for status in settled_wave.values() if status in {"ok", "empty"})
    from ouroboros.owner_mailbox import write_task_message

    try:
        write_task_message(
            pathlib.Path(str(usage_ctx.drive_root)),
            f"Plan review wave {fingerprint[:8] or '?'}: {len(settled_wave)} released reviewer "
            f"slot(s) settled ({ok} ok, {len(settled_wave) - ok} failed); not yet collected "
            f"(review_fingerprint {fingerprint}).",
            task_id, source_task_id=task_id, provenance="system",
        )
    except Exception:
        log.warning("plan review settled-wave frame failed for %s", task_id, exc_info=True)
