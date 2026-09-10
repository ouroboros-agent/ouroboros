"""Headless task helpers for CLI/workspace runs.

The gateway owns task transport; this module owns the small amount of local
filesystem state needed for isolated external runs and patch artifacts.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import shutil
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple  # noqa: F401

from ouroboros.contracts.task_constraint import normalize_task_constraint  # noqa: F401
from ouroboros.post_task_checkpoint import project_replica_task_result_fields
from ouroboros.task_results import (
    cancellation_blocks_child_result, load_task_result, validate_task_id, write_task_result,
)
from ouroboros.utils import atomic_write_json, utc_now_iso
from ouroboros.headless_status import (  # noqa: F401
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_FINALIZING,
    ARTIFACT_STATUS_MISSING,
    ARTIFACT_STATUS_PENDING,
    ARTIFACT_STATUS_READY,
    ARTIFACT_STATUS_READY_NO_CHANGES,
    ARTIFACT_STATUS_READY_WITH_CHANGES,
    ARTIFACT_TERMINAL_STATUSES,
    _ARTIFACT_LIFECYCLE_FIELDS,
    _FINAL_STATUSES,
    _LOCAL_READONLY_SUBAGENT_MODE,
)
from ouroboros.workspace_patch_capture import (  # noqa: F401
    SCRATCH_MANIFEST_NAME,
    _GIT_UNBORN_HEAD,
    _acting_constraint_from_task,
    _append_git_output,
    _empty_patch_manifest,
    _git_bytes,
    _git_empty_tree_oid,
    _git_path_list,
    _git_stdout,
    _head_reflog_exists,
    _looks_like_git_oid,
    _preflight_head_from_task,
    _preflight_head_present,
    _untracked_blob_exclude_reason,
    _workspace_patch_base,
    _write_patch_separator,
    build_workspace_patch,
    untracked_capture_veto_reason,
    write_workspace_patch_artifacts,
)

log = logging.getLogger(__name__)


class _RefPublicationChanged(Exception):
    """Retry preparation outside the result lock against the new CURRENT refs."""


HEADLESS_TASKS_DIR = pathlib.Path("state") / "headless_tasks"
ARTIFACTS_DIR = pathlib.Path("task_results") / "artifacts"
TASK_DRIVES_DIR = pathlib.Path("task_drives")


# Pure patch rules keep their original identities on this facade; file I/O and
# untracked_capture_veto_reason remain owned by workspace_patch_capture.
from ouroboros.workspace_patch_rules import (  # noqa: F401
    _ANY_SEGMENT_EXCLUDE_DIRS,
    _LOCKFILE_MANIFESTS,
    _PATCH_EXCLUDE_RULES_VERSION,
    _PATCH_JUNK_RE,
    _PATCH_MAX_UNTRACKED_FILE_BYTES,
    _SENSITIVE_EXAMPLE_SUFFIXES,
    _SENSITIVE_FILENAMES,
    _SENSITIVE_KEY_NAMES,
    _TOP_LEVEL_EXCLUDE_DIRS,
    _incidental_lockfile_excludes,
    _lockfile_manifest_for,
    _patch_exclude_reason,
    _sensitive_untracked_reason,
)


def task_state_dir(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / HEADLESS_TASKS_DIR / validate_task_id(task_id)


def task_artifacts_dir(drive_root: pathlib.Path, task_id: str, *, create: bool = True) -> pathlib.Path:
    path = pathlib.Path(drive_root) / ARTIFACTS_DIR / validate_task_id(task_id)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def write_workspace_preflight_artifact(
    parent_drive_root: pathlib.Path,
    task_id: str,
    preflight: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist the full workspace preflight report as a task artifact."""

    artifact_dir = task_artifacts_dir(parent_drive_root, task_id)
    path = artifact_dir / "workspace_preflight.json"
    atomic_write_json(path, preflight, trailing_newline=True)
    raw = path.read_bytes() if path.exists() else b""
    return {
        "kind": "workspace_preflight",
        "name": "workspace_preflight.json",
        "path": str(path),
        "size": len(raw),
        "sha256": sha256(raw).hexdigest() if raw else "",
        "workspace_root": str(preflight.get("workspace_root") or ""),
    }


def prepare_task_drive(parent_drive_root: pathlib.Path, task_id: str, memory_mode: str,
                       project_id: str = "") -> Optional[pathlib.Path]:
    """Create a forked or empty execution drive; other modes keep the parent.

    Forks copy identity/world/registry and global knowledge except on Project
    tasks, which copy only the shared patterns file. Memory initializes empties.
    """

    mode = str(memory_mode or "shared").strip().lower()
    if mode not in {"forked", "empty"}:
        return None

    task_id = validate_task_id(task_id)
    parent = pathlib.Path(parent_drive_root)
    child = task_state_dir(parent, task_id) / "data"
    child.mkdir(parents=True, exist_ok=True)
    for rel in ("memory", "logs", "state", "task_results"):
        (child / rel).mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        child / "state" / "state.json",
        {
            "schema_version": 1,
            "headless_task_id": str(task_id),
            "memory_mode": mode,
            "created_at": utc_now_iso(),
        },
        trailing_newline=True,
    )
    if mode == "forked":
        _copy_stable_memory(parent, child, project_id=str(project_id or "").strip())
    return child


