"""Model answer identity and host disclosures through the real terminal consumers."""

import asyncio
from collections import deque
import hashlib
import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros import agent_task_pipeline as pipeline, loop
from ouroboros.task_results import load_task_result, write_task_result
from tests.test_delivery_forced_finalization import _bind_host_pass, _forced_test_context
from tests.test_ui_smoke_playwright import direct_server_with_data as _direct_server_with_data

direct_server_with_data = _direct_server_with_data


ANSWER = "Exact model answer: λ\n\nThe useful result."
NOTICE = "Plan review remained open.\n\n⚠️ Deferred child result: child1."


@pytest.mark.parametrize("change", ["generation", "superseded", "replaced_panel"])
def test_equal_evidence_fields_cannot_revive_changed_owner_authority(tmp_path, monkeypatch, change):
    _loop, tools, ctx, trace = _forced_test_context(tmp_path)
    old = loop._replace_delivery_candidate(tools, ctx, trace, ANSWER, control="candidate")
    prior = _bind_host_pass(loop, tools, trace, old)
    before = old.evidence_revision, old.evidence_fingerprint
    if change == "generation":
        tools._ctx._task_acceptance_owner_generation = 1
        tools._ctx.owner_message_admission_agent = SimpleNamespace(_owner_message_generation=2)
    elif change == "superseded":
        loop._supersede_task_acceptance_for_owner_followup(tools._ctx, trace)
    else:
        prior["superseded_by_revision"] = True
        trace["review_runs"].append({**prior, "superseded_by_revision": False,
                                   "panel_id": "new-panel", "binding_hash": "new-binding", "aggregate_signal": "FAIL"})
        trace["review_decision"].update(panel_id="new-panel", binding_hash="new-binding")
    assert loop._current_delivery_candidate(ctx, trace) is None
    assert (old.evidence_revision, old.evidence_fingerprint) == before
    monkeypatch.setattr(loop, "call_llm_with_retry", lambda *_a, **_kw: (None, 0.0))
    text, usage, _trace = loop._forced_final_answer(
        ctx, prompt="finalize", fallback_text=ANSWER, reason_code="round_limit",
    )
    current = tools._ctx._delivery_candidate
    assert text == ANSWER and current is not old
    assert current.acceptance_binding["authoritative"] is False
    assert current.acceptance_binding["stale_evidence"] is True
    assert prior["superseded_by_revision"] is True
    assert "STALE-EVIDENCE NOTICE" in usage["terminal_host_notice"]


@pytest.mark.parametrize("answer", [ANSWER, " \n"])
def test_normal_finalization_without_a_candidate_keeps_raw_answer(tmp_path, monkeypatch, answer):
    _loop, tools, ctx, trace = _forced_test_context(tmp_path)
    assert loop._live_delivery_candidate(ctx) is None
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_a, **_kw: False)
    monkeypatch.setattr(loop, "_force_plan_disclosure", lambda *_a, **_kw: NOTICE)
    text, usage, _trace = loop._no_tool_final_answer(answer, ctx, trace, tools, queue.Queue(), set(), lambda _t: None)
    assert text == answer and usage["terminal_host_notice"] == NOTICE
    assert NOTICE not in text


