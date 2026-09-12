"""Task-start tool visibility policy.

This module determines which tools are available at the start of a task
without an explicit ``enable_tools`` call.  Tool sets themselves live in
``ouroboros.tool_capabilities`` (the single source of truth).
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol


class ToolSchemaProvider(Protocol):
    """Minimal registry contract needed by the loop/discovery helpers."""

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        ...


def initial_tool_schemas(registry: ToolSchemaProvider) -> List[Dict[str, Any]]:
    """Return the full capability envelope that should be present from round 1.

    Visibility is selected by the registry context: ordinary top-level tasks
    expose all available first-party built-ins plus live extension/MCP schemas;
    delegated-child, repair, credential, resource, and contract
    filters narrow independently. No enabled schema is silently skipped here.
    """

    return registry.schemas()


def list_non_core_tools(registry: ToolSchemaProvider) -> List[Dict[str, str]]:
    """Return name+description for tools that require explicit enable_tools."""

    return []


CAPABILITY_OMISSION_HEADER = "[CAPABILITY_OMISSION_MANIFEST]"


def format_capability_omissions(
    omissions: Any, *, header: str = CAPABILITY_OMISSION_HEADER,
) -> List[str]:
    """Render the capability-omission manifest — ONE formatter (v6.78.0, owner Q20).

    Replaces five divergent copies (two in ``tools/tool_discovery.py``, three in
    ``loop.py``) so a withheld capability is never rendered with a different amount
    of truth depending on which path the agent hit. Detail is the richest available
    fact: the loader ``error``, else the blocked ``resource``, else the REAL withheld
    tool NAMES (the four thinner copies printed "no detail" for exactly the rows that
    carry names — ``disabled_by_contract``/``missing_credential``). Never raises: an
    unrenderable row is skipped rather than breaking tool discovery.
    """

    lines: List[str] = [header] if header else []
    for item in omissions or []:
        if not isinstance(item, dict):
            continue
        names = item.get("tools")
        detail = (
            item.get("error")
            or item.get("resource")
            or (", ".join(str(name) for name in names) if isinstance(names, list) and names else "")
            or "no detail"
        )
        lines.append(
            f"- {item.get('surface', 'unknown')}: {item.get('reason', 'unknown')} ({detail})"
        )
    return lines
