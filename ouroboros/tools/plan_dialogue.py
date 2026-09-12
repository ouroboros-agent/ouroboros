"""Bind full room evidence to the existing plan request and source-handle custody."""
from __future__ import annotations

import json
import pathlib
from typing import Any

from ouroboros.artifacts import read_actor_source_bytes, store_actor_source_bytes, task_artifact_dir_path
from ouroboros.dialogue_evidence import own_room_chat, read_room_source, task_room_record
from ouroboros.projects_registry import all_task_bindings, list_reserved_projects
from ouroboros.task_results import load_plan_review_state


def related_rooms(ctx: Any, root: pathlib.Path, own_chat: int | None) -> list[dict]:
    """Only Main and task-lineage rooms, using existing registry activity facts."""
    task_id = str(getattr(ctx, "task_id", "") or "")
    task = {**(task_room_record(root, task_id)), **(getattr(ctx, "task_metadata", {}) or {}), "task_id": task_id}
    bindings = all_task_bindings(root)
    chats = {1} if own_chat != 1 else set()
    for field in ("task_id", "parent_task_id", "root_task_id"):
        tid = str(task.get(field) or "")
        if tid:
            source = task_room_record(root, tid)
            chat = bindings.get(tid) or source.get("chat_id")
            if chat is not None:
                chats.add(int(chat))
    from ouroboros.task_results import resolve_task_lineage
    lineage_root = resolve_task_lineage(task_id, metadata=task)["root_task_id"]
    for bound_id, chat in bindings.items():
        related = task_room_record(root, bound_id)
        if related and resolve_task_lineage(bound_id, metadata=related)["root_task_id"] == lineage_root:
            chats.add(int(chat))
    projects = {int(p["chat_id"]): p for p in list_reserved_projects(root)}
    pointers = []
    for chat in sorted(chats - {own_chat}):
        if chat != 1 and chat not in projects:
            continue
        row = projects.get(chat, {})
        pointers.append({"locator": f"chat:{chat}", "label": row.get("name") or "Main",
                         "last_active_at": row.get("last_active_at"), "message_count": None,
                         "count_status": "not_loaded", "delivery": "pointer_only"})
    return pointers


def _source_view(root: pathlib.Path, task_id: str, source: dict, ref: dict | None) -> dict:
    view = {key: source[key] for key in ("chat_id", "label", "captured_at", "coverage", "sha256", "bytes", "text", "secrets_redacted") if key in source}
    view["locator"] = f"chat:{source['chat_id']}@{source['sha256']}"
    view["lines"] = source["text"].count("\n")
    if ref:
        view["source_ref"] = ref
        view["file"] = str(task_artifact_dir_path(root, task_id, create=False) / ref["path"])
    return view


def attach_own_dialogue(ctx: Any, root: pathlib.Path, manifest: dict,
                        author_fingerprint: str, *, persist: bool = False) -> dict:
    """Identical author inputs reuse recorded sources, including during replay.

    Room growth is evidence for the next changed author request. It cannot
    itself mint another paid plan envelope. Health/roster/cycle rails stay with
    the existing engine, which still decides whether any dispatch is earned.
    """
    task_id = str(getattr(ctx, "task_id", "") or "")
    state = load_plan_review_state(root, task_id)
    recorded = next((wave for wave in reversed(state.get("waves") or [])
                     if wave.get("author_request_fingerprint") == author_fingerprint), None)
    if recorded:
        from ouroboros.tools.plan_review_artifacts import authority_wave

        exact = authority_wave(root, task_id, recorded)
        previous = (exact.get("evidence_manifest_full") or {}).get("own_dialogue") or {}
        if recorded.get("dialogue_source_ref"):
            ref = recorded["dialogue_source_ref"]
            raw = read_actor_source_bytes(root, task_id, ref)
            source = {**previous, "text": raw.decode("utf-8"), "sha256": ref["sha256"], "bytes": len(raw)}
            own = _source_view(root, task_id, source, ref)
        else:
            own = dict(previous)  # An explicit missing-room fact also replays exactly.
        pointers = (exact.get("evidence_manifest_full") or {}).get("related_rooms") or []
    else:
        chat = own_room_chat(ctx, root)
        source = read_room_source(root, chat, task_id=task_id, mailbox_root=ctx.drive_root) if chat is not None else None
        if source is None:
            own = {"chat_id": chat, "gap": "own_room_unavailable", "text": ""}
        else:
            ref = store_actor_source_bytes(
                root, task_id, category="context_checkpoints", source_id=f"plan-dialogue-{chat}",
                data=source["text"].encode("utf-8"), extension="jsonl",
            ) if persist else None
            own = _source_view(root, task_id, source, ref)
        pointers = related_rooms(ctx, root, chat)
    return {**manifest, "author_request_fingerprint": author_fingerprint,
            "own_dialogue": own, "related_rooms": pointers}