@pytest.mark.parametrize("verdict", ["FAIL", "PASS"])
def test_host_notice_does_not_replace_or_supersede_an_unchanged_answer(tmp_path, monkeypatch, verdict):
    import ouroboros.review_substrate as rs
    from ouroboros.contracts.task_contract import build_task_contract

    _loop, tools, ctx, trace = _forced_test_context(tmp_path)
    tools._ctx.task_contract = build_task_contract({"id": "parent1", "expected_output": "A report"})
    write_task_result(tmp_path, "parent1", "running", task_contract=tools._ctx.task_contract)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    monkeypatch.setattr(rs, "triad_delivery_slots", lambda **_kw: [object()])
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_a, **_kw: False)
    monkeypatch.setattr(loop, "_force_plan_disclosure", lambda *_a, **_kw: NOTICE)
    subjects = []

    def review(request, **_kwargs):
        subjects.append(request.subject)
        return rs.ReviewRunResult(
            request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
            actors=[{"slot_id": "s0", "signal": verdict, "parsed": {
                "verdict": verdict, "outcome_tier": "solved" if verdict == "PASS" else "best_effort",
                "criteria_used": [{"criterion": "report", "status": "supported", "evidence_refs": ["artifact:1"]}],
            }}], parsed_findings=[], aggregate_signal=verdict,
        )

    monkeypatch.setattr(rs, "run_review_request", review)

    def finalize():
        result = loop._no_tool_final_answer(ANSWER, ctx, trace, tools, queue.Queue(), set(), lambda _t: None)
        if result is not None:
            assert result[0] == ANSWER
            assert result[1]["terminal_host_notice"] == NOTICE
        return result

    first = finalize()
    assert (first is None) is (verdict == "FAIL")  # A real FAIL still requests its ordinary improvement.
    candidate = tools._ctx._delivery_candidate
    assert finalize() is not None  # An unchanged answer reuses the verdict; its notice cannot demand a rewrite.
    binding = dict(candidate.acceptance_binding)
    assert finalize() is not None
    assert subjects == [ANSWER]
    assert tools._ctx._delivery_candidate is candidate
    assert candidate.content_sha256 == hashlib.sha256(ANSWER.encode()).hexdigest()
    assert candidate.revision == 1 and candidate.acceptance_binding == binding
    assert not trace["review_runs"][0].get("superseded_by_revision")
    assert trace["acceptance_decision"]["status"] == ("accepted" if verdict == "PASS" else "finalized_unaccepted")

    # #533: transport/finalization warning must not erase the bound assessment.
    from ouroboros.outcomes import derive_loop_outcome, normalize_outcome_axes, public_task_result
    from ouroboros.task_results import load_task_result

    trace["delivery_candidate"].update(degraded=True, degraded_reason="advisory_plan_review_open")
    usage = {"terminal_host_notice": NOTICE}
    axes = derive_loop_outcome(ANSWER, usage, trace)["outcome_axes"]
    expected = "pass" if verdict == "PASS" else "fail"
    assert axes["objective"]["status"] == expected
    assert axes["objective"]["source"] == "task_acceptance_review"
    assert axes["execution"]["status"] == "degraded"
    env = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path)
    task = {"id": "parent1", "type": "task", "chat_id": 1, "text": "Produce a report",
            "task_contract": tools._ctx.task_contract, "_skip_post_task_synthesis": True}
    pipeline._store_task_result(env, task, ANSWER, usage, trace)
    stored = load_task_result(tmp_path, "parent1")
    assert stored["outcome_axes"]["objective"]["status"] == expected
    assert normalize_outcome_axes(public_task_result(stored))["objective"]["source"] == "task_acceptance_review"
    pending = []
    pipeline.emit_task_results(env, None, None, pending, task, ANSWER, usage, trace,
                              start_time=0.0, drive_logs=tmp_path / "logs")
    terminal = next(row for row in pending if row["type"] == "task_done")
    assert terminal["outcome_axes"]["objective"]["status"] == expected
    assert load_task_result(tmp_path, "parent1")["outcome_axes"]["execution"]["status"] == "degraded"

    # Real input changes still supersede the binding even when answer bytes match.
    tools._ctx._owner_directives = [{"text": "Use the newly supplied source."}]
    changed = loop._replace_delivery_candidate(tools, ctx, trace, ANSWER, control="candidate")
    assert changed is not candidate and changed.revision > candidate.revision
    assert changed.acceptance_binding["authoritative"] is False
    assert trace["review_runs"][0]["superseded_by_revision"] is True


def _emit_terminal(tmp_path, monkeypatch, *, ephemeral=False, project=False, child=False, notice=NOTICE, answer=ANSWER):
    from ouroboros.task_finalization import set_terminal_host_notice

    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *_a, **_kw: None)
    task = {"id": "notice-root", "type": "task", "chat_id": 1, "text": "Produce the report."}
    if child:
        task.update(id="child1", parent_task_id="parent1", root_task_id="parent1", delegation_role="subagent")
    if project:
        from ouroboros.projects_registry import bind_task_to_project, create_project

        row = create_project(tmp_path, "notice-project", name="Research")
        bind_task_to_project(tmp_path, task["id"], row["id"], row["chat_id"], origin={"absent": "system"})
        task.update(project_id=row["id"], chat_id=row["chat_id"])
    if ephemeral:
        task.update(_ephemeral_turn=True, _is_direct_chat=True)
    usage = {"terminal_origin": "model_final"}
    set_terminal_host_notice(usage, notice)
    pending = []
    pipeline.emit_task_results(
        SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path), None, None,
        pending, task, answer, usage, {"tool_calls": [], "reasoning_notes": []},
        start_time=0.0, drive_logs=tmp_path / "logs",
    )
    return task, next(row for row in pending if row["type"] == "send_message")


