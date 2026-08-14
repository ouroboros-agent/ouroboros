"""Host-owned pre-dispatch access and managed-update guard outcomes."""

from __future__ import annotations

import os
import pathlib
from collections.abc import Collection
from typing import Any, Optional

from ouroboros.contracts.skill_payload_policy import (
    constraint_bucket_skill,
    is_skill_payload_control_filename,
    is_skill_payload_path,
)
from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.tool_capabilities import (
    ACTING_SUBAGENT_TOOL_NAMES,
    LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
)
from ouroboros.tools.tool_result import ToolResult

_WEB_TOOLS = frozenset({"web_search", "browse_page", "browser_action", "youtube_transcript"})
_GITHUB_TOKEN_TOOLS = frozenset({
    "list_github_prs",
    "get_github_pr",
    "comment_on_pr",
    "list_github_issues",
    "get_github_issue",
    "comment_on_issue",
    "close_github_issue",
    "create_github_issue",
    "run_ci_tests",
    "submit_skill_to_hub",
    "generate_evolution_stats",
})


def _resource_allowed(ctx: Any, key: str) -> bool:
    metadata = (
        getattr(ctx, "task_metadata", {})
        if isinstance(getattr(ctx, "task_metadata", {}), dict)
        else {}
    )
    contract = metadata.get("task_contract") if isinstance(metadata.get("task_contract"), dict) else {}
    if not contract and isinstance(getattr(ctx, "task_contract", None), dict):
        contract = getattr(ctx, "task_contract")
    resources = {}
    for source in (metadata, contract):
        raw = source.get("allowed_resources") if isinstance(source, dict) else None
        if isinstance(raw, dict):
            resources.update(raw)
    if not resources:
        return True
    for name in (key, f"allow_{key}"):
        value = resources.get(name)
        if isinstance(value, bool):
            return value
    if key == "web":
        for name in ("network", "allow_network", "internet", "external_network"):
            value = resources.get(name)
            if isinstance(value, bool) and not value:
                return False
    if key == "network":
        for name in ("web", "allow_web", "internet", "external_network"):
            value = resources.get(name)
            if isinstance(value, bool) and not value:
                return False
    return True


def _disabled_tools(ctx: Any) -> frozenset:
    """Tool names the task contract withholds (declarative tool policy).

    Independent of ``allowed_resources``: a caller can disable specific tools
    (e.g. the agent's web_search/browser/VLM tools for a faithful benchmark)
    WITHOUT setting web/network=false — so shell network egress (git/pip) stays
    available and the web<->network cross-implication in ``_resource_allowed``
    never fires.
    """
    metadata = (
        getattr(ctx, "task_metadata", {})
        if isinstance(getattr(ctx, "task_metadata", {}), dict)
        else {}
    )
    contract = metadata.get("task_contract") if isinstance(metadata.get("task_contract"), dict) else {}
    if not contract and isinstance(getattr(ctx, "task_contract", None), dict):
        contract = getattr(ctx, "task_contract")
    names: set = set()
    for source in (metadata, contract):
        raw = source.get("disabled_tools") if isinstance(source, dict) else None
        if isinstance(raw, (list, tuple)):
            names.update(str(n).strip() for n in raw if str(n).strip())
    # D10 compatibility: `claude_code_edit` was retired; saved contracts that
    # withheld the external coding gateway keep withholding its SUCCESSOR — the
    # delegated coding session's start verb. The dead name stays in the set
    # too (harmless: nothing registers it), so old contracts round-trip as-is.
    if "claude_code_edit" in names:
        names.add("delegate_start")
    return frozenset(names)


def _builtin_tool_availability(name: str, ctx: Any = None) -> tuple[bool, str, str]:
    """Return ``(available, reason, detail)`` for built-in tool credential gates.

    Predicates are lazy to avoid registry import cycles and discovery-time side effects.
    """
    # A bare registry (unit tests, static policy inventory, import-time introspection)
    # is a structural surface, not a running task capability envelope.
    if not str(getattr(ctx, "task_id", "") or "").strip():
        metadata = getattr(ctx, "task_metadata", {}) if ctx is not None else {}
        contract = getattr(ctx, "task_contract", {}) if ctx is not None else {}
        if not metadata and not contract:
            return True, "", ""
    tool = str(name or "").strip()
    if tool == "web_search":
        try:
            from ouroboros.tools.search import _available_web_search_backends

            if not _available_web_search_backends():
                return False, "missing_credential", "web_search_backend"
        except ImportError:
            return True, "", ""
        except Exception:
            return True, "", ""
    if tool in _GITHUB_TOKEN_TOOLS and not os.environ.get("GITHUB_TOKEN", "").strip():
        return False, "missing_credential", "GITHUB_TOKEN"
    return True, "", ""


