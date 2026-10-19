"""One work, one status: the pure classification behind ``/api/state`` ``works``.

A work is one request — the owner's or Ouroboros's own — together with every
Continue episode and every live member (helper tasks, delegated runs still working,
answer upkeep still in flight). The census (``gateway/state.py``) keeps its
frozen per-root rows; this module groups the same facts into works and gives
each work one state: an activity word from a closed set, typed reasons with one
English sentence each, whose turn it is, and what can release it. Every surface
and Ouroboros's own context read the result; none derives a word of its own.

The vocabulary splits by what releases a state. ``paused`` stays until someone
presses Resume, and its reason says who or what stopped it (the owner,
Ouroboros, the app stopping, the spending limit, a technical problem).
``waiting`` continues when what it waits for happens: helpers of an earlier
attempt finishing, a model becoming available, the owner's sign-in or answer.
``turn`` names what waits for the owner; ``release`` says who can move it.

No I/O, no clocks, no thresholds: the server adapter (``work_projection``) and
the worker-side roster supply the observations; equal inputs give equal works.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

ACTIVITY_ORDER = ("working", "queued", "waiting", "paused", "unknown")
TRANSITIONS = ("none", "finalizing", "pausing")

# Hold reasons (supervisor/events_budget.py HOLD_*), read as data so this module imports nothing from the queue.
_WRITER_HOLD = "continuation_writer_unsettled"
_RESTART_HOLDS = {"owner_restart_hold": "after_restart", "saved_work_hold": "saved_on_stop"}
_SELECTION_HOLDS = {"root_fence_lifted_pending_selection", "root_fence_member_pending_selection"}

# One owner sentence per reason code (the TASK_CAUSE_PHRASES discipline): keyed on the code
# only, a code with no sentence stays raw, and the raw code stays typed beside it.
REASON_TEXT = {
    "earlier_attempt_finishing": ("Continue starts when the helper of the earlier attempt finishes",
                                  "Continue starts when {count} helpers of the earlier attempt finish"),
    "earlier_runs_finishing": ("Continue starts when the external run of the earlier attempt finishes",
                               "Continue starts when {count} external runs of the earlier attempt finish"),
    "earlier_work_unconfirmed": "Continue starts when the earlier attempt's work is confirmed finished",
    "helpers_running": ("1 helper still working", "{count} helpers still working"),
    "delegated_running": ("1 external run still working", "{count} external runs still working"),
    "owner_answer": "Waiting for your answer",
    "model_access": "The model needs you: sign in or restore access",
    "model_wait": "Waiting for the model to become available",
    "project_admission": "Waiting for Project verification",
    "queued": "Waiting for a free worker",
    "sleeping": "Sleeping until it is woken",
    "paused_by_owner": "Paused by you",
    "paused_by_mind": "Paused by Ouroboros",
    "paused_by_host": "Paused while the app stopped",
    "saved_on_stop": "Saved when the app stopped; Resume continues it",
    "after_restart": "Held after a restart; Resume continues it",
    "after_panic": "Stopped by Panic; Resume continues it",
    "spending_limit": "Spending limit reached",
    "technical_pause": "Paused after a technical problem; Resume continues it",
    "awaiting_selection": "Waiting for Ouroboros or you to resume it",
    "restore_failed": "Its saved state could not be restored",
    "review_work_finishing": "Review work finishing",
    "held": "Held",
}

# Reasons that only the owner's explicit action continues when the census says so (can_resume).
_OWNER_ACTION_CODES = {"saved_on_stop", "after_restart", "after_panic", "spending_limit", "technical_pause",
                       "restore_failed", "held"}


def reason(code: str, *, count: Optional[int] = None, ids: Iterable[str] = (), since: Any = None,
           raw: str = "") -> Dict[str, Any]:
    """One typed reason with its English sentence; an unknown code keeps its raw text."""
    phrase = REASON_TEXT.get(code)
    if isinstance(phrase, tuple):
        text = phrase[0] if count == 1 else phrase[1].format(count=count)
    else:
        text = phrase or code
    if code == "held" and raw:
        text = f"Held: {raw}"
    row: Dict[str, Any] = {"code": code, "text": text}
    if count is not None:
        row["count"] = count
    ids = [str(item) for item in ids if item]
    if ids:
        row["ids"] = ids
    if since:
        row["since"] = since
    return row


def _owner_wait(row: Mapping[str, Any]) -> str:
    """``owner_wait`` while a question positively waits for the owner, ``unknown`` on a gap, else ``''``.

    The census rule the cards apply (task_phase_chip.activityWaitPhase), on the server."""
    question = row.get("owner_wait") if isinstance(row.get("owner_wait"), dict) else row.get("required_question")
    question = question if isinstance(question, dict) else None
    if question:
        state = question.get("quiz_state") or question.get("state")
        if (question.get("owner_wait_state") == "resumed" or question.get("wait_ended_at")
                or state in {"answered", "expired_terminal", "superseded", "closed"}):
            return ""
        if question.get("owner_wait_state") == "waiting" or question.get("wait_for_answer") is True \
                or question.get("state") == "waiting":
            return "owner_wait"
        return "unknown"
    return "unknown" if row.get("required_question_unavailable") else ""


def _current_waits(row: Mapping[str, Any]) -> List[tuple]:
    attempt = int(row.get("task_attempt") or 1)
    waits = row.get("model_waits") if isinstance(row.get("model_waits"), dict) else {}
    return [(key, wait) for key, wait in waits.items()
            if isinstance(wait, dict) and wait.get("state") == "waiting" and wait.get("task_attempt") == attempt]


def _call(row: Mapping[str, Any]) -> str:
    """The exact-call word a working member is in (the cards' modelActivityPresentation)."""
    phase = row.get("phase")
    if phase == "preparing":
        return "preparing"
    activity = row.get("model_activity") if isinstance(row.get("model_activity"), dict) else {}
    operation = activity.get("phase")
    if operation in {"dispatching", "awaiting_response"}:
        return "sending"
    if operation == "preparing":
        return "preparing"
    if phase == "thinking":
        return "thinking"
    return "working"


def _paused_by(fence: Mapping[str, Any]) -> str:
    if fence.get("actor") == "mind":
        return "mind"
    requested = str(fence.get("requested_by") or "owner")
    return "owner" if requested == "owner" else "host"


def classify_member(row: Mapping[str, Any], raw: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Classify one census row (``decision_owners`` shape) with the raw facts the census drops.

    ``raw`` carries the queue row's hold and pause records, the owner fence's attribution and the
    task's own identity facts (see ``work_projection``). Returns the member's own state only."""
    raw = raw or {}
    task_id = str(row.get("activity_id") or "")
    phase = str(row.get("phase") or "unknown")
    state: Dict[str, Any] = {"task_id": task_id, "activity": "unknown", "transition": "none", "call": "",
                             "reasons": [], "turn": [], "release": "self", "paused_by": None, "confirmed": True}
    if phase == "unknown":
        state["confirmed"] = False
        return state
    hold = raw.get("hold") if isinstance(raw.get("hold"), dict) else {}
    hold = {} if hold.get("selected") else hold
    fence = raw.get("fence") if isinstance(raw.get("fence"), dict) else {}
    since = raw.get("paused_at") or row.get("started_at") or None
    if phase in {"budget_paused", "budget_pausing"}:
        cause = str(row.get("pause_cause") or "unknown")
        hold_reason = str(hold.get("reason") or "")
        code, activity, release = "", "paused", "owner"
        if hold_reason == _WRITER_HOLD:
            code, activity, release = "earlier_work_unconfirmed", "waiting", "self"
        elif hold_reason == "panic_hold":
            code = "after_panic"
        elif hold_reason in _RESTART_HOLDS:
            code = _RESTART_HOLDS[hold_reason]
        elif cause == "owner":
            state["paused_by"] = _paused_by(fence)
            code = f"paused_by_{state['paused_by']}"
            release = "mind" if state["paused_by"] == "mind" else "owner"
        elif cause == "budget":
            code = "spending_limit"
        elif cause == "authority":
            code = "technical_pause"
        elif hold_reason in _SELECTION_HOLDS:
            code, release = "awaiting_selection", "mind"
        elif hold_reason.startswith("restore_refused:"):
            code, release = "restore_failed", "none"
        elif cause == "sleep":
            code, activity = "sleeping", "waiting"
        elif cause == "restart":
            code = "after_restart"
        else:
            code, release = "held", "none"
        state["reasons"].append(reason(code, since=since, raw=hold_reason or cause))
        if phase == "budget_pausing" and activity == "paused":
            state.update(activity="working", transition="pausing", call="")
        else:
            state["activity"] = activity
        state["release"] = release
        if row.get("finishing_reviews"):
            state["reasons"].append(reason("review_work_finishing"))
        # Main's attention rule (main_decisions.owner_actions) is the turn: the census's explicit-Resume rule.
        if _owner_actions(row)[1]:
            state["turn"].append({"kind": "action", "task_id": task_id, "code": code})
            state["release"] = "owner"
        return state
    wait = _owner_wait(row)
    if wait == "owner_wait":
        question = row.get("owner_wait") if isinstance(row.get("owner_wait"), dict) else row.get("required_question")
        quiz = str((question or {}).get("quiz_id") or "")
        state.update(activity="waiting", release="owner")
        state["reasons"].append(reason("owner_answer"))
        state["turn"].append({"kind": "question", "task_id": task_id, **({"quiz_id": quiz} if quiz else {})})
        return state
    if wait == "unknown":
        state["confirmed"] = False
        return state
    waits = _current_waits(row)
    if waits:
        human = sorted(_owner_actions(row)[0])
        state["activity"] = "waiting"
        if human:
            state["release"] = "owner"
            state["reasons"].append(reason("model_access", ids=human))
            state["turn"].extend({"kind": "action", "task_id": task_id, "wait_id": key, "code": "model_access"}
                                 for key in human)
        else:
            state["reasons"].append(reason("model_wait", ids=[key for key, _ in waits]))
        if phase == "finalizing":
            state["transition"] = "finalizing"
        return state
    hold_fact = row.get("project_admission_hold") if isinstance(row.get("project_admission_hold"), dict) else {}
    if hold_fact:
        label = str(hold_fact.get("label") or "")
        state["activity"] = "waiting"
        entry = reason("project_admission")
        if label:
            entry["text"] = label
        state["reasons"].append(entry)
        return state
    if phase == "queued":
        state["activity"] = "queued"
        state["reasons"].append(reason("queued"))
        return state
    if phase == "finalizing":
        state.update(activity="working", transition="finalizing")
        return state
    if phase in {"working", "thinking", "preparing"}:
        state.update(activity="working", call=_call(row))
        return state
    state["confirmed"] = False
    return state


# Pauses the owner's Resume door releases: an owner/mind/host Pause over the tree, an explicit-Resume hold,
# a budget checkpoint, or a member waiting for selection. A writer hold releases itself; an unrestorable or
# unread hold has no Resume the door would accept (it answers cancel_or_new_run).
_RESUMABLE_CODES = {"paused_by_owner", "paused_by_mind", "paused_by_host", "saved_on_stop", "after_restart",
                    "after_panic", "spending_limit", "technical_pause", "awaiting_selection"}
_RUNNING_PHASES = {"working", "thinking", "preparing", "finalizing"}


def controls_for(current: Optional[Mapping[str, Any]], current_row: Optional[Mapping[str, Any]], *,
                 earlier_live: Iterable[str] = (), live_roots: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The actions the work's doors accept for its present state, each on its exact target.

    ``current``/``current_row`` are the live current episode's classified state and census row (None when the
    episode ended); ``earlier_live`` the earlier episode roots that still own live members; ``live_roots`` every
    episode root to stop, current first. The doors stay the authority: a refusal they still return is shown."""
    earlier = [str(task_id) for task_id in earlier_live if task_id]
    roots = [str(task_id) for task_id in live_roots if task_id]
    controls: List[Dict[str, Any]] = []
    if current is not None and current_row is not None:
        target = str(current.get("task_id") or "")
        phase = str(current_row.get("phase") or "")
        codes = {entry.get("code") for entry in current.get("reasons") or ()}
        # A live root, managed or a direct turn, and an answered root's upkeep: the hurry, stop and Pause doors
        # take them all; the Pause ends that upkeep at once.
        if current["activity"] == "working" and current.get("transition") in {"none", "finalizing"} \
                and phase in _RUNNING_PHASES:
            controls += [{"action": "wrap_up", "target": target}, {"action": "hurry", "target": target}]
        # A Pause still settling keeps its Pause: the door coalesces a repeat, which retries an uncertain reply.
        if current.get("transition") in {"none", "finalizing", "pausing"} \
                and current["activity"] in {"working", "queued", "waiting"} \
                and "earlier_work_unconfirmed" not in codes and phase != "budget_paused":
            controls.append({"action": "pause", "target": target})
        if current["activity"] == "paused" and phase == "budget_paused" and codes & _RESUMABLE_CODES:
            controls.append({"action": "resume", "target": target})
    for task_id in [*([str(current.get("task_id"))] if current is not None else []), *roots]:
        if task_id and task_id not in {entry["target"] for entry in controls if entry["action"] == "stop"}:
            controls.append({"action": "stop", "target": task_id, "scope": "cascade"})
    for task_id in earlier:
        controls.append({"action": "stop_earlier", "target": task_id, "scope": "cascade"})
    return controls


def _owner_actions(row: Mapping[str, Any]) -> tuple:
    from ouroboros.gateway.main_decisions import owner_actions

    return owner_actions(dict(row))


def _rank(activity: str) -> int:
    return ACTIVITY_ORDER.index(activity) if activity in ACTIVITY_ORDER else len(ACTIVITY_ORDER)


def _outcome(row: Mapping[str, Any]) -> str:
    axes = row.get("outcome_axes") if isinstance(row.get("outcome_axes"), dict) else {}
    lifecycle = axes.get("lifecycle") if isinstance(axes.get("lifecycle"), dict) else {}
    return str(lifecycle.get("status") or row.get("status") or "")


def roll_up_work(work_id: str, members: List[Dict[str, Any]], *, episode: Mapping[str, Any],
                 name: str = "", room: Any = None, focus: Any = None, delegated: int = 0,
                 outcome: str = "", gaps: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """One work from its classified members.

    ``episode`` is the current episode: ``{task_id, live, state?}`` — ``state`` is its own classified
    member when the episode root is live; an ended episode carries only its outcome. Helpers of any
    episode are the other members."""
    gaps = list(gaps)
    current = episode.get("state") if isinstance(episode.get("state"), dict) else None
    helpers = [member for member in members if member is not current]
    working_helpers = [member for member in helpers if member["activity"] == "working"]
    lead = current if current is not None else (min(members, key=lambda m: _rank(m["activity"])) if members else None)
    reasons: List[Dict[str, Any]] = []
    turn: List[Dict[str, Any]] = []
    if lead is not None and (current is not None or not working_helpers):
        reasons.extend(lead["reasons"])
    held = [entry for entry in reasons if entry.get("code") == "earlier_work_unconfirmed"]
    if held and working_helpers:
        reasons = [reason("earlier_attempt_finishing", count=len(working_helpers),
                          ids=[member["task_id"] for member in working_helpers])
                   if entry.get("code") == "earlier_work_unconfirmed" else entry for entry in reasons]
    elif held and delegated:
        reasons = [reason("earlier_runs_finishing", count=delegated)
                   if entry.get("code") == "earlier_work_unconfirmed" else entry for entry in reasons]
    elif current is None and working_helpers:
        reasons.append(reason("helpers_running", count=len(working_helpers),
                              ids=[member["task_id"] for member in working_helpers]))
    elif delegated and not working_helpers:
        reasons.append(reason("delegated_running", count=delegated))
    for member in members:
        turn.extend(member["turn"])
    known = [member["activity"] for member in members if member["activity"] != "unknown"]
    if delegated:
        known.append("working")
    activity = min(known, key=_rank) if known else "unknown"
    working = [member for member in members if member["activity"] == "working"]
    transitions = {member["transition"] for member in working}
    transition = "none"
    if activity == "working" and working and not delegated:
        if transitions == {"finalizing"}:
            transition = "finalizing"
        elif "pausing" in transitions and "none" not in transitions:
            transition = "pausing"
    calls = {member["call"] for member in working if member["call"]}
    call = calls.pop() if len(calls) == 1 and not delegated else ""
    if activity == "working":
        release = "self"
    elif lead is not None:
        release = lead["release"]
    else:
        release = "none"
    if turn:
        release = "owner"
    work: Dict[str, Any] = {
        "work_id": work_id, "room": room, "name": name,
        "episode": {"task_id": str(episode.get("task_id") or work_id), "live": bool(current is not None)},
        "activity": activity, "transition": transition, "call": call,
        "because": reasons, "turn": turn, "release": release,
        "paused_by": lead.get("paused_by") if lead is not None and activity == "paused" else None,
        "members": {"working": len(working_helpers), "waiting": sum(member["activity"] in {"waiting", "queued"}
                                                                    for member in helpers),
                    "delegated": int(delegated), "ids": [member["task_id"] for member in members]},
        "confirmed": all(member["confirmed"] for member in members) and not gaps,
    }
    if outcome:
        work["outcome"] = outcome
    if isinstance(focus, dict) and str(focus.get("text") or "").strip():
        work["focus"] = {"text": str(focus["text"]), "at": focus.get("at") or focus.get("authored_at")}
    if gaps:
        work["gaps"] = gaps
    return work


def roll_up_room(works: List[Mapping[str, Any]], *, complete: bool = True) -> Dict[str, Any]:
    """One room from its works: the most active work speaks; whose turn it is never replaces it."""
    if not works:
        activity = "idle" if complete else "unknown"
        return {"activity": activity, "transition": "none", "call": "", "turn": [], "amber": False,
                "mark": "none" if complete else "ring", "confirmed": complete, "works": []}
    known = [work["activity"] for work in works if work["activity"] != "unknown"]
    activity = min(known, key=_rank) if known else "unknown"
    working = [work for work in works if work["activity"] == "working"]
    transition = "none"
    if working and all(work["transition"] == "finalizing" for work in working):
        transition = "finalizing"
    elif working and all(work["transition"] in {"pausing", "finalizing"} for work in working) \
            and any(work["transition"] == "pausing" for work in working):
        transition = "pausing"
    calls = {work["call"] for work in working if work.get("call")}
    turn = [{"work_id": work["work_id"], **item} for work in works for item in work["turn"]]
    confirmed = complete and all(work["confirmed"] for work in works)
    mark = {"working": "moving", "queued": "still"}.get(activity, "dot" if activity in {"waiting", "paused"} else "ring")
    return {"activity": activity, "transition": transition, "call": calls.pop() if len(calls) == 1 else "",
            "turn": turn, "amber": bool(turn), "mark": mark if confirmed or activity == "working" else "ring",
            "confirmed": confirmed, "works": [work["work_id"] for work in works]}


def roll_up_summary(rooms: Mapping[Any, Mapping[str, Any]], works: List[Mapping[str, Any]],
                    *, complete: bool = True) -> Dict[str, Any]:
    """The tray's one line: counts by activity, how many wait for the owner, and confirmation."""
    counts = {activity: 0 for activity in ACTIVITY_ORDER}
    for work in works:
        counts[work["activity"] if work["activity"] in counts else "unknown"] += 1
    return {"counts": counts, "turn": sum(bool(work["turn"]) for work in works),
            "confirmed": complete and all(room.get("confirmed", True) for room in rooms.values())}