@pytest.mark.parametrize("answer", [ANSWER, ""], ids=["answer", "no_answer"])
@pytest.mark.parametrize("notice", [NOTICE, NOTICE + "\n" + "retained host evidence " * 1500 + "\nEND NOTICE"],
                         ids=["short_notice", "long_notice"])
def test_parent_readers_receive_full_notice_and_budget_the_complete_body(tmp_path, monkeypatch, answer, notice):
    from ouroboros.task_finalization import provider_terminal_body
    from ouroboros.task_status import format_subagent_absorption_message
    from ouroboros.tools.control_task_results import _get_task_result, _wait_for_task
    from tests.test_child_result_disposition import _parent_ctx

    task, _event = _emit_terminal(tmp_path, monkeypatch, child=True, answer=answer, notice=notice)
    stored = load_task_result(tmp_path, task["id"])
    assert stored["result"] == answer and stored["terminal_host_notice"] == notice
    model_hash = hashlib.sha256(stored["result"].encode()).hexdigest()
    parent = _parent_ctx(tmp_path)
    for output in (_get_task_result(parent, task["id"]), _wait_for_task(parent, task["id"], timeout_sec=0)):
        if answer:
            assert f"[BEGIN_SUBTASK_OUTPUT]\n{answer}\n[END_SUBTASK_OUTPUT]" in output
        else:
            assert stored["status"] == "failed" and "No details available." in output
        assert output.endswith("[Host status]\n" + notice)
        assert output.count(notice) == 1

    body = provider_terminal_body(answer, notice)
    full = format_subagent_absorption_message([stored], parent_task_id="parent1", budget_chars=len(body))
    assert body in full and "FULL RESULT OMITTED" not in full
    omitted = format_subagent_absorption_message([stored], parent_task_id="parent1", budget_chars=len(body) - 1)
    assert f"{len(body)} chars" in omitted and 'get_task_result("child1")' in omitted
    assert notice not in omitted and (not answer or answer not in omitted)
    combined = format_subagent_absorption_message(
        [stored, {**stored, "task_id": "child2"}], parent_task_id="parent1", budget_chars=2 * len(body) - 1,
    )
    assert combined.count(body) == 1 and 'get_task_result("child2")' in combined
    assert hashlib.sha256(load_task_result(tmp_path, task["id"])["result"].encode()).hexdigest() == model_hash


def test_changed_notice_reopens_parent_disposition_and_automatic_handoff(tmp_path, monkeypatch):
    from ouroboros.task_finalization import set_terminal_host_notice, terminal_result_fields
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256, _current_child_result_disposition
    from ouroboros.tools.task_tree import _tree_note
    from tests.test_child_result_disposition import _parent_ctx, _payload

    task, _event = _emit_terminal(tmp_path, monkeypatch, child=True)
    ctx = _parent_ctx(tmp_path)
    tools = SimpleNamespace(_ctx=ctx)
    first = load_effective_task_result(tmp_path, task["id"])
    old_hash = _child_result_sha256(first)
    model_hash = hashlib.sha256(first["result"].encode()).hexdigest()
    assert NOTICE in loop._compute_subagent_handoff(tools, tmp_path, "parent1", "")
    payload = _payload(task["id"], "integrated", old_hash)
    assert _tree_note(ctx, "decision", "absorbed answer and host notice", payload=payload).startswith("OK:")
    assert _current_child_result_disposition(load_effective_task_result(tmp_path, task["id"])) == "integrated"
    assert loop._compute_subagent_handoff(tools, tmp_path, "parent1", "") == ""

    notice = "The child result was preserved before newer evidence arrived."
    usage = {}
    set_terminal_host_notice(usage, notice)
    write_task_result(tmp_path, task["id"], first["status"], **terminal_result_fields(usage))
    changed = load_effective_task_result(tmp_path, task["id"])
    assert changed["result"] == first["result"] == ANSWER
    assert hashlib.sha256(changed["result"].encode()).hexdigest() == model_hash
    assert _child_result_sha256(changed) != old_hash
    assert _current_child_result_disposition(changed) == ""
    assert "CHILD_RESULT_STALE" in _tree_note(ctx, "decision", "old consumption", payload=payload)
    handoff = loop._compute_subagent_handoff(tools, tmp_path, "parent1", "")
    assert ANSWER + "\n\n[Host status]\n" + notice in handoff
    assert _child_result_sha256(changed) in handoff
    assert loop._compute_subagent_handoff(tools, tmp_path, "parent1", "") == ""


