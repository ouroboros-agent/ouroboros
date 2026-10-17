"""A live call's small records, and when its engine session is renewed.

The records are the driver's (``live_turn``): what Ouroboros said and how much of it was
played, and the acknowledgements a delivery boundary owes. The renewal is the session's own
lifecycle at completed turns. A Gemini Live session holds every spoken turn as audio, the
densest and dearest part of its context, and each answer re-bills all of it (no prompt cache);
its provider compression only slides the oldest turns out. So the host renews the session at a
completed turn: the call's earlier speech returns as text (``live_session_view.history_view``)
and the newest stretch stays audio, a sawtooth audio horizon. A renewal is due when the held
audio reaches the call's audio span, when the measured prompt would reach the sliding window on
the next turn, or when the provider says goodbye (``GoAway``); with no completed turn before the
goodbye's own deadline the session resumes with the provider's handle, as a transport loss
does. These are window and price physics, never judgment. The owner's audio heard while a
session renews waits in a bounded backlog and reaches the next session.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

# Physical bounds: how long before a goodbye's deadline the renewal still waits for a completed
# turn, and how much of the owner's audio one renewal's gap carries to the next session.
GO_AWAY_MARGIN_S = 2.0
GAP_AUDIO_MS = 30_000.0
# The usage keys that count what a session holds as input (``realtime_engine.USAGE_KEYS``).
HELD_INPUT_KEYS = ("text_input_tokens", "audio_input_tokens", "image_input_tokens", "video_input_tokens")


@dataclass(frozen=True)
class Acks:
    """A delivery boundary: the content acknowledgements owed for what was posted before it."""

    callbacks: Tuple[Callable[[], Any], ...]


@dataclass
class Spoken:
    text: str = ""
    final_at: float = 0.0
    played_ms: int = 0
    played: bool = False
    interrupted: bool = False
    audio_ms: float = 0
    ack_at: float = 0
    started_at: float = 0.0  # when its first audio went to the peers


def narration_meta(emit_progress: Callable[..., None]) -> Dict[str, Any]:
    owner = getattr(emit_progress, "__self__", None)
    build = getattr(owner, "_subagent_progress_meta", None)
    try:
        return dict(build("progress")) if callable(build) else {}
    except Exception:
        return {}


class SessionRenewal:
    """The call's session renewals: ``call`` is the live call whose sessions it renews."""

    def __init__(self, call: Any) -> None:
        self.call = call
        self.backlog: Deque[Tuple[Any, float]] = deque()
        self.backlog_ms = 0.0
        self.reset((0, 0))

    def reset(self, bounds: Tuple[int, int]) -> None:
        """A new session holds nothing yet; ``bounds`` is its ``(room, audio span)`` in provider tokens."""
        self.room, self.span = bounds
        self.rows_at_open = len(getattr(self.call, "messages", None) or ())  # what this session was opened with
        self.held_audio = self.held_prompt = self.growth = 0
        self.go_away_by: Optional[float] = None
        self.owner_speaking = False

    def observe(self, usage: Mapping[str, Any]) -> None:
        """One answer's prompt counts what the session holds; its growth bounds the next turn's."""
        prompt = sum(int(usage.get(key) or 0) for key in HELD_INPUT_KEYS)
        if self.held_prompt:
            self.growth = max(self.growth, prompt - self.held_prompt)
        self.held_prompt, self.held_audio = prompt, int(usage.get("audio_input_tokens") or 0)

    def owner_caption(self, text: str, final: bool) -> None:
        self.owner_speaking = bool(str(text or "").strip()) and not final

    def go_away(self, time_left_s: float) -> None:
        self.go_away_by = time.monotonic() + max(0.0, float(time_left_s or 0) - GO_AWAY_MARGIN_S)

    def due(self) -> str:
        if self.go_away_by is not None:
            return "go_away"
        if len(self.call.messages) <= self.rows_at_open:
            return ""  # nothing was said since this session opened: a renewal would replay the same text
        if self.span and self.held_audio >= self.span:
            return "audio_span"
        if self.room and self.held_prompt and self.held_prompt + self.growth >= self.room:
            return "window"
        return ""

    def at_turn_boundary(self) -> bool:
        """A completed turn: no answer open, nothing still playing, nobody speaking, no tool unanswered."""
        call, lane = self.call, self.call.tools_lane
        return (call.engine is not None and not call.closing and not call.reconnecting and not call.response_open
                and not call.spoken and not self.owner_speaking
                and not lane.running and not lane.pending and not lane.claimed)

    def tick(self, now: float) -> None:
        call, reason = self.call, self.due()
        if not reason or call.closing or call.reconnecting:
            return
        if self.at_turn_boundary():
            # A planned renewal at a completed turn: nothing to tell the owner, nothing lost.
            asyncio.create_task(call._reconnect("go_away" if reason == "go_away" else "handover",
                                                resume=False, announce=False))
        elif reason == "go_away" and now >= self.go_away_by:
            asyncio.create_task(call._reconnect("go_away", resume=True))

    def hold(self, event: Any, duration_ms: float) -> None:
        """The owner's audio while a session renews; past the gap's bound later audio is dropped."""
        if self.backlog_ms + duration_ms <= GAP_AUDIO_MS:
            self.backlog.append((event, duration_ms))
            self.backlog_ms += duration_ms

    def take_backlog(self) -> List[Any]:
        events = [event for event, _ms in self.backlog]
        self.backlog.clear()
        self.backlog_ms = 0.0
        return events
