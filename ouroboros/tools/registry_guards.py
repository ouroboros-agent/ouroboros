"""Host-owned pre-dispatch access and managed-update guard outcomes."""

from __future__ import annotations

import os
from collections.abc import Collection
from typing import Any

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
