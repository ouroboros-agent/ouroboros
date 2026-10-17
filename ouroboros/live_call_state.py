"""A live call's small records, and when its engine session is renewed.

The records are the driver's (``live_turn``): what Ouroboros said and how much of it was
played, and the acknowledgements a delivery boundary owes. The renewal is the session's own
lifecycle at completed turns. A Gemini Live session holds every spoken turn as audio, the
densest and dearest part of its context, and each answer re-bills all of it (no prompt cache);
the provider also drops a spoken session's oldest turns minutes into a call, whatever its window
and compression settings say (``live_call_route``: the route's held audio). So the host renews
the session at a completed turn: the call so far returns as text in the session's system
instruction, which the provider keeps (``live_session_view``), and the newest stretch stays
audio, a sawtooth audio horizon. A renewal is due when the next turn would take the held audio
past the call's audio span, when the measured prompt would reach the window on the next turn,
when the provider's counts show it dropped turns (the audio it held then lowers this call's
span), or when the provider says goodbye (``GoAway``); with no completed turn before the
goodbye's own deadline the session resumes with the provider's handle, as a transport loss
does. These are window and price physics, never judgment. The owner's audio heard while a
session renews waits in a bounded backlog and reaches the next session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

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
        self.held_at_drop = 0  # the least audio a session of this call held before the provider dropped turns
        self.drops = 0         # how often the provider dropped a session's turns in this call
        self.reset((0, 0))

    def reset(self, bounds: Tuple[int, int]) -> None:
        """A new session holds nothing yet; ``bounds`` is its ``(room, audio span)`` in provider tokens,
        the span no larger than what this call's sessions held before a drop."""
        self.room, span = bounds
        self.span = min(span, self.held_at_drop) if span and self.held_at_drop else (span or self.held_at_drop)
        self.rows_at_open = len(getattr(self.call, "messages", None) or ())  # what this session was opened with
        self.held_audio = self.held_prompt = self.growth = self.audio_growth = 0
        self.dropped = False
        self.go_away_by: Optional[float] = None
        self.owner_speaking = False

    def observe(self, usage: Mapping[str, Any]) -> None:
        """One answer's prompt counts what the session holds; its growth bounds the next turn's. Held
        audio only grows while the provider keeps the session's turns, so a smaller count is a drop."""
        prompt = sum(int(usage.get(key) or 0) for key in HELD_INPUT_KEYS)
        audio = int(usage.get("audio_input_tokens") or 0)
        if self.held_audio and audio < self.held_audio:
            self.dropped, self.drops = True, self.drops + 1
            self.held_at_drop = min(self.held_at_drop, self.held_audio) if self.held_at_drop else self.held_audio
            self.span = min(self.span, self.held_at_drop) if self.span else self.held_at_drop
            log.info("live call %s: the provider dropped earlier turns after holding %s audio tokens",
                     getattr(getattr(self.call, "spec", None), "call_id", ""), self.held_audio)
        elif self.held_prompt:
            self.growth = max(self.growth, prompt - self.held_prompt)
            self.audio_growth = max(self.audio_growth, audio - self.held_audio)
        self.held_prompt, self.held_audio = prompt, audio

    def owner_caption(self, text: str, final: bool) -> None:
        self.owner_speaking = bool(str(text or "").strip()) and not final

    def go_away(self, time_left_s: float) -> None:
        self.go_away_by = time.monotonic() + max(0.0, float(time_left_s or 0) - GO_AWAY_MARGIN_S)

    def due(self) -> str:
        if self.go_away_by is not None:
            return "go_away"
        if len(self.call.messages) <= self.rows_at_open:
            return ""  # nothing was said since this session opened: a renewal would replay the same text
        if self.dropped:
            return "dropped"  # the provider forgot turns: the next session carries them as text
        if self.span and self.held_audio and self.held_audio + self.audio_growth >= self.span:
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
