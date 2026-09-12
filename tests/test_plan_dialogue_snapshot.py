"""A growing room cannot mint a paid cycle during exact replay/collection."""
from __future__ import annotations

import json

from tests.test_plan_review_engine import harness as _harness, _call, _state, CLEAN

harness = _harness
from tests.test_plan_review_reconciliation import _collect, _install_barrier_substrate
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.tools.plan_dialogue import plan_chat_reader
from ouroboros.utils import append_jsonl


def test_room_growth_reuses_snapshot_until_author_changes_plan(harness):
    substrate = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    chat = harness.drive / "logs" / "chat.jsonl"
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "Owner chooses A"})
    append_jsonl(chat, {"direction": "out", "chat_id": 1, "text": "A costs less; B keeps options"})
    _call(ctx)
    first = _state(harness)["waves"][-1]
    source = read_actor_source_bytes(harness.drive, ctx.task_id, first["dialogue_source_ref"])
    append_jsonl(chat, {"direction": "out", "chat_id": 1, "text": "Panel complete, collecting"})
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "Actually keep B as well"})
    replay_text = _call(ctx)
    replay = _state(harness)["waves"][-1]
    from ouroboros.tools.plan_review_artifacts import read_wave
    own = read_wave(harness.drive, ctx.task_id, first["wave_artifact"])["evidence_manifest_full"]["own_dialogue"]
    assert first["dialogue_source_ref"]["sha256"] in replay_text
    assert first["dialogue_source_ref"]["path"] in replay_text and own["captured_at"] in replay_text
    assert "Later messages are not claimed reviewed" in replay_text
    assert "Snapshot coverage" in replay_text
    assert replay["request_fingerprint"] == first["request_fingerprint"]
    assert _state(harness)["cycles_paid"] == 1 and len(substrate.calls) == 1
    exact = plan_chat_reader(harness.drive, ctx.task_id)(f"1@{first['dialogue_source_ref']['sha256']}")
    assert exact["text"].encode() == source and "Actually keep B" not in exact["text"]
    _call(ctx, plan="Revise the outline to keep both A and B.")
    revised = _state(harness)["waves"][-1]
    assert revised["dialogue_source_ref"] != first["dialogue_source_ref"]
    assert b"Actually keep B as well" in read_actor_source_bytes(harness.drive, ctx.task_id, revised["dialogue_source_ref"])
    assert _state(harness)["cycles_paid"] == 2 and len(substrate.calls) == 2


def test_free_collection_keeps_exact_range_after_progress_and_mailbox_growth(harness, monkeypatch):
    calls = []
    _install_barrier_substrate(monkeypatch, calls)
    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    append_jsonl(harness.drive / "logs" / "chat.jsonl", {"direction": "out", "chat_id": 1, "text": "Before dispatch"})
    _call(ctx)
    first = _state(harness)["waves"][-1]
    append_jsonl(harness.drive / "logs" / "progress.jsonl", {"chat_id": 1, "content": "Panel done"})
    _collect(ctx, first["request_fingerprint"])
    collected = _state(harness)["waves"][-1]
    assert collected["dialogue_source_ref"] == first["dialogue_source_ref"]
    assert [row["reconcile_only"] for row in calls] == [False, True]
    assert _state(harness)["cycles_paid"] == 1
    from ouroboros.tools.plan_evidence import resolve_evidence
    ref = first["dialogue_source_ref"]
    locator = f"chat:1@{ref['sha256']}::lines=2-2"
    selected = resolve_evidence([locator], active_root=harness.workspace, allowed_roots=[],
                               resolve_chat=plan_chat_reader(harness.drive, ctx.task_id))["attached"][0]
    assert json.loads(selected["text"])["text"] == "Before dispatch"
    from ouroboros.task_results import _compact_plan_review_wave
    compact = _compact_plan_review_wave(collected)
    assert compact["dialogue_source_ref"] == ref
    assert compact["author_request_fingerprint"] == collected["author_request_fingerprint"]


