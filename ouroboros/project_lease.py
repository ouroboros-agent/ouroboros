"""One-writer-per-project lease helpers (multi-project, v6.32.0).

Pure functions consumed by ``supervisor/workers.py::assign_tasks`` under the
queue lock: a PENDING task whose ``project_id`` is already RUNNING is skipped
this assignment pass (projects serialize internally; parallelism happens
BETWEEN projects and via subagent swarms WITHIN a task).

``project_id == ""`` means "no lane": ordinary unscoped tasks never serialize
against each other. Subagents carry their parent's stored ``project_id`` but
hold no lease of their own — the parent task IS the project's writer and its
swarm must not deadlock against itself, so only top-level (non-subagent)
tasks count as lane occupants.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Set


def _as_task(item: Any) -> Any:
    """Unwrap the supervisor RUNNING meta shape ({"task": {...}, ...}) to the
    task dict; pass a bare task dict through unchanged."""
    if isinstance(item, dict) and isinstance(item.get("task"), dict):
        return item["task"]
    return item


def _task_project_id(task: Any) -> str:
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    return str(task.get("project_id") or "").strip()


def _is_lane_occupant(task: Any) -> bool:
    """Top-level project-scoped tasks occupy the lane; subagents do not."""
    task = _as_task(task)
    if not isinstance(task, dict):
        return False
    if str(task.get("delegation_role") or "") == "subagent":
        return False
    return bool(_task_project_id(task))


def running_project_ids(running: Iterable[Any]) -> Set[str]:
    """Project ids currently holding a writer lease.

    ``running`` is the supervisor's RUNNING mapping values (or any iterable of
    task dicts); read under the queue lock by the caller.
    """
    out: Set[str] = set()
    for task in running or ():
        if _is_lane_occupant(task):
            out.add(_task_project_id(task))
    return out


def candidate_is_leasable(candidate: Dict[str, Any], running_ids: Set[str]) -> bool:
    """True when ``candidate`` may be assigned now under the one-writer rule."""
    if not _is_lane_occupant(candidate):
        return True
    return _task_project_id(candidate) not in running_ids


def task_lane_project_id(running: Any, pending: Any, tid: Any) -> str:
    """The project id the live queue copy of ``tid`` carries right now, "" when the
    task holds no lane or is neither running nor pending.

    Walks the same RUNNING map and PENDING list as ``mark_task_project``, so the two
    can never disagree about where a task's in-memory project id lives. The UI
    conversion reads it under the queue lock right before it marks, so a durable bind
    that is then refused can put the lane back instead of leaving it on a project the
    binding does not name. The caller MUST hold the queue lock."""
    key = str(tid or "")
    if not key:
        return ""
    meta = running.get(key) if hasattr(running, "get") else None
    rtask = _as_task(meta) if isinstance(meta, dict) else None
    if isinstance(rtask, dict):
        return _task_project_id(rtask)
    for item in (pending or ()):
        ptask = _as_task(item)
        if isinstance(ptask, dict) and str(ptask.get("id") or "") == key:
            return _task_project_id(ptask)
    return ""


def mark_task_project(running: Any, pending: Any, tid: Any, pid: Any, *, authority: str = "") -> bool:
    """Set a task's ``project_id`` wherever it currently lives in the supervisor queue
    state — the live RUNNING map (``{tid: {"task": {...}}}``) AND the PENDING list (bare
    task dicts) — so a POST-HOC project conversion/scope makes it a one-writer lane
    occupant whether it has started yet or not. The lease + assignment read
    ``task['project_id']`` from these IN-MEMORY structures (assign_tasks checks the
    pending candidate's own dict, then copies it into RUNNING), NOT the durable bindings —
    so a converted PENDING task that is only bound durably would still start unscoped and
    miss its lane. This is the SSOT for both post-hoc convert paths — the supervisor
    in-task ``ensure_project_scope`` and the UI ``api_project_from_task`` — so they cannot
    drift apart again. The caller MUST hold the queue lock. Returns True if any in-memory
    task dict was updated; a no-op (False) when the task is neither running nor pending
    (then the durable bind alone is correct — there is no live lane to occupy).

    FILL-ONLY by default: a task already carrying a DIFFERENT project keeps it and
    False comes back. The durable binding is the one truth about a task's project
    (owner decision B4=A); this in-memory copy must never be what moves a task
    between projects, which is how a second, empty project acquired a live lane.

    ``authority="binding"`` is the ONE exception, for a conversion that owns the
    durable binding for ``pid`` itself: there the in-memory copy FOLLOWS the truth
    instead of contradicting it. Without it a project-SCOPED but unbound task (a
    bare-workspace promote carries a derived ``proj_<hash>``) converted durably
    while its lane stayed on the old id, so the new project's one-writer lane was
    left free and a concurrent task in it became assignable. That authority also
    carries the rollback: an EMPTY ``pid`` clears the lane, so a conversion whose
    durable bind was refused after the mark can restore the value the lane held
    (including none) instead of leaving it on a project no binding names. Without
    the authority an empty ``pid`` stays the no-op it has always been."""
    key = str(tid or "")
    project = str(pid or "").strip()
    if not key or (not project and authority != "binding"):
        return False
    rows = []
    meta = running.get(key) if hasattr(running, "get") else None
    rtask = _as_task(meta) if isinstance(meta, dict) else None
    if isinstance(rtask, dict):
        rows.append(rtask)
    for item in (pending or ()):
        ptask = _as_task(item)
        if isinstance(ptask, dict) and str(ptask.get("id") or "") == key:
            rows.append(ptask)
    if authority != "binding" and any(
        str(row.get("project_id") or "").strip() not in ("", project) for row in rows
    ):
        return False
    updated = False
    for row in rows:
        row["project_id"] = project
        updated = True
    return updated


__all__ = [
    "candidate_is_leasable",
    "mark_task_project",
    "running_project_ids",
    "task_lane_project_id",
]
