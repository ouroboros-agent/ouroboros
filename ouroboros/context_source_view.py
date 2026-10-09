"""Model-free source-address reconstruction after a real context refusal.

The compactor owns exact checkpoints, units and capsules. This leaf owns only
which complete sources an emergency view represents and the facts it shows.
It never writes a summary or claims that the mind understood an omitted body.
"""
from __future__ import annotations

import pathlib
from dataclasses import replace
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from ouroboros import context_compaction as compaction
from ouroboros.context_budget import ContextReclaimRequest, ContextReclaimReceipt, _AtomicUnit, _Selection, _SelectedUnit
from ouroboros.tool_result_record import read_tool_result_record

EmergencyRung = Literal["host_copies", "bodies"]
_EMERGENCY_LABELS = {  # the record's honest heading: no summary, exact rows retained by checkpoint
    "host_copies": ("Emergency host record after the provider's context refusal: obsolete host rows "
                    "replaced by typed state and exact addresses; nothing summarized; exact rows retained by checkpoint"),
    "bodies": ("Emergency host record after the provider's context refusal: tool bodies replaced by typed facts "
               "and exact addresses; understanding is being restored from the sources, nothing summarized; "
               "exact rows retained by checkpoint")}
_FACT_ARGUMENTS_CHARS = 400
_FACT_HEADER_CHARS = 80


def _obsolete_host_rows(messages: Sequence[Mapping[str, Any]]) -> set[int]:
    """Only producer-labelled state replaced by a later full snapshot is obsolete.

    Legacy/unlabelled prose stays raw. A user protocol role alone says nothing
    about authorship, and neither an old warning nor a peer message is obsolete
    merely because the mind has already seen it.
    """
    from ouroboros.loop_messages import CONTEXT_FACTS_NAME
    from ouroboros.peer_roster import ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME

    obsolete = set()
    for full_name, names in ((CONTEXT_FACTS_NAME, {CONTEXT_FACTS_NAME}),
                            (ROSTER_SNAPSHOT_NAME, {ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME})):
        latest = max((i for i, row in enumerate(messages)
                      if row.get("role") == "user" and row.get("name") == full_name), default=-1)
        obsolete.update(i for i, row in enumerate(messages[:max(0, latest)])
                        if row.get("role") == "user" and row.get("name") in names)
    return obsolete


def _unit_facts(messages: Sequence[Mapping[str, Any]], unit: _AtomicUnit, kind: str, trace_refs: Mapping[str, Any]) -> str:
    """Typed facts of one unit from its protocol rows and trace references, never a retelling."""
    rows = list(messages[unit.start:unit.end + 1])
    lines = [f"Unit {unit.unit_id}: {kind}, {unit.context_size_tokens} tokens estimated"]
    if kind != "tool":
        text = compaction._plain_text(rows[0].get("content")) or ""
        header = text.strip().splitlines()[0] if text.strip() else ""
        header = f"; starts {header!r}" if 0 < len(header) <= _FACT_HEADER_CHARS else ""
        return "\n".join([*lines, f"- {kind} text, {len(text)} chars, sha256 {compaction._sha256(text)[:16]}{header}"])
    results = {str(row.get("tool_call_id") or ""): row for row in rows if str(row.get("role") or "") == "tool"}
    for call in rows[0].get("tool_calls") or []:
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        call_id, arguments = str(call.get("id") or ""), str(function.get("arguments") or "")
        shown = (arguments if len(arguments) <= _FACT_ARGUMENTS_CHARS
                 else f"<arguments {len(arguments)} chars, sha256 {compaction._sha256(arguments)[:16]}>")
        result_row = results.get(call_id, {})
        record = read_tool_result_record(result_row)
        result = result_row.get("content")
        result_text = compaction._plain_text(result)
        result_chars = len(result_text) if result_text is not None else len(compaction._canonical_json(result))
        detail = f"; outcome {compaction._canonical_json(record['facts'])}"
        if record["state"] == "unknown":
            detail += f"; outcome binding unknown ({record['reason']})"
        else:
            detail += f"; invocation {record['invocation']['invocation_id']}"
        for label, key in (("trace", "trace_ref"), ("source", "source_ref")):
            if record.get(key):
                detail += f"; {label} {compaction._canonical_json(record[key])}"
        lines.append(f"- {function.get('name') or '?'} {shown} -> result {result_chars} chars{detail}")
    return "\n".join(lines)


