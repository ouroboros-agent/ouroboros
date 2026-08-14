"""Physical tool-target resolution and dispatch path normalization."""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict

from ouroboros.shell_parse import is_absolute_path_text
from ouroboros.tool_access import (
    binding_targets_system_repo,
    build_resolved_resource_binding,
    normalize_root_relative,
)


def _coerce_real_path(value: Any) -> pathlib.Path | None:
    if value is None or value.__class__.__module__.startswith("unittest.mock"):
        return None
    try:
        return pathlib.Path(os.fspath(value))
    except TypeError:
        return None


def active_repo_dir_for(ctx: Any) -> pathlib.Path:
    """Return the active repo/workspace root for real and lightweight test contexts."""
    active = getattr(ctx, "active_repo_dir", None)
    if callable(active):
        try:
            candidate = active()
        except Exception:
            candidate = None
        path = _coerce_real_path(candidate)
        if path is not None:
            return path

    workspace_root = getattr(ctx, "workspace_root", None)
    workspace_path = _coerce_real_path(workspace_root)
    if workspace_path is not None:
        workspace_mode = str(getattr(ctx, "workspace_mode", "") or "").strip()
        if workspace_mode:
            return workspace_path

    return pathlib.Path(getattr(ctx, "repo_dir"))


def system_repo_dir_for(ctx: Any) -> pathlib.Path:
    """Return the Ouroboros system repo root, not an external active workspace."""

    return pathlib.Path(getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir"))


# Path-bearing file tools whose active_workspace/system_repo path arg is normalized
# ONCE at dispatch (execute) so the handler AND every guard (protected-path,
# protected-artifact, shrink) resolve the identical target -- no desync bypass.
# apply_patch/edit_batch are absent because they carry no top-level `path` arg
# (their paths live inside the patch text / edits[] entries), so this seam has
# nothing to rewrite. They are NOT exempt from the canonicalization itself: both
# the dispatch guards below and their handlers run every payload path through
# `canonical_repo_relative_path`, the same normalization this seam applies.
_PATH_NORMALIZED_TOOLS = frozenset({"read_file", "write_file", "edit_text", "list_files", "search_code", "query_code"})


def _normalize_dispatch_path_args(ctx: Any, name: str, args: Dict[str, Any]) -> str:
    """ROOT-FIX (v6.35.0): normalize an absolute / redundant-root-basename
    active_workspace|system_repo path arg IN PLACE at the dispatch boundary, so
    the handler AND every downstream guard (protected-path, protected-artifact,
    accidental-truncation shrink guard) resolve the SAME target. One authoritative
    normalization point is what makes a guard unable to desync from the operation.

    v6.54.3 root-label fix: returns a dispatch note ("" when nothing rerouted).
    When ``root='user_files'`` carries an ABSOLUTE path that resolves under the
    ACTIVE WORKSPACE root, the root label is wrong, not the intent: reads
    (read_file/list_files/search_code) are auto-routed to
    ``root='active_workspace'`` with a visible note appended AFTER the result
    (trailing, so first-line failure classification is never masked),
    and writes (write_file/edit_text) return an actionable
    ROOT_REQUIRED_ACTIVE_WORKSPACE redirect instead of a generic access denial.
    The destination root still passes every downstream gate (profile access
    decision, protected-path guards, subagent filters) — only the label is
    corrected, never the authority. ``query_code`` is excluded: its
    root=user_files external-target contract handles absolute paths natively."""
    if name not in _PATH_NORMALIZED_TOOLS:
        return ""
    root_arg = str(args.get("root") or "active_workspace")
    if root_arg in ("active_workspace", "system_repo"):
        try:
            norm_root = active_repo_dir_for(ctx) if root_arg == "active_workspace" else system_repo_dir_for(ctx)
            for _key in ("path", "dir"):
                if isinstance(args.get(_key), str) and args[_key]:
                    args[_key] = normalize_root_relative(norm_root, args[_key])
            if isinstance(args.get("files"), list):
                for _f in args["files"]:
                    if isinstance(_f, dict) and isinstance(_f.get("path"), str) and _f["path"]:
                        _f["path"] = normalize_root_relative(norm_root, _f["path"])
        except Exception:
            pass
        return ""
    if root_arg != "user_files" or name == "query_code":
        return ""
    try:
        workspace = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    except Exception:
        return ""

    def _under_workspace(text: str) -> bool:
        if not is_absolute_path_text(text):
            return False
        try:
            pathlib.Path(text).expanduser().resolve(strict=False).relative_to(workspace)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    candidates: list[str] = []
    for _key in ("path", "dir"):
        if isinstance(args.get(_key), str) and args[_key]:
            candidates.append(args[_key])
    if isinstance(args.get("files"), list):
        for _f in args["files"]:
            if isinstance(_f, dict) and isinstance(_f.get("path"), str) and _f["path"]:
                candidates.append(_f["path"])
    hits = [text for text in candidates if _under_workspace(text)]
    if not hits:
        return ""
    if name in ("write_file", "edit_text"):
        return (
            "⚠️ ROOT_REQUIRED_ACTIVE_WORKSPACE: absolute path "
            f"{hits[0]!r} is under the active workspace, but root='user_files' does not "
            "write there. Retry the same call with root='active_workspace' (the same "
            "path is accepted)."
        )
    args["root"] = "active_workspace"
    try:
        for _key in ("path", "dir"):
            if isinstance(args.get(_key), str) and args[_key]:
                args[_key] = normalize_root_relative(workspace, args[_key])
        if isinstance(args.get("files"), list):
            for _f in args["files"]:
                if isinstance(_f, dict) and isinstance(_f.get("path"), str) and _f["path"]:
                    _f["path"] = normalize_root_relative(workspace, _f["path"])
    except Exception:
        pass
    return (
        "⚠️ AUTO_ROUTED_TO_ACTIVE_WORKSPACE: absolute path "
        f"{hits[0]!r} is under the active workspace; the call ran with "
        "root='active_workspace'. Pass root='active_workspace' directly for "
        "workspace paths."
    )


_GENERIC_VCS_TARGET_TOOLS = frozenset({
    "vcs_status",
    "vcs_diff",
    "vcs_pull_ff",
    "vcs_restore",
    "vcs_revert",
})

_TARGET_BINDING_OPERATIONS = {
    "read_file": "read",
    "list_files": "list",
    "search_code": "search",
    "query_code": "search",
    "write_file": "write",
    "edit_text": "edit",
    "apply_patch": "edit",
    "edit_batch": "edit",
    **{name: "vcs" for name in _GENERIC_VCS_TARGET_TOOLS},
}
_SKILL_LIFECYCLE_TARGET_TOOLS = frozenset({
    "skill_review",
    "skill_preflight",
    "submit_skill_to_hub",
})
_PROCESS_TARGET_TOOLS = frozenset({"run_command", "run_script", "start_service"})
_VERIFY_RUN_KINDS = frozenset({
    "visible_verifier",
    "explicit_command",
    "explicit_metric",
})


def _target_binding_operation(name: str, args: dict[str, Any]) -> str | None:
    operation = _TARGET_BINDING_OPERATIONS.get(name)
    if operation is not None:
        return operation
    if name in _SKILL_LIFECYCLE_TARGET_TOOLS:
        return "review"
    if name in _PROCESS_TARGET_TOOLS:
        return "service" if name == "start_service" else "shell"
    if name == "verify_and_record" and str(args.get("contract_kind") or "") in _VERIFY_RUN_KINDS:
        return "shell"
    return None


def _build_builtin_target_binding(ctx: Any, name: str, args: dict[str, Any]) -> Any:
    """Build the one private physical-target carrier for a builtin call."""

    operation = _target_binding_operation(name, args)
    if operation is None:
        return None
    if name in _SKILL_LIFECYCLE_TARGET_TOOLS:
        return build_resolved_resource_binding(
            ctx,
            root="skill_payload",
            operation="review",
            path=".",
            skill_name=str(args.get("skill") or ""),
        )
    if name in _PROCESS_TARGET_TOOLS or name == "verify_and_record":
        return build_resolved_resource_binding(
            ctx,
            operation=operation,
            process_cwd=str(args.get("cwd") or ""),
            bucket=str(args.get("bucket") or ""),
            skill_name=str(args.get("skill_name") or ""),
        )
    root = str(args.get("root") or "active_workspace")
    bucket = str(args.get("bucket") or "")
    skill_name = str(args.get("skill_name") or "")

    def _one(path: str) -> Any:
        return build_resolved_resource_binding(
            ctx,
            root=root,
            operation=operation,
            path=path or ".",
            bucket=bucket,
            skill_name=skill_name,
        )

    if name == "write_file" and args.get("files"):
        return tuple(
            _one(str(item.get("path") or ""))
            for item in args.get("files") or []
            if isinstance(item, dict)
        )
    if name == "apply_patch":
        from ouroboros.tools.edit_ops import patch_target_paths

        return tuple(_one(path) for path in patch_target_paths(str(args.get("patch") or "")))
    if name == "edit_batch":
        return tuple(
            _one(str(item.get("path") or ""))
            for item in args.get("edits") or []
            if isinstance(item, dict)
        )
    return _one(str(args.get("path") or "."))


def _binding_items(binding: Any) -> tuple[Any, ...]:
    if binding is None:
        return ()
    return binding if isinstance(binding, tuple) else (binding,)


def _binding_set_targets_system_repo(ctx: Any, binding: Any) -> bool:
    items = _binding_items(binding)
    return bool(items) and all(binding_targets_system_repo(ctx, item) for item in items)


def _binding_set_is_light_restricted(ctx: Any, binding: Any) -> bool:
    """Whether light mode must treat this file/VCS target as internal state."""
    items = _binding_items(binding)
    return bool(items) and all(
        binding_targets_system_repo(ctx, item)
        or (item.root == "runtime_data" and item.source == "runtime_data")
        for item in items
    )


def _binding_state_drive_root(ctx: Any, binding: Any) -> pathlib.Path:
    items = _binding_items(binding)
    if items:
        return pathlib.Path(items[0].state_drive_root)
    return pathlib.Path(ctx.drive_root)
