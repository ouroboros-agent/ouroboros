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
does. These are window and price physics, never judgment. A session also renews when the voice
recorded other recognition languages than it opened with (``call_languages``): the provider takes
them only at a session's setup. The owner's audio heard while a session renews waits in a
bounded backlog and reaches the next session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from ouroboros.call_languages import recorded_call_languages

log = logging.getLogger(__name__)

# Physical bounds: how long before a goodbye's deadline the renewal still waits for a completed
# turn, and how much of the owner's audio one renewal's gap carries to the next session.
GO_AWAY_MARGIN_S = 2.0
GAP_AUDIO_MS = 30_000.0
# A shared screen or camera sends an unchanged view again this often (call_video.js KEEPALIVE_MS), so a
# frame this fresh is still on show: a session that opens after it is shown it at once.
FRAME_KEEPALIVE_S = 15.0
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
        self.frame: Optional[Tuple[float, Any]] = None  # (when, the newest frame a peer showed)
        self.held_at_drop = 0  # the least audio a session of this call held before the provider dropped turns
        self.drops = 0         # how often the provider dropped a session's turns in this call
        # The languages the next session's recognition expects: the record when the call starts, then
        # what the voice records during it (``live_tool_batch``).
        spec = getattr(call, "spec", None)
        self.languages: Tuple[str, ...] = recorded_call_languages(
            getattr(call, "drive_root", None), getattr(spec, "presence_binding_id", "") or "")
        self.reset((0, 0), self.languages)

    def reset(self, bounds: Tuple[int, int], languages: Tuple[str, ...] = ()) -> None:
        """A new session holds nothing yet; ``bounds`` is its ``(room, audio span)`` in provider tokens,
        the span no larger than what this call's sessions held before a drop; ``languages`` are the
        recognition languages it opened with."""
        self.room, span = bounds
        self.opened_languages = tuple(languages)
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
        else:
            self.audio_growth = max(self.audio_growth, audio - self.held_audio)  # a session opens holding no audio
            if self.held_prompt:
                self.growth = max(self.growth, prompt - self.held_prompt)
        self.held_prompt, self.held_audio = prompt, audio

    def owner_caption(self, text: str, final: bool) -> None:
        self.owner_speaking = bool(str(text or "").strip()) and not final

    def go_away(self, time_left_s: float) -> None:
        self.go_away_by = time.monotonic() + max(0.0, float(time_left_s or 0) - GO_AWAY_MARGIN_S)

    def due(self) -> str:
        if self.go_away_by is not None:
            return "go_away"
        if self.languages != self.opened_languages:
            return "languages"  # the provider takes recognition languages only when a session opens
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

    def saw(self, frame: Any) -> None:
        self.frame = (time.monotonic(), frame)

    def on_show(self, now: float) -> Any:
        """The newest frame while its view is still shared, for a session that opened after it."""
        return self.frame[1] if self.frame and now - self.frame[0] <= FRAME_KEEPALIVE_S else None

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


class Openings:
    """Dialogue acts addressed to the call, held for a completed turn: completing the client turn
    interrupts whatever is under way, so they go out only at ``SessionRenewal.at_turn_boundary``,
    in order with the images and acknowledgements held behind them, and then one bare completion
    opens one response for all of them (measured 18.10: gemini-3.8-live answers a bare
    ``turnComplete`` over the context it holds). The first is the call becoming audible, once per
    call (a renewal or a resumption continues the conversation): a fact, never a scripted greeting;
    the voice chooses its words from the room, in the correspondence's language."""

    def __init__(self, renewal: "SessionRenewal") -> None:
        self.renewal = renewal
        self.held: List[Any] = []
        self.live_told = False

    def live(self, presence: bool) -> Optional[Any]:
        """The fact that the call can be heard now, the first time a session of this call is live."""
        from ouroboros.gateways.realtime_engine import ContextItem

        if self.live_told:
            return None
        self.live_told = True
        text = ("This call is now live: the other side can hear you." if presence
                else "The owner has just opened this call and can hear you now.")
        return ContextItem(text=text, source="call_live", opens_response=True)

    def hold(self, item: Any) -> None:
        self.held.append(item)

    def take(self) -> List[Any]:
        """Everything held, in order, then the one completion that opens the response; content
        acknowledgements last, since they may be taken only once a session holds the content."""
        if not self.held or not self.renewal.at_turn_boundary():
            return []
        from dataclasses import replace

        from ouroboros.gateways.realtime_engine import ContextItem

        items, self.held = self.held, []
        content = [replace(item, opens_response=False) if isinstance(item, ContextItem) else item
                   for item in items if not isinstance(item, Acks)]
        return [*content, ContextItem(text="", source="", opens_response=True),
                *(item for item in items if isinstance(item, Acks))]
