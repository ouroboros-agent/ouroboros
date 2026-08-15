"""Reviewer execution and plan-envelope policy for ``plan_task``."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import logging
import os
import pathlib

from ouroboros.config import review_model_uses_local
from ouroboros.deadline_utils import parse_deadline_ts, utc_now
from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext, active_repo_dir_for
from ouroboros.tools.review_helpers import load_checklist_section
from ouroboros.tools.review_synthesis import build_plan_review_messages, normalize_plan_scope
from ouroboros.tools.tool_result import (
    ToolResult,
    _publish_tool_result,
    _published_tool_result,
    _replace_tool_result,
)
from ouroboros.utils import utc_now_iso


PLAN_REVIEW_MAX_TOKENS = 65536
PLAN_REVIEW_EFFORT = "high"
PLAN_REVIEW_SLOT_TIMEOUT_SEC = 560
PLAN_CLASSES = ("self_mod", "external", "creative", "research")

log = logging.getLogger(__name__)


def append_plan_output_note(ctx: ToolContext, text: str, note: str) -> str:
    """Keep native plan metadata bound when compatibility notes append."""
    rendered = text + note
    base = _published_tool_result(ctx, None)
    if isinstance(base, ToolResult) and base.text == text:
        return _publish_tool_result(ctx, _replace_tool_result(base, text=rendered))
    return rendered


def publish_plan_review_projection(
    ctx: ToolContext,
    review: dict,
    text: str,
) -> str:
    """Publish control metadata only from validated structured review state."""
    aggregate = review.get("aggregate_signal")
    closed = review.get("closed")
    if aggregate not in {"GREEN", "REVIEW_REQUIRED", "REVISE_PLAN"}:
        raise ValueError(f"invalid plan review aggregate signal: {aggregate!r}")
    if type(closed) is not bool:
        raise ValueError("plan review closed state must be boolean")
    if (aggregate == "GREEN" and not closed) or (
        aggregate == "REVISE_PLAN" and closed
    ):
        raise ValueError(
            f"invalid plan review control state: outcome={aggregate}, closed={closed}"
        )
    return _publish_tool_result(
        ctx,
        ToolResult(
            status="ok",
            code="OK",
            text=text,
            meta={
                "plan_review_outcome": aggregate,
                "plan_review_closed": closed,
            },
        ),
    )


def plan_deadline_skip(ctx: ToolContext, *, emit: bool = False) -> str:
    """Project the existing deadline rail without starting paid planning work."""
    from ouroboros.config import get_plan_task_deadline_min_sec

    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    deadline = parse_deadline_ts(metadata.get("deadline_at"))
    if deadline is None:
        return ""
    remaining = (deadline - utc_now()).total_seconds()
    scaled = max(0.0, remaining / 4.0)
    minimum = get_plan_task_deadline_min_sec()
    if remaining > 0 and scaled >= minimum:
        return ""
    if emit:
        try:
            event_queue = getattr(ctx, "event_queue", None)
            if event_queue is not None:
                event_queue.put_nowait({
                    "type": "plan_task_deadline_skip",
                    "task_id": str(getattr(ctx, "task_id", "") or ""),
                    "remaining_sec": round(remaining, 1),
                    "scaled_ceiling_sec": round(scaled, 1),
                    "min_useful_sec": minimum,
                    "ts": utc_now_iso(),
                })
        except Exception:
            pass
    cause = (
        "the task deadline has expired; no new planning scout or reviewer work was started."
        if remaining <= 0 else
        f"insufficient time for useful planning — remaining {int(remaining)}s gives a "
        f"swarm window of {int(scaled)}s (< {int(minimum)}s useful floor)."
    )
    return (
        f"PLAN_TASK_SKIPPED_DEADLINE: {cause} Proceed with your own best plan "
        "directly; do not re-call plan_task under this deadline."
    )


def record_raw_plan_request_attempt(
    envelope: dict, state_root: pathlib.Path, task_id: str, *, reason: str,
) -> str:
    """Supersede prior authority before semantic decoding can fail."""
    payload = {"domain": "invalid_plan_task_attempt", "envelope": envelope}
    fingerprint = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    from ouroboros.task_results import record_plan_review_attempt

    record_plan_review_attempt(
        state_root, task_id, fingerprint=fingerprint, status="open", reason=reason,
    )
    return fingerprint


def reviewed_handoff_hashes(handoffs: dict) -> dict[str, str]:
    """Hash the exact in-memory scout snapshots before reviewer dispatch."""
    from ouroboros.tools.join_ledger import _child_result_sha256

    included = [str(item) for item in handoffs.get("included_task_ids") or [] if str(item)]
    wait = handoffs.get("wait") if isinstance(handoffs.get("wait"), dict) else {}
    tasks = wait.get("tasks") if isinstance(wait.get("tasks"), dict) else {}
    result: dict[str, str] = {}
    for task_id in included:
        snapshot = tasks.get(task_id)
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"PLAN_REVIEW_STATE_INVALID: included scout {task_id} has no reviewed snapshot"
            )
        result[task_id] = _child_result_sha256(snapshot)
    return result


def validate_plan_request_envelope(request, state_root: pathlib.Path, task_id: str) -> tuple[dict, str]:
    """Normalize a valid envelope or durably supersede stale review authority."""
    error = ""
    reason = "plan_input_invalid"
    if not str(request.plan or "").strip():
        error = "ERROR: plan parameter is required and must not be empty."
    elif not str(request.goal or "").strip():
        error = "ERROR: goal parameter is required and must not be empty."
    else:
        try:
            return normalize_plan_scope(request.scope), ""
        except ValueError as exc:
            reason = "plan_scope_invalid"
            error = f"ERROR: PLAN_SCOPE_INVALID: {exc}"
    envelope = {
        "plan": request.plan,
        "goal": request.goal,
        "files_to_touch": request.files_to_touch,
        "context_level": request.context_level,
        "context_notes": request.context_notes,
        "include_tests": request.include_tests,
        "plan_class": request.plan_class,
        "scope": request.scope,
    }
    try:
        record_raw_plan_request_attempt(envelope, state_root, task_id, reason=reason)
    except (OSError, TimeoutError, ValueError) as exc:
        return {}, f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    return {}, error


async def run_plan_review_slots(
    ctx: ToolContext,
    models: list[str],
    system_prompt: str,
    user_content: str,
    user_stable_len: int = 0,
    slot_ids: list[str] | None = None,
) -> list[dict]:
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
    from ouroboros.tools.review_synthesis import minted_plan_slot_ids

    # Owner decision D15: plan review stays api_chat — no route list is consulted here.
    row_ids = list(slot_ids) if slot_ids is not None else minted_plan_slot_ids(models)
    slots = [
        ReviewSlot(
            slot_id=row_id,
            model=str(model),
            effort=PLAN_REVIEW_EFFORT,
            timeout_sec=PLAN_REVIEW_SLOT_TIMEOUT_SEC,
            max_tokens=PLAN_REVIEW_MAX_TOKENS,
            temperature=0.2,
            role_hint="plan reviewer",
            use_local=review_model_uses_local(str(model)),
        )
        for row_id, model in zip(row_ids, models, strict=True)
    ]
    request = ReviewRequest(
        surface="plan_review",
        goal="Review the proposed implementation plan before code is written.",
        messages=build_plan_review_messages(system_prompt, user_content, user_stable_len),
        task_id=str(getattr(ctx, "task_id", "") or "plan_review"),
        call_type="plan_review",
        max_tokens=PLAN_REVIEW_MAX_TOKENS,
        temperature=0.2,
        no_proxy=True,
    )
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: run_review_request(
            request,
            slots=slots,
            drive_root=pathlib.Path(ctx.drive_root),
            llm=LLMClient(),
            usage_ctx=ctx,
        ),
    )
    return [
        _plan_raw_result_from_actor(actor, models[idx] if idx < len(models) else "")
        for idx, actor in enumerate(result.actors)
    ]


def _plan_raw_result_from_actor(actor: dict, request_model: str) -> dict:
    usage = actor.get("usage") or {}
    text = actor.get("raw_text") or ""
    error = actor.get("error") or ""
    if actor.get("status") not in {"ok", "empty"} and not error:
        error = str(actor.get("status") or "review failed")
    return {
        # Identity is CARRIED, never re-derived: the row keeps the slot_id the
        # substrate ran, so duplicate-model plan rows stay distinguishable.
        "slot_id": str(actor.get("slot_id") or ""),
        "model": str(usage.get("resolved_model") or actor.get("model") or request_model),
        "request_model": request_model or actor.get("model") or "",
        "text": text,
        "error": error or None,
        "prompt_ref": actor.get("prompt_ref") or {},
        "response_ref": actor.get("response_ref") or {},
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
        "cost": float(usage["cost"]) if usage.get("cost") is not None else None,
    }


def resolve_plan_class(ctx: ToolContext, plan_class: str, files_to_touch: list) -> tuple[str, str]:
    """Resolve the declared class and structurally escalate system-repo work."""
    from ouroboros.tool_access import path_is_relative_to

    declared = str(plan_class or "").strip().lower()
    if declared not in PLAN_CLASSES:
        declared = ""
    system_raw = getattr(ctx, "system_repo_dir", None)
    if system_raw is not None and system_raw.__class__.__module__.startswith("unittest.mock"):
        system_raw = None
    try:
        system_repo = pathlib.Path(system_raw or ctx.repo_dir).resolve(strict=False)
    except (TypeError, OSError, ValueError):
        return "self_mod", ""
    try:
        active = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    except Exception:
        active = system_repo
    touches_system = False
    if files_to_touch:
        if active == system_repo:
            touches_system = True
        else:
            for raw in files_to_touch:
                candidate = pathlib.Path(str(raw or ""))
                resolved = (
                    candidate if candidate.is_absolute() else active / candidate
                ).resolve(strict=False)
                if resolved == system_repo or path_is_relative_to(resolved, system_repo):
                    touches_system = True
                    break
    if touches_system:
        note = "" if declared in ("", "self_mod") else (
            f"plan_class escalated {declared!r} -> 'self_mod': files_to_touch resolve "
            "under the Ouroboros system repo (structural fact)."
        )
        return "self_mod", note
    if declared:
        return declared, ""
    return ("external" if active != system_repo else "self_mod"), ""


def classify_reviewer_error(exc: BaseException, model: str) -> str:
    """Return actionable reviewer failure text without swallowing details."""
    exc_type = type(exc).__name__
    exc_str = str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return (
            "API error (provider returned non-JSON response body — likely oversized prompt "
            f"or HTTP error from {model}): {exc_str}"
        )
    try:
        from openai import APIConnectionError, APIStatusError, BadRequestError, RateLimitError

        if isinstance(exc, RateLimitError):
            return f"Rate limit / quota exceeded for {model} (HTTP 429): {exc_str}"
        if isinstance(exc, BadRequestError):
            return (
                f"Bad request for {model} (HTTP 400 — prompt may be too large "
                f"for this model's context window): {exc_str}"
            )
        if isinstance(exc, APIConnectionError):
            return f"API connection error for {model} (network failure): {exc_str}"
        if isinstance(exc, APIStatusError):
            return f"API status error {getattr(exc, 'status_code', '?')} for {model}: {exc_str}"
    except ImportError:
        pass
    return f"{exc_type}: {exc_str}"


def get_review_models() -> list[str]:
    """Return configured reviewer slots, preserving explicit duplicates."""
    from ouroboros import config as cfg

    models = list(cfg.get_review_models() or [])
    if not models:
        models = [os.environ.get("OUROBOROS_MODEL", cfg.SETTINGS_DEFAULTS["OUROBOROS_MODEL"])]
    return models


def load_plan_checklist() -> str:
    try:
        return load_checklist_section("Plan Review Checklist")
    except Exception as exc:
        log.warning("Could not load Plan Review Checklist: %s", exc)
        return ""
