"""Typed panel records and hardness vocabulary for every review surface.

Owns what a review run IS: the configured reviewer row, the request handed to
a panel, the per-actor record a slot produces, the aggregate run result, the
typed transport-failure fact keys, and the three hardness levels that name how
a surface enforces its verdict. Slot identity is separate from model identity,
so duplicate model IDs are valid rows. Extracted from
ouroboros/review_substrate.py (v7 D06 split, re-cut on the v7next tip);
review_substrate.py re-exports every name.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from ouroboros.review_execution import ReviewRouteKind, delivery_retrieves


# One semantic author-finality record shared by review owners.  Surfaces keep
# their existing storage and reviewer evidence; this vocabulary only makes an
# author's final stance explicit and hash-bound when a review is advisory.
AUTHOR_DISPOSITION_VALUES = frozenset({"accepted", "rejected", "partial", "deferred"})


def build_author_disposition(
    *,
    disposition: str,
    rationale: str,
    subject_hash: str,
    reviewer_signal: str = "",
    enforcement: str = "",
    source: str = "author",
    recorded_at: str = "",
) -> Dict[str, Any]:
    """Build one bounded, current-subject author-finality record.

    This is a record helper, not a second review ledger.  Callers persist the
    returned object in their existing plan/skill/acceptance/commit owners and
    continue to retain raw reviewer rows beside it.  A missing hash or reason
    is rejected so an author finish can never look like an unbound PASS.
    """
    value = str(disposition or "").strip().lower()
    reason = " ".join(str(rationale or "").split()).strip()
    subject = str(subject_hash or "").strip()
    if value not in AUTHOR_DISPOSITION_VALUES:
        raise ValueError("AUTHOR_DISPOSITION_INVALID: unknown disposition")
    if not subject:
        raise ValueError("AUTHOR_DISPOSITION_INVALID: subject_hash is required")
    if not reason:
        raise ValueError("AUTHOR_DISPOSITION_INVALID: rationale is required")
    if len(reason) > 8_000:
        raise ValueError("AUTHOR_DISPOSITION_INVALID: rationale is too large")
    if not recorded_at:
        from ouroboros.utils import utc_now_iso

        recorded_at = utc_now_iso()
    return {
        "disposition": value,
        "rationale": reason,
        "subject_hash": subject,
        "reviewer_signal": str(reviewer_signal or "").strip(),
        "enforcement": str(enforcement or "").strip().lower(),
        "recorded_at": str(recorded_at),
        "source": str(source or "author"),
    }


def validate_author_disposition(
    record: Any,
    *,
    subject_hash: str = "",
    allow_stale: bool = False,
) -> Optional[Dict[str, Any]]:
    """Validate and return a safe copy, rejecting malformed or stale records."""
    if not isinstance(record, dict):
        return None
    try:
        normalized = build_author_disposition(
            disposition=record.get("disposition", ""),
            rationale=record.get("rationale", ""),
            subject_hash=record.get("subject_hash", ""),
            reviewer_signal=record.get("reviewer_signal", ""),
            enforcement=record.get("enforcement", ""),
            source=record.get("source", "author"),
            recorded_at=record.get("recorded_at", ""),
        )
    except (TypeError, ValueError):
        return None
    expected = str(subject_hash or "").strip()
    if expected and normalized["subject_hash"] != expected and not allow_stale:
        return None
    return normalized


def build_author_disposition_from_mapping(
    value: Any, *, subject_hash: str, reviewer_signal: str = "", enforcement: str = "",
) -> Dict[str, Any]:
    """Parse the public two-field author finish envelope."""
    if not isinstance(value, dict) or set(value) - {"disposition", "rationale"}:
        raise ValueError("AUTHOR_DISPOSITION_INVALID: envelope fields are invalid")
    return build_author_disposition(
        disposition=value.get("disposition", ""), rationale=value.get("rationale", ""),
        subject_hash=subject_hash, reviewer_signal=reviewer_signal, enforcement=enforcement,
    )


def apply_review_model_override(slot: Any, overrides: Dict[str, dict], *, slot_id: str = "") -> Any:
    """Project an explicit owner model choice onto one frozen reviewer row.

    Identity, effort and delivery are immutable here. A referenced native actor
    remains native, and an agent-session row never becomes a raw model call.
    Settings and the original row are untouched; empty profile means Auto.
    """
    identity = slot_id or str(getattr(slot, "slot_id", "") or "")
    value = overrides.get(f"reviewer:{identity}")
    route = getattr(slot, "kind", getattr(slot, "route", ""))
    if not value or str(getattr(route, "value", route)) == "agent_session":
        return slot
    configured = hasattr(slot, "target_id")
    changes = {"target_id" if configured else "model": value["model"],
               "profile_id" if configured else "session_profile": value["model_account_override"],
               "use_local": bool(value["use_local"])}
    return replace(slot, **changes)


@dataclass(frozen=True)
class ReviewSlot:
    slot_id: str
    model: str
    effort: str = "medium"
    timeout_sec: Optional[float] = None
    max_tokens: int = 16_384
    temperature: float | None = None
    role_hint: str = ""
    use_local: bool = False
    # Delivery route for this slot. ``use_local`` above is the existing
    # precedent for a per-slot transport hint; ``route`` is the general axis.
    route: ReviewRouteKind = ReviewRouteKind.API_CHAT
    # agent_session rows only: THIS row's opaque ``harness[=model]`` target
    # (6.1 — every slot is independently harness-or-API). Empty falls back to
    # the shared session-route key, which is the whole legacy behavior.
    session_target: str = ""
    # Optional manual credential pin (Q2-в); '' = the daemon's rotation (D28).
    session_profile: str = ""
    transport_timeout_sec: Optional[float] = None
    # Optional configured-subagent binding (resolved at admission; '' = direct).
    subagent_id: str = ""
    # Host sampling hint, resolved at dispatch; an explicit temperature wins.
    default_temperature: float | None = None

    @property
    def native_retrieval(self) -> bool:
        # An api-route actor row: bounded native tool rounds, never the packet.
        return bool(str(self.subagent_id or "").strip()) and str(getattr(self.route, "value", self.route) or "") == ReviewRouteKind.API_CHAT.value

    @property
    def retrieves(self) -> bool:
        # DELIVERY class for admission/fit/authority; transport tests the route.
        return delivery_retrieves(self.route, self.subagent_id)


@dataclass
class ReviewRequest:
    surface: str
    goal: str
    scope: str = ""
    subject: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    checklist: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    slot_messages: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    call_type: str = ""
    max_tokens: int | None = None
    temperature: float | None = None
    no_proxy: bool = False
    # RETRIEVING deliveries own a compact task and repository root: the session
    # route AND native API-route rows (`session_root`, `slot_session_tasks`).
    session_root: str = ""
    session_task: str = ""
    slot_session_tasks: Dict[str, str] = field(default_factory=dict)  # per-slot work order over session_task
    session_threads: Dict[str, str] = field(default_factory=dict)
    usage_attribution: Dict[str, str] = field(default_factory=dict)
    deadline_at: str = ""
    retry_key: str = ""
    reconcile_only: bool = False
    # Existing surface fingerprints, carried only to physical provenance.
    reconciliation_identity: Dict[str, Any] = field(default_factory=dict)
    task_attempt: Any = None
    default_temperature: float | None = None


@dataclass
class ReviewActorRecord:
    slot_id: str
    model: str
    status: str
    raw_text: str = ""
    parsed: Any = None
    # Per-actor parsed verdict (PASS/FAIL/DEGRADED/UNKNOWN). Carried here so the
    # objective axis can aggregate outcome_tier from only the actors that
    # CONTRIBUTED to a quorum PASS, instead of re-deriving the verdict downstream.
    signal: str = ""
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    prompt_ref: Dict[str, Any] = field(default_factory=dict)
    response_ref: Dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    # Compact typed truth for task-result/event projection.  Raw model output
    # remains in the existing private audit record; these fields prevent UI
    # consumers from conflating a transport failure, malformed JSON, and a
    # valid semantic DEGRADED verdict.
    transport_status: str = ""
    # B1 typed failure facts, allowlist-carried off the exception's ATTRIBUTES
    # (generic across every ClaudexorUnavailable subclass; never exc.__dict__):
    # the machine code, the healing instant and the HTTP status survive the
    # substrate as fields instead of flattening into `error` prose.
    failure_code: str = ""
    reset_at: str = ""
    http_status: Optional[int] = None
    parse_status: str = ""
    semantic_verdict: str = ""
    provider: str = ""
    actor_role: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)
    # Participation is independent of agreement with the aggregate: every
    # contract-valid PASS/FAIL response counts, while enforcement_impact says
    # whether that participant supports completion or vetoes it.
    quorum_contribution: bool = False
    reason: str = ""
    enforcement_impact: str = ""
    # Physical operation identity survives a logical timeout.  A pending actor
    # is custody/reconciliation state, not permission for a blind resend.
    operation_id: str = ""
    operation_state: str = "settled"
    late_result_pending: bool = False
    recovery_binding: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewRunResult:
    request: Dict[str, Any]
    actors: List[Dict[str, Any]]
    parsed_findings: List[Dict[str, Any]]
    aggregate_signal: str
    degraded: bool = False
    degraded_reasons: List[str] = field(default_factory=list)
    # Bible P3: a single configured reviewer is honored but the lost cross-model
    # diversity is recorded LOUDLY and DURABLY here (centralized for every surface
    # that runs through ReviewCoordinator — acceptance, etc. — so a one-slot review
    # can never quietly look like an ordinary multi-reviewer PASS).
    single_reviewer_no_diversity: bool = False
    panel_id: str = ""


HARDNESS_ADVISORY_VISIBLE = "advisory_visible"  # fed back as a compact capsule, never blocks


HARDNESS_LABEL_ONLY = "label_only"              # recorded on the objective axis, not shown


HARDNESS_HARD_GATE = "hard_gate"                # blocking commit/scope immune gate (unchanged)


TYPED_FAILURE_FACT_KEYS = ("failure_code", "reset_at", "http_status", "transport_status")
