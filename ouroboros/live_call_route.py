"""The realtime route of a live call and what the host knows about it before the call opens.

A call's engine route is resolved BEFORE the turn's context is fitted, because the
fit must measure against the realtime window and the realtime reply allowance, not
Main's. Those numbers are not a table the host trusts blindly (BIBLE: external
capabilities are sourced, auditable evidence): a known realtime route's facts are
written into the capability-evidence store as what they are, documented facts with
their source and read date, never as metadata the provider confirmed on the wire,
so the ordinary context fit, the record and a later audit all read the same fact
with the same provenance; an unknown route stays unknown and the fit keeps its own
unknown-window behaviour. The fit against the route (``call_context_plan``) is the
whole fit: a setup the provider still refuses ends the call with the provider's
words. The call's initial Nano tool view and the owner's duration ceiling live here
too, so the agent (context preparation) and the driver (the session) read one answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.call_languages import SET_CALL_LANGUAGES_SCHEMA
from ouroboros.call_protocol import CallSpec
from ouroboros.gateways.realtime_engine import ENGINE_GOOGLE, GOOGLE_INPUT_RATE, GOOGLE_OUTPUT_RATE, EngineRoute
from ouroboros.tool_capabilities import COGNITIVE_MEMORY_TOOL_NAMES

log = logging.getLogger(__name__)

# The call's initial Nano view: memory of the mind, the web, and the room's chat (what
# reads better than it sounds — links, code, lists — the voice writes there itself);
# read_file, because every retained host section the call view points at is a
# runtime_data read and a Gemini session reopens to gain a tool; and start_task,
# because long work leaves the call as a task whose result reaches it through live
# awareness; and sight: view_image looks at an image a tool or the owner offers, browse_page
# can look at a page, send_photo hands the owner an image; and the started task's progress,
# result and a word to it (peek_task, get_task_result, tell_task), never a wait: the call has
# one tool lane, and a task's result reaches the call by itself. Everything else is one
# ``enable_tools`` away (growth reaches the engine as ``UpdateTools``).
CALL_INITIAL_TOOL_NAMES: Tuple[str, ...] = tuple(sorted(
    COGNITIVE_MEMORY_TOOL_NAMES | {"web_search", "send_user_message", "read_file", "start_task",
                                   "view_image", "browse_page", "send_photo",
                                   "peek_task", "get_task_result", "tell_task"}))
# The text loop's own request to compact its history: a call never applies it, so its schema
# does not spend the call's window.
CALL_INERT_TOOL_NAMES = frozenset({"compact_context"})
# The call's own hang-up, declared to the session beside the registry's tools and answered by the
# call itself (no tool runs): an owner call ends once the goodbye it asked to say has been heard.
END_CALL_TOOL = "end_call"
END_CALL_SCHEMA = {"type": "function", "function": {
    "name": END_CALL_TOOL,
    "description": ("Hang up this live call when the owner says goodbye or asks to end it. Say your goodbye "
                    "first; the call ends once it has been heard."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
END_CALL_RESULT = "The call ends once your goodbye has been heard."


def call_local_schemas(spec: Optional[CallSpec]) -> Tuple[Dict[str, Any], ...]:
    """The call's own tools beside the registry's: every call records the languages its recognition
    expects (``call_languages``); an owner call can hang up, while a Presence call belongs to its
    binding, which decides when it ends."""
    if spec is None:
        return ()
    return (SET_CALL_LANGUAGES_SCHEMA,) if spec.presence_binding_id else (END_CALL_SCHEMA, SET_CALL_LANGUAGES_SCHEMA)


def call_local_tool_names(spec: Optional[CallSpec]) -> frozenset:
    return frozenset(schema["function"]["name"] for schema in call_local_schemas(spec))
REALTIME_EVIDENCE_KIND = "realtime_windows"   # capability_evidence.json namespace of the documented windows
# A speech-sized reply: about five minutes of spoken answer, the physical allowance a call's
# fit leaves for one response. A provider's output maximum is documentation, not this budget.
CALL_REPLY_ALLOWANCE_TOKENS = 8_192
# The newest stretch of a call one session holds as audio before it is renewed with that stretch
# as text (``live_call_state``): a price and window bound in the route's tokens, since every answer
# re-bills what the session holds and audio counts several times its own words as text. About ten
# minutes of speech at the design's rate; the real rate is in each response's AUDIO count.
CALL_AUDIO_SPAN_TOKENS = 12_000


@dataclass(frozen=True)
class RouteFact:
    """A documented fact about a realtime route, with its source and read date.

    ``window_tokens`` is the input the route holds (Gemini Live's input token limit).
    ``max_output_tokens`` is the provider's documented output maximum, recorded as
    evidence and never the fit's reserve; ``reply_allowance_tokens`` is what the call's
    fit leaves for one spoken response.
    """

    window_tokens: int
    max_output_tokens: int
    session_cap_minutes: Optional[int]
    source_url: str
    read_on: str
    reply_allowance_tokens: int = CALL_REPLY_ALLOWANCE_TOKENS
    # How many of the route's tokens one chars/4 estimate token costs until the route's own witness
    # exists (a first call, or a witness past its freshness): measured, conservative, never 1.0 by default.
    cold_density: float = 1.0
    # The most audio a spoken session was seen to hold before the provider dropped its oldest turns
    # (0 = never seen): the renewal's audio span starts here and a call's own drops lower it.
    held_audio_tokens: int = 0
    # The route thinks before it speaks (models.get ``thinking``): its setup takes a thinking level.
    thinking: bool = False


REALTIME_ROUTE_FACTS: Dict[Tuple[str, str], RouteFact] = {
    # Window and output maximum from the model page (also models.list); the cold density from Gemini's
    # own TEXT counts of a Russian-and-English memory view: 1.19-1.22 per estimate token (2026-10-16).
    # The held audio was measured 2026-10-18: four spoken sessions under an owner-scale call view
    # (about 92k text tokens) dropped every turn but the last exchange once they held 2.5-2.9k audio
    # tokens (two to three minutes of talk), with and without compression settings; a 7k view held 7k;
    # text-only turns held 116k with nothing dropped. The system instruction was never dropped.
    (ENGINE_GOOGLE, "gemini-3.8-live"): RouteFact(
        131_072, 65_536, None, "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live", "2026-10-16",
        cold_density=1.25, held_audio_tokens=2_500),
    # The same window and output maximum in models.get, with thinking true (2026-10-18); the cold density is
    # the family's until the route's own witness; its held audio was never measured.
    (ENGINE_GOOGLE, "gemini-3.8-live-extended-thinking"): RouteFact(
        131_072, 65_536, None, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-live-extended-thinking",
        "2026-10-18", cold_density=1.25, thinking=True),
}
EVIDENCE_SOURCE_DOCUMENTED = "documented_route_fact"   # the evidence store's source label for these rows


@dataclass(frozen=True)
class CallRoute:
    engine: EngineRoute
    fact: Optional[RouteFact]
    evidence_fp: str

    @property
    def window_tokens(self) -> Optional[int]:
        return self.fact.window_tokens if self.fact else None


@dataclass(frozen=True)
class CallContextPlan:
    spec: CallSpec
    route: CallRoute
    tool_schemas: List[Dict[str, Any]]
    fit_kwargs: Dict[str, Any]


def call_spec_of(task: Dict[str, Any]) -> Optional[CallSpec]:
    """The call this task is, or ``None`` for a text turn; malformed call metadata raises."""
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    raw = meta.get("live_call")
    return CallSpec.from_metadata(raw) if isinstance(raw, dict) and raw else None


def resolve_call_route(spec: CallSpec, drive_root: Any) -> CallRoute:
    """The engine route of the call; a known route's documented window becomes evidence."""
    from ouroboros.capability_evidence import route_fingerprint

    route = EngineRoute(engine=spec.engine, model=spec.model, voice=spec.voice,
                        input_rate=GOOGLE_INPUT_RATE, output_rate=GOOGLE_OUTPUT_RATE)
    fact = REALTIME_ROUTE_FACTS.get((spec.engine, spec.model))
    fp = route_fingerprint(provider=spec.engine, model=spec.model)
    if fact is not None:
        _record_window_evidence(drive_root, route, fact, fp)
    return CallRoute(route, fact, fp)


def _record_window_evidence(drive_root: Any, route: EngineRoute, fact: RouteFact, fp: str) -> None:
    """The shipped fact as evidence the fit can use, labelled as what it is.

    ``asserted`` from a documented source: the provider confirmed nothing on the
    wire, so the record never claims provider metadata; a reader sees the URL and
    the read date and can re-probe.
    """
    from ouroboros.capability_evidence import STATUS_ASSERTED, CapabilityEvidence, _store_evidence, canonical_evidence_root

    observed = f"{fact.read_on}T00:00:00Z"
    detail = f"documented context window; {fact.source_url}; read {fact.read_on}"
    evidence = CapabilityEvidence(
        window_tokens=fact.window_tokens, status=STATUS_ASSERTED, source=EVIDENCE_SOURCE_DOCUMENTED, route_fp=fp,
        model=route.model, provider=route.engine, ts=observed, detail=detail, provenance="provider_documentation",
        response_limit={"max_output_tokens": fact.max_output_tokens, "status": STATUS_ASSERTED,
                        "source": EVIDENCE_SOURCE_DOCUMENTED, "observed_at": observed, "route_fp": fp},
    )
    _store_evidence(drive_root if drive_root is not None else canonical_evidence_root(),
                    REALTIME_EVIDENCE_KIND, fp, evidence.to_json())


def window_evidence(drive_root: Any, spec: CallSpec) -> Optional[Dict[str, Any]]:
    """The recorded window evidence of the call's route (``None`` when unknown)."""
    from ouroboros.capability_evidence import _load, route_fingerprint

    record = _load(drive_root).get(REALTIME_EVIDENCE_KIND, {}).get(route_fingerprint(provider=spec.engine, model=spec.model))
    return dict(record) if isinstance(record, dict) else None


# A skill manifest's declaration that its tools open every owner call (``live_call: first_view``, an
# extra frontmatter key): the desktop skills declare it, so a call can look at the screen and press a
# key the moment it is asked, without reopening its session to load them.
CALL_VIEW_MANIFEST_KEY, CALL_VIEW_FIRST = "live_call", "first_view"


def call_view_extension_names(drive_root: Any) -> frozenset:
    """The loaded extension tools whose skill declares ``live_call: first_view``. Which of them a call
    sees is still the registry's answer (live, granted, permitted), like any other schema."""
    from ouroboros.extension_loader import _lock, _tools
    from ouroboros.skill_loader import find_skill

    with _lock:
        rows = [(name, str(tool.get("skill") or ""), str(tool.get("skills_repo_path") or "") or None)
                for name, tool in _tools.items()]
    declared: Dict[Tuple[str, Optional[str]], bool] = {}
    for key in {(skill, repo) for _name, skill, repo in rows if skill}:
        try:
            loaded = find_skill(drive_root, key[0], repo_path=key[1]) if drive_root else None
        except Exception:
            log.debug("call view: skill %s unreadable", key[0], exc_info=True)
            loaded = None
        extras = getattr(getattr(loaded, "manifest", None), "raw_extra", None) or {}
        declared[key] = isinstance(extras, dict) and extras.get(CALL_VIEW_MANIFEST_KEY) == CALL_VIEW_FIRST
    return frozenset(name for name, skill, repo in rows if declared.get((skill, repo)))


def initial_call_schemas(tools: Any, spec: Optional[CallSpec] = None) -> List[Dict[str, Any]]:
    """The call's initial view of the room's permitted schemas.

    An owner call opens with the Nano path, the call's initial names and the tools of
    skills that declare themselves part of a call (``call_view_extension_names``). A
    Presence call opens with every schema its binding's ceiling permits: the binding
    author chose that set, and the registry already narrowed it.
    """
    from ouroboros.tool_policy import initial_tool_schemas

    if spec is not None and spec.presence_binding_id:
        return initial_tool_schemas(tools, context_mode="max")
    ctx = getattr(tools, "_ctx", None)
    names = (*CALL_INITIAL_TOOL_NAMES, *sorted(call_view_extension_names(getattr(ctx, "drive_root", None))))
    return [schema for schema in initial_tool_schemas(tools, context_mode="nano", schema_names=names)
            if schema["function"]["name"] not in CALL_INERT_TOOL_NAMES]


def call_context_plan(task: Dict[str, Any], tools: Any, drive_root: Any) -> Optional[CallContextPlan]:
    """Resolve the route BEFORE the context is fitted; ``None`` for a text turn.

    The call's view (``call_view``) rides into the ordinary preparation with the
    realtime route itself: its identity names the window evidence and the calibration
    witness (``known_route``), its documented window is ``known_window_tokens`` and its
    reply allowance ``known_output_reserve_tokens``, so the fit measures against the
    realtime route, not Main's; an unknown route keeps the unknown-window behaviour.
    """
    spec = call_spec_of(task)
    if spec is None:
        return None
    route = resolve_call_route(spec, drive_root)
    fit_kwargs: Dict[str, Any] = {
        "known_route": {"provider": spec.engine, "model": spec.model, "route_fp": route.evidence_fp,
                        **({"cold_density": route.fact.cold_density} if route.fact else {})},
        "known_output_reserve_tokens": route.fact.reply_allowance_tokens if route.fact else CALL_REPLY_ALLOWANCE_TOKENS,
        "call_view": True,
    }
    if route.fact is not None:
        fit_kwargs["known_window_tokens"] = route.fact.window_tokens
    return CallContextPlan(spec, route, initial_call_schemas(tools, spec), fit_kwargs)


def _call_setting(key: str) -> str:
    from ouroboros.settings_integrity import runtime_setting

    return str(runtime_setting(key, "") or "").strip()


def call_minutes_ceiling(spec: CallSpec) -> Optional[int]:
    """The owner's ceiling for this call: the spec's, else the configured one for its kind."""
    if spec.max_minutes:
        return int(spec.max_minutes)
    raw = _call_setting("OUROBOROS_PRESENCE_CALL_MAX_MINUTES" if spec.presence_binding_id else "OUROBOROS_CALL_MAX_MINUTES")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


# How long a silence ends the owner's turn, by his Settings choice: Google advises 500-800 ms and
# defaults near 800, so Short is the low end of that advice and Long twice the default; Normal
# leaves the provider's own default in place.
PAUSE_SILENCE_MS = {"short": 500, "normal": 0, "long": 1600}


def call_turn_taking(spec: CallSpec, languages: Tuple[str, ...] = (), fact: Optional[RouteFact] = None) -> Dict[str, Any]:
    """The session's ``silence_ms`` and ``interruptions`` from what the owner chose for calls, the
    recognition ``language_hints`` the voice recorded (``call_languages``), and a thinking route's
    ``thinking_level`` (the owner's choice, low unless set; a route that does not think takes none)."""
    thinking = (_call_setting("OUROBOROS_CALL_THINKING_LEVEL") or "low").lower() if fact is not None and fact.thinking else ""
    return {"silence_ms": PAUSE_SILENCE_MS.get(str(spec.pause or "").lower(), 0), "interruptions": bool(spec.interruptions),
            "language_hints": tuple(languages), "thinking_level": thinking}


def native_search_enabled(spec: CallSpec) -> bool:
    """Gemini's own search grounding for this call, the owner's choice (on unless switched off)."""
    raw = (_call_setting("OUROBOROS_CALL_NATIVE_SEARCH") or "true").lower()
    return raw in ("1", "true", "yes", "on")
