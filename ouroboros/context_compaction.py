"""Checkpoint-bound, complete-input context-reclaim materialization."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import pathlib
from typing import Any, Dict, List, Literal, Mapping, MutableSet, Optional, Sequence, Tuple

from ouroboros.context_budget import (
    CONTEXT_OVERFLOW_CODES as _TYPED_CONTEXT_OVERFLOW_CODES,
    context_overflow_message as _context_overflow_message,
    ContextReclaimReceipt,
    ContextReclaimRequest,
    ReclaimStatus,
    SummarizerContextOverflow,
    _AtomicUnit,
    _Part,
    _SelectedUnit,
    _Selection,
    _UnitSummaryFailure,
    _UnsafeVisual,
)
from ouroboros.anthropic_native_custody import anthropic_tool_unit_active, custody_private_key
from ouroboros.config import runtime_setting

log = logging.getLogger(__name__)

_CAPSULE_VERSION = 1
_SUMMARY_CONTRACT_VERSION = 1
_SUMMARY_OUTPUT_TOKENS = 32_768
_BLOCKS_PER_BATCH = 8
_MIN_CAPSULE_BYTES = 512
_SHA256_HEX = frozenset("0123456789abcdef")
_SUMMARY_GUIDANCE = (
    "Summarize each supplied context source without dropping late facts. Preserve "
    "the actor's hypotheses, exact consequential errors, tool inputs when they "
    "affect meaning, results, decisions, owner-visible constraints, and unresolved "
    "next steps. Every source is complete input: do not assume that a head excerpt "
    "represents its tail. Honor each source's summary_budget_tokens. Write in first "
    "person as Ouroboros."
)

_CONTEXT_SUMMARIES_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_context_summaries",
        "description": "Emit exactly one non-empty summary for every supplied source_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "summaries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["source_id", "summary"],
                    },
                }
            },
            "required": ["summaries"],
        },
    },
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_bytes(value: Any) -> bytes:
    return _canonical_json(value).encode("utf-8")


def _reclaim_tokens_for_byte_delta(byte_delta: int, measurement_density: float) -> int:
    density = float(measurement_density)
    if not math.isfinite(density) or density <= 0:
        raise ValueError("context reclaim measurement_density must be finite and positive")
    return max(0, math.ceil((max(0, int(byte_delta)) / 4) * density))


def _context_tokens_for_messages(
    messages: Sequence[Mapping[str, Any]], measurement_density: float,
) -> int:
    from ouroboros.context_fit import estimate_context_prompt_tokens
    _reclaim_tokens_for_byte_delta(0, measurement_density)
    estimate = estimate_context_prompt_tokens(list(messages))
    return max(0, math.ceil(estimate * float(measurement_density)))


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def context_reclaim_transcript_sha256(messages: Sequence[Mapping[str, Any]]) -> str:
    return _sha256(_canonical_bytes(list(messages)))


def _unique_strings(values: Sequence[Any]) -> Tuple[str, ...]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _unique_refs(values: Sequence[Any]) -> Tuple[Dict[str, Any], ...]:
    seen: set[str] = set()
    result: List[Dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping) or not value:
            continue
        normalized = dict(value)
        identity = _sha256(_canonical_bytes(normalized))
        if identity not in seen:
            seen.add(identity)
            result.append(normalized)
    return tuple(result)


def _summary_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        kind = str(value.get("type") or "").strip().lower()
        if kind in {"image", "image_url"}:
            descriptor = str(
                value.get("_caption")
                or value.get("caption")
                or value.get("alt")
                or value.get("text")
                or ""
            ).strip()
            if not descriptor:
                raise _UnsafeVisual("image/base64 content has no safe textual descriptor")
            return {"type": "image_descriptor", "text": descriptor}
        projected: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key) == "_context_capsule" or custody_private_key(key):
                continue
            projected[str(key)] = _summary_projection(item)
        return projected
    if isinstance(value, (list, tuple)):
        return [_summary_projection(item) for item in value]
    if isinstance(value, bytes):
        raise _UnsafeVisual("opaque byte content has no safe textual descriptor")
    if isinstance(value, str) and "data:image/" in value.lower():
        raise _UnsafeVisual("embedded image data has no safe textual descriptor")
    return value


def _capsule_metadata(message: Mapping[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    content = message.get("content")
    if str(message.get("role") or "") != "assistant" or not isinstance(content, list):
        return False, None
    blocks = [block for block in content
              if isinstance(block, Mapping) and "_context_capsule" in block]
    if not blocks:
        return False, None
    if len(content) != 1 or len(blocks) != 1 or message.get("tool_calls"):
        return True, None
    block = blocks[0]
    meta = block.get("_context_capsule")
    text = str(block.get("text") or "")
    if not isinstance(meta, Mapping) or not text.strip():
        return True, None
    try:
        generation = int(meta.get("generation") or 0)
        version = int(meta.get("version") or 0)
        source_length = int(meta.get("source_length_chars"))
        part_count = int(meta.get("part_count"))
        summary_contract_version = int(meta.get("summary_contract_version"))
    except (TypeError, ValueError):
        return True, None
    source_hashes, source_refs = meta.get("source_hashes"), meta.get("source_refs")
    parts, checkpoint_ref = meta.get("parts"), meta.get("checkpoint_ref")
    source_unit_sha256 = str(meta.get("source_unit_sha256") or "")
    summary_contract_digest = str(meta.get("summary_contract_digest") or "")
    valid = (
        str(block.get("type") or "") == "text"
        and version == _CAPSULE_VERSION
        and generation >= 1
        and str(meta.get("retention") or "") == "summarized"
        and bool(str(meta.get("unit_id") or "").strip())
        and str(meta.get("visible_sha256") or "") == _sha256(text)
        and len(source_unit_sha256) == 64 and set(source_unit_sha256) <= _SHA256_HEX
        and isinstance(source_hashes, list) and bool(source_hashes)
        and source_unit_sha256 in source_hashes
        and all(isinstance(item, str) and len(item) == 64 and set(item) <= _SHA256_HEX
                for item in source_hashes)
        and isinstance(source_refs, list)
        and all(isinstance(item, Mapping) and item for item in source_refs)
        and isinstance(parts, list) and bool(parts)
        and part_count == len(parts)
        and source_length > 0
        and isinstance(checkpoint_ref, Mapping) and bool(checkpoint_ref)
        and any(dict(item) == dict(checkpoint_ref) for item in source_refs)
        and str(meta.get("measurement_basis") or "")
        in {"fresh_route_usage", "fresh_model_usage", "cold_estimate"}
        and summary_contract_version == _SUMMARY_CONTRACT_VERSION
        and summary_contract_digest == _SUMMARY_CONTRACT_DIGEST
    )
    if not valid:
        return True, None
    expected_start = 0
    seen_source_ids: set[str] = set()
    for part in parts:
        if not isinstance(part, Mapping):
            return True, None
        try:
            start = int(part.get("start_char"))
            end = int(part.get("end_char"))
        except (TypeError, ValueError):
            return True, None
        source_id = str(part.get("source_id") or "")
        digest = str(part.get("sha256") or "")
        if (not source_id or source_id in seen_source_ids or start != expected_start
                or end <= start or len(digest) != 64 or not set(digest) <= _SHA256_HEX
                or digest not in source_hashes):
            return True, None
        seen_source_ids.add(source_id)
        expected_start = end
    return (True, dict(meta)) if expected_start == source_length else (True, None)


def _unit_from_slice(
    messages: Sequence[Mapping[str, Any]],
    start: int,
    end: int,
    *,
    trace_refs_by_tool_call_id: Mapping[str, Any],
    capsule_meta: Optional[Mapping[str, Any]] = None,
    measurement_density: float = 1.0,
) -> Optional[_AtomicUnit]:
    raw_messages = list(messages[start:end + 1])
    raw_bytes = _canonical_bytes(raw_messages)
    try:
        source_text = _canonical_json(_summary_projection(raw_messages))
    except _UnsafeVisual:
        return None
    raw_sha = _sha256(raw_bytes)
    source_sha = _sha256(source_text)
    generation = int((capsule_meta or {}).get("generation") or 0)
    lineage_hashes: List[str] = list((capsule_meta or {}).get("source_hashes") or [])
    refs: List[Any] = list((capsule_meta or {}).get("source_refs") or [])
    if capsule_meta:
        refs.append(capsule_meta.get("checkpoint_ref"))
    for message in raw_messages:
        for call in message.get("tool_calls") or []:
            call_id = str((call or {}).get("id") or "")
            if call_id:
                refs.append(trace_refs_by_tool_call_id.get(call_id))
    lineage_hashes.extend((raw_sha, source_sha))
    context_size_tokens = _context_tokens_for_messages(raw_messages, measurement_density)
    capsule_floor = _reclaim_tokens_for_byte_delta(_MIN_CAPSULE_BYTES, measurement_density)
    predicted = max(0, context_size_tokens - capsule_floor)
    return _AtomicUnit(
        unit_id=f"unit:{start}:{end}:{raw_sha[:16]}",
        start=start,
        end=end,
        raw_sha256=raw_sha,
        raw_size_bytes=len(raw_bytes),
        context_size_tokens=context_size_tokens,
        source_text=source_text,
        source_sha256=source_sha,
        predicted_reclaim_tokens=predicted,
        generation=generation,
        lineage_hashes=_unique_strings(lineage_hashes),
        source_refs=_unique_refs(refs),
    )


def _atomic_units(
    messages: Sequence[Mapping[str, Any]],
    *,
    trace_refs_by_tool_call_id: Optional[Mapping[str, Any]] = None,
    measurement_density: float = 1.0,
) -> Tuple[_AtomicUnit, ...]:
    """Find completed tool units and capsules; leave malformed units raw."""
    trace_refs = trace_refs_by_tool_call_id or {}
    units: List[_AtomicUnit] = []
    idx = 0
    while idx < len(messages):
        message = messages[idx]
        capsule_present, capsule_meta = _capsule_metadata(message)
        if capsule_present:
            if capsule_meta is not None:
                unit = _unit_from_slice(
                    messages, idx, idx,
                    trace_refs_by_tool_call_id=trace_refs,
                    capsule_meta=capsule_meta,
                    measurement_density=measurement_density,
                )
                if unit is not None:
                    units.append(unit)
            idx += 1
            continue

        calls = message.get("tool_calls") or []
        if (str(message.get("role") or "") != "assistant"
                or not isinstance(calls, list) or not calls
                or not all(isinstance(call, Mapping)
                           and isinstance(call.get("function"), Mapping)
                           and str(call["function"].get("name") or "").strip()
                           for call in calls)):
            idx += 1
            continue
        call_ids = [str((call or {}).get("id") or "") for call in calls]
        if not all(call_ids) or len(call_ids) != len(set(call_ids)):
            idx += 1
            continue
        end = idx
        results: List[Mapping[str, Any]] = []
        cursor = idx + 1
        while cursor < len(messages) and str(messages[cursor].get("role") or "") == "tool":
            results.append(messages[cursor])
            end = cursor
            cursor += 1
        result_ids = [str(result.get("tool_call_id") or "") for result in results]
        complete = (
            len(result_ids) == len(call_ids)
            and all(result_ids)
            and len(result_ids) == len(set(result_ids))
            and set(result_ids) == set(call_ids)
        )
        if complete and not anthropic_tool_unit_active(messages, idx, end):
            unit = _unit_from_slice(
                messages, idx, end,
                trace_refs_by_tool_call_id=trace_refs,
                measurement_density=measurement_density,
            )
            if unit is not None:
                units.append(unit)
        idx = max(idx + 1, end + 1)
    return tuple(units)


def _typed_context_overflow(exc: BaseException) -> bool:
    try:
        from ouroboros.llm import LocalContextTooLargeError

        if isinstance(exc, LocalContextTooLargeError):
            return True
    except Exception:
        pass
    candidates = [getattr(exc, key, None) for key in ("kind", "code", "type")]
    body = getattr(exc, "body", None)
    payloads = [body] if isinstance(body, Mapping) else []
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            response_body = response.json()
        except Exception:
            response_body = None
        if isinstance(response_body, Mapping):
            payloads.append(response_body)
    for payload in payloads:
        candidates.extend(payload.get(key) for key in ("kind", "code", "type"))
        nested = payload.get("error")
        if isinstance(nested, Mapping):
            candidates.extend(nested.get(key) for key in ("kind", "code", "type"))
    capture = getattr(exc, "physical_attempt_capture", None)
    candidates.extend((getattr(capture, "provider_code", None),
                       getattr(capture, "provider_error_type", None)))
    if any(str(value or "").strip().lower() in _TYPED_CONTEXT_OVERFLOW_CODES for value in candidates):
        return True
    return _context_overflow_message(str(exc))


def _record_usage(total: Dict[str, Any], usage: Mapping[str, Any]) -> None:
    from ouroboros.llm import add_usage

    add_usage(total, dict(usage))
    for key in ("provider", "model", "prompt_cache_ttl"):
        if usage.get(key) is not None:
            total[key] = usage[key]


def _summarizer_spec() -> Dict[str, Any]:
    from ouroboros.config import get_light_model

    model = str(get_light_model() or "")
    use_local = runtime_setting("USE_LOCAL_LIGHT", "").strip().lower() in {"1", "true", "yes", "on"}
    route: Dict[str, Any] = {"model": model, "use_local": use_local}
    if use_local:
        route.update({"provider": "local", "resolved_model": model})
    else:
        try:
            from ouroboros.llm import LLMClient

            target = LLMClient()._resolve_remote_target(model)
            route.update({"provider": str(target.get("provider") or ""),
                          "resolved_model": str(target.get("usage_model") or target.get("resolved_model") or model),
                          "base_url": str(target.get("base_url") or "")})
        except Exception:
            route.update({"provider": "unknown", "resolved_model": model, "base_url": ""})
    route["route_fp"] = _sha256(_canonical_bytes(route))
    route["effort"] = "low"
    route["output_budget"] = _SUMMARY_OUTPUT_TOKENS
    return route


_SUMMARY_CONTRACT_DIGEST = _sha256(_canonical_bytes({
    "version": _SUMMARY_CONTRACT_VERSION, "guidance": _SUMMARY_GUIDANCE,
    "tool": _CONTEXT_SUMMARIES_TOOL,
    "input_fields": ("source_id", "start_char", "end_char", "sha256",
                     "summary_budget_tokens", "content"),
}))


def _summary_map_from_entries(entries: Any) -> Dict[str, str]:
    if not isinstance(entries, list):
        return {}
    result: Dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            return {}
        source_id = str(entry.get("source_id") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not source_id or not summary or source_id in result:
            return {}
        result[source_id] = summary
    return result


def _parse_structured_summaries(message: Mapping[str, Any]) -> Dict[str, str]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], Mapping):
        return {}
    function = calls[0].get("function") or {}
    if not isinstance(function, Mapping) or str(function.get("name") or "") != "emit_context_summaries":
        return {}
    try:
        payload = json.loads(function.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return _summary_map_from_entries(payload.get("summaries"))


def _parse_json_summaries(content: Any) -> Dict[str, str]:
    try:
        payload = json.loads(str(content or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return _summary_map_from_entries(payload.get("summaries") if isinstance(payload, Mapping) else None)


def _call_summarizer(
    parts: Sequence[_Part],
    *,
    drive_root: pathlib.Path,
    task_id: str,
    phase: Literal["map", "fold"],
    spec: Mapping[str, Any],
    summary_budgets: Mapping[str, int],
    usage_total: Dict[str, Any],
) -> Dict[str, str]:
    """Summarize complete parts. Typed overflow is never retried unchanged."""
    from ouroboros.llm import LLMClient
    from ouroboros.llm_observability import chat_observed
    payload = [
        {
            "source_id": part.source_id,
            "start_char": part.start_char,
            "end_char": part.end_char,
            "sha256": part.sha256,
            "summary_budget_tokens": int(summary_budgets[part.root_id]),
            "content": part.text,
        }
        for part in parts
    ]
    source_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    client = LLMClient()
    use_local = bool(spec.get("use_local"))
    common = {
        "drive_root": drive_root,
        "task_id": task_id,
        "call_type": f"context_compaction_{phase}",
        "model_role": "light",
        "model": str(spec.get("model") or ""),
        "reasoning_effort": str(spec.get("effort") or "low"),
        "max_tokens": int(spec.get("output_budget") or _SUMMARY_OUTPUT_TOKENS),
        "use_local": use_local,
    }

    if not use_local:
        prompt = (_SUMMARY_GUIDANCE
                  + "\nCall emit_context_summaries exactly once, with one entry for every source_id.\n"
                  + source_json)
        try:
            from ouroboros.openai_chat_dispatch import call_with_custom_validation_continuation
            message, observed_usage, executable = call_with_custom_validation_continuation(
                lambda request_messages: chat_observed(
                    client, messages=request_messages,
                    tools=[_CONTEXT_SUMMARIES_TOOL], tool_choice="required", **common,
                ),
                [{"role": "user", "content": prompt}],
            )
            for _usage in observed_usage:
                _record_usage(usage_total, _usage)
            if executable:
                parsed = _parse_structured_summaries(message)
                if parsed:
                    return parsed
        except Exception as exc:
            if _typed_context_overflow(exc):
                raise SummarizerContextOverflow(str(exc)) from exc
            from ouroboros.llm_claudexor import propagate_model_error
            propagate_model_error(exc)
            log.warning("Structured context summary failed; trying JSON response", exc_info=True)

    prompt = (_SUMMARY_GUIDANCE
              + "\nReturn only a JSON object {\"summaries\":[{\"source_id\":...,\"summary\":...}]}, "
                "with one entry for every source_id.\n"
              + source_json)
    try:
        message, _usage = chat_observed(
            client,
            messages=[{"role": "user", "content": prompt}],
            **common,
        )
        _record_usage(usage_total, _usage)
    except Exception as exc:
        if _typed_context_overflow(exc):
            raise SummarizerContextOverflow(str(exc)) from exc
        from ouroboros.llm_claudexor import propagate_model_error
        propagate_model_error(exc)
        raise _UnitSummaryFailure(
            f"summary call failed: {type(exc).__name__}: {exc}",
        ) from exc
    return _parse_json_summaries(message.get("content"))


def _part(source_id: str, text: str, start: int = 0) -> _Part:
    end = start + len(text)
    return _Part(root_id=source_id,
                 source_id=f"{source_id}:{start}:{end}:{_sha256(text)[:12]}",
                 start_char=start, end_char=end, text=text, sha256=_sha256(text))


def _split_part(part: _Part) -> Optional[Tuple[_Part, _Part]]:
    if len(part.text) < 2:
        return None
    midpoint = len(part.text) // 2
    # Newline boundary only NEAR the midpoint: a lone newline near an edge
    # used to produce degenerate 1%/99% splits that re-overflowed unchanged.
    window = max(1, len(part.text) // 4)
    before = part.text.rfind("\n", 1, midpoint + 1)
    after = part.text.find("\n", midpoint, len(part.text) - 1)
    candidates = [pos + 1 for pos in (before, after)
                  if pos > 0 and abs((pos + 1) - midpoint) <= window]
    split_at = min(candidates, key=lambda pos: abs(pos - midpoint)) if candidates else midpoint
    if split_at <= 0 or split_at >= len(part.text):
        split_at = midpoint
    if split_at <= 0 or split_at >= len(part.text):
        return None
    left_text = part.text[:split_at]
    right_text = part.text[split_at:]
    return (_part(part.root_id, left_text, part.start_char),
            _part(part.root_id, right_text, part.start_char + split_at))


def _map_complete_parts(
    parts: Sequence[_Part],
    *,
    drive_root: pathlib.Path,
    task_id: str,
    spec: Mapping[str, Any],
    summary_budgets: Mapping[str, int],
    usage_total: Dict[str, Any],
) -> Tuple[Tuple[_Part, ...], Dict[str, str], set[str]]:
    try:
        summaries = _call_summarizer(
            parts,
            drive_root=drive_root,
            task_id=task_id,
            phase="map",
            spec=spec,
            summary_budgets=summary_budgets,
            usage_total=usage_total,
        )
    except SummarizerContextOverflow:
        if len(parts) > 1:
            middle = len(parts) // 2
            subsets: Sequence[Sequence[_Part]] = (parts[:middle], parts[middle:])
        else:
            split = _split_part(parts[0])
            if split is None:
                return (), {}, {parts[0].root_id}
            # One call PER half: both halves together equal the failed payload.
            subsets = ((split[0],), (split[1],))
        leaves: List[_Part] = []
        merged: Dict[str, str] = {}
        failed: set[str] = set()
        for subset in subsets:
            child_leaves, child_map, child_failed = _map_complete_parts(
                subset,
                drive_root=drive_root,
                task_id=task_id,
                spec=spec,
                summary_budgets=summary_budgets,
                usage_total=usage_total,
            )
            leaves.extend(child_leaves)
            merged.update(child_map)
            failed.update(child_failed)
        return tuple(leaves), merged, failed
    except _UnitSummaryFailure:
        return (), {}, {part.root_id for part in parts}
    expected = {part.source_id: part.root_id for part in parts}
    if set(summaries) - set(expected):
        return tuple(parts), {}, set(expected.values())
    missing = set(expected) - set(summaries)
    failed = {expected[source_id] for source_id in missing}
    return tuple(parts), summaries, failed


def _fold_summaries(
    leaves: Sequence[_Part],
    summaries: Mapping[str, str],
    *,
    drive_root: pathlib.Path,
    task_id: str,
    spec: Mapping[str, Any],
    summary_budget_tokens: int,
    usage_total: Dict[str, Any],
) -> str:
    nodes = [(leaf.source_id, summaries[leaf.source_id]) for leaf in leaves]
    while len(nodes) > 1:
        next_nodes: List[Tuple[str, str]] = []
        for offset in range(0, len(nodes), _BLOCKS_PER_BATCH):
            group = nodes[offset:offset + _BLOCKS_PER_BATCH]
            if len(group) == 1:
                next_nodes.append(group[0])
                continue
            labelled = "\n\n".join(f"[{source_id}]\n{summary}" for source_id, summary in group)
            fold_id = "fold:" + _sha256(labelled)[:16]
            fold_part = _part(fold_id, labelled)
            try:
                folded = _call_summarizer(
                    [fold_part],
                    drive_root=drive_root,
                    task_id=task_id,
                    phase="fold",
                    spec=spec,
                    summary_budgets={fold_part.root_id: summary_budget_tokens},
                    usage_total=usage_total,
                )
                text = str(folded.get(fold_part.source_id) or "").strip() \
                    if set(folded) == {fold_part.source_id} else ""
            except (SummarizerContextOverflow, _UnitSummaryFailure):
                text = ""
            next_nodes.append((fold_id, text or labelled))
        nodes = next_nodes
    return nodes[0][1]


def _negative_memo_key(
    unit: _AtomicUnit,
    *,
    summary_budget_tokens: int,
    spec: Mapping[str, Any],
) -> str:
    return _sha256(_canonical_bytes({
        "unit_raw_sha256": unit.raw_sha256,
        "unit_source_sha256": unit.source_sha256,
        "lineage_hashes": unit.lineage_hashes,
        "source_ref_hashes": [_sha256(_canonical_bytes(ref)) for ref in unit.source_refs],
        "requested_unit_summary_budget": summary_budget_tokens,
        "summarizer_route_fp": str(spec.get("route_fp") or ""),
        "summarizer_model": str(spec.get("resolved_model") or spec.get("model") or ""),
        "summarizer_effort": str(spec.get("effort") or ""),
        "summarizer_output_budget": int(spec.get("output_budget") or 0),
        "summary_contract_digest": _SUMMARY_CONTRACT_DIGEST,
        "summary_contract_version": _SUMMARY_CONTRACT_VERSION,
    }))


def _select_units(
    messages: Sequence[Mapping[str, Any]],
    request: ContextReclaimRequest,
    *,
    keep_recent: int,
    trace_refs_by_tool_call_id: Mapping[str, Any],
    negative_memo: MutableSet[str],
    spec: Mapping[str, Any],
) -> Tuple[Optional[_Selection], ReclaimStatus]:
    units = list(_atomic_units(
        messages, trace_refs_by_tool_call_id=trace_refs_by_tool_call_id,
        measurement_density=request.measurement_density))
    if keep_recent > 0:
        units = units[:-keep_recent] if len(units) > keep_recent else []
    if not units:
        return None, "no_eligible"
    if int(request.reclaim_goal_tokens) <= 0:
        return None, "no_positive_reclaim"
    selected: List[_SelectedUnit] = []
    predicted = 0
    for unit in units:
        if unit.predicted_reclaim_tokens <= 0:
            continue
        source_tokens = max(1, len(unit.source_text.encode("utf-8")) // 4)
        summary_budget = min(_SUMMARY_OUTPUT_TOKENS, max(
            512, min(source_tokens // 2, max(512, int(request.reclaim_goal_tokens))),
        ))
        memo_key = _negative_memo_key(unit, summary_budget_tokens=summary_budget, spec=spec)
        if memo_key in negative_memo:
            continue
        selected.append(_SelectedUnit(unit, summary_budget, memo_key))
        predicted += unit.predicted_reclaim_tokens
        if predicted >= int(request.reclaim_goal_tokens):
            break
    if not selected or (not request.allow_partial_shrink and predicted < int(request.reclaim_goal_tokens)):
        return None, "no_positive_reclaim"
    fingerprint = _sha256(_canonical_bytes({
        "request": {
            "route_fp": request.route_fp,
            "round_id": request.round_id,
            "transcript_sha256": request.transcript_sha256,
            "measurement_basis": request.measurement_basis,
            "measurement_density": float(request.measurement_density),
            "reclaim_goal_tokens": int(request.reclaim_goal_tokens),
            "allow_partial_shrink": bool(request.allow_partial_shrink),
        },
        "units": [
            {
                "unit_id": item.unit.unit_id,
                "raw_sha256": item.unit.raw_sha256,
                "memo_key": item.negative_memo_key,
            }
            for item in selected
        ],
    }))
    return _Selection(tuple(selected), fingerprint, predicted), "applied"


def _persist_reclaim_checkpoint(
    messages: Sequence[Mapping[str, Any]],
    request: ContextReclaimRequest,
    selection: _Selection,
    *,
    drive_root: pathlib.Path,
    task_id: str,
) -> Optional[Dict[str, Any]]:
    from ouroboros.observability import new_call_id, persist_call
    payload = {"messages": list(messages), "request": {
            "route_fp": request.route_fp, "round_id": request.round_id,
            "transcript_sha256": request.transcript_sha256,
            "measurement_basis": request.measurement_basis, "measurement_density": float(request.measurement_density),
            "reclaim_goal_tokens": int(request.reclaim_goal_tokens),
        },
        "selection_fingerprint": selection.fingerprint, "selected_unit_ids": [item.unit.unit_id for item in selection.units],
    }
    try:
        persisted = persist_call(
            pathlib.Path(drive_root).resolve(strict=False),
            task_id=str(task_id or "context_compaction"),
            call_id=new_call_id("context_reclaim_checkpoint"),
            call_type="context_reclaim_checkpoint",
            payload=payload,
            manifest={
                "route_fp": request.route_fp, "round_id": request.round_id,
                "selection_fingerprint": selection.fingerprint, "selected_unit_ids": [item.unit.unit_id for item in selection.units],
            },
            keep_raw=True,
        )
    except Exception:
        log.debug("Failed to persist selected context-reclaim checkpoint", exc_info=True)
        return None
    if not isinstance(persisted, Mapping) or not persisted.get("manifest_ref"): return None
    try:
        from ouroboros.artifacts import store_actor_source_bytes
        return store_actor_source_bytes(
            pathlib.Path(drive_root), str(task_id or "context_compaction"),
            category="context_checkpoints", source_id=selection.fingerprint,
            data=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            extension="json",
        )
    except Exception:
        log.debug("Failed to persist actor-readable context-reclaim checkpoint", exc_info=True)
        return None


def _capsule_message(
    selected: _SelectedUnit,
    summary: str,
    parts: Sequence[_Part],
    checkpoint_ref: Mapping[str, Any],
    request: ContextReclaimRequest,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    unit = selected.unit
    generation = unit.generation + 1
    text = (
        f"[Context capsule generation {generation}; exact source retained by checkpoint]\n"
        + str(summary or "").strip()
    )
    source_hashes = _unique_strings([
        *unit.lineage_hashes, unit.raw_sha256, unit.source_sha256,
        *(part.sha256 for part in parts),
    ])
    source_refs = _unique_refs([*unit.source_refs, dict(checkpoint_ref)])
    metadata: Dict[str, Any] = {
        "version": _CAPSULE_VERSION,
        "generation": generation,
        "retention": "summarized",
        "unit_id": unit.unit_id,
        "source_unit_sha256": unit.raw_sha256,
        "source_length_chars": len(unit.source_text),
        "source_hashes": list(source_hashes),
        "source_refs": list(source_refs),
        "checkpoint_ref": dict(checkpoint_ref),
        "parts": [{
            "source_id": part.source_id, "start_char": part.start_char,
            "end_char": part.end_char, "sha256": part.sha256,
        } for part in parts],
        "part_count": len(parts),
        "route_fp": request.route_fp,
        "round_id": request.round_id,
        "measurement_basis": request.measurement_basis,
        "summary_contract_version": _SUMMARY_CONTRACT_VERSION,
        "summary_contract_digest": _SUMMARY_CONTRACT_DIGEST,
        "visible_sha256": _sha256(text),
    }
    message = {"role": "assistant", "content": [{
        "type": "text", "text": text, "_context_capsule": metadata,
    }]}
    capsule_ref = {
        "unit_id": unit.unit_id,
        "generation": generation,
        "checkpoint_ref": dict(checkpoint_ref),
        "source_hashes": list(source_hashes),
        "source_refs": list(source_refs),
    }
    return message, capsule_ref


def _receipt(
    status: ReclaimStatus,
    *,
    before_sha: str,
    after_sha: Optional[str] = None,
    selection: Optional[_Selection] = None,
    reclaimed_tokens: int = 0,
    goal_reached: bool = False,
    checkpoint_ref: Optional[Dict[str, Any]] = None,
    capsule_refs: Sequence[Dict[str, Any]] = (),
) -> ContextReclaimReceipt:
    return ContextReclaimReceipt(
        status=status,
        before_transcript_sha256=before_sha,
        after_transcript_sha256=after_sha or before_sha,
        selection_fingerprint=selection.fingerprint if selection else "",
        selected_unit_ids=tuple(item.unit.unit_id for item in selection.units) if selection else (),
        reclaimed_tokens=max(0, int(reclaimed_tokens)),
        goal_reached=bool(goal_reached),
        checkpoint_ref=checkpoint_ref,
        capsule_refs=tuple(dict(item) for item in capsule_refs),
    )


def compact_tool_history_llm(
    messages: list,
    keep_recent: int = 0,
    *,
    request: Optional[ContextReclaimRequest] = None,
    drive_root: Optional[pathlib.Path] = None,
    task_id: str = "context_compaction",
    trace_refs_by_tool_call_id: Optional[Mapping[str, Any]] = None,
    negative_memo: Optional[MutableSet[str]] = None,
) -> Tuple[list, ContextReclaimReceipt, Optional[Dict[str, Any]]]:
    """Reclaim atomically; ``keep_recent`` is the manual-tool retention control."""

    before_sha = context_reclaim_transcript_sha256(messages)
    effective_request = request or ContextReclaimRequest(
        route_fp="manual",
        round_id=str(task_id or "context_compaction"),
        transcript_sha256=before_sha,
        measurement_basis="cold_estimate",
        measurement_density=1.0,
        reclaim_goal_tokens=2**31 - 1,
        allow_partial_shrink=True,
    )
    if str(effective_request.transcript_sha256 or "") != before_sha:
        return messages, _receipt("binding_mismatch", before_sha=before_sha), None
    _reclaim_tokens_for_byte_delta(0, effective_request.measurement_density)

    memo = negative_memo if negative_memo is not None else set()
    trace_refs = trace_refs_by_tool_call_id or {}
    spec = _summarizer_spec()
    selection, empty_status = _select_units(
        messages,
        effective_request,
        keep_recent=max(0, int(keep_recent)),
        trace_refs_by_tool_call_id=trace_refs,
        negative_memo=memo,
        spec=spec,
    )
    if selection is None:
        return messages, _receipt(empty_status, before_sha=before_sha), None

    root = pathlib.Path(drive_root) if drive_root is not None else pathlib.Path("../data").resolve(strict=False)
    checkpoint_ref = _persist_reclaim_checkpoint(
        messages, effective_request, selection, drive_root=root, task_id=str(task_id or "context_compaction"),
    )
    if checkpoint_ref is None:
        return messages, _receipt(
            "checkpoint_failed", before_sha=before_sha, selection=selection,
        ), None

    replacements: Dict[int, Tuple[int, Dict[str, Any], Dict[str, Any]]] = {}
    memo_candidates: List[str] = []
    usage_total: Dict[str, Any] = {}
    summary_failures = 0
    initial_parts = [_part(item.unit.unit_id, item.unit.source_text) for item in selection.units]
    summary_budgets = {part.root_id: item.summary_budget_tokens for part, item in zip(initial_parts, selection.units)}
    leaves_by_root: Dict[str, List[_Part]] = {part.root_id: [] for part in initial_parts}
    summaries: Dict[str, str] = {}
    failed_roots: set[str] = set()
    for offset in range(0, len(initial_parts), _BLOCKS_PER_BATCH):
        leaves, batch_map, failed = _map_complete_parts(
            initial_parts[offset:offset + _BLOCKS_PER_BATCH], drive_root=root,
            task_id=str(task_id or "context_compaction"), spec=spec,
            summary_budgets=summary_budgets, usage_total=usage_total)
        for leaf in leaves:
            leaves_by_root[leaf.root_id].append(leaf)
        summaries.update(batch_map)
        failed_roots.update(failed)

    for selected in selection.units:
        leaves = tuple(leaves_by_root[selected.unit.unit_id])
        if (selected.unit.unit_id in failed_roots or not leaves
                or any(leaf.source_id not in summaries for leaf in leaves)):
            summary_failures += 1
            continue
        summary = _fold_summaries(
            leaves, summaries, drive_root=root,
            task_id=str(task_id or "context_compaction"), spec=spec,
            summary_budget_tokens=selected.summary_budget_tokens,
            usage_total=usage_total,
        )
        if not str(summary or "").strip():
            summary_failures += 1
            continue
        replacement, capsule_ref = _capsule_message(
            selected, summary, leaves, checkpoint_ref, effective_request,
        )
        if _context_tokens_for_messages(
            [replacement], effective_request.measurement_density,
        ) >= selected.unit.context_size_tokens:
            memo_candidates.append(selected.negative_memo_key)
            continue
        replacements[selected.unit.start] = (selected.unit.end, replacement, capsule_ref)

    if context_reclaim_transcript_sha256(messages) != before_sha:
        receipt = _receipt("binding_mismatch", before_sha=before_sha, selection=selection,
                           checkpoint_ref=checkpoint_ref)
        return messages, receipt, usage_total or None

    memo.update(memo_candidates)

    if not replacements:
        status: ReclaimStatus = "summarizer_failed" if summary_failures else "no_measurable_shrink"
        receipt = _receipt(status, before_sha=before_sha, selection=selection, checkpoint_ref=checkpoint_ref)
        return messages, receipt, usage_total or None

    rebuilt: List[Dict[str, Any]] = []
    capsule_refs: List[Dict[str, Any]] = []
    idx = 0
    while idx < len(messages):
        replacement = replacements.get(idx)
        if replacement is None:
            rebuilt.append(messages[idx])
            idx += 1
            continue
        end, capsule, capsule_ref = replacement
        rebuilt.append(capsule)
        capsule_refs.append(capsule_ref)
        idx = end + 1

    before_tokens = _context_tokens_for_messages(messages, effective_request.measurement_density)
    after_tokens = _context_tokens_for_messages(rebuilt, effective_request.measurement_density)
    if after_tokens >= before_tokens:
        receipt = _receipt("no_measurable_shrink", before_sha=before_sha, selection=selection,
                           checkpoint_ref=checkpoint_ref)
        return messages, receipt, usage_total or None
    reclaimed_tokens = before_tokens - after_tokens
    after_sha = context_reclaim_transcript_sha256(rebuilt)
    return rebuilt, _receipt(
        "applied",
        before_sha=before_sha,
        after_sha=after_sha,
        selection=selection,
        reclaimed_tokens=reclaimed_tokens,
        goal_reached=reclaimed_tokens >= int(effective_request.reclaim_goal_tokens),
        checkpoint_ref=checkpoint_ref,
        capsule_refs=capsule_refs,
    ), usage_total or None