@pytest.mark.parametrize("mode", ["all_terminal", "any_terminal"])
@pytest.mark.parametrize("answer", [ANSWER, ANSWER + "\n" + "model detail " * 1500, ""],
                         ids=["short_answer", "long_answer", "no_answer"])
@pytest.mark.parametrize("notice", [NOTICE, NOTICE + "\n" + "retained host evidence " * 1500 + "\nEND NOTICE"],
                         ids=["short_notice", "long_notice"])
def test_batch_wait_delivers_notice_before_current_hash_disposition(tmp_path, monkeypatch, mode, answer, notice):
    """The real batch reader must deliver the warning before disposition hides handoff."""
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256, _current_child_result_disposition
    from ouroboros.tools.registry import ToolRegistry
    from tests.test_child_result_disposition import _payload

    task, _event = _emit_terminal(tmp_path, monkeypatch, child=True, answer=answer, notice=notice)
    stored = load_task_result(tmp_path, task["id"])
    tools = ToolRegistry(tmp_path / "repo", tmp_path / "parent-execution")
    tools._ctx.task_id = "parent1"
    tools._ctx.task_metadata = {"budget_drive_root": str(tmp_path), "root_task_id": "parent1"}
    args = {"task_ids": [task["id"]], "timeout_sec": 0, "mode": mode}
    result = tools.execute_result("wait_tasks", args)
    assert result.status == "ok"
    batch = json.loads(result.text)
    assert batch["all_terminal"] is True
    shown = batch["tasks"][task["id"]]
    assert shown["result"] == answer
    assert shown["terminal_host_notice"] == notice
    assert notice not in shown["result"]
    assert shown["child_result_sha256"] == _child_result_sha256(load_effective_task_result(tmp_path, task["id"]))
    assert "get_task_result" in batch["tasks_note"]
    assert not {"trace_refs", "loop_outcome", "verification_ledger"} & shown.keys()

    disposition = tools.execute("tree_note", {
        "kind": "decision", "text": "Absorbed the answer and its separately authored host limitation.",
        "payload": _payload(task["id"], "integrated", shown["child_result_sha256"]),
    })
    assert disposition.startswith("OK:")
    assert _current_child_result_disposition(load_effective_task_result(tmp_path, task["id"])) == "integrated"
    assert loop._compute_subagent_handoff(tools, tmp_path, "parent1", "") == ""
    assert json.loads(tools.execute("wait_tasks", args))["tasks"][task["id"]] == shown
    assert tools.execute("get_task_result", {"task_id": task["id"]}).endswith("[Host status]\n" + notice)
    assert load_task_result(tmp_path, task["id"]) == stored


def test_batch_wait_without_notice_keeps_the_original_projection(tmp_path, monkeypatch):
    from ouroboros.outcomes import normalize_outcome_axes
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.control_task_results import _wait_for_tasks
    from ouroboros.tools.join_ledger import _child_result_sha256
    from tests.test_child_result_disposition import _parent_ctx

    task, _event = _emit_terminal(tmp_path, monkeypatch, child=True, notice="")
    stored = load_task_result(tmp_path, task["id"])
    current = load_effective_task_result(tmp_path, task["id"])
    assert "terminal_host_notice" not in current
    batch = json.loads(_wait_for_tasks(_parent_ctx(tmp_path), [task["id"]], timeout_sec=0))
    assert batch["tasks"][task["id"]] == {
        "task_id": task["id"], "status": current["status"],
        "accounted_upper_bound_usd": current["accounted_upper_bound_usd"],
        "cost_final": current.get("cost_final"),
        "child_result_sha256": _child_result_sha256(current),
        "outcome_axes": normalize_outcome_axes(current),
        "result": ANSWER, "trace_summary": current.get("trace_summary"),
    }
    assert load_task_result(tmp_path, task["id"]) == stored


