"""Shared nanny-verb helpers: the typed refusal, the custody-rooted emit, and
run-ownership resolution.

Extracted from ``ouroboros/tools/delegate.py`` to break the import cycle the
delegate split left behind: ``delegate_interactions`` imported these three
helpers back from the facade (``tools/delegate`` → ``delegate_interactions`` →
``tools/delegate``), and the house seam pattern is one-way — an extracted
module never imports the facade back. ``tools.delegate`` re-exports all three,
so every existing reference and monkeypatch target keeps the same objects.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, Mapping, Optional, Tuple

from ouroboros import delegate_custody as custody
from ouroboros.delegate_custody import RunCustody as _RunCustody
from ouroboros.tools.registry import ToolContext

log = logging.getLogger(__name__)


def _fail(tool: str, code: str, detail: str, **extra: Any) -> str:
    payload = {"status": "refused", "tool": tool, "reason": code, "detail": detail, **extra}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _emit(ctx: ToolContext, kind: str, payload: Dict[str, Any]) -> None:
    custody.emit(custody.custody_root(ctx), kind, {
        "task_id": str(getattr(ctx, "task_id", "") or ""), **payload,
    })


def _owned_run(ctx: ToolContext, tool: str, run_id: str) -> Tuple[Optional[str], Optional[_RunCustody]]:
    """Resolve custody for a run, or return a typed refusal payload.

    The daemon bearer token grants the ENTIRE Claudexor API, so a run id is not a
    capability the way a file descriptor is — anything that can name a run can reach it,
    read it, or CANCEL it, and cancelling a reviewer destroys the verdict that was the
    point of running it. Ownership is therefore replayed from the durable start row:
    a restarted worker keeps its runs, and an id with NO durable record is UNKNOWN
    (refused as unresolvable), which is a different fact from a run that demonstrably
    belongs to someone else.
    """
    status, entry = custody.lookup(custody.custody_root(ctx), str(getattr(ctx, "task_id", "") or ""), run_id)
    if status == custody.UNKNOWN:
        return _fail(tool, "run_ownership_unknown",
                     "No durable record of that run id exists on this drive, so ownership "
                     "cannot be established. Unknown ownership is refused, not waved through.",
                     run_id=run_id,
                     hint="The run may belong to a different drive or the id may be "
                          "mistyped; get_task_result(<task_id>) is the ownership-free "
                          "way to read another task's delegated-run outcome."), None
    if status == custody.FOREIGN:
        # The refusal stays a refusal; the additive facts give the caller the
        # two things it needs to stop being stuck — the run is over, and whom
        # to ask (get_task_result(owner_task_id) is the legitimate cross-task
        # read that already carries the delegated_runs_* counters).
        return _fail(tool, "run_not_owned",
                     "That run belongs to another task. A delegated run may only be "
                     "waited on or cancelled by the task that started it.",
                     run_id=run_id,
                     owner_task_id=str(getattr(entry, "task_id", "") or ""),
                     run_settled=bool(getattr(entry, "settled", False)),
                     run_terminal_state=str(getattr(entry, "terminal_state", "") or "")), None
    return None, entry


def orphan_disposition_status(
    ctx: ToolContext, drive: Any, run_id: str,
) -> Tuple[str, Optional[_RunCustody], str]:
    """Custody for a DISPOSITION, with the orphan rule applied.

    An obligation is held by the run's durable rows, not by the task that
    created them. While the owning task is LIVE, only that identity may decide
    its captured patch. Once that task is terminal, a live TOP-LEVEL task may
    apply or reject the orphan; every apply-path guard still runs unchanged
    (recorded-target match, protected paths, proven drift, the whole-payload
    CAS), and the PATCH_DISPOSED row records who wrote it.

    This is a DISPOSITION-ONLY upgrade. ``_owned_run`` governs wait/cancel/
    answer and is deliberately NOT widened: cancelling or answering a foreign
    run destroys work instead of closing an obligation.

    Returns ``(status, entry, orphan_of)``, where ``orphan_of`` is the terminal
    owner's task id when the upgrade applied and "" otherwise.
    """
    from ouroboros.delegate_terminal import _task_is_terminal
    from ouroboros.tool_access import _TOP_LEVEL_PRINCIPAL_PROFILES, active_tool_profile

    status, entry = custody.lookup(drive, str(getattr(ctx, "task_id", "") or ""), run_id)
    if (status == custody.FOREIGN and entry is not None
            and entry.settled and not entry.patch_disposed
            and str(active_tool_profile(ctx)) in _TOP_LEVEL_PRINCIPAL_PROFILES
            and _task_is_terminal(drive, entry.task_id)):
        return custody.OWNED, entry, str(entry.task_id or "")
    return status, entry, ""


def orphan_apply_target_ok(target: Any, active_root: Any) -> bool:
    """May a disposition APPLY a run recorded against ``target`` from ``active_root``?

    The nanny's own run still needs exact equality. An ORPHAN of a terminal
    owner may additionally apply into a target NESTED inside the caller's
    active root, provided BOTH live under ``get_subagent_projects_root()`` —
    the host-minted project area. That is the aggregator shape the host itself
    creates: a swarm fans into ``<project>/contributions/<track>`` clones of
    the very tree the parent works in, and the host already checkpoint-commits
    exactly those descendants (``coop_checkpoint._task_tree_coop_roots``), so
    the widened set adds no tree the host was not already writing to. An
    owner-attached folder never lives there, so this can never reach one.

    ONE predicate for the apply gate, the health invariant, and the tool
    description: a rule stated three ways drifts into three rules.
    """
    from ouroboros.config import get_subagent_projects_root
    from ouroboros.tool_access import path_is_relative_to

    try:
        target_path = pathlib.Path(str(target or "")).expanduser().resolve(strict=False)
        root_path = pathlib.Path(str(active_root or "")).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return False
    if not str(target or "").strip() or not str(active_root or "").strip():
        return False
    if target_path == root_path:
        return True
    projects_root = pathlib.Path(get_subagent_projects_root()).expanduser().resolve(strict=False)
    return (path_is_relative_to(target_path, root_path)
            and path_is_relative_to(target_path, projects_root)
            and path_is_relative_to(root_path, projects_root))


def orphan_capture_read_target(
    ctx: ToolContext, candidate: Any, *,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> Optional[pathlib.Path]:
    """READ anchor for the capture of a TERMINAL OWNER's orphan, or None.

    ``artifacts.delegated_capture_read_target`` rebinds ``artifact_store``
    reads for the caller's OWN ``delegated_runs/`` prefix only, so the task the
    orphan rule already authorizes to APPLY a foreign capture could not READ
    it: the sanctioned recovery root got ``outside selected root=artifact_store``
    twice on the very patch it was told to dispose. Authority is not widened
    here -- ``orphan_disposition_status`` remains the single decision, and this
    only lets the actor that MAY apply also read.

    Resolution is by PATH SHAPE first: a candidate must sit under
    ``<canonical data root>/task_results/artifacts/<owner_tid>/delegated_runs/
    <capture>/``. Everything else returns before custody is touched, so a plain
    refusal never pays for an event-log replay. The custody row is read from a
    SHARED ``delegate_terminal.custody_audit_snapshot`` when the caller has one,
    and only without one does this replay -- exactly once.
    """
    from ouroboros.artifacts import DELEGATED_CAPTURE_PREFIX
    from ouroboros.headless import ARTIFACTS_DIR
    from ouroboros.tool_access import canonical_data_root, path_is_relative_to

    try:
        resolved = pathlib.Path(candidate).expanduser().resolve(strict=False)
        artifacts_root = (pathlib.Path(canonical_data_root(ctx)) / ARTIFACTS_DIR).resolve(strict=False)
        parts = resolved.relative_to(artifacts_root).parts
    except (OSError, TypeError, ValueError):
        return None
    if len(parts) < 3 or parts[1] != DELEGATED_CAPTURE_PREFIX:
        return None
    owner_tid, capture_name = parts[0], parts[2]
    if not owner_tid or owner_tid == str(getattr(ctx, "task_id", "") or ""):
        return None  # the caller's OWN prefix is delegated_capture_read_target's job
    drive = custody.custody_root(ctx)
    state = (snapshot or {}).get("state")
    rows = (custody.undisposed_patches(drive, state=state) if state is not None
            else custody.undisposed_patches(drive))
    for row in rows:
        if str(row.task_id or "") != owner_tid:
            continue
        cap_dir = custody.delegated_capture_dir(drive, row.task_id, row.snapshot_id or row.run_id)
        if cap_dir.name != capture_name or not path_is_relative_to(resolved, cap_dir):
            continue
        # The rows we just read ARE the durable authority; seeding the memo the
        # ownership lookup consults keeps this read at the traversal budget
        # above instead of replaying the same log a second time.
        custody._CUSTODY.setdefault(str(row.run_id), row)
        status, _entry, orphan_of = orphan_disposition_status(ctx, drive, str(row.run_id))
        if status == custody.OWNED and orphan_of:
            return resolved
    return None


__all__ = ["_emit", "_fail", "_owned_run", "orphan_apply_target_ok",
           "orphan_capture_read_target", "orphan_disposition_status"]