def _capability_resource_guard_result(
    ctx: Any,
    name: str,
    args: dict[str, Any],
    ext_tool: Any = None,
    is_mcp: bool = False,
) -> ToolResult | None:
    """Apply direct task capability and resource admission in legacy order."""
    if name in _disabled_tools(ctx):
        return ToolResult(
            status="blocked",
            code="RESOURCE_BLOCKED",
            text=(
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: task_contract.disabled_tools "
                f"withholds {name!r} for this task."
            ),
        )
    available, unavailable_reason, unavailable_detail = _builtin_tool_availability(name, ctx)
    if not available:
        suffix = f" ({unavailable_detail})" if unavailable_detail else ""
        return ToolResult(
            status="unavailable",
            code="CAPABILITY_UNAVAILABLE",
            text=f"⚠️ CAPABILITY_UNAVAILABLE: {name!r} is unavailable: {unavailable_reason}{suffix}.",
        )
    if name == "vlm_query" and str(args.get("image_url") or "").strip() and (
        not _resource_allowed(ctx, "web") or not _resource_allowed(ctx, "network")
    ):
        return ToolResult(
            status="blocked",
            code="RESOURCE_BLOCKED",
            text=(
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: remote image_url for vlm_query "
                "requires allowed_resources.web/network."
            ),
        )
    if name in _WEB_TOOLS and not _resource_allowed(ctx, "web"):
        return ToolResult(
            status="blocked",
            code="RESOURCE_BLOCKED",
            text=(
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: task_contract.allowed_resources.web=false "
                f"blocks {name!r}."
            ),
        )
    if name == "vcs_pull_ff" and not _resource_allowed(ctx, "network"):
        return ToolResult(
            status="blocked",
            code="RESOURCE_BLOCKED",
            text=(
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: task_contract.allowed_resources.network=false "
                "blocks 'vcs_pull_ff'."
            ),
        )
    if (is_mcp or ext_tool) and not _resource_allowed(ctx, "network"):
        return ToolResult(
            status="blocked",
            code="RESOURCE_BLOCKED",
            text=(
                "⚠️ RESOURCE_CONSTRAINT_BLOCKED: task_contract.allowed_resources.network=false "
                f"blocks external tool {name!r}."
            ),
        )
    return None


# CW3: a short-lived same-route decision turn decides, routes, steers, or
# answers; durable work belongs to the task it spawns. This is a curated
# default-deny allowlist, not a projection of the local-readonly subagent set:
# that broader set includes child spawning, blocking waits, and browser page
# interaction. New mutators therefore cannot silently become reachable.
_EPHEMERAL_ALLOWED_TOOLS = frozenset({
    "read_file", "query_code", "search_code", "list_files", "web_search", "browse_page",
    "chat_history", "recent_tasks", "get_task_result", "vcs_diff", "vcs_status",
    "analyze_screenshot", "vlm_query",
    "route_to_project", "promote_chat_to_task", "steer_task", "list_projects", "send_photo",
})


def _ephemeral_block_result(
    ctx: Any,
    name: str,
    ext_tool: Any = None,
    is_mcp: bool = False,
) -> ToolResult | None:
    """Return the decision-turn denial, or ``None`` when dispatch may continue."""
    if not getattr(ctx, "is_ephemeral_turn", False):
        return None
    if ext_tool or is_mcp:
        text = (
            f"⚠️ EPHEMERAL_TURN_RESTRICTED: external tool '{name}' can have durable side "
            "effects, which a short same-route decision turn must not do. Answer inline, "
            "or promote_chat_to_task to do that work in a supervised task."
        )
    elif name not in _EPHEMERAL_ALLOWED_TOOLS:
        text = (
            f"⚠️ EPHEMERAL_TURN_RESTRICTED: '{name}' is not in the decision-turn allowlist "
            "(read/inspect + answer/route/spawn/steer only) — a short same-route turn must "
            "not do durable/control/review/skill work or run shell. Answer inline, or "
            "promote_chat_to_task to do it in a supervised task."
        )
    else:
        return None
    return ToolResult(status="blocked", code="ACCESS_BLOCKED", text=text)


