"""Thin per-project journal/workpad tools (multi-project, v6.32.0).

The journal is the project's durable milestone memory (start / blocked /
checkpoint / done / note rows); the workpad is a free-form scratch page. Both
live in the per-project store (``data/projects/<id>/``), which generic data
tools cannot reach (``project_store_access_block``) — these scoped tools are
the only write path, exactly like project knowledge.

Tools resolve the project from the CURRENT task (``ctx.project_id``); an
explicit ``project_id`` argument lets the main-chat agent annotate a specific
project (e.g. when curating from the штаб).
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from typing import Any, Dict, List

from ouroboros.project_facts import (
    project_journal_path,
    project_workpad_path,
    sanitize_project_id,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import (
    append_jsonl,
    jsonl_generation_signature,
    utc_now_iso,
)

log = logging.getLogger(__name__)

_JOURNAL_KINDS = ("start", "checkpoint", "blocked", "done", "note")
_MAX_TEXT_CHARS = 4000
_WORKPAD_MAX_BYTES = 256 * 1024


def _authorized_project_id(ctx: ToolContext, explicit: Any) -> str:
    """AUTHORIZATION (not membership): which project THIS journal write may touch.
    Distinct from project_facts.resolve_project_id (task->project MEMBERSHIP): a
    project-scoped task may only journal into ITS OWN project (no cross-project
    writes); an explicit id is honored only from an unscoped (main/штаб) context,
    where curating a specific project is legitimate. Never consults post-hoc UI
    bindings — only the task's resolved scope (ctx.project_id) + an explicit arg."""
    own = sanitize_project_id(getattr(ctx, "project_id", "") or "")
    requested = sanitize_project_id(explicit) if explicit else ""
    if own:
        return own
    return requested


def _journal_write(ctx: ToolContext, kind: str, text: str, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return ("⚠️ TOOL_ARG_ERROR (journal_write): no project scope — this task is not "
                "project-scoped and no explicit project_id was given.")
    kind_norm = str(kind or "note").strip().lower()
    if kind_norm not in _JOURNAL_KINDS:
        return f"⚠️ TOOL_ARG_ERROR (journal_write): kind must be one of {_JOURNAL_KINDS}"
    body = str(text or "").strip()
    if not body:
        return "⚠️ TOOL_ARG_ERROR (journal_write): text is required"
    # The journal is durable cognitive memory — never silently slice a stored
    # entry. Reject over-limit writes (same contract as workpad_write) so the
    # agent shortens the milestone or moves detail to the workpad/knowledge.
    if len(body) > _MAX_TEXT_CHARS:
        return (f"⚠️ TOOL_ARG_ERROR (journal_write): entry exceeds {_MAX_TEXT_CHARS} chars "
                f"({len(body)}) — a journal entry is a milestone note; keep it short and "
                "move long detail to workpad_write or knowledge_write.")
    path = project_journal_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": utc_now_iso(),
        "kind": kind_norm,
        "text": body,
        "task_id": str(getattr(ctx, "task_id", "") or ""),
    }
    append_jsonl(path, row)
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.projects_registry import touch_project

        # Registry lives on the CANONICAL data dir (like project_journal_path),
        # not a forked child drive — touching ctx.drive_root would scatter
        # stray projects.json files onto subagent worktrees.
        touch_project(pathlib.Path(DATA_DIR), pid)
    except Exception:
        log.debug("journal touch_project failed", exc_info=True)
    return f"OK: journal[{pid}] += {kind_norm} entry ({len(body)} chars)."


