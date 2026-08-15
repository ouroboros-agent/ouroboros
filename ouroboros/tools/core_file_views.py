"""Canonical line-window rendering and repeated-view annotation."""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.tool_result import (
    ToolResult,
    _publish_tool_result,
    _published_tool_result,
    _replace_tool_result,
)


def _render_line_slice(
    path: str,
    content: str,
    max_lines: int = 2000,
    start_line: int = 1,
    start_char: int = 0,
) -> str:
    """Return a line-ranged file view with the shared read-tool header.

    ``start_char`` is a SUB-LINE cursor: it skips that many characters of the selected
    window's body before rendering. It exists because delivery is char-bounded (the
    outer tool-result truncator cuts at ``tool_result_limit``): a single line longer
    than the budget can never be delivered whole by any line window, so the reader
    advances WITHIN it by re-reading the same window with a growing ``start_char``.
    Disclosed in the header, so the view never silently masquerades as the whole line.
    """
    start_raw, max_raw = _coerce_line_window(start_line, max_lines)
    max_raw = max(1, max_raw)
    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(1, min(start_raw, total + 1))
    end = min(start + max_raw - 1, total)
    result = "".join(lines[start - 1:end])
    offset = _coerce_start_char(start_char)
    if offset:
        result = result[offset:]
        header = (
            f"# {path} — lines {start}–{end} of {total} "
            f"(from char {offset} of this window)\n"
        )
    else:
        header = f"# {path} — lines {start}–{end} of {total}\n"
    return header + result


def _coerce_start_char(start_char: Any = 0) -> int:
    try:
        return max(0, int(start_char))
    except (TypeError, ValueError):
        return 0


def _coerce_line_window(
    start_line: Any = 1,
    max_lines: Any = 2000,
) -> tuple[int, int]:
    try:
        start_raw = int(start_line)
    except (TypeError, ValueError):
        start_raw = 1
    try:
        max_raw = int(max_lines)
    except (TypeError, ValueError):
        max_raw = 2000
    return start_raw, max(1, max_raw)


def _republish_builtin_text(
    ctx: ToolContext,
    original: str,
    rendered: str,
) -> str:
    """Preserve structural facts when a view annotation changes public text."""

    base = _published_tool_result(ctx, None)
    if isinstance(base, ToolResult) and base.text == original:
        return _publish_tool_result(ctx, _replace_tool_result(base, text=rendered))
    return rendered


def _annotate_reread(
    ctx: ToolContext,
    target: Any,
    start_line: int,
    max_lines: int,
    result: str,
    start_char: int = 0,
) -> str:
    """Append an advisory hint when the SAME file slice is re-read unchanged.

    Per-task, key on (resolved path, slice); the change signal is (size, mtime).
    A repeat read of an unchanged slice is usually wasted budget — nudge the model
    to act on what it has. Advisory only (never blocks; different slices and
    changed files are not flagged).
    """
    try:
        resolved = pathlib.Path(target).resolve(strict=False)
        st = resolved.stat()
    except (OSError, TypeError, ValueError):
        return result
    if not isinstance(result, str) or result.startswith("⚠️"):
        return result
    key = (
        f"{resolved}|{int(start_line)}|{int(max_lines)}|"
        f"{_coerce_start_char(start_char)}"
    )
    sig = (st.st_size, st.st_mtime_ns)
    seen = getattr(ctx, "_read_file_seen", None)
    if not isinstance(seen, dict):
        seen = {}
        ctx._read_file_seen = seen
    prev = seen.get(key)
    seen[key] = sig
    if prev is not None and prev == sig:
        rendered = (
            result
            + "\n\nℹ️ This exact view is unchanged since you already read it this task — "
            "re-reading is usually wasted budget; act on what you have."
        )
        return _republish_builtin_text(ctx, result, rendered)
    return result
