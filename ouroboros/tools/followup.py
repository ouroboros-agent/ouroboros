"""``schedule_followup`` — deferred follow-up through the EXISTING scheduler.

The W=A wait affordance (rotation sprint, owner-approved): when waiting for an
external instant (a subscription window reset, an embargo, a slow dependency) beats
burning rounds, the agent registers a one-shot follow-up in the supervisor's
scheduled-task table (``state/scheduled_tasks.json``). A task may also register a
recurring 5-field cron follow-up through that same table. The supervisor's ordinary
scheduler tick enqueues either form as an ordinary ROOT task (normal admission,
normal budget). No second scheduler exists: this module only WRITES the table the
supervisor already consumes.

Authority is narrower than the parent's, not wider: a delegated subagent may not
mint future root tasks (typed refusal), the objective is the agent's own plain
text (no host template), and a task may hold at most ``_MAX_PENDING_FOLLOWUPS``
pending follow-ups — past the cap the refusal is typed and discloses the pending
records.
"""

from __future__ import annotations

from ouroboros.tools.tool_result import ToolResult, _publish_tool_result

import uuid
from typing import Any, Dict, List

from ouroboros.deadline_utils import parse_deadline_ts
from ouroboros.tools.registry import ToolContext, ToolEntry

_MAX_PENDING_FOLLOWUPS = 2
FOLLOWUP_SOURCE = "task_followup"
_MAX_OBJECTIVE_CHARS = 4_000
_MAX_CONTEXT_CHARS = 8_000


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="schedule_followup",
            schema={
                "name": "schedule_followup",
                "description": (
                    "Register a deferred follow-up task that the supervisor scheduler "
                    "enqueues as an ordinary root task. Root tasks only; subagents report "
                    "the proposed follow-up to their parent. Supply exactly one trigger: run_at "
                    "for a one-shot ISO 8601 instant (naive = UTC), or cron for a recurring "
                    "5-field expression with an optional IANA timezone. Write the objective "
                    "in your own words — it becomes each future task's text verbatim. The "
                    "record is durable in state/scheduled_tasks.json, and the owner can "
                    "disable or delete it from the Schedules surface. A task may hold at "
                    f"most {_MAX_PENDING_FOLLOWUPS} pending follow-ups."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_at": {
                            "type": "string",
                            "description": "One-shot ISO 8601 instant to fire at/after (naive = UTC).",
                        },
                        "cron": {
                            "type": "string",
                            "description": "Recurring 5-field cron expression; mutually exclusive with run_at.",
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Optional IANA timezone for cron (blank = system local timezone).",
                        },
                        "objective": {
                            "type": "string",
                            "description": (
                                "Plain-language objective for the future task, in your own words "
                                f"(max {_MAX_OBJECTIVE_CHARS} chars; longer is a typed refusal, never truncated)."
                            ),
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "Optional context the future task should start from (facts, ids, paths; "
                                f"max {_MAX_CONTEXT_CHARS} chars; longer is a typed refusal, never truncated)."
                            ),
                        },
                    },
                    "required": ["objective"],
                },
            },
            handler=_handle_schedule_followup,
            timeout_sec=30,
        )
    ]


def _is_delegated_subagent(ctx: ToolContext) -> bool:
    for attr in ("task_metadata", "task_contract"):
        data = getattr(ctx, attr, None)
        if isinstance(data, dict) and str(data.get("delegation_role") or "").strip() == "subagent":
            return True
    return False


def _pending_followups(records: List[Dict[str, Any]], task_id: str) -> List[Dict[str, Any]]:
    out = []
    for record in records:
        if not isinstance(record, dict) or not record.get("enabled", True):
            continue
        if str(record.get("source") or "") != FOLLOWUP_SOURCE:
            continue
        template = record.get("task") if isinstance(record.get("task"), dict) else {}
        metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
        if str(metadata.get("origin_task_id") or "") == task_id:
            out.append(record)
    return out