def append_journal_milestone(
    project_id: str, kind: str, text: str, task_id: str = "", extra: dict | None = None,
) -> None:
    """Append an AUTOMATIC project journal milestone (e.g. task-completion 'letters
    home'), enforcing the SAME durable per-row contract as the journal_write tool.

    The tool REJECTS over-limit input (it teaches the agent to keep milestones
    short); an automatic milestone MUST be recorded, so when the composed text
    exceeds ``_MAX_TEXT_CHARS`` it is bounded with a VISIBLE pointer instead of
    being silently sliced or dropped (the full text always survives in the task's
    task_results and the consciousness digest). Centralizing here keeps every
    project-journal append on one bounded path (no raw append_jsonl elsewhere)."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return
    raw_kind = str(kind or "").strip().lower()
    kind_norm = raw_kind or "note"
    if kind_norm not in _JOURNAL_KINDS:
        # Fail LOUD but never LOSE the entry: an explicitly-passed unknown kind is a caller
        # bug worth surfacing, yet the milestone must still be durably recorded (as a note)
        # rather than silently dropped. (An omitted/empty kind defaults to note quietly.)
        log.warning(
            "append_journal_milestone: unknown kind %r recorded as 'note' (project=%s, task=%s)",
            raw_kind, pid, str(task_id or ""),
        )
        kind_norm = "note"
    body = str(text or "").strip()
    if not body:
        return
    if len(body) > _MAX_TEXT_CHARS:
        keep = _MAX_TEXT_CHARS - 80
        body = body[:keep] + f"… [+{len(body) - keep} chars; full text in this task's task_results / digest]"
    path = project_journal_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, {
        "ts": utc_now_iso(),
        "kind": kind_norm,
        "text": body,
        "task_id": str(task_id or ""),
        # Optional typed payload (e.g. the work-location row's path/sha facts);
        # the reserved row keys always win.
        **{k: v for k, v in dict(extra or {}).items()
           if k not in {"ts", "kind", "text", "task_id"}},
    })
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.projects_registry import touch_project

        touch_project(pathlib.Path(DATA_DIR), pid)
    except Exception:
        log.debug("append_journal_milestone touch_project failed", exc_info=True)


def _record_work_location(project_id: str, task: dict) -> None:
    """Q8: ONE typed "work lives at <path> @ <sha>" row when the finished task's
    effective working tree is NOT the project's registered working_dir (or the
    registry has none). Continuation promotions read the registry/journal —
    without this row an off-registry tree is invisible to every later task (the
    saga rebuilt a finished game because nothing durable said where the first
    build lived). Uses only facts the task record already holds; never spawns git.
    The sha, when present, is the admission-time preflight head — a tree
    identifier, not a claim about the final commit."""
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    workspace = str(task.get("workspace_root") or meta.get("workspace_root") or "").strip()
    if not workspace:
        return
    registered = ""
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.projects_registry import get_project

        registered = str(
            (get_project(pathlib.Path(DATA_DIR), sanitize_project_id(project_id)) or {})
            .get("working_dir") or ""
        ).strip()
    except Exception:
        log.debug("work-location registry lookup failed", exc_info=True)
    try:
        same = bool(registered) and (
            pathlib.Path(registered).resolve(strict=False)
            == pathlib.Path(workspace).resolve(strict=False)
        )
    except (OSError, ValueError):
        same = registered == workspace
    if same:
        return
    preflight = meta.get("workspace_preflight") if isinstance(meta.get("workspace_preflight"), dict) else {}
    git = preflight.get("git") if isinstance(preflight.get("git"), dict) else {}
    sha = str(git.get("head") or "").strip()
    append_journal_milestone(
        project_id,
        "note",
        f"work lives at {workspace}" + (f" @ {sha}" if sha else ""),
        task_id=str(task.get("id") or ""),
        extra={"type": "work_location", "path": workspace, "sha": sha,
               "registered_working_dir": registered},
    )


def record_project_last_result(project_id: str, task_id: str, drive_root: Any) -> None:
    """Stamp the project's durable last-result pointer (read first by
    ``_latest_project_task_result``). THE one writer of that pointer, shared by the
    pooled-task finalization below and the project room's direct-chat root - which
    writes a durable result carrying ``project_id`` but no letters home, so without
    this the per-project fallback for "continue from this result" stayed empty.

    A split-drive task's canonical copy-back may land moments later; the reader
    validates the pointed file and falls back to the scan. Fail-soft."""
    if drive_root is None or not str(task_id or "").strip() or not str(project_id or "").strip():
        return
    try:
        from ouroboros.projects_registry import update_project

        update_project(drive_root, project_id, last_task_result_id=str(task_id))
    except Exception:
        log.debug("project last-task-result pointer update failed", exc_info=True)


def record_task_finalization(
    project_id: str, task: dict, *, objective: str, kind: str, exec_status: str,
    drive_root: Any = None,
) -> None:
    """One seam for a project root's durable "letters home" at finalization:
    the task-finished milestone, the off-registry work-location row (Q8), the
    registry last-result pointer, and — for the swarm ROOT (no parent) — the
    ephemeral tree-ledger coordination mirror (see
    mirror_tree_coordination_to_journal). Fail-soft per row."""
    tid = str(task.get("id") or "")
    try:
        append_journal_milestone(
            project_id, kind, f"Task finished ({exec_status}): {objective}", task_id=tid,
        )
    except Exception:
        log.debug("project journal task-done entry failed", exc_info=True)
    record_project_last_result(project_id, tid, drive_root)
    try:
        _record_work_location(project_id, task)
    except Exception:
        log.debug("project journal work-location entry failed", exc_info=True)
    if not str(task.get("parent_task_id") or "").strip():
        try:
            mirror_tree_coordination_to_journal(
                project_id, str(task.get("root_task_id") or tid), task_id=tid,
            )
        except Exception:
            log.debug("project journal swarm-coordination mirror failed", exc_info=True)


_TREE_MIRROR_KINDS = {
    # task-tree ledger kind -> durable journal kind. Only the high-signal coordination
    # survives: attention beacons + interface contracts. The low-signal coordination
    # (fact/note/decision) and routine progress (milestone/partial_finding) are NOT
    # mirrored — the journal stays a curated durable record, not a tree echo.
    "blocker": "blocked",
    "question": "note",
    "interface_contract": "note",
    "contract": "note",
}


def mirror_tree_coordination_to_journal(project_id: str, root_id: str, task_id: str = "") -> None:
    """F2 (v6.39): mirror the EPHEMERAL task-tree ledger's durable-worthy swarm coordination
    (attention beacons blocker/question/interface_contract + interface contracts) into the
    DURABLE project journal, so a swarm's decisions/blockers survive the tree's GC. Call once
    on the swarm ROOT's terminal (not per sibling) to avoid re-mirroring the same rows.
    Fail-soft and bounded (each row goes through the same per-row journal contract)."""
    pid = sanitize_project_id(project_id)
    rid = str(root_id or "").strip()
    if not pid or not rid:
        return
    try:
        from ouroboros.task_tree_ledger import tree_ledger_rows
        rows = tree_ledger_rows(rid)
    except Exception:
        log.debug("mirror_tree_coordination_to_journal read failed", exc_info=True)
        return
    for r in rows:
        kind = str(r.get("kind") or "").strip().lower()
        journal_kind = _TREE_MIRROR_KINDS.get(kind)
        if not journal_kind:
            continue
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        who = str(r.get("role") or "") or str(r.get("task_id") or "")[:8]
        append_journal_milestone(pid, journal_kind, f"[swarm {kind}] ({who}): {text}", task_id=task_id)


def _journal_snapshot(source: Dict[str, Any], project_id: str) -> str:
    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "source": source,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _journal_snapshot_rows(
    path: pathlib.Path,
    project_id: str,
) -> tuple[List[Dict[str, Any]], str, bool, int]:
    """Capture one stateless journal generation, retrying one concurrent append."""
    rows: List[Dict[str, Any]] = []
    snapshot = ""
    unreadable = 0
    for _attempt in range(2):
        before = jsonl_generation_signature(path)
        rows = []
        unreadable = 0
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    try:
                        line = raw.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        unreadable += 1
                        continue
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        unreadable += 1
                        continue
                    if not isinstance(entry, dict):
                        unreadable += 1
                        continue
                    rows.append(entry)
        except OSError:
            unreadable += 1
        after = jsonl_generation_signature(path)
        snapshot = _journal_snapshot(after, project_id)
        if before and before == after:
            return rows, snapshot, True, unreadable
    return rows, snapshot, False, unreadable


def _journal_read(
    ctx: ToolContext,
    project_id: str = "",
    limit: int = 30,
    offset: int = 0,
    snapshot: str = "",
) -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return ("⚠️ TOOL_ARG_ERROR (journal_read): no project scope — this task is not "
                "project-scoped and no explicit project_id was given.")
    path = project_journal_path(pid)
    if not path.is_file():
        if str(snapshot or "").strip():
            return (
                "JOURNAL_READ_SNAPSHOT_CHANGED: the journal source is no longer "
                "available; no mixed page was returned; restart with offset=0 and no snapshot."
            )
        return f"(journal for project {pid} is empty)"
    try:
        take = max(1, min(int(limit or 30), 200))
    except (TypeError, ValueError):
        take = 30
    try:
        skip = max(0, int(offset or 0))
    except (TypeError, ValueError):
        skip = 0
    rows, current_snapshot, stable, unreadable = _journal_snapshot_rows(path, pid)
    requested_snapshot = str(snapshot or "").strip().lower()
    if not stable:
        return (
            "JOURNAL_READ_SNAPSHOT_CHANGED_DURING_READ: the journal changed while "
            "the page was captured; no mixed page was returned; retry with offset=0 "
            "and no snapshot."
        )
    if requested_snapshot and requested_snapshot != current_snapshot:
        return (
            "JOURNAL_READ_SNAPSHOT_CHANGED: the journal changed after the prior page; "
            "no mixed page was returned; restart with offset=0 and no snapshot."
        )
    valid_total = len(rows)
    total = valid_total + unreadable
    end = max(0, valid_total - skip)
    start = max(0, end - take)
    page = rows[start:end]
    remaining = start
    lines = [
        f"Page: total={total} valid_total={valid_total} unreadable={unreadable} "
        f"returned={len(page)} offset={skip} remaining={remaining} "
        f"remaining_scope=valid_rows coverage={'partial' if unreadable else 'complete'} "
        f"snapshot={current_snapshot}"
    ]
    if unreadable:
        lines.append(
            f"JOURNAL_READ_GAP: coverage is partial; {unreadable} non-empty physical "
            "row(s) were malformed or non-object JSON. Valid rows remain pageable."
        )
    if remaining:
        lines.append(
            "Next older page: "
            f"journal_read(project_id='{pid}', limit={take}, "
            f"offset={skip + len(page)}, snapshot='{current_snapshot}')"
        )
    for row in page:
        lines.append(
            f"[{str(row.get('ts') or '')[:19]}] {str(row.get('kind') or 'note').upper()}: "
            f"{str(row.get('text') or '')}"
        )
    return f"## Project journal ({pid})\n\n" + "\n".join(lines)


def _workpad_read(ctx: ToolContext, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return "⚠️ TOOL_ARG_ERROR (workpad_read): no project scope."
    path = project_workpad_path(pid)
    if not path.is_file():
        return f"(workpad for project {pid} is empty)"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"⚠️ TOOL_ERROR (workpad_read): {exc}"


def _workpad_write(ctx: ToolContext, content: str, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return "⚠️ TOOL_ARG_ERROR (workpad_write): no project scope."
    body = str(content or "")
    if len(body.encode("utf-8", errors="ignore")) > _WORKPAD_MAX_BYTES:
        return ("⚠️ TOOL_ARG_ERROR (workpad_write): workpad exceeds 256KB — keep it a "
                "working page; move durable facts to knowledge_write and history to journal_write.")
    path = project_workpad_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return f"⚠️ TOOL_ERROR (workpad_write): {exc}"
    return f"OK: workpad[{pid}] written ({len(body)} chars)."


def journal_tail_digest(project_id: str, *, limit: int = 40) -> str:
    """Recent project-journal milestones for context injection (no ctx needed).

    Cognitive artifact (BIBLE P1): each milestone is shown in FULL — never
    per-row prefix-sliced. Older entries beyond the tail are represented by a
    VISIBLE index pointer to journal_read (horizon preserved via pointer,
    granularity varies), never silently dropped."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return ""
    path = project_journal_path(pid)
    if not path.is_file():
        return ""
    rows, snapshot, stable, unreadable = _journal_snapshot_rows(path, pid)
    if not rows and not unreadable:
        return ""
    page_size = max(1, int(limit))
    take = rows[-page_size:]
    omitted = len(rows) - len(take)
    lines = [
        f"- [{str(r.get('ts') or '')[:16]}] {str(r.get('kind') or 'note')}: {str(r.get('text') or '')}"
        for r in take
    ]
    if unreadable:
        lines.insert(0, (
            f"- ⚠ coverage partial: {unreadable} unreadable journal row(s); "
            f"inspect valid rows with journal_read(project_id='{pid}')"
        ))
    if omitted:
        if stable:
            lines.insert(0, (
                f"- …[{omitted} earlier milestones; source="
                f"journal_read(project_id='{pid}'); next="
                f"journal_read(project_id='{pid}', limit={page_size}, "
                f"offset={len(take)}, snapshot='{snapshot}') ]"
            ))
        else:
            lines.insert(0, (
                f"- …[{omitted} observed earlier milestones; journal changed during "
                f"capture; restart with journal_read(project_id='{pid}', limit={page_size})]"
            ))
    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    common = {
        "project_id": {
            "type": "string",
            "description": "Explicit project id (defaults to the current task's project scope).",
            "default": "",
        },
    }
    return [
        ToolEntry(
            "journal_write",
            {
                "name": "journal_write",
                "description": (
                    "Append a milestone entry to the current project's durable journal. "
                    "kind: start | checkpoint | blocked | done | note. The journal is the "
                    "project's long-term progress memory (survives task restarts; feeds "
                    "the owner-visible project digest)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(_JOURNAL_KINDS)},
                        "text": {"type": "string", "description": "Milestone text (<=4000 chars)."},
                        **common,
                    },
                    "required": ["kind", "text"],
                },
            },
            lambda ctx, kind, text, project_id="": _journal_write(ctx, kind, text, project_id),
            timeout_sec=15,
        ),
        ToolEntry(
            "journal_read",
            {
                "name": "journal_read",
                "description": "Read the tail of the current project's journal (newest last).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 30, "description": "Max entries (<=200)."},
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Number of newer entries already consumed.",
                        },
                        "snapshot": {
                            "type": "string",
                            "default": "",
                            "description": "Stable cursor returned by the preceding page.",
                        },
                        **common,
                    },
                },
            },
            lambda ctx, limit=30, offset=0, snapshot="", project_id="": _journal_read(
                ctx, project_id, limit, offset, snapshot,
            ),
            timeout_sec=15,
        ),
        ToolEntry(
            "workpad_read",
            {
                "name": "workpad_read",
                "description": "Read the current project's free-form workpad page.",
                "parameters": {"type": "object", "properties": dict(common)},
            },
            lambda ctx, project_id="": _workpad_read(ctx, project_id),
            timeout_sec=15,
        ),
        ToolEntry(
            "workpad_write",
            {
                "name": "workpad_write",
                "description": (
                    "Overwrite the current project's workpad page (<=256KB). A working "
                    "page for plans/links/state — durable facts belong in knowledge_write, "
                    "history in journal_write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        **common,
                    },
                    "required": ["content"],
                },
            },
            lambda ctx, content, project_id="": _workpad_write(ctx, content, project_id),
            timeout_sec=15,
        ),
    ]


__all__ = ["get_tools", "journal_tail_digest"]
