"""Realtime speech engines: one event vocabulary for every live voice provider.

A live call is an ordinary Ouroboros turn whose model is a realtime speech model
(ARCHITECTURE §6 "Live calls"). This module is the outbound vocabulary of that model
transport and nothing more (DEVELOPMENT "Gateway Boundary Pattern"): the typed
commands the live driver sends, the typed events an engine emits, and the registry
that maps an engine id to its adapter. Which context, which tools, when to speak and
when to stay quiet belong to the mind and the live driver, never to an adapter.

Adapters (``realtime_openai``, ``realtime_gemini``) translate this vocabulary to one
provider wire and back. Every event keeps the provider's own identifiers so the
ledger, the record and reconnect reconciliation can bind to them; audio is PCM16
little-endian mono at the rate the route declares, and conversion between device
and engine rates happens at the peer boundary, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, Mapping, Protocol, Tuple, Union

ENGINE_OPENAI = "openai"
ENGINE_GOOGLE = "google"
ENGINE_IDS = (ENGINE_OPENAI, ENGINE_GOOGLE)

SPEAKER_OWNER = "owner"
SPEAKER_OUROBOROS = "ouroboros"

# Gemini Live result scheduling; OpenAI ignores it (a late result plus one
# coalesced ``response.create`` is its only delivery form). Barge-in and Stop use
# ``Interrupt``; no host rule decides that a result deserves to cut the speech.
SCHEDULE_WHEN_IDLE = "when_idle"
SCHEDULE_SILENT = "silent"

# The one usage shape every adapter fills on ``ResponseDone``/``TranscriptionUsage``
# (missing = 0); the provider's own fields stay beside it in ``raw_usage``. Audio
# counts never fold into text counts: they are priced separately.
USAGE_KEYS = (
    "text_input_tokens", "audio_input_tokens", "image_input_tokens", "cached_input_tokens",
    "text_output_tokens", "audio_output_tokens",
)


@dataclass(frozen=True)
class EngineRoute:
    """The selected realtime route: engine id, provider model id and voice.

    ``input_rate``/``output_rate`` are the PCM16 mono rates the provider wire
    takes and returns; the adapter that owns the wire fills its defaults.
    """

    engine: str
    model: str
    voice: str = ""
    input_rate: int = 24000
    output_rate: int = 24000


@dataclass(frozen=True)
class SessionConfig:
    """Everything an engine needs to open or re-open one provider session.

    ``instructions`` is the turn's fitted system text (tier-0 whole); ``tools``
    are ordinary registry schemas in the OpenAI function shape; ``history`` is a
    host-built view of earlier dialogue items for a rollover or a fresh session.
    ``resume_handle`` lets an engine that supports provider-side resumption
    continue a session instead of rebuilding it.
    """

    route: EngineRoute
    instructions: str
    tools: Tuple[Mapping, ...] = ()
    history: Tuple[Mapping, ...] = ()
    native_search: bool = False
    resume_handle: str = ""


# ---- commands: driver -> engine -------------------------------------------------

@dataclass(frozen=True)
class AudioIn:
    pcm: bytes
    rate: int


@dataclass(frozen=True)
class ContextItem:
    """Text the session should know: typed owner words, another turn's reply,
    a task result, a peer-observed fact. ``source`` names its provenance in the
    prefix the mind reads. It never cuts the current speech; the mind decides
    whether and when to speak about it."""

    text: str
    source: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str
    images: Tuple[bytes, ...] = ()
    scheduling: str = SCHEDULE_WHEN_IDLE


@dataclass(frozen=True)
class UpdateTools:
    tools: Tuple[Mapping, ...]


@dataclass(frozen=True)
class PlayedAudio:
    """The owner's peer reports how much of a spoken item it actually rendered."""

    item_id: str
    played_ms: int


@dataclass(frozen=True)
class Interrupt:
    item_id: str = ""
    played_ms: int = 0


@dataclass(frozen=True)
class Close:
    reason: str


EngineCommand = Union[AudioIn, ContextItem, ToolResult, UpdateTools, PlayedAudio, Interrupt, Close]


# ---- events: engine -> driver ---------------------------------------------------

@dataclass(frozen=True)
class Ready:
    effective: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class AudioOut:
    pcm: bytes
    rate: int
    item_id: str


@dataclass(frozen=True)
class Caption:
    speaker: str
    text: str
    final: bool
    item_id: str = ""


@dataclass(frozen=True)
class SpeechStarted:
    """Input speech started (the provider's turn detection).

    ``item_id`` names the input item and ``audio_start_ms`` its position in the
    session's input audio, when the provider reports them; the host reads who fed
    that stretch of audio from them.
    """

    item_id: str = ""
    audio_start_ms: int = -1


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCallCancelled:
    call_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ResponseStarted:
    """A billed model response began: the ledger reserves here, settles on ``ResponseDone``."""

    item_id: str
    response_id: str = ""


@dataclass(frozen=True)
class ResponseDone:
    """One billed model response; ``usage`` uses ``USAGE_KEYS``, ``raw_usage`` the provider's own."""

    item_id: str
    status: str
    usage: Mapping = field(default_factory=dict)
    raw_usage: Mapping = field(default_factory=dict)
    response_id: str = ""


@dataclass(frozen=True)
class TranscriptionUsage:
    """Usage of a separate transcription model the engine ran for captions (its own price)."""

    model: str
    usage: Mapping = field(default_factory=dict)
    raw_usage: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class ToolsApplied:
    names: Tuple[str, ...]


@dataclass(frozen=True)
class ResumeCheckpoint:
    handle: str
    resumable: bool


@dataclass(frozen=True)
class GoAway:
    time_left_s: float


@dataclass(frozen=True)
class EngineError:
    kind: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Closed:
    reason: str


EngineEvent = Union[
    Ready, AudioOut, Caption, SpeechStarted, ToolCall, ToolCallCancelled, ResponseStarted,
    ResponseDone, TranscriptionUsage, ToolsApplied, ResumeCheckpoint, GoAway, EngineError, Closed,
]


class LiveEngine(Protocol):
    """One provider session. ``events()`` ends after ``Closed``."""

    async def open(self, config: SessionConfig) -> None: ...

    async def send(self, command: EngineCommand) -> None: ...

    def events(self) -> AsyncIterator[EngineEvent]: ...

    async def close(self, reason: str) -> None: ...


_FACTORIES: Dict[str, Callable[[], LiveEngine]] = {}


def register_engine(engine_id: str, factory: Callable[[], LiveEngine]) -> None:
    """Adapters register themselves at import; a later registration replaces it."""
    _FACTORIES[engine_id] = factory


def create_engine(route: EngineRoute) -> LiveEngine:
    """A fresh adapter for ``route.engine``; an unknown id is a typed refusal."""
    factory = _FACTORIES.get(route.engine)
    if factory is None:
        raise KeyError(f"realtime engine not available: {route.engine!r}")
    return factory()