def test_mixed_delivery_keeps_full_file_and_exact_overflow_range(harness, monkeypatch):
    from types import SimpleNamespace
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools.plan_dialogue import attach_own_dialogue, dialogue_slot_inputs, render_dialogue
    from ouroboros.tools import review_synthesis
    from ouroboros import review_native_episode

    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    append_jsonl(harness.drive / "logs" / "chat.jsonl", {"direction": "out", "chat_id": 1,
                 "text": "OLDER " + "discussion " * 22000})
    append_jsonl(harness.drive / "logs" / "chat.jsonl", {"direction": "in", "chat_id": 1,
                 "text": "LATEST CHOICE"})
    manifest = attach_own_dialogue(ctx, harness.drive, {}, "a" * 64, persist=True)
    own = manifest["own_dialogue"]
    packet = render_dialogue(manifest)
    def slot(name, native=False, session=False):
        return SimpleNamespace(slot_id=name, model="same/model", role_hint="plan reviewer", use_local=False, session_profile=name,
                               route=ReviewRouteKind.AGENT_SESSION if session else ReviewRouteKind.API_CHAT,
                               retrieves=native or session, native_retrieval=native)
    slots = [slot("small"), slot("large"), slot("native", native=True), slot("delegated", session=True)]
    monkeypatch.setattr(review_synthesis, "per_slot_input_token_limits", lambda *a, **k: {"small": 4000, "large": 200000})
    declarations = []
    def bound(*a, **kwargs):
        declarations.append(kwargs)
        return 100000
    monkeypatch.setattr(review_native_episode, "review_native_transcript_bound", bound)
    delivery = dialogue_slot_inputs(slots, system_prompt="governance", user_content=packet,
                                   session_task=packet, manifest=manifest, slot_messages={}, native_mandatory_chars=len(packet), session_root=str(harness.workspace), task_id=ctx.task_id)
    small = json.dumps(delivery["slot_messages"]["small"], ensure_ascii=False)
    large = json.dumps(delivery["slot_messages"]["large"], ensure_ascii=False)
    assert "LATEST CHOICE" in small and "exact omitted prefix" in small
    assert "OLDER " not in small and "OLDER " in large
    native = delivery["slot_session_tasks"]["native"]
    assert "LATEST CHOICE" in native and "exact omitted prefix" in native
    assert declarations[0]["mandatory_read_chars"] == len(packet)
    delegated = delivery["slot_session_tasks"]["delegated"]
    assert "MANDATORY FULL READ" in delegated and own["file"] in delegated
    assert "no numerical window evidence" in delegated and "1M" not in delegated
    assert "discussion discussion discussion" not in delegated
    source = read_actor_source_bytes(harness.drive, ctx.task_id, own["source_ref"])
    assert len(source) > 120000 and source.decode() == own["text"]
    import re
    match = re.search(r'::bytes=0-(\d+)', native)
    assert match and 0 < int(match[1]) < len(source)
    tail_start = int(match[1]) + 1
    assert source[tail_start:].decode() in native
    assert not (harness.workspace / ".ouroboros-review").exists()


def test_dialogue_source_survives_real_child_promotion_and_cleanup(harness):
    from ouroboros.headless import prepare_task_drive, copy_child_task_result, remove_subagent_task_drive
    from ouroboros.task_results import write_task_result, load_plan_review_state
    from ouroboros.tools.plan_review_artifacts import authority_wave

    ctx = harness.make_ctx(task_id="source")
    parent = harness.drive
    child = prepare_task_drive(parent, "source", "empty")
    ctx.drive_root = child
    ctx.current_chat_id = 1
    from tests.test_plan_review_engine import _finding
    from ouroboros.tools import plan_review as pr
    harness.install({"s1": json.dumps([_finding("note", "note")]), "s2": CLEAN, "s3": CLEAN})
    append_jsonl(child / "logs" / "chat.jsonl", {"direction": "in", "chat_id": 1, "text": "Must survive cleanup"})
    _call(ctx)
    first = load_plan_review_state(child, "source")["waves"][-1]
    raw = read_actor_source_bytes(child, "source", first["dialogue_source_ref"])
    predecessor = first["wave_artifact"]
    pr._handle_plan_task(ctx, review_disposition={"review_fingerprint": first["request_fingerprint"],
        "items": [{"finding_id": "s1:note", "decision": "defer", "rationale": "Later cosmetic work"}]})
    write_task_result(child, "source", "completed")
    copied = copy_child_task_result(parent, {"id": "source", "drive_root": str(child)})
    assert copied["child_ref_promotion"]["status"] == "complete"
    assert remove_subagent_task_drive(parent, "source") is True
    assert not child.exists()
    wave = load_plan_review_state(parent, "source")["waves"][-1]
    assert read_actor_source_bytes(parent, "source", wave["dialogue_source_ref"]) == raw
    restored = authority_wave(parent, "source", wave)
    assert restored["evidence_manifest_full"]["own_dialogue"]["file"].startswith(str(parent))
    from ouroboros.tools.plan_review_artifacts import read_wave
    exact = read_wave(parent, "source", wave["wave_artifact"])
    assert exact["supersedes_wave_artifact"]["sha256"] == predecessor["sha256"]
    assert read_wave(parent, "source", exact["supersedes_wave_artifact"])["plan_prose"]


