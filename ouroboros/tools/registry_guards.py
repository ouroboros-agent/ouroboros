"""Host-owned pre-dispatch access and managed-update guard outcomes."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from ouroboros.tool_capabilities import (
    ACTING_SUBAGENT_TOOL_NAMES,
    LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
)
from ouroboros.tools.tool_result import ToolResult


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