def plan_chat_reader(root: pathlib.Path, task_id: str):
    """Resolve snapshot-qualified chat ranges only through recorded task custody."""
    def read(locator: str):
        chat, sep, digest = locator.partition("@")
        try:
            chat_id = int(chat)
        except ValueError:
            return None
        if not sep:
            return read_room_source(root, chat_id)
        state = load_plan_review_state(root, task_id)
        for wave in reversed(state.get("waves") or []):
            ref = wave.get("dialogue_source_ref") or {}
            if ref.get("sha256") == digest and wave.get("dialogue_chat_id") == chat_id:
                raw = read_actor_source_bytes(root, task_id, ref)
                return {"text": raw.decode("utf-8"), "coverage": json.loads(raw.split(b"\n", 1)[0]).get("coverage", {})}
        return None
    return read


def render_dialogue(manifest: Any) -> str:
    own = manifest.get("own_dialogue") or {}
    metadata = {key: value for key, value in own.items() if key != "text"}
    return (
        "## OWN ROOM DIALOGUE (exact recorded snapshot)\n\n"
        "Both speakers, questions, options and answers retain their source and author. "
        "A peer suggestion is not an owner instruction. Later messages are not claimed reviewed by this snapshot.\n"
        + json.dumps(metadata, ensure_ascii=False, default=str) + "\n"
        + str(own.get("text") or "Explicit gap: own room source unavailable.") + "\n"
        + "## RELATED ROOMS (pointers only; request chat:<id> with need_evidence)\n\n"
        + json.dumps(manifest.get("related_rooms") or [], ensure_ascii=False, default=str) + "\n"
    )


def fit_dialogue_text(packet: str, own: dict, capacity_chars: int, *, measure=len) -> tuple[str, dict]:
    """Keep the largest newest suffix fitting this delivery's actual measure.

    Only automatic dialogue yields room; required governance and operative
    inputs stay intact. Ranges describe the immutable UTF-8 source exactly.
    """
    source = str(own.get("text") or "")
    raw = source.encode("utf-8")
    coverage = {"source_sha256": own.get("sha256"), "source_bytes": len(raw),
                "inline_bytes": [0, len(raw) - 1] if raw else None, "omitted_prefix": None}
    if not source or measure(packet) <= capacity_chars or source not in packet:
        return packet, coverage

    def selected(take):
        tail = source[-take:] if take else ""
        start = len(raw) - len(tail.encode("utf-8"))
        notice = (f"Dialogue coverage: newest bytes {start}-{len(raw) - 1} attached; "
                  if tail else "Dialogue coverage: no inline source bytes fit; ")
        omitted = f"{own['locator']}::bytes=0-{start - 1}"
        notice += f"exact omitted prefix: {omitted}. The complete redacted snapshot remains at the recorded source handle.\n"
        return packet.replace(source, notice + tail, 1), start, omitted

    low, high = 0, len(source) - 1
    while low < high:
        count = (low + high + 1) // 2
        if measure(selected(count)[0]) <= capacity_chars:
            low = count
        else:
            high = count - 1
    fitted, start, omitted = selected(low)
    coverage.update(inline_bytes=[start, len(raw) - 1] if low else None, omitted_prefix=omitted)
    return fitted, coverage


