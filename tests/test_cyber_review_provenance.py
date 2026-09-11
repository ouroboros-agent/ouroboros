"""Recovered first-review and exact-author evidence regressions from the preserved Ouroboros candidate."""
import copy
import json
from types import SimpleNamespace
import pytest
from tests.test_loop_acceptance_gate import _seed_acceptance_root
from tests.test_plan_review_engine import harness, _call, _control, _state  # noqa: F401

pytestmark = pytest.mark.serial

def test_plan_first_dispatch_then_author_repeat_keeps_exact_raw_wave(harness, monkeypatch):  # noqa: F811
    from ouroboros.tools import plan_review
    from ouroboros.tools.plan_review_artifacts import read_wave

    h = harness
    h.state["enforcement"] = "advisory"
    raw = json.dumps([{"id": "f", "class": "blocking", "breaks": "claim_1",
                       "summary": "Unresolved criterion", "recommendation": "Prove it"}])
    from tests.test_review_agent_session_route import AccountedFakeLLM
    from ouroboros.tools import plan_review_runtime

    transport = AccountedFakeLLM(h.drive, reply=raw)
    monkeypatch.setattr(plan_review_runtime, "LLMClient", lambda: transport)
    ctx = h.make_ctx()
    first = _control(_call(ctx))
    assert first["outcome"] == "REVISE_PLAN"
    wave = _state(h)["waves"][-1]
    assert wave["paid"] and transport.calls
    fingerprint = wave["request_fingerprint"]
    request = {"review_fingerprint": fingerprint, "items": [],
               "author_disposition": {"disposition": "deferred", "rationale": "Keep the verified scope."}}
    calls = len(transport.calls)
    for _ in range(2):
        out = plan_review._handle_plan_task(ctx, review_disposition=request)
        assert "Author finish" in out
        assert len(transport.calls) == calls
    current = _state(h)["waves"][-1]
    assert not current["closed"] and current["aggregate"] == "REVISE_PLAN"
    assert current["findings"] == wave["findings"]
    exact = read_wave(h.drive, ctx.task_id, current["wave_artifact"])
    assert raw in json.dumps(exact, ensure_ascii=False) or any(
        row.get("text") == raw or row.get("raw_text") == raw for row in exact.get("reviewer_outputs", [])
    )
    assert current["author_disposition"]["subject_hash"] == fingerprint


def test_plan_author_cannot_replace_first_dispatch_or_bind_stale_wave(harness):  # noqa: F811
    from ouroboros.tools import plan_review

    h = harness
    h.state["enforcement"] = "advisory"
    transport = h.install({"s1": "", "s2": "", "s3": ""})
    ctx = h.make_ctx()
    request = {"review_fingerprint": "a" * 64, "items": [],
               "author_disposition": {"disposition": "rejected", "rationale": "No repeat."}}
    assert "UNBINDABLE" in plan_review._handle_plan_task(ctx, review_disposition=request)
    assert not transport.calls
    _call(ctx)
    request["review_fingerprint"] = _state(h)["waves"][-1]["request_fingerprint"]
    _call(ctx, goal="A different subject")
    calls = len(transport.calls)
    assert "STALE" in plan_review._handle_plan_task(ctx, review_disposition=request)
    assert len(transport.calls) == calls


def test_plan_cancellation_preserves_review_without_author_finish(harness):  # noqa: F811
    from ouroboros.cancel_intents import request_cancel
    from ouroboros.tools import plan_review

    h = harness
    h.state["enforcement"] = "advisory"
    transport = h.install({"s1": "", "s2": "", "s3": ""})
    ctx = h.make_ctx()
    _call(ctx)
    prior = _state(h)["waves"][-1]
    request_cancel(h.drive, ctx.task_id, source="test", reason="Owner stopped")
    out = plan_review._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": prior["request_fingerprint"], "items": [],
        "author_disposition": {"disposition": "deferred", "rationale": "Stop reviewing"},
    })
    assert "cancellation prevents" in out
    assert _state(h)["waves"][-1] == prior
    assert len(transport.calls) == 1


@pytest.mark.parametrize("case", ["same", "no_first", "stale", "blocking", "post_changed"])
def test_commit_author_record_requires_prior_review_of_exact_attempt(tmp_path, monkeypatch, case):
    from types import SimpleNamespace
    from ouroboros.review_records import build_author_disposition
    from ouroboros.review_state import load_state
    from ouroboros.tools.commit_gate import _record_commit_attempt

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking" if case == "blocking" else "advisory")
    ctx = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path, task_id="",
                          _review_advisory=[], _current_review_attempt_number=0)
    raw = [{"slot_id": "critic", "text": "Raw unresolved finding"}]
    if case != "no_first":
        _record_commit_attempt(ctx, commit_message="fixture", status="reviewing",
                               paid=True, pre_review_fingerprint="a" * 64, triad_raw_results=raw)
    author = build_author_disposition(
        disposition="deferred", rationale="The remaining finding is deferred.",
        subject_hash=("b" if case == "stale" else "a") * 64, enforcement="advisory",
    )
    _record_commit_attempt(ctx, commit_message="fixture", status="blocked",
                           pre_review_fingerprint="a" * 64,
                           post_review_fingerprint=("b" if case == "post_changed" else "a") * 64,
                           author_disposition=author)
    row = load_state(tmp_path).attempts[-1]
    assert row.status == "blocked"
    assert bool(row.author_disposition) is (case == "same")
    if case != "no_first":
        assert row.triad_raw_results == raw