def _managed_update_code_tool_block_result(ctx: Any, name: str) -> ToolResult | None:
    """Block repo mutation owned by a different managed-update resolver task."""
    try:
        from supervisor.update_merge import managed_assisted_tx_for

        if managed_assisted_tx_for(
            getattr(ctx, "task_id", ""),
            getattr(ctx, "task_metadata", None),
        )[1]:
            return ToolResult(
                status="blocked",
                code="ACCESS_BLOCKED",
                text=(
                    f"⚠️ MANAGED_UPDATE_IN_PROGRESS: {name!r} is blocked while a managed update merge "
                    "is being resolved (only its authorized resolution task may write the repo). "
                    "Retry after the update lands or is rolled back."
                ),
            )
    except Exception:
        return ToolResult(
            status="unavailable",
            code="CAPABILITY_UNAVAILABLE",
            text=(
                f"⚠️ MANAGED_UPDATE_STATE_UNAVAILABLE: {name!r} is blocked because the managed "
                "update transaction state could not be verified. Retry after the update state is "
                "available or repaired."
            ),
        )
    return None


def _managed_update_code_tool_block(ctx: Any, name: str) -> str:
    """Compatibility projection for direct callers of the legacy helper."""
    result = _managed_update_code_tool_block_result(ctx, name)
    return result.text if result is not None else ""


def _subagent_and_update_guard_result(
    ctx: Any,
    name: str,
    entry: Any,
    ext_tool: Any,
    is_mcp: bool,
    local_readonly_subagent: bool,
    acting_subagent: bool,
    acting_tool_grants: Collection[str],
    repo_mutation: bool,
) -> ToolResult | None:
    """Apply delegated-child access and managed-update guards in legacy order."""
    if local_readonly_subagent and entry is not None and name not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES:
        return ToolResult(
            status="blocked",
            code="ACCESS_BLOCKED",
            text=(
                "⚠️ LOCAL_READONLY_SUBAGENT_BLOCKED: this subagent may inspect "
                "local repo/data/history plus web/browser surfaces and enabled "
                "external tools, but may not call first-party local tool "
                f"{name!r}. Parent tasks must perform writes, commits, review "
                "gates, tool expansion, runtime control, shell, and skills. "
                "Nested readonly delegation is allowed only through schedule_subagent "
                "within configured depth/cap limits."
            ),
        )
    if acting_subagent and entry is not None and name not in ACTING_SUBAGENT_TOOL_NAMES:
        return ToolResult(
            status="blocked",
            code="ACCESS_BLOCKED",
            text=(
                "⚠️ ACTING_SUBAGENT_BLOCKED: this mutative subagent may read and "
                "write inside its isolated write root and run shell/services "
                f"there, but may not call first-party tool {name!r}. It cannot "
                "commit the live body, run review/runtime/skills lifecycle, enable "
                "tools, or write cognitive memory; the parent integrates the "
                "returned patch and is the sole committer."
            ),
        )
    if acting_subagent and entry is None and (ext_tool or is_mcp) and name not in acting_tool_grants:
        return ToolResult(
            status="blocked",
            code="ACCESS_BLOCKED",
            text=(
                "⚠️ ACTING_SUBAGENT_TOOL_NOT_GRANTED: extension/MCP tool "
                f"{name!r} is not in this acting subagent's external_tool_grants. "
                "The parent must grant dynamic tools explicitly per child."
            ),
        )
    if entry is not None and repo_mutation:
        return _managed_update_code_tool_block_result(ctx, name)
    return None


def _task_constraint_path_allowed(
    path_text: str,
    constraint: Optional[TaskConstraint],
    drive_root: pathlib.Path,
) -> bool:
    return is_skill_payload_path(
        drive_root,
        path_text or "",
        constraint=constraint,
        allow_short_relative=True,
        allow_control_plane=True,
    )


_HEAL_MODE_ALLOWED_TOOLS = frozenset({
    "read_file",
    "list_files",
    "write_file",
    "edit_text",
    "list_skills",
    "skill_review", "skill_preflight",
})


def _heal_protected_payload_sidecar(path_text: str) -> bool:
    return is_skill_payload_control_filename(path_text)