def _resolve_retention_days(retention_days: Optional[int]) -> int:
    """Only None reads the owner knob; explicit days reach age_cutoff unchanged.
    That shared owner floors at zero, which prunes everything before now."""
    from ouroboros.retention import get_gc_retention_days

    if retention_days is None:
        return get_gc_retention_days()
    return retention_days


def _timestamp_from_result(result: Dict[str, Any], fallback: float) -> float:
    for key in ("artifact_finalized_at", "completed_at", "finished_at", "ts"):
        raw = str(result.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return float(parsed.timestamp())
        except ValueError:
            continue
    return fallback


def _effective_task_result(parent: pathlib.Path, task_id: str) -> Dict[str, Any]:
    """Prune reads the projected result; a projection failure reads canonical."""
    try:
        from ouroboros.task_status import load_effective_task_result

        return load_effective_task_result(parent, task_id) or {}
    except Exception:
        return load_task_result(parent, task_id) or {}


def _live_unpromoted_child_refs(
    promotion: Any, expected_child: pathlib.Path,
) -> List[Dict[str, Any]]:
    if not isinstance(promotion, dict):
        return []
    try:
        version = int(promotion.get("schema_version") or 0)
    except (TypeError, ValueError):
        return []
    if version < 1:
        return []
    if str(promotion.get("status") or "") == "complete":
        return []
    child_root = expected_child.resolve(strict=False)
    live: List[Dict[str, Any]] = []
    for row in promotion.get("pending_refs") or []:
        if not isinstance(row, dict) or not row.get("path"):
            continue
        try:
            path = pathlib.Path(str(row["path"])).resolve(strict=False)
            path.relative_to(child_root)
        except (OSError, ValueError):
            continue
        if path.is_file():
            live.append(dict(row))
    return live


def prune_headless_task_drives(
    parent_drive_root: pathlib.Path,
    *,
    retention_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort startup prune for copied-back terminal child drives."""

    from ouroboros.retention import age_cutoff

    parent = pathlib.Path(parent_drive_root)
    base = parent / HEADLESS_TASKS_DIR
    days = _resolve_retention_days(retention_days)
    cutoff = age_cutoff(days, now)
    report: Dict[str, Any] = {
        "retention_days": days,
        "scanned": 0,
        "pruned": [],
        "skipped": [],
        "errors": [],
        "promotion_retry": {},
    }
    if not base.is_dir():
        return report
    from ouroboros.observability import retry_pending_child_ref_promotions

    report["promotion_retry"] = retry_pending_child_ref_promotions(parent)
    for task_dir in sorted(base.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        report["scanned"] += 1
        try:
            validate_task_id(task_id)
            dir_mtime = task_dir.stat().st_mtime
            result = _effective_task_result(parent, task_id)
            status = str(result.get("status") or "").lower()
            if status not in _FINAL_STATUSES:
                report["skipped"].append({"task_id": task_id, "reason": "parent_not_terminal", "status": status})
                continue
            artifact_status = str(result.get("artifact_status") or "").lower()
            if artifact_status and artifact_status not in ARTIFACT_TERMINAL_STATUSES:
                report["skipped"].append({"task_id": task_id, "reason": "artifacts_not_terminal", "artifact_status": artifact_status})
                continue
            retention_ts = _timestamp_from_result(result, dir_mtime)
            if retention_ts > cutoff:
                report["skipped"].append({"task_id": task_id, "reason": "younger_than_retention"})
                continue
            expected_child = str((task_dir / "data").resolve(strict=False))
            known_child = str(
                result.get("child_drive_root")
                or result.get("headless_child_drive_root")
                or result.get("drive_root")
                or ""
            ).strip()
            if known_child and str(pathlib.Path(known_child).resolve(strict=False)) != expected_child:
                report["skipped"].append({"task_id": task_id, "reason": "child_drive_mismatch"})
                continue
            live_unpromoted = _live_unpromoted_child_refs(
                result.get("child_ref_promotion"), pathlib.Path(expected_child),
            )
            if live_unpromoted:
                report["skipped"].append({
                    "task_id": task_id,
                    "reason": "child_refs_unpromoted",
                    "pending_ref_count": len(live_unpromoted),
                })
                continue
            shutil.rmtree(task_dir)
            report["pruned"].append({"task_id": task_id, "path": str(task_dir)})
        except Exception as exc:
            report["errors"].append({"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"})
    return report


def prune_task_drives(
    parent_drive_root: pathlib.Path,
    *,
    retention_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort startup prune for direct-task scratch drives."""

    from ouroboros.retention import age_cutoff

    parent = pathlib.Path(parent_drive_root)
    base = parent / TASK_DRIVES_DIR
    days = _resolve_retention_days(retention_days)
    cutoff = age_cutoff(days, now)
    report: Dict[str, Any] = {"retention_days": days, "scanned": 0, "pruned": [], "skipped": [], "errors": []}
    if not base.is_dir():
        return report
    from ouroboros.observability import retry_pending_child_ref_promotions
    retry_pending_child_ref_promotions(parent)
    for task_dir in sorted(base.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        report["scanned"] += 1
        try:
            validate_task_id(task_id)
            dir_mtime = task_dir.stat().st_mtime
            result = _effective_task_result(parent, task_id)
            status = str(result.get("status") or "").lower()
            if status not in _FINAL_STATUSES:
                report["skipped"].append({"task_id": task_id, "reason": "task_not_terminal", "status": status})
                continue
            if _live_unpromoted_child_refs(result.get("child_ref_promotion"), task_dir):
                report["skipped"].append({"task_id": task_id, "reason": "unpromoted_child_refs"})
                continue
            if _timestamp_from_result(result, dir_mtime) > cutoff:
                report["skipped"].append({"task_id": task_id, "reason": "younger_than_retention"})
                continue
            shutil.rmtree(task_dir)
            report["pruned"].append({"task_id": task_id, "path": str(task_dir)})
        except Exception as exc:
            report["errors"].append({"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"})
    return report


def prune_task_trees(
    parent_drive_root: pathlib.Path,
    *,
    retention_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Prune ephemeral task-tree ledgers once the root is terminal or absent and
    older than GC retention; durable project memory is outside this plane."""

    from ouroboros.retention import age_cutoff

    parent = pathlib.Path(parent_drive_root)
    base = parent / "task_trees"
    days = _resolve_retention_days(retention_days)
    cutoff = age_cutoff(days, now)
    report: Dict[str, Any] = {"retention_days": days, "scanned": 0, "pruned": [], "skipped": [], "errors": []}
    if not base.is_dir():
        return report
    for tree_dir in sorted(base.iterdir()):
        if not tree_dir.is_dir():
            continue
        root_id = tree_dir.name
        report["scanned"] += 1
        try:
            dir_mtime = tree_dir.stat().st_mtime
            result = _effective_task_result(parent, root_id)
            status = str(result.get("status") or "").lower()
            if status and status not in _FINAL_STATUSES:
                report["skipped"].append({"root_task_id": root_id, "reason": "root_not_terminal", "status": status})
                continue
            if _timestamp_from_result(result, dir_mtime) > cutoff:
                report["skipped"].append({"root_task_id": root_id, "reason": "younger_than_retention"})
                continue
            shutil.rmtree(tree_dir)
            report["pruned"].append({"root_task_id": root_id, "path": str(tree_dir)})
        except Exception as exc:
            report["errors"].append({"root_task_id": root_id, "error": f"{type(exc).__name__}: {exc}"})
    return report


def remove_subagent_task_drive(parent_drive_root: pathlib.Path, task_id: str) -> bool:
    """Remove scratch after settled copyback/salvage, preserving completion wins.
    Pending live refs retain their source. Returns whether any drive was removed.
    """
    parent = pathlib.Path(parent_drive_root)
    try:
        validate_task_id(task_id)
    except Exception:
        return False
    headless_base = parent / HEADLESS_TASKS_DIR / task_id
    task_drive_base = parent / TASK_DRIVES_DIR / task_id
    try:
        result = load_task_result(parent, task_id) or {}
        promotion = result.get("child_ref_promotion")
        if (
            _live_unpromoted_child_refs(promotion, headless_base / "data")
            or _live_unpromoted_child_refs(promotion, task_drive_base)
        ):
            return False
    except Exception:
        # Legacy/no-metadata cleanup behavior remains fail-soft. Newly produced
        # valid metadata is parsed by the helper without raising.
        pass
    bases = (headless_base, task_drive_base)
    removed = False
    for base in bases:
        try:
            if base.is_dir():
                shutil.rmtree(base)
                removed = True
        except Exception:
            log.debug("Failed to remove subagent task drive %s", base, exc_info=True)
    return removed


def copy_child_task_result(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Copy a child-drive task result back to the parent data root."""
    from ouroboros.observability import child_ref_promotion_scope
    with child_ref_promotion_scope():
        task_id = str(task.get("id") or "")
        if not task_id:
            return None
        canonical_existing = load_task_result(parent_drive_root, task_id) or {}
        # Cancellation is authoritative before any child-root read or artifact copy.
        if cancellation_blocks_child_result(canonical_existing):
            return canonical_existing
        child_drive = _child_drive_from_task(task)
        if child_drive is None:
            return None
        child_result = load_task_result(child_drive, task_id)
        if not isinstance(child_result, dict):
            return None
        # Bulk refs precede publication/GC; review refs first select CURRENT below.
        from ouroboros.observability import promote_child_task_refs

        review_fields = {key: value for key, value in child_result.items() if key == "review_projection"}
        child_result, ref_promotion = promote_child_task_refs(
            pathlib.Path(parent_drive_root), child_drive, task_id,
            {key: value for key, value in child_result.items() if key != "review_projection"})
        child_result.update(review_fields)
        _publish_child_verification_receipts(parent_drive_root, task_id, child_drive)
        child_status = str(child_result.pop("status", None) or "completed")
        child_result.pop("task_id", None)
        child_result["child_ref_promotion"] = ref_promotion
        if isinstance(child_result.get("artifacts"), list):
            try:
                from ouroboros.outcomes import artifact_bundle_from_result
                child_result["artifact_bundle"] = artifact_bundle_from_result(child_result)
            except Exception:
                child_result.pop("artifact_bundle", None)
        child_result.setdefault("headless_child_drive_root", str(child_drive))
        if (child_status in _FINAL_STATUSES and _workspace_root_from_task(task) is not None
                and not task_is_readonly_subagent(task)):
            artifact_status = str(canonical_existing.get("artifact_status") or "").strip().lower()
            if artifact_status in ARTIFACT_TERMINAL_STATUSES | {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
                child_result["artifacts"] = _merge_artifacts(
                    list(canonical_existing.get("artifacts") or []), list(child_result.get("artifacts") or []))
                child_result.update({key: canonical_existing[key] for key in _ARTIFACT_LIFECYCLE_FIELDS
                                if key in canonical_existing})
            else:
                child_result["artifact_status"] = ARTIFACT_STATUS_FINALIZING
            child_result["child_status"] = child_status

        return retry_child_task_refs(parent_drive_root, child_drive, task_id,
                                     replica={**child_result, "status": child_status})


def retry_child_task_refs(parent: pathlib.Path, child: pathlib.Path, task_id: str,
                          *, replica: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One optimistic publisher for pending CURRENT refs and prepared copyback.

    Normal copyback supplies its already-copied replica; only its selected review
    still needs I/O. Retry has no replica and never reads an old child body. A
    changed CURRENT basis repeats preparation under the same file-I/O memo.
    """
    from ouroboros.observability import (
        _has_pending_ref_promotion, _rewrite_child_ref_tree,
        child_ref_promotion_scope, promote_child_task_ref_patch,
    )
    with child_ref_promotion_scope():
        while True:
            source = load_task_result(parent, task_id, strict=True) or {}
            if replica is None and not source:
                raise ValueError("pending child-ref authority is missing")
            if replica is None:
                if not _has_pending_ref_promotion(source.get("child_ref_promotion")):
                    return source
                patch = promote_child_task_ref_patch(parent, child, task_id, source)
                basis = {key: source.get(key) for key in patch}
            else:
                source = {**source, **project_replica_task_result_fields(source, replica)}
                review = source.get("review_projection")
                promotion = copy.deepcopy(replica["child_ref_promotion"])
                prepared = _rewrite_child_ref_tree(review, pathlib.Path(parent), child, task_id, promotion)
                if promotion["pending_refs"]:
                    promotion["status"] = "incomplete"
                patch = {"child_ref_promotion": promotion}
                if isinstance(review, dict):
                    patch["review_projection"] = prepared
                basis = {"review_projection": review}

            def project(current: dict, _incoming: dict) -> dict:
                selected = {**current, **project_replica_task_result_fields(current, replica)} if replica is not None else current
                if {key: selected.get(key) for key in basis} != basis:
                    raise _RefPublicationChanged()
                return {**(selected if replica is not None else {}), **patch, "status": selected["status"]}

            try:
                return write_task_result(parent, task_id, source["status"],
                                         _field_projector=project, strict_existing_dict=True,
                                         **{key: value for key, value in (replica or {}).items() if key != "status"})
            except _RefPublicationChanged:
                continue


def _child_result_adopted(child: pathlib.Path, result: Dict[str, Any]) -> bool:
    promotion = result.get("child_ref_promotion")
    source = str(result.get("headless_child_drive_root") or "")
    return bool(
        "result" in result and isinstance(promotion, dict)
        and type(promotion.get("schema_version")) is int and promotion["schema_version"] == 1
        and promotion.get("status") in {"complete", "incomplete"}
        and source and pathlib.Path(source).resolve(strict=False) == child.resolve(strict=False)
    )


def terminal_task_files_ready(canonical_root: pathlib.Path, task: Dict[str, Any], result: Any) -> bool:
    """Read-only CURRENT readiness, not proof that every ref is available.
    Split tasks require adopted body facts, not an early terminal checkpoint.
    """
    task_id = str(task.get("id") or task.get("task_id") or "")
    if not isinstance(result, dict) or not task_id or str(result.get("task_id") or "") != task_id:
        return False
    if str(result.get("status") or "") not in _FINAL_STATUSES:
        return False
    if cancellation_blocks_child_result(result):
        return True
    try:
        child = _child_drive_from_task(task)
        if child is not None and child.resolve(strict=False) != pathlib.Path(canonical_root).resolve(strict=False):
            if not _child_result_adopted(child, result):
                return False
    except (OSError, ValueError, RuntimeError):
        return False
    return not (
        _workspace_root_from_task(task) is not None and not task_is_readonly_subagent(task)
        and str(result.get("artifact_status") or "") in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}
    )


def prepare_terminal_task_files(canonical_root: pathlib.Path, task: Dict[str, Any]) -> Dict[str, Any]:
    """Finish one file-save attempt without turning an I/O failure into a worker crash.

    The dispatcher re-reads CURRENT; returned result is diagnostic. Pending refs
    keep existing custody. terminal_source_present is True after a strict terminal
    read, False for absence/nonterminal, None for unknown; later write failure
    preserves that observation and never manufactures a completed source.
    """
    task_id = str(task.get("id") or task.get("task_id") or "")
    report: Dict[str, Any] = {"task_id": task_id, "result": None, "error": "", "terminal_source_present": None}
    try:
        root = pathlib.Path(canonical_root)
        current = load_task_result(root, task_id, strict=True) or {}
        child = _child_drive_from_task(task)
        cancelled = cancellation_blocks_child_result(current)
        adopted = not cancelled and child is not None and _child_result_adopted(child, current)
        source = current
        if child is not None and not adopted and not cancelled:
            source = load_task_result(child, task_id, strict=True)
        report["terminal_source_present"] = bool(source and str(source.get("status") or "") in _FINAL_STATUSES)
        if not report["terminal_source_present"]:
            raise ValueError("terminal task source is missing or not settled")
        if adopted and child is not None:
            current = retry_child_task_refs(root, child, task_id)
        elif child is not None and not cancelled:
            current = copy_child_task_result(root, {**task, "id": task_id}) or current
        if str(current.get("status") or "") not in _FINAL_STATUSES:
            raise ValueError("terminal task result is missing or not settled")
        if not task_is_readonly_subagent(task) and current.get("artifact_status") not in ARTIFACT_TERMINAL_STATUSES:
            finalize_task_artifacts(root, {**task, "id": task_id})
        report["result"] = load_task_result(root, task_id, strict=True)
    except Exception as exc:
        from ouroboros.observability import redact_projection
        report["error"] = str(redact_projection(f"{type(exc).__name__}: {exc}").value)
        log.warning("Terminal file preparation failed for %s: %s", task_id, report["error"])
        # Only a real terminal source may stamp an artifact failure; absent/nonterminal source follows crash recovery.
        if report["terminal_source_present"] is not True:
            return report
        try:
            existing = load_task_result(canonical_root, task_id, strict=True)
            if existing:
                from ouroboros.outcomes import artifact_bundle_from_result
                fields = dict(artifact_status=ARTIFACT_STATUS_FAILED, artifact_error=report["error"],
                              artifact_finalized_at=utc_now_iso())
                provisional = {**existing, **fields}
                provisional.pop("artifact_bundle", None)
                fields["artifact_bundle"] = artifact_bundle_from_result(provisional)
                report["result"] = write_task_result(
                    canonical_root, task_id, existing["status"],
                    _field_projector=lambda live, fields: {**fields, "status": live["status"]},
                    strict_existing_dict=True, **fields,
                )
        except Exception:
            log.warning("Terminal file failure could not be persisted for %s", task_id, exc_info=True)
    return report


def _publish_child_verification_receipts(
    parent_drive_root: pathlib.Path, task_id: str, child_drive: pathlib.Path
) -> None:
    """Union child verification receipts with canonical delegation_zero_run rows.
    Exact-row dedup makes re-entry idempotent; failures log without blocking."""
    try:
        from ouroboros.outcome_receipt_store import publish_verification_receipt_union

        publish_verification_receipt_union(
            parent_drive_root, task_id, child_drive,
        )
    except Exception:
        log.warning("Failed to publish child receipts for task %s", task_id, exc_info=True)


def _copy_child_artifacts_to_parent(
    parent_drive_root: pathlib.Path,
    task_id: str,
    child_drive: pathlib.Path,
    artifacts: List[Dict[str, Any]],
    *, promotion: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Verify/rebase files; failed copy-back shares the existing ref custody/GC."""
    from ouroboros.artifacts import copy_artifact_file
    from ouroboros.outcome_receipt_store import is_verification_receipts_path

    parent_dir = task_artifacts_dir(parent_drive_root, task_id)
    rebased: List[Dict[str, Any]] = []
    for artifact in artifacts:
        item = dict(artifact)
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            rebased.append(item)
            continue
        src = pathlib.Path(raw_path)
        if not src.is_absolute():
            src = (child_drive / raw_path).resolve(strict=False)
        if is_verification_receipts_path(child_drive, task_id, src):
            # Receipt union has its own locked writer; never replace its rows.
            continue
        try:
            src.resolve(strict=False).relative_to(parent_dir.resolve(strict=False))
            dest = src.resolve(strict=False)
        except ValueError:
            dest = parent_dir / src.name
            if dest.exists() and dest.resolve(strict=False) != src.resolve(strict=False):
                from ouroboros.artifacts import stream_artifact_file
                try:
                    if not item.get("sha256"):
                        raise OSError("legacy artifact has no captured digest")
                    stream_artifact_file(dest, expected=item)
                    src = dest  # Exact canonical bytes already survive this copy-back.
                except OSError:
                    dest = parent_dir / f"{src.stem}_{sha256(str(src).encode('utf-8')).hexdigest()[:8]}{src.suffix}"
        try:
            measured = copy_artifact_file(src, dest, expected=item)
        except OSError as exc:
            item.update(copy_status="failed", copy_error=f"{type(exc).__name__}: {exc}")
            if promotion is not None:
                promotion["pending_refs"].append({"path": str(src), "kind": "task_artifact",
                                                   "reason": item["copy_error"]})
            rebased.append(item)
            continue
        item.pop("copy_status", None)
        item.pop("copy_error", None)
        item.update(path=str(dest), name=str(item.get("name") or dest.name), **measured)
        rebased.append(item)
    return rebased


def task_is_readonly_subagent(task: Dict[str, Any]) -> bool:
    """The shared completion/reaper exception: readonly children need no artifacts."""
    if not isinstance(task, dict):
        return False
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    task_constraint = task.get("task_constraint") if isinstance(task.get("task_constraint"), dict) else {}
    if not task_constraint and isinstance(metadata.get("task_constraint"), dict):
        task_constraint = metadata.get("task_constraint") or {}
    return (
        str(task.get("delegation_role") or metadata.get("delegation_role") or "") == "subagent"
        and str(task_constraint.get("mode") or "") == _LOCAL_READONLY_SUBAGENT_MODE
    )


def _build_deliverable_manifest(
    workspace_root: pathlib.Path, task_id: str, project_id: str
) -> Dict[str, Any]:
    """List genesis outputs with streamed hashes, junk excluded, symlinks unfollowed.
    Read/change gaps are disclosed, not delivery failures; actual copies stay strict.
    """
    from ouroboros.artifacts import stream_artifact_file

    contents: List[Dict[str, Any]] = []
    def failed(exc: OSError) -> None:
        path = pathlib.Path(exc.filename) if exc.filename else workspace_root
        contents.append({"rel": path.relative_to(workspace_root).as_posix(), "type": "directory",
                         "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"})
    for root, dirs, files in os.walk(workspace_root, onerror=failed):
        dirs[:] = sorted(d for d in dirs if d not in _TOP_LEVEL_EXCLUDE_DIRS and d != ".git")
        for fname in sorted(files):
            fpath = pathlib.Path(root) / fname
            entry = {"rel": fpath.relative_to(workspace_root).as_posix()}
            try:
                if fpath.is_symlink():
                    entry.update(symlink=True, sha256="")
                else:
                    entry.update(stream_artifact_file(fpath))
            except OSError as exc:
                entry.update(status="unavailable", error=f"{type(exc).__name__}: {exc}")
            contents.append(entry)
    gap_count = sum(item.get("status") == "unavailable" for item in contents)
    return {
        "schema_version": 1, "task_id": task_id, "project_id": project_id,
        "project_root": str(workspace_root), "created_at": utc_now_iso(),
        "file_count": sum(item.get("type") != "directory" for item in contents),
        "complete": gap_count == 0, "gap_count": gap_count,
        "truncated": False, "contents": contents,
    }


def _file_artifact(kind: str, path: pathlib.Path, **facts: Any) -> Dict[str, Any]:
    """One row shape for an artifact written straight into the task artifact dir."""
    return {"kind": kind, "name": path.name, "path": str(path),
            "size": path.stat().st_size if path.exists() else 0, **facts}


def finalize_task_artifacts(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Write patch/memory-export artifacts for a completed headless task."""

    artifacts: List[Dict[str, Any]] = []
    task_id = str(task.get("id") or "")
    if not task_id:
        return artifacts

    existing = load_task_result(parent_drive_root, task_id) or {}
    # A cancellation latch wins before artifact creation or surviving-root reads.
    if cancellation_blocks_child_result(existing):
        return artifacts
    artifact_dir = task_artifacts_dir(parent_drive_root, task_id)
    workspace_root = _workspace_root_from_task(task)
    status = str(existing.get("status") or "completed")
    artifact_status = ARTIFACT_STATUS_READY
    artifact_error = ""
    if workspace_root is not None:
        write_task_result(
            parent_drive_root,
            task_id,
            status,
            artifact_status=ARTIFACT_STATUS_FINALIZING,
        )
        try:
            patch_artifacts, manifest = write_workspace_patch_artifacts(
                workspace_root,
                artifact_dir,
                task=task,
            )
            artifacts.extend(patch_artifacts)
            artifact_status = str(manifest.get("status") or ARTIFACT_STATUS_READY_WITH_CHANGES)
            if manifest.get("status") == ARTIFACT_STATUS_FAILED:
                artifact_status = ARTIFACT_STATUS_FAILED
                artifact_error = "; ".join(str(err.get("message") or err) for err in manifest.get("errors") or [])[:1000]
        except Exception as exc:
            artifact_status = ARTIFACT_STATUS_FAILED
            artifact_error = f"{type(exc).__name__}: {exc}"
            manifest_path = artifact_dir / "workspace_patch.json"
            manifest = _empty_patch_manifest(
                workspace_root,
                status=ARTIFACT_STATUS_FAILED,
                errors=[{"type": "exception", "message": artifact_error}],
            )
            atomic_write_json(
                manifest_path,
                manifest,
                trailing_newline=True,
            )
            artifacts.append(_file_artifact("workspace_patch_manifest", manifest_path,
                                            workspace_root=str(workspace_root)))

    child_drive = _child_drive_from_task(task)
    if child_drive is not None:
        try:
            export_path = artifact_dir / "memory_export.json"
            atomic_write_json(export_path, build_memory_export(child_drive, task), trailing_newline=True)
            artifacts.append(_file_artifact("memory_export", export_path,
                                            memory_mode=str(task.get("memory_mode") or "")))
        except Exception as exc:
            if workspace_root is not None:
                artifact_status = ARTIFACT_STATUS_FAILED
            message = f"{type(exc).__name__}: {exc}"
            artifact_error = f"{artifact_error}; {message}" if artifact_error else message

    # Deferral 3: a from-scratch (genesis) project gets a typed deliverable manifest on
    # the artifact axis, so its OUTPUT files (not only the patch diff) are inspectable.
    tc = task.get("task_constraint") if isinstance(task.get("task_constraint"), dict) else \
        (existing.get("task_constraint") if isinstance(existing.get("task_constraint"), dict) else {})
    if (
        workspace_root is not None
        and str((tc or {}).get("surface") or "") == "genesis"
        and workspace_root.is_dir()
    ):
        try:
            manifest_path = artifact_dir / "deliverable_manifest.json"
            dm = _build_deliverable_manifest(workspace_root, task_id, str(task.get("project_id") or ""))
            atomic_write_json(manifest_path, dm, trailing_newline=True)
            artifacts.append(_file_artifact(
                "deliverable_manifest", manifest_path,
                file_count=int(dm.get("file_count") or 0), truncated=bool(dm.get("truncated")),
                complete=dm["complete"], gap_count=dm["gap_count"],
                errors=([f"Automatic workspace listing is partial: {dm['gap_count']} read gaps."]
                        if dm["gap_count"] else []),
                workspace_root=str(workspace_root),
            ))
        except Exception as exc:
            artifact_status = ARTIFACT_STATUS_FAILED
            artifact_error = f"Deliverable manifest failed: {type(exc).__name__}: {exc}"
            log.warning("deliverable_manifest build failed for %s", task_id, exc_info=True)

    if artifacts or workspace_root is not None:
        existing = load_task_result(parent_drive_root, task_id) or {}
        drop_kinds = {"workspace_patch"} if workspace_root is not None and artifact_status == ARTIFACT_STATUS_FAILED else set()
        merged = _merge_artifacts(list(existing.get("artifacts") or []), artifacts, drop_kinds=drop_kinds)
        fields: Dict[str, Any] = {
            "artifacts": merged,
            "artifact_status": artifact_status if workspace_root is not None else str(existing.get("artifact_status") or ""),
            "artifact_finalized_at": utc_now_iso(),
        }
        if artifact_error:
            fields["artifact_error"] = artifact_error
        # ``fields`` already carries "artifacts" and "artifact_status".
        provisional = {**existing, **fields}
        provisional.pop("artifact_bundle", None)
        try:
            from ouroboros.outcomes import artifact_bundle_from_result, refresh_verification_ledger_artifacts

            artifact_bundle = artifact_bundle_from_result(provisional)
            fields["artifact_bundle"] = artifact_bundle
            axes = existing.get("outcome_axes") if isinstance(existing.get("outcome_axes"), dict) else {}
            if axes:
                axes = dict(axes)
                artifact_axis = dict(axes.get("artifacts") or {})
                artifact_axis["status"] = str(artifact_bundle.get("status") or artifact_status or "")
                axes["artifacts"] = artifact_axis
                fields["outcome_axes"] = axes
            refreshed_ledger = refresh_verification_ledger_artifacts(
                existing.get("verification_ledger"),
                artifact_bundle,
            )
            if refreshed_ledger is not None:
                fields["verification_ledger"] = refreshed_ledger
            for item in merged:
                if not isinstance(item, dict) or str(item.get("kind") or "") != "verification_ledger":
                    continue
                ledger_path = pathlib.Path(str(item.get("path") or ""))
                if not ledger_path.is_file():
                    continue
                try:
                    raw_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    refreshed_artifact_ledger = refresh_verification_ledger_artifacts(raw_ledger, artifact_bundle)
                    if isinstance(refreshed_artifact_ledger, dict):
                        atomic_write_json(ledger_path, refreshed_artifact_ledger, trailing_newline=True)
                        data = ledger_path.read_bytes()
                        item["size"] = len(data)
                        item["sha256"] = sha256(data).hexdigest()
                        item["status"] = ARTIFACT_STATUS_READY
                        stub = fields.get("verification_ledger")
                        if isinstance(stub, dict) and stub.get("omitted_to_artifact") and isinstance(refreshed_artifact_ledger.get("summary"), dict):
                            fields["verification_ledger"] = {**stub, "summary": dict(refreshed_artifact_ledger["summary"])}
                except Exception:
                    log.debug("Failed to refresh verification ledger artifact for task %s", task_id, exc_info=True)
            # The loop rewrote the ledger file in place, so the bundle computed
            # before it no longer describes the bytes on disk.
            fields["artifact_bundle"] = artifact_bundle_from_result(provisional)
        except Exception:
            log.warning("Artifact bundle/ledger refresh failed for task %s", task_id, exc_info=True)
        write_task_result(
            parent_drive_root,
            task_id,
            str(existing.get("status") or status or "completed"),
            **fields,
        )
    return artifacts


def build_memory_export(child_drive_root: pathlib.Path, task: Dict[str, Any]) -> Dict[str, Any]:
    """Create an explicit export artifact without merging it into parent memory."""

    root = pathlib.Path(child_drive_root)
    memory_root = root / "memory"
    files: Dict[str, str] = {}
    if memory_root.is_dir():
        for path in sorted(memory_root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                rel = str(path.relative_to(memory_root)).replace(os.sep, "/")
                files[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "task_id": str(task.get("id") or ""),
        "memory_mode": str(task.get("memory_mode") or ""),
        "child_drive_root": str(root),
        "files": files,
    }


def _copy_stable_memory(parent: pathlib.Path, child: pathlib.Path, *, project_id: str = "") -> None:
    parent_memory = parent / "memory"
    child_memory = child / "memory"
    project = bool(str(project_id or "").strip())
    # Projects inherit shared patterns, never the global knowledge topics/index.
    paths = ["identity.md", "WORLD.md", "registry.md"]
    if project:
        paths.append("knowledge/patterns.md")
    for rel in paths:
        src = parent_memory / rel
        if src.is_file():
            dst = child_memory / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    src_knowledge = parent_memory / "knowledge"
    dst_knowledge = child_memory / "knowledge"
    if not project and src_knowledge.is_dir():
        shutil.copytree(src_knowledge, dst_knowledge, dirs_exist_ok=True)


def _child_drive_from_task(task: Dict[str, Any]) -> Optional[pathlib.Path]:
    text = str(task.get("drive_root") or task.get("child_drive_root") or "").strip()
    return pathlib.Path(text) if text else None


def _workspace_root_from_task(task: Dict[str, Any]) -> Optional[pathlib.Path]:
    text = str(task.get("workspace_root") or "").strip()
    if not text:
        meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        text = str(meta.get("workspace_root") or "").strip()
    return pathlib.Path(text) if text else None


def _merge_artifacts(
    existing: List[Dict[str, Any]],
    new_items: List[Dict[str, Any]],
    *,
    drop_kinds: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:

    drop = drop_kinds or set()
    key_for = lambda item: (
        str(item.get("kind") or ""),
        str(item.get("name") or pathlib.Path(str(item.get("path") or "")).name),
    )
    keys = {key_for(item) for item in new_items if isinstance(item, dict)}
    return [item for item in existing if isinstance(item, dict)
            and key_for(item)[0] not in drop and key_for(item) not in keys] + new_items


__all__ = [
    "ARTIFACT_STATUS_FAILED",
    "ARTIFACT_STATUS_FINALIZING",
    "ARTIFACT_STATUS_PENDING",
    "ARTIFACT_STATUS_READY",
    "build_memory_export",
    "build_workspace_patch",
    "copy_child_task_result",
    "prepare_terminal_task_files",
    "terminal_task_files_ready",
    "finalize_task_artifacts",
    "task_is_readonly_subagent",
    "prepare_task_drive",
    "prune_headless_task_drives",
    "prune_task_drives",
    "task_artifacts_dir",
    "task_state_dir",
    "write_workspace_patch_artifacts",
    "write_workspace_preflight_artifact",
]