@pytest.fixture
def actual_acceptance(tmp_path, monkeypatch):
    from ouroboros import config, loop, review_substrate
    from ouroboros.loop_acceptance_review import _run_task_acceptance_review_once

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: config.get_review_enforcement())
    from tests.test_review_agent_session_route import AccountedFakeLLM

    slot = review_substrate.ReviewSlot(slot_id="s0", model="test-review", effort="high")
    monkeypatch.setattr(review_substrate, "triad_delivery_slots", lambda **k: [slot])
    raw = json.dumps({"verdict": "FAIL", "outcome_tier": "best_effort",
                      "completion_coach": "Check the criterion",
                      "findings": [{"item": "Unaccepted raw finding", "severity": "critical",
                                    "recommendation": "Check the criterion"}]})
    state = {"calls": 0, "unknown": False, "dispatch": True}

    class ControlledLLM(AccountedFakeLLM):
        def chat(self, **kwargs):
            if not state["dispatch"]:
                raise RuntimeError("Route refused before dispatch")
            if state["unknown"]:
                self.calls.append(kwargs)
                exc = TimeoutError("RPC outcome unknown")
                exc.physical_attempt_capture = SimpleNamespace(
                    state="unresolved", provider_status_code=None, provider_code="",
                    provider_error_type="TimeoutError",
                )
                raise exc
            return super().chat(**kwargs)

    physical = ControlledLLM(tmp_path, reply=raw)
    actual_run = review_substrate.run_review_request
    ctx = SimpleNamespace(_task_acceptance_reviewed=False, is_direct_chat=True, drive_root=tmp_path,
                          drive_logs=lambda: tmp_path / "logs")
    _seed_acceptance_root(tmp_path, "task", ctx)
    trace = {"tool_calls": [{"tool": "write_file", "args": {"path": "result.txt"}}]}
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "goal"}]
    def transport(request, *, usage_ctx, **kwargs):
        state["calls"] += 1
        return actual_run(request, usage_ctx=usage_ctx, llm=physical, **kwargs)

    monkeypatch.setattr(review_substrate, "run_review_request", transport)

    def run(content="done"):
        return _run_task_acceptance_review_once(
            tools=SimpleNamespace(_ctx=ctx), content=content, task_id="task", task_type="task",
            llm_trace=trace, drive_root=tmp_path, messages=messages, emit_progress=lambda *a, **k: None,
        )

    def annotate(rationale="The remaining criticism is outside this scope."):
        from jsonschema import validate
        from ouroboros.tools.review import _handle_task_acceptance_review, get_tools
        from ouroboros.loop_tool_execution import process_tool_results
        from tests.provider_contract_catalog import assert_portable_tool_schemas

        args = {"claim": "Verified revised answer", "goal": "Complete the task",
                "agent_disposition": "partial", "rationale": rationale}
        schema = get_tools()[0].schema
        assert_portable_tool_schemas([{"type": "function", "function": schema}])
        validate(args, schema["parameters"])
        result = _handle_task_acceptance_review(ctx, **args)
        process_tool_results([{"fn_name": "task_acceptance_review", "tool_call_id": "author",
                              "result": result, "is_error": False, "args_for_log": args,
                              "tool_args": args, "result_meta": {"status": "ok"}}],
                             messages, trace, emit_progress=lambda *a, **k: None,
                             tools=SimpleNamespace(_ctx=ctx))
        return result

    return SimpleNamespace(ctx=ctx, trace=trace, state=state, run=run, annotate=annotate, root=tmp_path, physical=physical)


@pytest.mark.parametrize("unknown", [False, True])
def test_real_task_panel_then_revised_author_finish_preserves_custody(actual_acceptance, unknown):
    h = actual_acceptance
    h.state["unknown"] = unknown
    # An explicit pre-panel stance still cannot replace the first physical call.
    h.annotate()
    assert not h.physical.calls
    h.run("Initial answer")
    assert h.state["calls"] == 1 and len(h.physical.calls) == 1
    first = copy.deepcopy(h.trace["review_runs"][-1])
    h.annotate()
    h.run("Verified revised answer")
    assert h.state["calls"] == 1 and len(h.physical.calls) == 1
    assert h.trace["review_runs"][-1]["actors"] == first["actors"]
    decision = h.trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    if unknown:
        assert not first.get("feedback_delivered")
        assert decision["reason"] == "review_degraded"
        assert not decision.get("author_disposition")
        actor = first["actors"][0]
        assert actor["operation_state"] == "custody_lost" and actor["late_result_pending"]
        assert "RPC outcome unknown" in actor["error"]
    else:
        assert decision["reason"] == "author_finish"
        assert decision["reviewer_binding_hash"] == first["binding_hash"]
        assert decision["author_disposition"]["subject_hash"] != first["binding_hash"]
