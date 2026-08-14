"""Intrinsic tool descriptors shared by tool modules and registry dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass
class ToolEntry:
    """Single tool descriptor."""

    name: str
    schema: Dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 360
    # Capability flag: tool can mutate the live repo worktree. The dispatcher
    # snapshots `git status --porcelain` around flagged tools and invalidates
    # advisory freshness when the worktree ACTUALLY changed — covering error
    # and timeout paths uniformly, and never invalidating for read-only runs.
    mutates_worktree: bool = False


__all__ = ["ToolEntry"]