def _handle_schedule_followup(ctx: ToolContext, **params) -> str:
    if _is_delegated_subagent(ctx):
        return (
            _publish_tool_result(ctx, ToolResult(status="blocked", code="ACCESS_BLOCKED", text=("ERROR: FOLLOWUP_SUBAGENT_REFUSED: a delegated subagent holds narrower-than-parent "
            "authority and may not mint future root tasks. Report the wait instant to your "
            "parent instead; the parent (or the owner) decides whether to schedule a follow-up.")))
        )
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return _publish_tool_result(ctx, ToolResult(status="unavailable", code="CAPABILITY_UNAVAILABLE", text=("ERROR: FOLLOWUP_TASK_ID_REQUIRED: a durable follow-up must belong to a real task.")))
    run_at_raw = str(params.get("run_at") or "").strip()
    cron = str(params.get("cron") or "").strip()
    if bool(run_at_raw) == bool(cron):
        return (
            _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=("ERROR: FOLLOWUP_TRIGGER_REQUIRED: supply exactly one of run_at (one-shot) "
            "or cron (recurring).")))
        )
    timezone = str(params.get("timezone") or "").strip()
    if run_at_raw:
        if timezone:
            return (
                _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=("ERROR: FOLLOWUP_TIMEZONE_WITH_RUN_AT: timezone applies only to recurring "
                "cron follow-ups; run_at is an absolute instant.")))
            )
        instant = parse_deadline_ts(run_at_raw)
        if instant is None:
            return (
                _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=(f"ERROR: FOLLOWUP_RUN_AT_INVALID: {run_at_raw!r} is not a parseable ISO 8601 "
                "instant. Example: 2026-08-19T12:20:00+03:00 (naive times read as UTC).")))
            )
        trigger = {"type": "once", "run_at": instant.isoformat()}
    else:
        from ouroboros.schedule_contract import cron_error, timezone_error

        if error := cron_error(cron):
            return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=(f"ERROR: FOLLOWUP_CRON_INVALID: {error}")))
        if error := timezone_error(timezone):
            return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=(f"ERROR: FOLLOWUP_TIMEZONE_INVALID: {error}")))
        trigger = {"type": "cron", "expr": cron}
    objective = str(params.get("objective") or "").strip()
    if not objective:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=("ERROR: FOLLOWUP_OBJECTIVE_REQUIRED: write the future task's objective in plain language.")))
    # Typed refusal, never a silent cut: the text rides VERBATIM into the future
    # task, so truncating it here would silently change what that task is.
    if len(objective) > _MAX_OBJECTIVE_CHARS:
        return (
            _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=(f"ERROR: FOLLOWUP_TEXT_TOO_LONG: objective is {len(objective)} chars; the limit is "
            f"{_MAX_OBJECTIVE_CHARS}. Shorten it — nothing was truncated and nothing was scheduled.")))
        )
    context = str(params.get("context") or "").strip()
    if len(context) > _MAX_CONTEXT_CHARS:
        return (
            _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=(f"ERROR: FOLLOWUP_TEXT_TOO_LONG: context is {len(context)} chars; the limit is "
            f"{_MAX_CONTEXT_CHARS}. Shorten it — nothing was truncated and nothing was scheduled.")))
        )
    from ouroboros.tool_access import canonical_data_root
    from supervisor.queue import list_scheduled_tasks, upsert_scheduled_task

    try:
        drive_root = canonical_data_root(ctx)
    except Exception as exc:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ERROR", text=(f"ERROR: FOLLOWUP_DATA_ROOT_UNRESOLVED: {exc}")))
    records = [r for r in (list_scheduled_tasks(drive_root).get("tasks") or []) if isinstance(r, dict)]
    pending = _pending_followups(records, task_id)
    if len(pending) >= _MAX_PENDING_FOLLOWUPS:
        def _trigger_label(record: Dict[str, Any]) -> str:
            item = record.get("trigger") if isinstance(record.get("trigger"), dict) else {}
            if str(item.get("type") or "") == "cron":
                zone = str(record.get("timezone") or "").strip() or "system local time"
                return f"cron {item.get('expr')} ({zone})"
            return f"fires at/after {item.get('run_at')}"

        listing = "; ".join(
            f"{record.get('id')} ({_trigger_label(record)})" for record in pending
        )
        return (
            _publish_tool_result(ctx, ToolResult(status="blocked", code="RESOURCE_CONSTRAINT_BLOCKED", text=(f"ERROR: FOLLOWUP_CAP_REACHED: this task already holds {len(pending)} pending "
            f"follow-up(s) of the {_MAX_PENDING_FOLLOWUPS} allowed: {listing}. Wait for a "
            "one-shot to fire, or the owner can disable/delete records from the Schedules surface.")))
        )
    metadata_src = getattr(ctx, "task_metadata", None)
    root_task_id = metadata_src.get("root_task_id") if isinstance(metadata_src, dict) else None
    record = {
        "id": f"followup-{task_id}-{uuid.uuid4().hex[:6]}",
        "name": f"Follow-up of task {task_id}",
        "description": objective,
        "source": FOLLOWUP_SOURCE,
        "enabled": True,
        "timezone": timezone,
        "trigger": trigger,
        "task": {
            "type": "task",
            "text": objective,
            "description": objective,
            **({"context": context} if context else {}),
            "metadata": {
                "source": FOLLOWUP_SOURCE,
                "origin_task_id": task_id,
                # `or ""` before str(): an absent/None root_task_id must fall back
                # to task_id, never become the literal string "None".
                "origin_root_task_id": str(root_task_id or "") or task_id,
            },
        },
    }
    presence = metadata_src.get("presence") if isinstance(metadata_src, dict) else None
    contract = getattr(ctx, "task_contract", None)
    if isinstance(presence, dict) and presence and isinstance(contract, dict):
        record["task"]["metadata"]["presence"] = dict(presence)
        record["task"]["task_contract"] = dict(contract)
    try:
        stored = upsert_scheduled_task(record, drive_root=drive_root)
    except Exception as exc:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ERROR", text=(f"ERROR: FOLLOWUP_PERSIST_FAILED: {type(exc).__name__}: {exc}")))
    if trigger["type"] == "once":
        timing = f"once at/after {trigger['run_at']}"
        lifecycle = "fires exactly once"
    else:
        zone = timezone or "system local time"
        timing = f"recurring cron {cron} ({zone})"
        lifecycle = "remains active after each run"
    return (
        f"FOLLOWUP_SCHEDULED: follow-up {stored.get('id')} registered as {timing}. It will "
        "enqueue ordinary root tasks through the supervisor scheduler under normal admission; "
        f"pending follow-ups for this task: {len(pending) + 1}/{_MAX_PENDING_FOLLOWUPS}. The "
        f"record is durable in state/scheduled_tasks.json and {lifecycle}; the owner can "
        "disable or delete it from the Schedules surface."
    )