def emergency_address_view(
    messages: list,
    request: ContextReclaimRequest,
    *,
    rung: EmergencyRung,
    protected_texts: Sequence[str] = (),
    trace_refs_by_tool_call_id: Optional[Mapping[str, Any]] = None,
    drive_root: pathlib.Path,
    task_id: str,
) -> Tuple[list, ContextReclaimReceipt]:
    """Model-free pass after the provider's typed refusal of this very request.

    ``host_copies`` turns replaced, producer-labelled snapshots into exact
    checkpoint addresses; ``bodies`` turns raw tool units into typed facts (tool,
    arguments, result size, trace reference) with addresses. No helper, no summary, no
    invented note: the exact rows stay in the checkpoint and return through the same
    restore path (``restore_unit_refs`` from the record's checkpoint and ``units``).
    The system view, the assignment, owner rows, earlier capsules, active protocol and
    host-typed rows are never selected; a record not shorter than its rows leaves them raw.
    """
    trace_refs = trace_refs_by_tool_call_id or {}
    density = request.measurement_density
    before_sha = compaction.context_reclaim_transcript_sha256(messages)
    if str(request.transcript_sha256 or "") != before_sha:
        return messages, compaction._receipt("binding_mismatch", before_sha=before_sha)
    units = compaction.context_units(messages, scope="dialogue", trace_refs_by_tool_call_id=trace_refs,
                          measurement_density=density)
    protected = compaction.owner_protected_unit_ids(messages, units, protected_texts)
    obsolete = _obsolete_host_rows(messages) if rung == "host_copies" else set()
    wanted = "user" if rung == "host_copies" else "tool"
    # A refused body need never have reached a usable reply: this is a model-free
    # address view, explicitly NOT the mind's understanding or a helper summary.
    eligible = [u for u in units if compaction.unit_kind(messages, u) == wanted and u.generation == 0
                and u.unit_id not in protected and (rung == "bodies" or u.start in obsolete)]
    if not eligible:
        return messages, compaction._receipt("no_eligible", before_sha=before_sha)
    fingerprint = compaction._sha256(compaction._canonical_bytes({"rung": rung, "transcript": before_sha,
                                           "units": [u.unit_id for u in eligible]}))
    selection = _Selection(tuple(_SelectedUnit(u, 0, "") for u in eligible), fingerprint,
                           sum(u.predicted_reclaim_tokens for u in eligible))
    checkpoint_ref = compaction._persist_reclaim_checkpoint(messages, request, selection, drive_root=drive_root, task_id=task_id)
    if checkpoint_ref is None:
        return messages, compaction._receipt("checkpoint_failed", before_sha=before_sha, selection=selection)
    # One record per uninterrupted range (a retained row breaks adjacency), every member
    # individually addressable; records follow the transcript order.
    groups: List[List[_AtomicUnit]] = []
    for unit in eligible:
        if groups and groups[-1][-1].end + 1 == unit.start:
            groups[-1].append(unit)
        else:
            groups.append([unit])
    replacements: Dict[int, tuple] = {}
    source_refs: List[Dict[str, Any]] = []
    for group in groups:
        start, end = group[0].start, group[-1].end
        combined = compaction._unit_from_slice(messages, start, end, trace_refs_by_tool_call_id=trace_refs,
                                    measurement_density=density)
        if combined is None:
            continue
        members = [{"unit_id": u.unit_id, "raw_sha256": u.raw_sha256} for u in group]
        member_refs = [{"checkpoint_ref": checkpoint_ref, **member} for member in members]
        combined = replace(
            combined,
            lineage_hashes=compaction._unique_strings([*combined.lineage_hashes, *(h for u in group for h in u.lineage_hashes)]),
            source_refs=compaction._unique_refs([*combined.source_refs, *(r for u in group for r in u.source_refs), *member_refs]))
        record, ref = compaction._capsule_message(
            _SelectedUnit(combined, 0, ""), "\n".join(_unit_facts(messages, u, wanted, trace_refs) for u in group),
            [compaction._part(combined.unit_id, combined.source_text)], checkpoint_ref, request, retention="source_view",
            label=_EMERGENCY_LABELS[rung], address={"checkpoint_ref": dict(checkpoint_ref), "units": members})
        if compaction._context_tokens_for_messages([record], density) >= sum(u.context_size_tokens for u in group):
            continue
        replacements[start] = (end, [record], [ref])
        source_refs.extend(member_refs)
    rebuilt, capsule_refs = compaction._materialize_replacements(messages, replacements) if replacements else (messages, [])
    reclaimed = compaction._context_tokens_for_messages(messages, density) - compaction._context_tokens_for_messages(rebuilt, density)
    if reclaimed <= 0:
        return messages, compaction._receipt("no_measurable_shrink", before_sha=before_sha, selection=selection,
                                  checkpoint_ref=checkpoint_ref)
    return rebuilt, compaction._receipt(
        "applied", before_sha=before_sha, after_sha=compaction.context_reclaim_transcript_sha256(rebuilt),
        selection=selection, reclaimed_tokens=reclaimed, goal_reached=reclaimed >= int(request.reclaim_goal_tokens),
        checkpoint_ref=checkpoint_ref, capsule_refs=capsule_refs,
        source_refs=compaction._unique_refs([*source_refs, checkpoint_ref]),
    )

