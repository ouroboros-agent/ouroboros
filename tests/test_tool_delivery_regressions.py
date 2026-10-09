"""A source address and a chosen view survive every batch delivery policy."""
import json

from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.context_fit import estimate_context_prompt_tokens
from ouroboros.tool_result_delivery import project_tool_result_batch


def test_consolidator_receives_the_source_in_its_actual_message_text(tmp_path):
    original = "Retained source. " * 1000
    rows, receipt = project_tool_result_batch(
        [{"tool_call_id": "read", "result": original}], [], [], drive_root=tmp_path,
        task_id="t", fit_candidate=lambda messages, tools: {
            "accepted": len(json.dumps(messages)) < 2400})
    assert receipt["status"] == "projected"
    # The consolidator forwards only row['result'], not the row's host metadata.
    shown = rows[0]["result"]
    marker = json.loads(shown.split("\n[Tool result source view]\n", 1)[1])
    assert marker["source_status"] == "ready"
    assert read_actor_source_bytes(tmp_path, "t", marker["source_ref"]).decode() == original


def test_unknown_window_keeps_an_explicit_head_tail_request(tmp_path):
    original = "HEAD " + "middle " * 5000 + " TAIL"
    rows, receipt = project_tool_result_batch(
        [{"tool_call_id": "run", "result": original, "fn_name": "run_command",
          "tool_args": {"view_head_chars": 20, "view_tail_chars": 20}}], [], [],
        drive_root=tmp_path, task_id="t", policy="measured_frame",
        fit_candidate=lambda messages, tools: {"accepted": True, "capacity_total_tokens": None})
    assert receipt["status"] == "capacity_unknown"
    assert rows[0]["result_partial"]
    assert rows[0]["result_source_view"]["shown_ranges"] == [[0, 20], [len(original) - 20, len(original)]]
    assert read_actor_source_bytes(tmp_path, "t", rows[0]["result_source_ref"]).decode() == original


def test_measured_eighth_holds_with_different_json_escape_densities(tmp_path):
    def fit(messages, tools):
        return {"accepted": True, "estimated_input_tokens": estimate_context_prompt_tokens(messages, tools),
                "capacity_total_tokens": 18000, "response_reserve_tokens": 1024}

    rows, receipt = project_tool_result_batch(
        [{"tool_call_id": "plain", "result": "a" * 100000},
         {"tool_call_id": "escaped", "result": '"\n' * 6000}], [], [],
        drive_root=tmp_path, task_id="t", policy="measured_frame", fit_candidate=fit)
    assert receipt["status"] == "projected"
    assert all(row["result_partial"] for row in rows)
    assert 0 < receipt["unsolicited_body_tokens"] <= receipt["unsolicited_allowance_tokens"]
    assert receipt["fit"]["estimated_input_tokens"] + receipt["reserve_tokens"] <= 18000


def test_producer_footer_and_notes_are_in_the_measured_candidate(tmp_path):
    from ouroboros.tools.tool_result import ToolResult
    from tests.test_tool_result_delivery import _ctx, _deliver, _row

    measured_texts = []

    def fit(messages, tools):
        measured_texts.append(messages[-1]["content"])
        return {"accepted": True, "estimated_input_tokens": len(json.dumps(messages)),
                "capacity_total_tokens": 14000, "response_reserve_tokens": 1000}

    body = "Source body " * 10000
    note = "Recorded host warning: " + "w" * 2000
    typed = ToolResult(status="error", code="TOOL_REPORTED_FAILURE", text=body + note,
                       producer_text=body, host_annotations=(note,))
    messages, trace = _deliver(_ctx(tmp_path), [_row("one", "run_command", typed.text,
        is_error=True, typed=typed)], fit)
    delivered = messages[-1]["content"]
    assert delivered in measured_texts
    assert note in delivered and "PRODUCER_RESULT_SOURCE_JSON=" in delivered
    assert trace["tool_result_delivery"][-1]["status"] == "projected"
    assert len(json.dumps(messages)) + 1000 <= 14000
