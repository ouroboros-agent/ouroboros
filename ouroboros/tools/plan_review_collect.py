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


def run_plan_coroutine(coro: Any) -> Any:
    """Run one plan-review coroutine to completion from a synchronous tool handler.

    The ToolEntry envelope is the outer settlement bound: the substrate owns each
    review slot's logical window and late-result custody, so no second
    ``asyncio.wait_for`` is nested here (it would cancel the coroutine while its
    executor worker keeps running, then ``asyncio.run`` waits for that worker at
    shutdown and defeats the apparent timeout). ``copy_context``: the registry's
    tool-result sidecar is a ContextVar, and the published native plan result must
    reach the dispatching thread's slot (D02) — a bare pool thread would publish
    into the void."""
    import asyncio
    import concurrent.futures
    import contextvars

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result()


def prepared_from_wave(ctx: Any, exact: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
    """The exact inputs of a RECORDED wave, for the engine's resume path: the
    restored spec, prose and evidence manifest of the wave itself — never a
    re-read of the evidence, so the collection cannot change the wave's identity."""
    from ouroboros.review_substrate import review_repo_dirs_for
    from ouroboros.tools.plan_review import _PlanRequest

    system_root, active_root = review_repo_dirs_for(ctx)
    spec = dict(exact.get("spec") or {})
    request = _PlanRequest(
        goal=str(spec.get("goal") or ""), plan=str(exact.get("plan_prose") or ""), spec=spec,
        reviewer_effort=str(exact.get("reviewer_effort") or ""),  # the same roster the wave dispatched with
    )
    prepared = {
        "spec": spec, "system_root": system_root, "active_root": active_root,
        "constitutional": bool(exact.get("constitutional")),
        "constitutional_note": str(exact.get("constitutional_note") or ""),
        "manifest": dict(exact.get("evidence_manifest_full") or exact.get("evidence_manifest") or {}),
        "manifest_hash": str(exact.get("evidence_manifest_hash") or ""),
        "reminder": "", "fingerprint": str(exact.get("request_fingerprint") or ""),
    }
    return request, prepared


async def collect_open_wave(ctx: Any, *, state_root: Any, task_id: str, wave: Dict[str, Any]) -> str:
    """Collect one open wave: the engine's own resume path over the wave's recorded
    inputs with drain window 0 — settled slots are reconciled, nothing is re-sent,
    nothing is waited for, and the engine remains the sole wave writer/reducer."""
    from ouroboros.tools import plan_review as engine

    exact = engine._authority_wave(state_root, task_id, wave) or wave
    request, prepared = prepared_from_wave(ctx, exact)
    return await engine._run_plan_review_async(ctx, request, collect=prepared)


def collect_wave_sync(ctx: Any, *, state_root: Any, task_id: str, wave: Dict[str, Any]) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Disposition-mode collection: ``(rendered text, reloaded state, authority wave)``."""
    from ouroboros.task_results import load_plan_review_state, plan_review_wave
    from ouroboros.tools import plan_review as engine

    fingerprint = str(wave.get("request_fingerprint") or "")
    text = run_plan_coroutine(collect_open_wave(ctx, state_root=state_root, task_id=task_id, wave=wave))
    state = load_plan_review_state(state_root, task_id)
    stored = plan_review_wave(state, fingerprint) or wave
    return text, state, engine._authority_wave(state_root, task_id, stored) or stored


async def collect_before_supersede(
    ctx: Any, *, state_root: Any, task_id: str, state: Dict[str, Any], fingerprint: str,
) -> Dict[str, Any]:
    """Reconcile-before-supersede (I3): a NEW envelope arriving over an in-flight
    wave first collects what has settled of that wave at $0 (window 0), then the
    caller writes its superseding reference. Returns the (re)loaded state; an
    unreadable old wave is logged and superseded as before."""
    from ouroboros.task_results import current_plan_review_wave, load_plan_review_state

    current = current_plan_review_wave(state)
    if not current or not current.get("custody_pending") or str(current.get("request_fingerprint") or "") == fingerprint:
        return state
    try:
        await collect_open_wave(ctx, state_root=state_root, task_id=task_id, wave=current)
    except (OSError, ValueError) as exc:
        log.warning("in-flight plan wave %s could not be collected before supersede: %s",
                    str(current.get("request_fingerprint") or "")[:8], exc)
        return state
    return load_plan_review_state(state_root, task_id)


def in_flight_hold(state: Dict[str, Any], *, fingerprint: str, cap: Any) -> str:
    """The typed refusal for a REVISED envelope while a wave is still custody-pending
    and the cap has no room for another committed panel, or ``''`` when the envelope
    may proceed. A panel dispatched at the barrier is committed money, but whether it
    SPENDS a cycle is proven only by its collection (a wave of typed $0 refusals leaves
    the cap untouched), so nothing is written here: no superseding reference, no
    cycles_exhausted. The pending wave stays the current, collectible wave and the
    text names its $0 collection. The identical envelope is never held (it resumes)."""
    if cap is None:
        return ""
    pending = [
        w for w in state.get("waves") or []
        if isinstance(w, dict) and w.get("custody_pending") and not w.get("paid")
        and str(w.get("request_fingerprint") or "") != str(fingerprint or "")
    ]
    if not pending or int(state.get("cycles_paid") or 0) + len(pending) < int(cap):
        return ""
    wave = pending[-1]
    fp = str(wave.get("request_fingerprint") or "")
    running = sum(1 for a in wave.get("actors") or []
                  if isinstance(a, dict) and a.get("operation_state") in {"pending_dispatch", "in_flight"})
    return (
        f"ERROR: PLAN_REVIEW_IN_FLIGHT: plan-review wave {fp[:8]} still has {running} reviewer slot(s) "
        f"in flight and the cycle cap ({cap}) has no room for another panel until that wave is collected. "
        f"Collect it at $0 with plan_task(review_disposition={{review_fingerprint: '{fp}', items: []}}) "
        "(a wave that proves no physical dispatch leaves the cap untouched; a paid one spends it), or "
        "resubmit the identical envelope to wait for it. No plan attempt was recorded; the in-flight wave "
        "stays the current wave."
    )


def collect_before_gate(ctx: Any, state: Dict[str, Any]) -> Dict[str, Any]:
    """ONE free collection before a blocking finalization verdict (owner batch 3,
    6e=A): when the current wave still has custody pending, collect what has
    settled at $0 (window 0, never a wait) and return the reloaded state; any
    other state is returned untouched. The call site is the finalization gate
    (``owner_hurry.force_plan_decision``), which then projects the verdict."""
    from ouroboros.task_results import current_plan_review_wave, load_plan_review_state
    from ouroboros.tools.plan_review import _planning_state_location

    current = current_plan_review_wave(state)
    if not current or not current.get("custody_pending"):
        return state
    try:
        state_root, task_id = _planning_state_location(ctx)
        run_plan_coroutine(collect_open_wave(ctx, state_root=state_root, task_id=task_id, wave=current))
        return load_plan_review_state(state_root, task_id)
    except (OSError, ValueError, TimeoutError) as exc:
        log.warning("plan wave %s could not be collected before the gate: %s",
                    str(current.get("request_fingerprint") or "")[:8], exc)
        return state
