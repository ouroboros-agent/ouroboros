"""What a live session is opened with: its compression watermarks and the call rows it replays.

Pure functions over the call's route fact, its fitted messages and tool schemas, so the
driver (``live_turn``) stays the lifecycle and these physical numbers are testable alone.
The numbers are window physics, never judgment: the route's input limit less one spoken
reply is the room a session has; the call view takes its share and the conversation the rest.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from ouroboros.live_tool_batch import images_of, text_of
from ouroboros.utils import estimate_tokens

REPLY_ALLOWANCE_FALLBACK = 8_192  # a ~5-minute spoken answer, when the route fact names none


def _room(fact: Any) -> int:
    reply = int(getattr(fact, "reply_allowance_tokens", 0) or REPLY_ALLOWANCE_FALLBACK)
    return int(fact.window_tokens) - reply


def _view_tokens(instructions: str, tool_schemas: Sequence[Mapping], ratio: float = 1.0) -> int:
    return math.ceil((estimate_tokens(instructions) + estimate_tokens(json.dumps(list(tool_schemas)))) * ratio)


def compression_watermarks(fact: Any, instructions: str, tool_schemas: Sequence[Mapping],
                           ratio: float = 1.0) -> Tuple[int, int]:
    """Where a self-compressing window starts sliding and how much it keeps; ``(0, 0)`` = provider default.

    Compression starts at the input limit less one spoken reply and keeps the call view plus
    half of the room left for the conversation, so the model always hears at least half of
    that room verbatim and compression runs once per half-room, not every turn. The core (the
    system instruction) never slides out. With no room at all the provider's own refusal speaks.
    """
    if fact is None:
        return 0, 0
    start, view = _room(fact), _view_tokens(instructions, tool_schemas, ratio)
    if view >= start:
        return 0, 0
    return start, view + (start - view) // 2


def history_rows(messages: Sequence[Mapping]) -> List[Dict[str, Any]]:
    """The call's dialogue after the system message as history items (text, a user row's images)."""
    rows: List[Dict[str, Any]] = []
    for msg in messages[1:] if messages and messages[0].get("role") == "system" else messages:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant", "tool"):
            continue
        content, text = msg.get("content"), text_of(msg.get("content"))
        images = images_of(content)
        if images and role == "user":
            parts = [part for part in content if isinstance(part, dict) and part.get("type") in ("text", "image_url")]
            item: Dict[str, Any] = {"role": role, "content": parts}
        elif images:
            item = {"role": role, "content": f"{text}\n[{len(images)} image(s) of this row cannot be replayed in history]"}
        else:
            item = {"role": role, "content": text}
        for key in ("tool_calls", "tool_call_id", "name"):
            if msg.get(key):
                item[key] = msg[key]
        rows.append(item)
    return rows


def history_view(fact: Any, messages: Sequence[Mapping], tool_schemas: Sequence[Mapping]) -> Tuple[Dict[str, Any], ...]:
    """The newest rows a fresh session can replay within half the room beside the call view.

    A long call's own words would otherwise fill a reopened window before anyone speaks;
    the rows left out are named by count, since they stay in the room's transcript.
    Without a route fact nothing bounds the replay.
    """
    rows = history_rows(messages)
    if fact is None:
        return tuple(rows)
    system = messages[0] if messages and messages[0].get("role") == "system" else {}
    budget = max(0, (_room(fact) - _view_tokens(text_of(system.get("content")), tool_schemas)) // 2)
    kept: List[Dict[str, Any]] = []
    spent = 0
    for item in reversed(rows):
        content = item.get("content")
        cost = estimate_tokens(content if isinstance(content, str) else text_of(content))
        if spent + cost > budget:
            break
        kept.append(item)
        spent += cost
    kept.reverse()
    if len(kept) < len(rows):
        kept.insert(0, {"role": "user", "content": (
            f"[Earlier in this call: {len(rows) - len(kept)} rows (speech, typed words, tool results) are not "
            "replayed into this session; they are in this room's transcript.]")})
    return tuple(kept)


def calibration_ratio(ctx: Any) -> float:
    """The fitted projection's calibration ratio for the active mode (1.0 when unknown)."""
    plan = getattr(ctx, "context_fit_plan", None)
    if plan is None:
        return 1.0
    try:
        mode = str(getattr(ctx, "active_context_mode", "") or plan.initial_mode)
        return float(plan.projection(mode).calibration_ratio or 1.0)
    except Exception:
        return 1.0
