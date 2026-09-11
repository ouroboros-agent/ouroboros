"""Session delivery for the commit scope reviewer (phase 5.2/5.6/5.7).

The api pack and the session task are TWO deliveries of ONE review: same role,
checklist, output contract, calibration and intent context. This module owns
only what is session-specific — retrieval pointers instead of assembled
evidence, governance docs as navigation maps, and the forensic (never gating)
coverage manifest. Everything shared is imported from its existing owner, so
the two deliveries cannot drift apart.

Imports from ``scope_review`` are lazy and one-way at call time: this module is
itself imported lazily by ``run_scope_review``, so neither import can cycle at
module load.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ouroboros.tools.review_helpers import (
    CRITICAL_FINDING_CALIBRATION,
    build_goal_section,
    build_rebuttal_section,
    build_scope_section,
    load_checklist_section,
    load_governance_doc,
)
from ouroboros.tools.review_synthesis import build_scope_review_prompt

# What the coverage manifest calls this delivery (D-12). The value names the
# DELIVERY — the reviewer retrieved the surface itself — rather than the wire it
# arrived on; `agent_session` is the transport's own name in `ReviewRouteKind`,
# and reusing it made the manifest describe the route twice and the delivery not
# at all.
#
# D-12 also asked that readers stay compatible with the old spelling. Measured,
# there are NO readers: nothing in `ouroboros/` or `web/` reads this key. The
# manifest is a durable forensic row whose consumer is a person reading it, so
# the rename cannot break a caller, and a compatibility helper here would be
# machinery guarding nothing. Stated rather than built.
AGENTIC_RETRIEVAL_DELIVERY = "agentic_retrieval"


def governance_nav_maps(repo_dir: pathlib.Path, doc_paths: Tuple[str, ...]) -> str:
    """The canonical governance docs as navigation maps (5.7).

    Session delivery replaces the inlined canonical texts with the atlas idea
    reduced to a MAP: every ``##`` through ``####`` heading with its inclusive
    complete-subtree line range, read on demand by the session with its own tools.
    ``generate_doc_nav_map`` is the one existing mapper — no second repository
    scanner (§8 item 8)."""
    from ouroboros.context_layout import generate_doc_nav_map

    parts: list[str] = []
    for rel_path in doc_paths:
        text = load_governance_doc(repo_dir, rel_path, on_missing="placeholder")
        if str(text or "").strip():
            parts.append(generate_doc_nav_map(text, title=rel_path, rel_path=rel_path))
    return (
        "The governance docs are NOT inlined in session delivery. The maps below "
        "index them by line range; the paths are relative to the repository root — "
        "read the sections you need with your own tools.\n\n" + "\n\n---\n\n".join(parts)
    )


@dataclass(frozen=True)
class ScopeIntentContext:
    """The intent/history cluster BOTH scope deliveries take, as one value.

    These five always travel together — the api pack builder and the session task
    builder each need exactly this set — so they ride as one immutable parameter
    object (the ``ReviewAssignment`` pattern) instead of five parallel arguments
    that a caller can silently mis-order.
    """

    goal: str = ""
    scope: str = ""
    review_rebuttal: str = ""
    review_history: Optional[list] = None
    scope_review_history: Optional[list] = None


def build_scope_session_task(
    repo_dir: pathlib.Path,
    commit_message: str,
    intent: ScopeIntentContext,
    drive_root: Optional[pathlib.Path] = None,
    governance_repo_dir: Optional[pathlib.Path] = None,
    managed_subject: Optional[Any] = None,
    task_evidence_section: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """The scope task in SESSION delivery, plus its forensic coverage manifest.

    Same role, checklist, contract, calibration and intent context as the api
    pack (5.3), with retrieval pointers instead of assembled evidence (5.2): no
    touched-file pack, no inlined diff, no atlas. The instruction text is the
    SAME builder the api pack uses, so the two deliveries cannot drift apart.

    The returned manifest is FORENSICS, not a gate (5.6/D16): it records that
    coverage is the session's own retrieval, and that the host did not observe
    which files it opened, as the non-blocking
    ``host_file_read_attestation`` fact. That is a provenance limit on what may
    be CLAIMED about coverage, not a finding that the review was incomplete
    (BIBLE P3, retrieving scope reviewers). The atlas's
    ``excluded_sensitive`` class is preserved on the host side (nothing
    sensitive is assembled at all here); what the harness reads with its own
    tools is not host-filtered, and the manifest says so instead of implying an
    attestation nobody performed."""
    from ouroboros.tools.scope_review import (
        _CANONICAL_CONTEXT_DOCS,
        _build_review_history_section,
        _build_scope_history_section,
    )

    goal, scope, review_rebuttal = intent.goal, intent.scope, intent.review_rebuttal
    review_history, scope_review_history = intent.review_history, intent.scope_review_history

    scope_checklist = load_checklist_section("Intent / Scope Review Checklist")
    if not str(scope_checklist or "").strip():
        raise RuntimeError(
            "Intent / Scope Review Checklist could not be loaded from docs/CHECKLISTS.md — "
            "scope review cannot run without its checklist (fail-closed)."
        )
    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)
    rebuttal_section = build_rebuttal_section(review_rebuttal)
    open_obligations = []
    if drive_root is not None:
        try:
            from ouroboros.review_state import load_state, make_repo_key
            state = load_state(pathlib.Path(drive_root))
            open_obligations = state.get_open_obligations(repo_key=make_repo_key(repo_dir))
        except Exception:
            open_obligations = []  # Non-fatal: best-effort hint
    history_section = _build_review_history_section(
        review_history or [], open_obligations=open_obligations,
    )
    scope_history_section = _build_scope_history_section(scope_review_history)
    nav_docs = governance_nav_maps(
        pathlib.Path(governance_repo_dir or repo_dir), _CANONICAL_CONTEXT_DOCS,
    )
    if managed_subject is not None and not managed_subject.fallback_full_diff:
        # Managed resolution (Δ4): the AUTHORITATIVE delta artifact is inlined —
        # a session that retrieved `git diff --cached` itself would re-review the
        # whole two-parent candidate instead of the resolver's work.
        touched_slot = (
            "(managed resolution — the reviewed path set is the resolution delta "
            "plus its conflict anchors: "
            + (", ".join(managed_subject.touched_paths()) or "(none)") + ")"
        )
        diff_slot = (
            "The inlined artifact below is the AUTHORITATIVE review subject for "
            "this managed-update resolution commit. Judge it as rendered; do NOT "
            "substitute your own `git diff --cached` — the staged diff is the "
            "whole two-parent merge candidate and re-renders already-released "
            "code. Read the touched files with your own tools as needed.\n\n"
            + managed_subject.render_prompt_diff()
        )
    else:
        touched_slot = (
            "(not inlined — session delivery: find the touched files yourself, with "
            "`git diff --cached --name-status` if your read-only mode lets you run "
            "commands and by reading the tree if it does not)"
        )
        diff_slot = (
            "(not inlined — session delivery: retrieve the staged change in this "
            "repository root yourself, with `git diff --cached` when your read-only "
            "mode permits commands and by reading the files when it does not)"
        )
        if managed_subject is not None:  # M0 missing: disclose, keep retrieval
            # Session delivery renders no diff body: the header's fallback line
            # must instruct retrieval, never claim a rendering below.
            diff_slot = f"{managed_subject.header(body_rendered=False)}\n\n{diff_slot}"
    task_text, _stable_len = build_scope_review_prompt(
        touched_slot,
        scope_checklist=scope_checklist,
        canonical_docs=nav_docs,
        intent_context=f"{scope_section}\n\n{goal_section}",
        history_block=f"{rebuttal_section}{history_section}{scope_history_section}",
        diff_text=diff_slot,
        repo_pack_placeholder=(
            "(no assembled repository pack in session delivery — the navigation "
            "maps above index the governance docs; retrieve everything else with "
            "your own tools)"
        ),
        critical_calibration=CRITICAL_FINDING_CALIBRATION,
        task_evidence_section=task_evidence_section,
    )
    manifest: Dict[str, Any] = {
        # D-12's ratified spelling. It is deliberately NOT `agent_session`: that
        # is the TRANSPORT's name (`ReviewRouteKind`), and reusing it here made
        # the manifest answer "how was this delivered" with the name of the wire
        # rather than of the delivery. What this field states is that the
        # reviewer RETRIEVED the surface itself. Nothing reads this key, so the
        # rename breaks no caller — see the constant's own note.
        "delivery": AGENTIC_RETRIEVAL_DELIVERY,
        "coverage": "agent_retrieval",
        # Non-blocking by construction (D16): forensics, never a gate, and the
        # fixed_overflow ladder does not apply to sessions (5.7).
        "host_file_read_attestation": "unobserved",
        "coverage_note": (
            "the reviewer session retrieves context with its own tools; the host "
            "assembled no pack and does not observe which files the session opened"
        ),
        "excluded_sensitive": {"policy": "preserved", "host_enforced": False},
        "nav_mapped_docs": list(_CANONICAL_CONTEXT_DOCS),
    }
    return task_text, manifest


# --------------------------------------------------------------------------
# Session authority (the floor this delivery's verdict actually rests on)
# --------------------------------------------------------------------------
# The api (push) delivery's authority rests on the whole assembled pack fitting
# the reviewer's context, so its floor is the constitutional >=1M. This delivery
# assembles NO pack, so that floor is not its test — but a verdict may only gate
# a commit if the reviewer's window has been ESTABLISHED. Hence: SOURCED evidence
# (confirmed | asserted) at or above SESSION_WINDOW_FLOOR.
#
# The floor coincides numerically with scope_review's conservative fallback
# window, which is exactly why PROVENANCE and not the number is the gate: a
# reviewer nobody ever probed resolves to that same number under
# `unknown_conservative`, and admitting it would be "assumed adequate" — the one
# thing BIBLE P3 forbids ("treated as too small rather than assumed adequate").
SESSION_WINDOW_FLOOR = 200_000
SOURCED_PROVENANCE = frozenset({"confirmed", "asserted"})


def session_window_is_authoritative(window: Any, provenance: str) -> bool:
    """Whether a retrieving row's window evidence can carry a BLOCKING verdict."""
    return str(provenance or "") in SOURCED_PROVENANCE and int(window or 0) >= SESSION_WINDOW_FLOOR