def _heal_mode_guard_result(
    ctx: Any,
    name: str,
    args: dict[str, Any],
    task_constraint: TaskConstraint | None,
    ext_tool: Any,
    is_mcp: bool,
) -> ToolResult | None:
    """Apply skill-repair confinement in the established pre-dispatch order."""
    heal_skill = task_constraint.skill_name if task_constraint else ""
    if (
        name in {"read_file", "list_files", "write_file", "edit_text"}
        and str(args.get("root", "") or "") == "skill_payload"
    ):
        expected_bucket, expected_skill = constraint_bucket_skill(task_constraint)
        requested_bucket = str(args.get("bucket", "") or "").strip()
        requested_skill = str(args.get("skill_name", "") or "").strip()
        if (
            (requested_bucket and requested_bucket != expected_bucket)
            or (requested_skill and requested_skill != expected_skill)
        ):
            if name in {"write_file", "edit_text"}:
                return ToolResult(
                    status="blocked",
                    code="HEAL_MODE_BLOCKED",
                    text=(
                        "⚠️ SKILL_REDIRECT_BLOCKED: active skill_repair "
                        "task is scoped to the selected skill payload."
                    ),
                )
            return ToolResult(
                status="blocked",
                code="HEAL_MODE_BLOCKED",
                text=(
                    "⚠️ HEAL_MODE_BLOCKED: Repair payload access is limited "
                    "to the selected skill payload."
                ),
            )
    if name in {"read_file", "write_file"} and str(args.get("root", "") or "") == "skill_payload":
        payload_paths = []
        maybe_path = str(args.get("path", "") or "")
        if maybe_path:
            payload_paths.append(maybe_path)
        for f_entry in args.get("files") or []:
            if isinstance(f_entry, dict):
                payload_paths.append(str(f_entry.get("path", "") or ""))
        for payload_path in payload_paths or ["."]:
            if not _task_constraint_path_allowed(
                payload_path,
                task_constraint,
                pathlib.Path(ctx.drive_root),
            ):
                return ToolResult(
                    status="blocked",
                    code="HEAL_MODE_BLOCKED",
                    text=(
                        "⚠️ HEAL_MODE_BLOCKED: Repair data access is limited "
                        "to the selected skill payload under data/skills/external "
                        "data/skills/clawhub, or data/skills/ouroboroshub."
                    ),
                )
            if name == "write_file" and _heal_protected_payload_sidecar(payload_path):
                return ToolResult(
                    status="blocked",
                    code="HEAL_MODE_BLOCKED",
                    text=(
                        "⚠️ HEAL_MODE_BLOCKED: Repair may not edit marketplace "
                        "or official provenance sidecars (.clawhub.json, "
                        ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
                        "Edit the user-authored payload files instead."
                    ),
                )
    if name == "list_files" and str(args.get("root", "") or "") == "skill_payload":
        data_dir = str(args.get("path", "") or "")
        if not _task_constraint_path_allowed(
            data_dir,
            task_constraint,
            pathlib.Path(ctx.drive_root),
        ):
            return ToolResult(
                status="blocked",
                code="HEAL_MODE_BLOCKED",
                text=(
                    "⚠️ HEAL_MODE_BLOCKED: Repair data listing is limited "
                    "to the selected skill payload under data/skills/external "
                    "data/skills/clawhub, or data/skills/ouroboroshub."
                ),
            )
    if name == "edit_text":
        edit_path = str(args.get("path", "") or "")
        if not _task_constraint_path_allowed(
            edit_path,
            task_constraint,
            pathlib.Path(ctx.drive_root),
        ):
            return ToolResult(
                status="blocked",
                code="HEAL_MODE_BLOCKED",
                text="⚠️ HEAL_MODE_BLOCKED: Repair edit_text is limited to the selected skill payload.",
            )
        if _heal_protected_payload_sidecar(edit_path):
            return ToolResult(
                status="blocked",
                code="HEAL_MODE_BLOCKED",
                text=(
                    "⚠️ HEAL_MODE_BLOCKED: Repair may not edit marketplace "
                    "or official provenance sidecars (.clawhub.json, "
                    ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
                    "Edit the user-authored payload files instead."
                ),
            )
    if name == "skill_review" and str(args.get("skill", "") or "").strip() != heal_skill:
        return ToolResult(
            status="blocked",
            code="HEAL_MODE_BLOCKED",
            text="⚠️ HEAL_MODE_BLOCKED: Repair may only review the selected skill.",
        )
    if name == "skill_preflight" and str(args.get("skill", "") or "").strip() != heal_skill:
        return ToolResult(
            status="blocked",
            code="HEAL_MODE_BLOCKED",
            text="⚠️ HEAL_MODE_BLOCKED: Repair may only preflight the selected skill.",
        )
    if ext_tool or is_mcp or name not in _HEAL_MODE_ALLOWED_TOOLS:
        return ToolResult(
            status="blocked",
            code="HEAL_MODE_BLOCKED",
            text=(
                "⚠️ HEAL_MODE_BLOCKED: Repair tasks may inspect/edit skill "
                "payloads and run skill_review only. Shell, browser automation, "
                "repo mutation, skill execution, extension tools, MCP tools, "
                "delegation, and enable/disable flows are unavailable. Use "
                "the Skills UI after a fresh executable review."
            ),
        )
    return None