def dialogue_slot_inputs(slots: list, *, system_prompt: str, user_content: str,
                         session_task: str, manifest: dict, slot_messages: dict,
                         native_mandatory_chars: int, data_root: Any = "",
                         frozen: dict | None = None, session_root: str = "", task_id: str = "") -> dict:
    """Project a fresh request, or reuse the recorded delivery at collection."""
    if frozen is not None:
        from ouroboros.tools.plan_review_artifacts import frozen_delivery_inputs
        return frozen_delivery_inputs(frozen, slots)
    from ouroboros.tools.plan_review_runtime import PLAN_REVIEW_MAX_TOKENS, slot_retrieves, slot_is_session
    from ouroboros.tools.review_synthesis import build_plan_review_messages, per_slot_input_token_limits
    from ouroboros.tools.plan_packet import plan_user_stable_len
    from ouroboros.tools.plan_spec import PLAN_FINDINGS_ARRAY_CONTRACT
    from ouroboros.review_native_episode import review_native_transcript_bound, native_landing_at, native_first_send_chars
    from ouroboros.reviewer_window import reviewer_window_binding
    from ouroboros.review_execution import _messages_char_count

    own = manifest.get("own_dialogue") or {}
    messages, tasks, lengths, coverage = dict(slot_messages), {}, {}, {}
    api = [slot for slot in slots if not slot_retrieves(slot)]
    limits = per_slot_input_token_limits([s.model for s in api], output_reserve=PLAN_REVIEW_MAX_TOKENS,
                                       tokenizer_margin=155_000, slots=api)
    for slot in slots:
        sid = str(slot.slot_id)
        if not slot_retrieves(slot):
            capacity = int(limits[sid]) * 4
            existing = messages.get(sid)
            if existing:
                # Continuation history is already exact; only this turn's new
                # automatic source can shrink, never its prior paid inputs.
                total = _messages_char_count(existing)
                view, coverage[sid] = fit_dialogue_text(user_content, own, capacity - total + len(user_content))
                messages[sid] = [{**m, "content": view} if i == len(existing) - 1 and m.get("role") == "user" else dict(m)
                                 for i, m in enumerate(existing)]
            else:
                view, coverage[sid] = fit_dialogue_text(user_content, own, capacity - len(system_prompt))
                messages[sid] = build_plan_review_messages(system_prompt, view, plan_user_stable_len(view))
            lengths[sid] = _messages_char_count(messages[sid])
        elif not slot_is_session(slot):
            bound = review_native_transcript_bound(slot.model, output_reserve=PLAN_REVIEW_MAX_TOKENS,
                                                   mandatory_read_chars=native_mandatory_chars,
                                                   **reviewer_window_binding(slot))
            governance_read = max(0, native_mandatory_chars - len(session_task))
            def first_send(task):
                return native_first_send_chars(session_root, surface="plan_review", role_hint=slot.role_hint,
                    slot_id=sid, session_task=task, output_contract=PLAN_FINDINGS_ARRAY_CONTRACT, task_id=task_id)
            tasks[sid], coverage[sid] = fit_dialogue_text(session_task, own,
                native_landing_at(bound) - governance_read - 1, measure=first_send)
        elif own.get("file") and own.get("text"):
            instruction = (
                f"MANDATORY FULL READ: {own['file']} (redacted immutable room dialogue; "
                f"sha256={own['sha256']}; bytes={own['bytes']}; lines=1-{own['lines']}). "
                "Read the whole source using your file tools. Your harness owns its context window; "
                "the host has no numerical window evidence for this route. If it actually cannot fit, "
                "read the newest part and report exact included and omitted ranges of this snapshot "
                f"using {own['locator']}::lines or ::bytes. Do not claim unread messages reviewed. "
                "File access is available; full-read coverage remains reviewer-declared, not host-attested.\n"
            )
            tasks[sid] = session_task.replace(str(own["text"]), instruction, 1)
            coverage[sid] = {"source_sha256": own["sha256"], "source_bytes": own["bytes"],
                             "inline_bytes": None, "full_file": own["file"], "read_coverage": "unobserved"}
        if slot_retrieves(slot):
            lengths[sid] = (first_send(tasks.get(sid, session_task)) if not slot_is_session(slot)
                            else len(tasks.get(sid, session_task)))
        if sid in coverage:
            coverage[sid]["delivery"] = ("delegated_file" if slot_is_session(slot) else
                                         "native_retrieving" if slot_retrieves(slot) else "packet")
    return {"slot_messages": messages, "slot_session_tasks": tasks, "slot_prompt_chars": lengths,
            "dialogue_delivery": coverage,
            "native_mandatory_read_chars": native_mandatory_chars,
            "request_policy": {"output_contract": PLAN_FINDINGS_ARRAY_CONTRACT,
                               "native_data_root": str(data_root),
                               "native_mandatory_read_chars": native_mandatory_chars}}