def _unproven_window_sentence(scope_model: str, phrase: str) -> str:
    """The ONE sentence naming why this row's window authorises nothing.

    Both the advisory finding and the block message are built from it, so the
    disclosure the owner reads and the reason the commit stopped cannot drift."""
    return (
        f"retrieving scope reviewer {scope_model} has a {phrase}, which does not meet "
        f"the >={SESSION_WINDOW_FLOOR} SOURCED-evidence floor this owner-declared "
        "agentic delivery needs to carry a blocking verdict (BIBLE P3: a window that "
        "cannot be established by evidence is treated as too small, never assumed "
        "adequate)."
    )


def _restoration_sentence() -> str:
    """What the owner can do to get an authoritative verdict back."""
    return (
        "Owner-ack this route's window (the scope-slot save offers the ack), configure "
        "a reviewer with confirmed window evidence, or deliver this row over the api "
        "route to restore an authoritative verdict."
    )


def session_window_unproven_finding(scope_model: str, phrase: str) -> Dict[str, Any]:
    """Disclosure for a retrieving row whose window is not SOURCED >= the floor.

    ``phrase`` is the caller's already-formatted provenance wording, so the
    honest four-way phrasing keeps its single owner in ``scope_review``."""
    return {
        "verdict": "FAIL",
        "severity": "advisory",
        "item": "scope_review_session_window_unproven",
        "reason": (
            f"⚠️ SCOPE_SESSION_ADVISORY_ONLY: {_unproven_window_sentence(scope_model, phrase)} "
            "Its findings are ADVISORY-ONLY and are not counted toward the authoritative "
            "scope quorum; the row therefore cannot supply the authoritative scope verdict "
            f"required to commit. {_restoration_sentence()}"
        ),
        "model": scope_model,
    }