def test_same_root_sibling_rooms_are_pointers_and_unrelated_rooms_stay_private(harness):
    from ouroboros.projects_registry import create_project, bind_task_to_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.plan_dialogue import related_rooms

    projects = {name: create_project(harness.drive, name, name=name) for name in ('own', 'sibling', 'unrelated')}
    for tid, name, root in [('child-a', 'own', 'parent'), ('child-b', 'sibling', 'parent'), ('private', 'unrelated', 'different-root')]:
        bind_task_to_project(harness.drive, tid, projects[name]['id'], origin={'absent': 'system'})
        write_task_result(harness.drive, tid, 'running', parent_task_id=root, root_task_id=root)
    ctx = harness.make_ctx(task_id='child-a')
    ctx.task_metadata = {'parent_task_id': 'parent', 'root_task_id': 'parent'}
    pointers = related_rooms(ctx, harness.drive, projects['own']['chat_id'])
    assert {p['locator'] for p in pointers} == {'chat:1', f"chat:{projects['sibling']['chat_id']}"}
    assert all(p['delivery'] == 'pointer_only' and 'text' not in p for p in pointers)


def test_budget_prices_the_actual_window_fitted_inputs(harness, monkeypatch):
    from ouroboros.tools import plan_review as pr, review_synthesis
    from ouroboros import usage_accounting as ua
    from tests.test_plan_review_engine import _user_text
    from ouroboros.tools.plan_review_runtime import PLAN_REVIEW_MAX_TOKENS

    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    append_jsonl(harness.drive / 'logs/chat.jsonl', {'ts': '2026-09-01T00:00:00Z',
        'direction': 'out', 'chat_id': 1, 'text': 'Prior substantive discussion. ' * 20000})
    monkeypatch.setattr(review_synthesis, 'per_slot_input_token_limits',
                        lambda models, **kw: {slot.slot_id: 10000 for slot in kw['slots']})
    substrate = harness.install({'s1': CLEAN, 's2': CLEAN, 's3': CLEAN})
    captured = []
    monkeypatch.setattr(pr, 'review_wave_budget_gate', lambda *a, **kw: captured.append(kw))
    _call(ctx)
    request = substrate.calls[0]['request']
    slots = substrate.calls[0]['slots']
    actual = [sum(len(_user_text(message['content'])) for message in request.slot_messages[slot.slot_id]) for slot in slots]
    assert captured[0]['prompt_chars'] == actual
    assert captured[0]['max_completion_tokens'] == PLAN_REVIEW_MAX_TOKENS
    # Illustrative price replaces only vendor lookup, not the admission math.
    monkeypatch.setattr(ua, 'estimate_cost_optional', lambda model, prompt, completion, **kw: prompt / 100000)
    admission = ua.review_wave_admission(root_task_id='budget-probe', models=[slot.model for slot in slots],
        prompt_chars=captured[0]['prompt_chars'], max_completion_tokens=PLAN_REVIEW_MAX_TOKENS, remaining_usd_override=1.0)
    assert admission['fits'] is True and admission['estimated_wave_usd'] == 0.3
    assert len(substrate.calls) == 1


def test_free_collection_reuses_policy_after_live_exploration_changes(harness, monkeypatch):
    from tests.test_plan_review_event_route import _install_real_substrate, _wait_until, _mailbox_entries
    from ouroboros.tools import plan_review_runtime
    from tests.test_plan_review_engine import _control
    live = ['Read the initial discussion.']
    monkeypatch.setattr(plan_review_runtime, 'root_exploration_log', lambda _ctx: live[0])
    executor = _install_real_substrate(monkeypatch)  # Only the model executor is fake; custody is real.
    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    try:
        _call(ctx)
        first = _state(harness)['waves'][-1]
        from ouroboros.tools.plan_review_artifacts import read_wave
        sent = read_wave(harness.drive, ctx.task_id, first['wave_artifact'])
        assert sent['request_policy']['native_mandatory_read_chars'] > 0
        assert _wait_until(lambda: executor.execute_calls == 3)
        live[0] += ' Processed an owner clarification and waited for the panel.'
        executor.release.set()
        assert _wait_until(lambda: len(_mailbox_entries(harness.drive, ctx.task_id)) == 1)
        collected = _collect(ctx, first['request_fingerprint'])
        assert _control(collected) == {'outcome': 'GREEN', 'closed': True}
        assert executor.execute_calls == 3 and _state(harness)['cycles_paid'] == 1
        assert 'custody is unavailable' not in collected
        settled = read_wave(harness.drive, ctx.task_id, _state(harness)['waves'][-1]['wave_artifact'])
        assert settled['request_policy'] == sent['request_policy']
        assert settled['slot_prompt_chars'] == sent['slot_prompt_chars']
        assert [row['request_messages'] for row in settled['reviewer_outputs']] == [row['request_messages'] for row in sent['reviewer_outputs']]
    finally:
        executor.release.set()


def test_missing_recorded_policy_does_not_infer_current_paid_contract():
    import pytest
    from ouroboros.tools.plan_review_artifacts import frozen_delivery_inputs, PlanReviewSourceUnavailable
    with pytest.raises(PlanReviewSourceUnavailable, match='original request policy/fit was not recorded'):
        frozen_delivery_inputs({'reviewer_outputs': []}, [])