def test_child_notice_hash_extension_preserves_legacy_hash_and_telemetry_exclusions():
    from ouroboros.tools.join_ledger import _child_result_sha256

    legacy = {"status": "completed", "result": "legacy answer", "trace_summary": "trace",
              "artifact_status": "ready", "artifacts": []}
    assert _child_result_sha256(legacy) == "cc3314bd27a9639006ccfafe9500bf25cfe5c3c6746b47c4d40736768b8b5985"
    current = {**legacy, "terminal_host_notice": NOTICE}
    assert _child_result_sha256(current) != _child_result_sha256(legacy)
    assert _child_result_sha256({**current, "terminal_host_notice": "changed"}) != _child_result_sha256(current)
    telemetry = {"cost_usd": 9, "accounted_upper_bound_usd": 10, "updated_at": "later", "ts": "later",
                 "parent_decision": "integrated", "queue_reconciliation_warning": "diagnostic",
                 "terminal_provider_notice": "older metadata remains outside this hash",
                 "terminal_origin": "host_salvage"}
    for row in (legacy, current):
        assert _child_result_sha256({**row, **telemetry}) == _child_result_sha256(row)


@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize("project", [False, True])
def test_notice_is_a_system_row_live_and_on_history_replay(tmp_path, monkeypatch, ephemeral, project):
    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.utils import append_jsonl
    from supervisor import events_chat_delivery as delivery, message_bus
    from supervisor.terminal_delivery import build_completed_result_event, pending_deliveries

    task, event = _emit_terminal(tmp_path, monkeypatch, ephemeral=ephemeral, project=project)
    assert event["text"] == event["log_text"] == ANSWER
    if not ephemeral:
        stored = load_task_result(tmp_path, task["id"])
        assert stored["result"] == ANSWER and stored["terminal_host_notice"] == NOTICE
        replay = build_completed_result_event(tmp_path, task, task["id"], stored)
        assert replay["text"] == ANSWER and replay["terminal_host_notice"] == NOTICE
        assert replay["delivery_id"] == event["delivery_id"]
        assert pending_deliveries(tmp_path)[0]["terminal_host_notice"] == NOTICE
    else:
        assert load_task_result(tmp_path, task["id"]) is None

    bridge = message_bus.LocalChatBridge({})
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"owner_id": 7})
    monkeypatch.setattr(message_bus, "_advance_project_visible_revision", lambda _chat: None)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, append_jsonl=append_jsonl,
                          send_with_budget=message_bus.send_with_budget)
    delivery._handle_send_message(event, ctx)
    if not ephemeral:  # A transient turn has no terminal outbox identity of its own.
        delivery._handle_send_message(event, ctx)
    chats = [row for row in frames if row.get("type") == "chat"]
    assert [(row["role"], row["content"]) for row in chats] == [("assistant", ANSWER), ("system", NOTICE)]
    assert all(row["chat_id"] == task["chat_id"] for row in chats)
    response = asyncio.run(make_chat_history_endpoint(tmp_path)(SimpleNamespace(query_params={"chat_id": str(task["chat_id"])})))
    messages = json.loads(response.body)["messages"]
    assert [(row["role"], row["text"]) for row in messages] == [("assistant", ANSWER), ("system", NOTICE)]
    assert pending_deliveries(tmp_path) == []


@pytest.mark.parametrize("outcome", ["message", "deferred", "silent", "tool_delivered"])
def test_presence_delivers_host_notice_once_and_preserves_silence(tmp_path, monkeypatch, outcome):
    from ouroboros.presence_runner import PresenceTurnGate, run_presence_turn
    from tests.test_presence_runner import _admission, _event

    class Agent:
        def handle_task(self, task):
            task["_skip_post_task_synthesis"] = True
            ctx = SimpleNamespace(_presence_completion={"outcome": outcome, "message": ANSWER},
                                  _swarm_handoff_attempt={"status": "scheduled", "task_id": "next-task"})
            pending = []
            pipeline.emit_task_results(
                SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path), None, None,
                pending, task, ANSWER, {"terminal_origin": "model_final", "terminal_host_notice": NOTICE},
                {"tool_calls": [], "reasoning_notes": []}, start_time=0.0, drive_logs=tmp_path / "logs", ctx=ctx,
            )
            return pending

    args = dict(admission=_admission(), event=_event(), repo_dir=tmp_path, drive_root=tmp_path,
                agent_factory=lambda **_kw: Agent(), gate=PresenceTurnGate(2))
    first = run_presence_turn(**args)
    assert run_presence_turn(**args) == first
    assert first.outcome == outcome
    assert load_task_result(tmp_path, first.task_id)["result"] == ANSWER
    assert first.text == (ANSWER + "\n\n[Host status]\n" + NOTICE if outcome in {"message", "deferred"} else "")