def session_window_unproven_block_message(scope_model: str, phrase: str) -> str:
    """The retrieving row's BLOCK message — the twin of the api row's sub-floor one."""
    return (
        f"⚠️ SCOPE_REVIEW_BLOCKED: {_unproven_window_sentence(scope_model, phrase)} Its "
        "findings were preserved as advisory evidence, but it cannot supply the "
        f"authoritative scope verdict required to commit. {_restoration_sentence()}"
    )


def session_scope_authority(
    critical_findings: list,
    advisory_findings: list,
    *,
    scope_model: str,
    window: Any,
    provenance: str,
    phrase: str,
    result_kwargs: Dict[str, Any],
) -> Tuple[list, list, Any]:
    """The retrieving delivery's whole authority decision.

    ``(critical, advisory, early_result)``: with SOURCED evidence at or above the
    floor the row keeps blocking authority and nothing changes (``early_result``
    is None, so the caller proceeds to its normal blocking logic). Otherwise its
    criticals are PRESERVED as advisory evidence (never discarded — a real finding
    stays readable), tagged so their origin is unmistakable, the disclosure names
    what would restore an authoritative verdict, and the typed
    ``session_advisory`` result is returned.

    That result BLOCKS, exactly as the api row's ``sub_floor`` twin does. The two
    halves of the same sentence are easy to confuse, so both are stated: the row's
    FINDINGS are advisory (an unestablished window cannot certify a verdict) and
    the PANEL is short one authoritative verdict (which is what stops the commit).
    Returning ``blocked=False`` here made the blocking scope gate of BIBLE P3 fail
    OPEN: a panel of retrieving rows produced zero authoritative verdicts, the
    aggregate's partial-quorum shortfall only fires above zero responders, and the
    commit sailed through a gate the owner had every reason to believe was armed —
    while the identical api panel blocked. Authority is decided in ONE place per
    delivery (here for retrieving rows, ``_apply_scope_authority`` for api rows);
    the quorum aggregate stays a discloser, never a second decider."""
    if session_window_is_authoritative(window, provenance):
        return critical_findings, advisory_findings, None
    for finding in critical_findings:
        finding["severity"] = "advisory"
        finding["reason"] = "[advisory-only session scope reviewer] " + str(finding.get("reason", ""))
    advisory = list(critical_findings) + list(advisory_findings)
    advisory.append(session_window_unproven_finding(scope_model, phrase))
    from ouroboros.tools.scope_review import ScopeReviewResult

    return [], advisory, ScopeReviewResult(
        blocked=True,
        block_message=session_window_unproven_block_message(scope_model, phrase),
        critical_findings=[], advisory_findings=advisory,
        status="session_advisory", **result_kwargs,
    )
