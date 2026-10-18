"""What a live session is opened with: its compression watermarks and the call so far it carries.

Pure functions over the call's route fact, its fitted messages and tool schemas, so the
driver (``live_turn``) stays the lifecycle and these physical numbers are testable alone.
The numbers are window physics, never judgment: the route's input limit less one spoken
reply is the room a session has; the call view takes its share and the conversation the rest.
A fresh session leaves its own speech one audio span (the route's held audio or
``CALL_AUDIO_SPAN_TOKENS``, at most half of the room) and carries the call so far as text in
the rest: every row whole while it fits, then the oldest tool results, tool arguments and images
shortened to a line, and only then the oldest rows named by count (``live_call_state`` renews
the session before its audio outgrows it). That text rides in the session's system instruction
(``with_call_so_far``): Gemini Live drops a spoken session's oldest turns minutes into a call,
whatever its window and compression settings say, keeping its system instruction (and at most the last exchange).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ouroboros.context_fit import estimate_context_prompt_tokens
from ouroboros.live_call_route import CALL_AUDIO_SPAN_TOKENS, CALL_REPLY_ALLOWANCE_TOKENS
from ouroboros.live_tool_batch import images_of, text_of
from ouroboros.utils import estimate_tokens


# What a shortened row keeps of a tool result or tool arguments: a line, not the body.
SHORT_ROW_CHARS = 400


def _room(fact: Any) -> int:
    reply = int(getattr(fact, "reply_allowance_tokens", 0) or CALL_REPLY_ALLOWANCE_TOKENS)
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


def audio_reserve(fact: Any, instructions: str, tool_schemas: Sequence[Mapping], ratio: float = 1.0) -> int:
    """What a fresh session leaves for its own speech: the audio the route holds before it drops turns
    (the call's audio span when that was never seen), at most half of the room."""
    if fact is None:
        return 0
    span = min(CALL_AUDIO_SPAN_TOKENS, int(getattr(fact, "held_audio_tokens", 0) or CALL_AUDIO_SPAN_TOKENS))
    return min(span, max(0, _room(fact) - _view_tokens(instructions, tool_schemas, ratio)) // 2)


def renewal_bounds(fact: Any, instructions: str, tool_schemas: Sequence[Mapping],
                   ratio: float = 1.0) -> Tuple[int, int]:
    """A session's ``(room, audio span)`` for its renewal (``live_call_state``); ``(0, 0)`` = unbounded."""
    if fact is None:
        return 0, 0
    return _room(fact), audio_reserve(fact, instructions, tool_schemas, ratio)


def mark_spoken(messages: Sequence[Mapping], item_id: str, status: str) -> None:
    """How Ouroboros's spoken row ended (played, interrupted, generated), for a later replay."""
    for msg in reversed(messages):
        if item_id and msg.get("_item_id") == item_id:
            msg["_spoken"] = status  # type: ignore[index]
            return


def history_rows(messages: Sequence[Mapping]) -> List[Dict[str, Any]]:
    """The call's dialogue after the system message as history items (text, a user row's images);
    a spoken row the owner cut off says so, since only its start was heard."""
    rows: List[Dict[str, Any]] = []
    for msg in messages[1:] if messages and messages[0].get("role") == "system" else messages:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant", "tool"):
            continue
        content, text = msg.get("content"), text_of(msg.get("content"))
        if role == "assistant" and msg.get("_spoken") == "interrupted" and text:
            content = text = f"[The owner interrupted this; only its start was heard] {text}"
        images = images_of(content)
        if images and role == "user":
            parts = [part for part in content if isinstance(part, dict) and part.get("type") in ("text", "image_url")]
            item: Dict[str, Any] = {"role": role, "content": parts}
        elif images:
            item = {"role": role, "content": f"{text}\n[{len(images)} image(s) of this row cannot be replayed in history]"}
        else:
            item = {"role": role, "content": text}
        for key in ("tool_calls", "tool_call_id", "name", "_context_source"):  # the source names a context row
            if msg.get(key):
                item[key] = msg[key]
        rows.append(item)
    return rows


def _replayed_tokens(item: Mapping) -> int:
    """What one replayed row weighs: its text, its images (the bounded proxy) and the earlier tool
    calls it carries as text, whose arguments can dwarf an assistant row's words."""
    return int(estimate_context_prompt_tokens([dict(item)]))


def _shortened(item: Mapping) -> Optional[Dict[str, Any]]:
    """The row with its tool body, tool arguments or images cut to a line; ``None`` if nothing is heavy."""
    note = " … [shortened in this session; the whole row is in this room's transcript]"
    short, changed = dict(item), False
    content = item.get("content")
    if isinstance(content, list):  # a row's images: their words stay, the pixels are named
        images = len([part for part in content if isinstance(part, dict) and part.get("type") == "image_url"])
        short["content"] = f"{text_of(content)} [{images} image(s) shown here earlier in this call]".strip()
        changed = True
    elif item.get("role") == "tool" and isinstance(content, str) and len(content) > SHORT_ROW_CHARS:
        short["content"], changed = content[:SHORT_ROW_CHARS] + note, True
    calls = []
    for call in item.get("tool_calls") or ():
        function = dict((call or {}).get("function") or {})
        arguments = str(function.get("arguments") or "")
        if len(arguments) > SHORT_ROW_CHARS:
            function["arguments"], changed = arguments[:SHORT_ROW_CHARS] + note, True
        calls.append({**call, "function": function})
    if calls:
        short["tool_calls"] = calls
    return short if changed else None


def _omission_notice(count: int) -> Dict[str, Any]:
    return {"role": "user", "content": (
        f"[Earlier in this call: {count} rows (speech, typed words, tool results) are not "
        "replayed into this session; they are in this room's transcript.]")}


def history_view(fact: Any, messages: Sequence[Mapping], tool_schemas: Sequence[Mapping],
                 ratio: float = 1.0) -> Tuple[Dict[str, Any], ...]:
    """The call so far that a fresh session replays beside the call view and its audio span.

    Every row while they fit; then the oldest heavy rows shortened to a line (tool results,
    tool arguments, images), so dialogue outlasts tool weight; only then the newest rows
    that fit, the rest named by count, since they stay in the room's transcript. A long
    call's own words would otherwise fill a reopened window before anyone speaks. Without
    a route fact nothing bounds the replay. ``ratio`` is the route's density, as for the
    watermarks: the room is the provider's count, the estimates are chars/4.
    """
    rows = history_rows(messages)
    if fact is None:
        return tuple(rows)
    system = messages[0] if messages and messages[0].get("role") == "system" else {}
    instructions = text_of(system.get("content"))
    room = max(0, _room(fact) - _view_tokens(instructions, tool_schemas, ratio))
    budget = room - audio_reserve(fact, instructions, tool_schemas, ratio)
    costs = [math.ceil(_replayed_tokens(item) * ratio) for item in rows]
    for index, item in enumerate(rows):  # the oldest tool weight goes first
        if sum(costs) <= budget:
            break
        short = _shortened(item)
        if short is not None:
            rows[index], costs[index] = short, math.ceil(_replayed_tokens(short) * ratio)
    if sum(costs) > budget:  # rows stay out, so the notice naming them is replayed too and weighs its share
        budget -= math.ceil(_replayed_tokens(_omission_notice(len(rows))) * ratio)
    kept: List[Dict[str, Any]] = []
    spent = 0
    for item, cost in zip(reversed(rows), reversed(costs)):
        if spent + cost > budget:
            break
        kept.append(item)
        spent += cost
    kept.reverse()
    if len(kept) < len(rows):
        kept.insert(0, _omission_notice(len(rows) - len(kept)))
    return tuple(kept)


# The heading of the call so far in a session's system instruction, and the label its replayed pixels carry.
CALL_SO_FAR_HEADING = "## This call so far"
IMAGE_REPLAY_LABEL = "[An image shown earlier in this call; its words are in the call so far]"


def _transcript_line(row: Mapping) -> str:
    """One row of the call so far as a line: who spoke, or the bracketed name a context row carries."""
    role, text = str(row.get("role") or ""), text_of(row.get("content")).strip()
    if images_of(row.get("content")):
        text = f"{text} [image shown here; replayed after this instruction]".strip()
    if role == "assistant":
        calls = [f"Ouroboros called {(call.get('function') or {}).get('name', '')} "
                 f"{(call.get('function') or {}).get('arguments', '')}".rstrip() for call in row.get("tool_calls") or ()]
        return "\n".join(part for part in (f"Ouroboros: {text}" if text else "", *calls) if part)
    if role == "tool":
        return f"Tool result ({row.get('name') or row.get('tool_call_id') or 'tool'}): {text}"
    if not text or text.startswith("["):
        return text
    return f"[{row['_context_source']}] {text}" if row.get("_context_source") else f"Owner: {text}"


def with_call_so_far(instructions: str, history: Sequence[Mapping]) -> Tuple[str, Tuple[Dict[str, Any], ...]]:
    """The session's system instruction with the call so far, and the rows still replayed as turns.

    The provider keeps a spoken session's system instruction and drops its oldest turns (measured
    on Gemini Live 2026-10-18), so the call's transcript rides there; only a user row that still
    carries images is replayed as a turn, since its pixels cannot be written into an instruction.
    """
    lines = [line for line in (_transcript_line(row) for row in history) if line]
    if not lines:
        return instructions, ()
    block = (f"{CALL_SO_FAR_HEADING}\n\nWhat was said and done in this call before this session, oldest first; "
             "this session's own turns continue it.\n\n" + "\n".join(lines))
    # The pixels alone, under a short label: their row's words are already in the instruction.
    images = tuple({"role": "user", "content": [{"type": "text", "text": IMAGE_REPLAY_LABEL}, *(
        part for part in row["content"] if isinstance(part, dict) and part.get("type") == "image_url")]}
        for row in history if row.get("role") == "user" and images_of(row.get("content")))
    return (f"{instructions}\n\n{block}" if instructions else block), images


def text_estimate(instructions: str, history: Sequence[Mapping], tool_schemas: Sequence[Mapping]) -> int:
    """The fit estimator's own count of the text a fresh session is opened with (the density basis)."""
    return int(estimate_context_prompt_tokens([{"role": "system", "content": instructions}, *history],
                                              list(tool_schemas) or None))


def record_density_witness(route: Any, estimate: int, text_tokens: int) -> None:
    """Teach the next call's fit how its route counts text: the same witness a text turn's usage writes.

    The first response of a session opened with the call view, and sent no other text before it,
    counts exactly that text (Gemini's ``promptTokensDetails`` TEXT: system instruction, tool
    declarations, replayed rows). Its ratio to the fit estimator's count is the route's density,
    kept in the one observation store under the route identity the fit reads (``known_route``).
    """
    if estimate <= 0 or text_tokens <= 0:
        return
    from ouroboros.capability_evidence import MAIN_DENSITY_BASIS, canonical_evidence_root, record_token_density
    from ouroboros.provider_models import normalize_model_identity

    record_token_density(canonical_evidence_root(), normalize_model_identity(route.engine.model),
                         prompt_chars=estimate * 4, prompt_tokens=text_tokens, source="live_usage",
                         route_fp=route.evidence_fp, basis=MAIN_DENSITY_BASIS)


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