def test_failed_notice_remains_owed_after_answer_delivery(tmp_path, monkeypatch):
    from ouroboros.utils import append_jsonl
    from supervisor import events_chat_delivery as delivery
    from supervisor.terminal_delivery import pending_deliveries

    _task, event = _emit_terminal(tmp_path, monkeypatch)
    sent = []

    def fail_notice(_chat, text, **kwargs):
        if kwargs.get("role") == "system":
            raise OSError("transport failed")
        sent.append(text)

    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, append_jsonl=append_jsonl, send_with_budget=fail_notice)
    delivery._handle_send_message(event, ctx)
    assert sent == [ANSWER]
    [owed] = pending_deliveries(tmp_path)
    assert owed["text"] == NOTICE and owed["role"] == "system"
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    ctx.send_with_budget = lambda _chat, text, **_kw: sent.append(text)
    delivery._handle_send_message(event, ctx)
    assert sent == [ANSWER, NOTICE]
    assert pending_deliveries(tmp_path) == []


@pytest.mark.parametrize("jsonl", [False, True])
def test_cli_no_stream_keeps_notices_without_changing_result_bytes(tmp_path, monkeypatch, capsys, jsonl):
    from ouroboros import cli
    from ouroboros.gateway.tasks import api_task_get

    task, _event = _emit_terminal(tmp_path, monkeypatch)
    response = asyncio.run(api_task_get(SimpleNamespace(
        path_params={"task_id": task["id"]}, app=SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
    )))
    assert response.status_code == 200
    stored = {**json.loads(response.body), "cost_final": True, "cost_with_children_partial": False}
    monkeypatch.setattr(cli, "_client", lambda *_a, **_kw: SimpleNamespace(
        request=lambda *_a, **_kw: {"task_id": task["id"]},
    ))
    monkeypatch.setattr(cli, "_wait_task", lambda *_a, **_kw: stored)
    assert cli.main(["run", "--no-stream", *(["--jsonl"] if jsonl else []), "Produce the report."]) == 0
    captured = capsys.readouterr()
    if jsonl:
        final = json.loads(captured.out.splitlines()[-1])["result"]
        assert final["result"] == ANSWER and final["terminal_host_notice"] == NOTICE
        assert NOTICE not in captured.err
    else:
        assert captured.out == ANSWER + "\n"
        assert captured.err == "[Host status]\n" + NOTICE + "\n"


def test_synthesis_keeps_notice_authorship_separate():
    from ouroboros.task_finalization import build_sealed_final_package, sealed_final_prompt_section

    sealed = build_sealed_final_package({"terminal_host_notice": NOTICE}, ANSWER)
    assert sealed["final_result_text"] == ANSWER
    assert sealed["terminal_host_notice"] == NOTICE
    assert "Host-authored terminal notice (separate from the model answer):\n" + NOTICE in sealed_final_prompt_section(sealed)


@pytest.mark.ui_browser
def test_browser_renders_model_answer_and_host_notice_separately(direct_server_with_data, monkeypatch):
    """Real terminal producer, delivery writer, HTTP history and rendered SPA."""
    from playwright.sync_api import sync_playwright
    from ouroboros.utils import append_jsonl
    from supervisor import events_chat_delivery as delivery, message_bus

    data = direct_server_with_data["data_dir"]
    _task, event = _emit_terminal(data, monkeypatch)
    bridge = message_bus.LocalChatBridge({})
    monkeypatch.setattr(message_bus, "DATA_DIR", data)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"owner_id": 7})
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    delivery._handle_send_message(event, SimpleNamespace(
        DRIVE_ROOT=data, RUNNING={}, append_jsonl=append_jsonl, send_with_budget=message_bus.send_with_budget,
    ))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            answer = page.locator(".chat-bubble.assistant").filter(has_text="Exact model answer")
            notice = page.locator(".chat-bubble.system").filter(has_text="Plan review remained open")
            answer.wait_for(state="visible", timeout=15000)
            notice.wait_for(state="visible", timeout=15000)
            assert "Plan review remained open" not in answer.inner_text()
            assert "Exact model answer" not in notice.inner_text()
            page.screenshot(path=str(data.parent / "terminal-host-notice.png"), full_page=True)
            page.reload(wait_until="domcontentloaded")
            notice.wait_for(state="visible", timeout=15000)
            assert answer.count() == notice.count() == 1
        finally:
            browser.close()
