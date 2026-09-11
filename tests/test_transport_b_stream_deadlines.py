"""B5/B6 physical-send contracts. All providers are in-memory or loopback fakes."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from ouroboros import deadline_utils, model_wait, usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.llm_attempt import PhysicalDispatchInterrupted
from ouroboros.llm_stream import AssembledResponse, IncompleteProviderStream, ProviderStreamError


MESSAGES = [{"role": "user", "content": "preserve the complete answer"}]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {
    "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
    "additionalProperties": False}}}]


def target(provider="openrouter"):
    return {"provider": provider, "resolved_model": "vendor/test-stream", "usage_model": "vendor/test-stream",
            "base_url": "https://provider.invalid/v1", "supports_openrouter_extensions": provider == "openrouter",
            "supports_generation_cost": False, "api_key": "fixture"}


def payload(**extra):
    return {"model": "vendor/test-stream", "messages": copy.deepcopy(MESSAGES), "max_tokens": 1024,
            "reasoning_effort": "high", **extra}


def completion(**extra):
    return {"id": "gen-test", "object": "chat.completion", "model": "vendor/test-stream",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.25}, **extra}


def chunk(delta=None, finish=None, *, index=0, **extra):
    return {"id": "gen-test", "object": "chat.completion.chunk", "model": "vendor/test-stream",
            "choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}], **extra}


def sse(*events, done=True):
    wire = b""
    for event in events:
        if isinstance(event, tuple):
            kind, body = event
            wire += f"event: {kind}\r\n".encode()
        else:
            body = event
        wire += ("data: " + json.dumps(body, ensure_ascii=False) + "\r\n\r\n").encode()
    return wire + (b"data: [DONE]\r\n\r\n" if done else b"")


class WireResponse:
    status_code = 200
    reason = "OK"
    url = "https://provider.invalid/v1/messages"

    def __init__(self, wire, *, failure=None, step=None):
        self.wire, self.failure, self.step = wire, failure, step
        self.headers = {"x-generation-id": "header-generation"}
        self.closed = False

    def iter_bytes(self):
        # Fragment through UTF-8, CRLF and JSON boundaries.
        for offset in range(0, len(self.wire), 13):
            if self.step:
                self.step()
            yield self.wire[offset:offset + 13]
        if self.failure:
            raise self.failure

    def iter_content(self, **kwargs):
        return self.iter_bytes()

    async def aiter_bytes(self):
        for item in self.iter_bytes():
            yield item

    def close(self):
        self.closed = True

    async def aclose(self):
        self.close()


class Rejected(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"type": "invalid_request_error", "message": message}}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import ouroboros.request_wire_contract as wire
    import ouroboros.pricing as pricing

    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 1.0)
    monkeypatch.setattr(LLMClient, "_get_supported_parameters", lambda *a, **k: None)
    monkeypatch.setattr(LLMClient, "_fetch_generation_cost", lambda *a, **k: None)
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id="stream-task", root_task_id="stream-task")):
        yield tmp_path


def rows(root):
    path = root / ua.LEDGER_REL
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_driver(create, kwargs, route, *, asynchronous=False):
    client = LLMClient()
    if asynchronous:
        async def send(**values):
            return create(**values)
        return asyncio.run(client._create_chat_completion_with_retries_async(send, kwargs, route))
    return client._create_chat_completion_with_retries(create, kwargs, route)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_json_and_stream_parity_with_usage_tail(isolated, asynchronous):
    wire = sse(chunk({"role": "assistant", "content": "do"}), chunk({"content": "ne"}, "stop"),
               chunk({"content": "", "role": "assistant"}, "stop", usage=completion()["usage"]))
    response = WireResponse(wire, step=lambda: assert_unsettled(isolated))
    result = run_driver(lambda **k: SimpleNamespace(response=response), payload(stream=True), target(),
                        asynchronous=asynchronous).model_dump()
    receipt = result.pop("_stream_receipt")
    assert result == completion()
    client = LLMClient()
    assert client._normalize_remote_response(result, target(), skip_cost_fetch=True) == client._normalize_remote_response(
        completion(), target(), skip_cost_fetch=True)
    assert receipt["complete"] is True and receipt["manifest_ref"]
    assert response.closed
    assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "settled"]
    assert rows(isolated)[-1]["cost_usd"] == 0.25


def assert_unsettled(root):
    assert rows(root)[-1]["state"] == "dispatched"


def test_interleaved_choices_tools_reasoning_annotations_and_usage(isolated):
    first = chunk({"tool_calls": [{"index": 1, "id": "b", "type": "custom", "custom": {"name": "lookup", "input": '{"q":'}},
                                   {"index": 0, "id": "a", "type": "function", "function": {"name": "look", "arguments": '{"q":'}}],
                   "reasoning": "think ", "reasoning_content": "native ",
                   "reasoning_details": [{"index": 0, "type": "reasoning.encrypted", "data": "abc", "format": "opaque"}],
                   "annotations": [{"type": "url_citation", "url_citation": {"url": "https://example.test", "title": "cite"}}]},
                  usage={"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.1})
    second = chunk({"tool_calls": [{"index": 0, "function": {"name": "up", "arguments": '"a"}'}},
                                    {"index": 1, "custom": {"input": '"b"}'}}],
                    "reasoning": "again", "reasoning_content": "again",
                    "reasoning_details": [{"index": 0, "data": "def"}]}, "tool_calls")
    other = chunk({"content": "second", "refusal": "no"}, "stop", index=1)
    usage = {"prompt_tokens": 10, "completion_tokens": 12, "cost": 0.5,
             "prompt_tokens_details": {"cached_tokens": 8}, "completion_tokens_details": {"reasoning_tokens": 7},
             "server_tool_use": {"web_search_requests": 1}}
    tail = {"id": "gen-test", "choices": [], "usage": usage, "provider": "fake-serving", "service_tier": "default"}
    response = WireResponse(sse(first, other, second, tail))
    result = run_driver(lambda **k: response, payload(stream=True, n=2), target()).model_dump()
    msg = result["choices"][0]["message"]
    assert [call["id"] for call in msg["tool_calls"]] == ["a", "b"]
    assert msg["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"q":"a"}'}
    assert msg["tool_calls"][1]["custom"]["input"] == '{"q":"b"}'
    assert msg["reasoning_details"][0]["data"] == "abcdef"
    assert msg["reasoning"] == "think again" and msg["reasoning_content"] == "native again"
    assert msg["annotations"][0]["url_citation"]["title"] == "cite"
    assert result["choices"][1]["message"]["refusal"] == "no"
    assert result["usage"] == usage and result["provider"] == "fake-serving"
    assert rows(isolated)[-1]["cost_usd"] == 0.5


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["eof", "missing_finish", "error_first", "error_mid", "error_free_claim",
                                     "disconnect", "cancel", "malformed"])
def test_incomplete_stream_never_settles_or_replays(isolated, asynchronous, failure):
    wire = sse(chunk({"content": "partial", "tool_calls": [{"index": 0, "id": "t", "type": "function",
                           "function": {"name": "lookup", "arguments": '{"q":"ok"}'}}]}), done=False)
    error = {"error": {"code": "server_error", "message": "stream_options unsupported; upstream lost"}}
    if failure == "error_free_claim":
        error = {"error": {"code": 404, "message": "No endpoints found; Error code: 404"}}
    exception = None
    if failure == "missing_finish":
        wire += b"data: [DONE]\n\n"
    elif failure.startswith("error"):
        wire = (wire if failure == "error_mid" else b"") + sse(error, done=False)
    elif failure == "disconnect":
        exception = httpx.ReadError("read lost")
    elif failure == "cancel":
        exception = asyncio.CancelledError()
    elif failure == "malformed":
        wire += b"data: {broken}\n\n"
    response = WireResponse(wire, failure=exception)
    calls = []
    def send(**kw):
        calls.append(kw)
        return response
    with pytest.raises(BaseException) as caught:
        run_driver(send, payload(stream=True), target(), asynchronous=asynchronous)
    exc = caught.value
    assert len(calls) == 1 and response.closed
    from ouroboros.transport_custody import _capture_on_chain, transport_exception_cause
    assert _capture_on_chain(exc).state == "unresolved"
    while not hasattr(exc, "stream_receipt") and transport_exception_cause(exc) is not None:
        exc = transport_exception_cause(exc)
    assert exc.stream_receipt["complete"] is False
    assert exc.stream_receipt["generation_id"] == "header-generation"
    assert rows(isolated)[-1]["state"] == "unresolved"
    assert all(row["state"] != "settled" for row in rows(isolated))
    if failure.startswith("error"):
        assert isinstance(exc, ProviderStreamError) and exc.body["error"] == error["error"]
    from ouroboros.observability import read_blob_ref
    manifest = json.loads((isolated / exc.stream_receipt["manifest_ref"]["path"]).read_text())
    projection = read_blob_ref(isolated, manifest["full_payload_ref"])
    evidence = read_blob_ref(isolated, projection["private_wire_ref"])
    assert base64.b64decode(evidence["wire_base64"]) == wire
    assert evidence["attempt_id"] == exc.physical_attempt_capture.attempt_id


def test_complete_stream_without_usage_keeps_money_unknown(isolated):
    response = WireResponse(sse(chunk({"content": "done"}, "stop")))
    result = run_driver(lambda **k: response, payload(stream=True), target())
    assert result.model_dump()["choices"][0]["message"]["content"] == "done"
    assert rows(isolated)[-1]["cost_usd"] is None
    assert rows(isolated)[-1]["cost_final"] is False


def test_sse_bom_multiline_data_and_split_unicode(isolated):
    event = json.dumps(chunk({"content": "π\u2028done"}, "stop", usage=completion()["usage"]), ensure_ascii=False, indent=2)
    wire = b"\xef\xbb\xbf: comment\r\n\r\n" + b"".join(b"data: " + line.encode() + b"\r\n" for line in event.split("\n"))
    wire += b"\r\ndata: [DONE]\r\n\r\n"
    result = run_driver(lambda **kw: WireResponse(wire), payload(stream=True), target())
    assert result.model_dump()["choices"][0]["message"]["content"] == "π\u2028done"


@pytest.mark.parametrize("delta,finish", [
    ({"tool_calls": 1}, "stop"),
    ({"function_call": {"name": "lookup", "arguments": '{"q":'}}, "length"),
    ({"tool_calls": [{"index": 0, "id": "t", "type": "function", "function": {"name": "lookup"}}]}, "tool_calls"),
])
def test_terminal_does_not_complete_malformed_tool_fields(isolated, delta, finish):
    with pytest.raises(IncompleteProviderStream) as caught:
        run_driver(lambda **kw: WireResponse(sse(chunk(delta, finish))), payload(stream=True), target())
    assert caught.value.stream_receipt["manifest_ref"]
    assert rows(isolated)[-1]["state"] == "unresolved"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("field", ["stream", "stream_options"])
def test_stream_rejection_uses_existing_wire_recovery(isolated, asynchronous, field):
    calls = []
    def send(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise Rejected(f"Unsupported parameter: '{field}'")
        if kw.get("stream"):
            return WireResponse(sse(chunk({"content": "done"}, "stop", usage=completion()["usage"])))
        return AssembledResponse(completion())
    run_driver(send, payload(stream=True, stream_options={"include_usage": True}), target(), asynchronous=asynchronous)
    assert len(calls) == 2 and field not in calls[1]
    assert ("stream" in calls[1]) == (field == "stream_options")
    assert calls[1]["max_tokens"] == 1024 and calls[1]["reasoning_effort"] == "high"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_each_recovery_reads_current_deadline(isolated, monkeypatch, asynchronous):
    now = [100.0]
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    calls = []
    def send(**kw):
        calls.append(kw)
        now[0] += 3
        if len(calls) == 1:
            raise Rejected("temperature unsupported")
        if len(calls) == 2:
            raise Rejected("response_format unsupported")
        return AssembledResponse(completion())
    with model_wait.execution_deadline_scope(110):
        run_driver(send, payload(temperature=0.2, response_format={"type": "json_object"}, timeout=600),
                   target(), asynchronous=asynchronous)
    assert [call["timeout"] for call in calls] == [10, 7, 4]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_exhaustion_preserves_earlier_paid_capture(isolated, monkeypatch, asynchronous):
    now = [100.0]
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    calls = []
    def send(**kw):
        calls.append(kw)
        now[0] = 111
        raise Rejected("temperature unsupported")
    with model_wait.execution_deadline_scope(110), pytest.raises(PhysicalDispatchInterrupted) as caught:
        run_driver(send, payload(temperature=0.2), target(), asynchronous=asynchronous)
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure
    assert len(calls) == 1
    assert not is_pre_dispatch_transport_failure(caught.value)
    assert caught.value.physical_attempt_capture.state == "unresolved"
    assert caught.value.physical_attempt_capture.attempt_id == rows(isolated)[0]["attempt_id"]
    assert len(rows(isolated)) == 3


def test_expired_initial_window_reserves_nothing(isolated, monkeypatch):
    run_driver(lambda **kw: AssembledResponse(completion()), payload(), target())
    previous_rows = rows(isolated)
    assert ua.last_physical_attempt_capture() is not None
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: 100)
    with model_wait.execution_deadline_scope(100), pytest.raises(PhysicalDispatchInterrupted):
        run_driver(lambda **kw: pytest.fail("dispatched"), payload(), target())
    assert rows(isolated) == previous_rows
    assert ua.last_physical_attempt_capture() is None


def test_slow_candidate_preparation_rechecks_before_dispatch(isolated, monkeypatch):
    import ouroboros.observability as observability
    now = [100]
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    original = observability.persist_physical_candidate
    def persist(*a, **kw):
        result = original(*a, **kw)
        now[0] = 111
        return result
    monkeypatch.setattr(observability, "persist_physical_candidate", persist)
    with model_wait.execution_deadline_scope(110), pytest.raises(PhysicalDispatchInterrupted) as caught:
        run_driver(lambda **kw: pytest.fail("dispatched"), payload(), target())
    assert [row["state"] for row in rows(isolated)] == ["reserved", "released"]
    assert caught.value.physical_attempt_capture.candidate_manifest_ref


@pytest.mark.parametrize("asynchronous", [False, True])
def test_slow_recovery_preparation_keeps_both_attempt_receipts(isolated, monkeypatch, asynchronous):
    import ouroboros.observability as observability
    from ouroboros.transport_custody import is_pre_dispatch_transport_failure
    now, prepared, sent = [100], [], []
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    original = observability.persist_physical_candidate
    def persist(*a, **kw):
        result = original(*a, **kw)
        prepared.append(result)
        if len(prepared) == 2:
            now[0] = 111
        return result
    def send(**kw):
        sent.append(kw)
        raise Rejected("temperature unsupported")
    monkeypatch.setattr(observability, "persist_physical_candidate", persist)
    with model_wait.execution_deadline_scope(110), pytest.raises(PhysicalDispatchInterrupted) as caught:
        run_driver(send, payload(temperature=0.2), target(), asynchronous=asynchronous)
    assert len(sent) == 1
    assert caught.value.physical_attempt_capture.state == "unresolved"
    assert caught.value.deadline_attempt_capture.state == "released"
    assert caught.value.deadline_attempt_capture.candidate_manifest_ref
    assert not is_pre_dispatch_transport_failure(caught.value)
    assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "unresolved", "reserved", "released"]


def test_later_free_recovery_cannot_replace_earlier_unknown_custody(isolated, monkeypatch):
    now, sent = [100], []
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    def send(**kw):
        sent.append(kw)
        if len(sent) == 1:
            raise Rejected("temperature unsupported")
        now[0] = 111
        raise Rejected("No endpoints found; response_format unsupported", status=404)
    with model_wait.execution_deadline_scope(110), pytest.raises(PhysicalDispatchInterrupted) as caught:
        run_driver(send, payload(temperature=0.2, response_format={"type": "json_object"}), target())
    assert len(sent) == 2 and rows(isolated)[-1]["state"] == "settled"
    assert caught.value.physical_attempt_capture.attempt_id == rows(isolated)[0]["attempt_id"]
    assert caught.value.physical_attempt_capture.state == "unresolved"


def test_quota_pause_is_execution_only_and_network_wait_spends_time(isolated, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(model_wait.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deadline_utils, "utc_now", lambda: datetime.fromtimestamp(now[0], timezone.utc))
    owner = model_wait.TaskModelWait(task={"id": "clock"}, drive_root=isolated, event_queue=None,
                                    worker_slot_held=False, owner_control=lambda: None)
    token = model_wait._CURRENT.set(owner)
    try:
        with model_wait.execution_deadline_scope(110):
            owner.clocks[""].enter("quota", now[0])
            now[0] = 120
            assert model_wait.dispatch_deadline_remaining_sec() == 10
            with model_wait.calendar_scope(datetime.fromtimestamp(115, timezone.utc).isoformat()):
                assert model_wait.dispatch_deadline_remaining_sec() == 0
            owner.clocks[""].leave("quota", now[0])
            now[0] = 123
            assert model_wait.dispatch_deadline_remaining_sec() == 7
    finally:
        model_wait._CURRENT.reset(token)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_default_timeout_and_no_proxy_phases(isolated, monkeypatch, asynchronous):
    seen = []
    run_driver(lambda **kw: (seen.append(kw), AssembledResponse(completion()))[1], payload(), target(), asynchronous=asynchronous)
    assert "timeout" not in seen[0]
    timeout = LLMClient._no_proxy_timeout(120)
    assert timeout.connect == 30 and timeout.read == 120
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: 100)
    with model_wait.execution_deadline_scope(105):
        run_driver(lambda **kw: (seen.append(kw), AssembledResponse(completion()))[1], payload(timeout=timeout), target(), asynchronous=asynchronous)
    assert seen[-1]["timeout"].as_dict() == {"connect": 5, "read": 5, "write": 5, "pool": 5}


def native_events():
    blocks = [{"type": "thinking", "thinking": "reasoning", "signature": "private-signature"},
              {"type": "redacted_thinking", "data": "private-redacted"},
              {"type": "text", "text": "read this", "citations": [{"type": "char_location", "cited_text": "source"}]},
              {"type": "tool_use", "id": "native-tool", "name": "lookup", "input": {"q": "yes"}}]
    body = {"id": "msg-test", "type": "message", "role": "assistant", "model": "vendor/test-stream", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 10}}
    events = [("message_start", {"type": "message_start", "message": body})]
    for index, block in enumerate(blocks):
        start = copy.deepcopy(block)
        deltas = []
        if block["type"] == "thinking":
            start.update(thinking="", signature="")
            deltas = [{"type": "thinking_delta", "thinking": "reason"}, {"type": "thinking_delta", "thinking": "ing"},
                      {"type": "signature_delta", "signature": "private-"}, {"type": "signature_delta", "signature": "signature"}]
        elif block["type"] == "tool_use":
            start["input"] = {}
            deltas = [{"type": "input_json_delta", "partial_json": '{"q":'}, {"type": "input_json_delta", "partial_json": '"yes"}'}]
        elif block["type"] == "text":
            start.update(text="", citations=[])
            deltas = [{"type": "text_delta", "text": "read "}, {"type": "text_delta", "text": "this"},
                      {"type": "citations_delta", "citation": block["citations"][0]}]
        events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": start}))
        events.extend(("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}) for delta in deltas)
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    usage = {"output_tokens": 12, "cache_creation_input_tokens": 3,
             "cache_creation": {"ephemeral_1h_input_tokens": 3}, "cost": 0.4}
    events.extend([("ping", {"type": "ping"}),
                   ("message_delta", {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5, "cost": 0.2}}),
                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": usage}),
                   ("message_stop", {"type": "message_stop"})])
    return events, {**body, "content": blocks, "stop_reason": "tool_use", "usage": {**body["usage"], **usage}}


@pytest.mark.parametrize("no_proxy", [False, True])
def test_native_sse_private_receipt_and_json_parity(isolated, monkeypatch, no_proxy):
    import requests
    from ouroboros.anthropic_native_custody import anthropic_replay_scope, native_content_for_replay, public_custody_projection
    events, expected = native_events()
    sent = []
    response = WireResponse(sse(*events, done=False), step=lambda: assert_unsettled(isolated))
    def post(*a, **kw):
        sent.append(kw)
        return response
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", post)
    client = LLMClient()
    msg, usage = client._chat_anthropic(target("anthropic"), MESSAGES, TOOLS, "high", 1024, "auto",
                                      no_proxy=no_proxy, stream=True)
    json_msg, json_usage = client._normalize_anthropic_response(expected, target("anthropic"),
                                                             prompt_cache_ttl=usage.get("prompt_cache_ttl"))
    usage.pop("stream_receipt")
    usage.pop("request_wire", None)
    assert msg == json_msg and usage == json_usage
    with anthropic_replay_scope():
        assert native_content_for_replay(msg, target("anthropic"), ["native-tool"]) == expected["content"]
    assert "private-signature" not in json.dumps(public_custody_projection(msg))
    assert sent[0]["stream"] is True and sent[0]["json"]["stream"] is True
    assert response.closed and rows(isolated)[-1]["cost_usd"] == 0.4


@pytest.mark.parametrize("omit", ["message_stop", "content_block_stop"])
def test_native_incomplete_blocks_or_message_cannot_return_tools(isolated, monkeypatch, omit):
    import requests
    events, expected = native_events()
    events = [(kind, body) for kind, body in events if kind != omit]
    monkeypatch.setattr(requests, "post", lambda *a, **k: WireResponse(sse(*events, done=False)))
    with pytest.raises(IncompleteProviderStream):
        LLMClient()._chat_anthropic(target("anthropic"), MESSAGES, TOOLS, "high", 1024, "auto", stream=True)
    assert rows(isolated)[-1]["state"] == "unresolved"


@pytest.mark.parametrize("before_content", [False, True])
def test_native_http_200_sse_error_keeps_producer_facts_and_custody(isolated, monkeypatch, before_content):
    import requests
    events, _ = native_events()
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    response = WireResponse(sse(*(events[:3] if not before_content else []), ("error", error), done=False))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    with pytest.raises(ProviderStreamError) as caught:
        LLMClient()._chat_anthropic(target("anthropic"), MESSAGES, TOOLS, "high", 1024, "auto", stream=True)
    assert caught.value.body == error and caught.value.type == "overloaded_error"
    assert caught.value.stream_receipt["generation_id"] == "header-generation"
    assert response.closed and rows(isolated)[-1]["state"] == "unresolved"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("no_proxy", [False, True])
def test_public_remote_chat_carries_stream_deadline_and_custom_receipts(isolated, monkeypatch, asynchronous, no_proxy):
    client = LLMClient()
    calls = []
    wire = sse(chunk({"tool_calls": [{"index": 0, "id": "c", "type": "custom", "custom": {"name": "lookup", "input": '{"q":"yes"}'}}]},
                     "tool_calls", usage=completion()["usage"]))
    def send(**kw):
        calls.append(kw)
        return WireResponse(wire)
    async def async_send(**kw):
        return send(**kw)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=async_send if asynchronous else send)))
    class Closer:
        def close(self):
            pass
        async def aclose(self):
            pass
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: target("openai"))
    monkeypatch.setattr(client, "_get_remote_client", lambda t: sdk)
    monkeypatch.setattr(client, "_get_async_remote_client", lambda t: sdk)
    monkeypatch.setattr(client, "_make_no_proxy_client", lambda t, timeout: (sdk, Closer()))
    monkeypatch.setattr(client, "_make_no_proxy_async_client", lambda t, timeout: (sdk, Closer()))
    monkeypatch.setattr(deadline_utils, "utc_now", lambda: datetime.fromtimestamp(100, timezone.utc))
    kwargs = dict(messages=MESSAGES, model="openai::test", tools=TOOLS, stream=True, caller_deadline_ts=110,
                  reasoning_effort="high", max_tokens=1024, no_proxy=no_proxy)
    msg, usage = asyncio.run(client.chat_async(**kwargs)) if asynchronous else client.chat(**kwargs)
    assert msg["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"q":"yes"}'}
    assert usage["_request_wire_custom_receipts"][0].allows_execution
    assert calls[0]["stream"] is True and calls[0]["stream_options"] == {"include_usage": True}
    assert calls[0]["max_completion_tokens"] == 1024 and calls[0]["reasoning_effort"] == "high"
    timeout = calls[0]["timeout"]
    assert (timeout.read if isinstance(timeout, httpx.Timeout) else timeout) == 10


def test_late_complete_stream_settles_original_attempt(isolated, monkeypatch):
    now = [100]
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    def advance():
        now[0] = 200
    response = WireResponse(sse(chunk({"content": "done"}, "stop", usage=completion()["usage"])), step=advance)
    with model_wait.execution_deadline_scope(110):
        result = run_driver(lambda **kw: response, payload(stream=True), target())
    assert result.model_dump()["choices"][0]["message"]["content"] == "done"
    assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "settled"]


def test_cancelled_control_during_recovery_keeps_paid_custody(isolated, monkeypatch):
    reason = [None]
    owner = SimpleNamespace(control_reason=lambda: reason[0])
    monkeypatch.setattr(model_wait, "current_model_wait", lambda: owner)
    calls = []
    def send(**kw):
        calls.append(kw)
        reason[0] = "cancelled"
        raise Rejected("temperature unsupported")
    with pytest.raises(PhysicalDispatchInterrupted) as caught:
        run_driver(send, payload(temperature=0.2), target())
    assert len(calls) == 1 and caught.value.control_reason == "cancelled"
    assert caught.value.physical_attempt_capture.state == "unresolved"


@pytest.mark.parametrize("no_proxy", [False, True])
def test_native_recovery_rereads_execution_deadline(isolated, monkeypatch, no_proxy):
    import requests
    now = [100]
    monkeypatch.setattr(model_wait, "monotonic_now", lambda slot=None: now[0])
    seen = []
    def post(*a, **kw):
        seen.append(kw)
        now[0] += 3
        if len(seen) == 1:
            raise Rejected("temperature unsupported")
        return SimpleNamespace(status_code=200, json=lambda: native_events()[1])
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", post)
    with model_wait.execution_deadline_scope(110):
        LLMClient()._chat_anthropic(target("anthropic"), MESSAGES, TOOLS, "high", 1024, "auto",
                                   temperature=0.2, no_proxy=no_proxy)
    assert [kw["timeout"] for kw in seen] == [10, 7]
    assert seen[0]["json"]["max_tokens"] == seen[1]["json"]["max_tokens"] == 1024


@pytest.mark.parametrize("asynchronous", [False, True])
def test_local_and_model_operation_transports_receive_no_stream_option(isolated, monkeypatch, asynchronous):
    import ouroboros.llm_claudexor as operations
    client = LLMClient()
    seen = []
    def local(messages, tools, max_tokens, choice, *, timeout=None):
        seen.append({"timeout": timeout})
        return {"content": "local"}, {}
    def operation(*args, **kwargs):
        seen.append(kwargs)
        return {"content": "operation"}, {}
    async def async_operation(*args, **kwargs):
        return operation(*args, **kwargs)
    monkeypatch.setattr(client, "_chat_local", local)
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: target("claudexor"))
    monkeypatch.setattr(operations, "chat_claudexor", operation)
    monkeypatch.setattr(operations, "chat_claudexor_async", async_operation)
    for use_local in (True, False):
        kwargs = dict(messages=MESSAGES, model="claudexor::fixture::model", stream=True, use_local=use_local)
        result = asyncio.run(client.chat_async(**kwargs)) if asynchronous else client.chat(**kwargs)
        assert result[0]["content"] == ("local" if use_local else "operation")
    assert all("stream" not in kwargs and "stream_options" not in kwargs for kwargs in seen)


@pytest.mark.serial
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("no_proxy", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_actual_sdk_loopback_sse_and_cleanup(isolated, monkeypatch, asynchronous, no_proxy, terminal):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    observed = []
    wire = b": fixture comment\n\n" + sse(chunk({"content": "done"}, "stop"),
                                          {"id": "gen-test", "choices": [], "usage": completion()["usage"]}, done=terminal)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Generation-Id", "loopback-generation")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
            self.wfile.flush()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LLMClient()
    route = {**target("openai-compatible"), "base_url": f"http://127.0.0.1:{server.server_port}/v1"}
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: route)
    try:
        kwargs = dict(messages=MESSAGES, model="openai-compatible::fixture", stream=True, no_proxy=no_proxy, timeout=5)
        if asynchronous:
            async def call_and_close():
                try:
                    return await client.chat_async(**kwargs)
                finally:
                    for sdk in client._async_remote_clients.values():
                        await sdk.close()
            operation = lambda: asyncio.run(call_and_close())
        else:
            operation = lambda: client.chat(**kwargs)
        if terminal:
            msg, usage = operation()
            assert msg["content"] == "done"
            assert usage["stream_receipt"]["generation_id"] == "loopback-generation"
        else:
            with pytest.raises(IncompleteProviderStream) as caught:
                operation()
            assert caught.value.stream_receipt["generation_id"] == "loopback-generation"
        assert observed[0]["stream"] is True and observed[0]["stream_options"]["include_usage"] is True
        assert [row["state"] for row in rows(isolated)] == ["reserved", "dispatched", "settled" if terminal else "unresolved"]
    finally:
        for sdk in client._remote_clients.values():
            sdk.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.serial
def test_native_async_cancellation_keeps_worker_until_physical_terminal(isolated, monkeypatch):
    import threading
    import requests
    entered, release = threading.Event(), threading.Event()
    events, expected = native_events()
    response = WireResponse(sse(*events, done=False))
    original_iter = response.iter_content
    def chunks(**kwargs):
        entered.set()
        assert release.wait(5)
        yield from original_iter(**kwargs)
    response.iter_content = chunks
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    client = LLMClient()
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: target("anthropic"))
    async def call():
        pending = asyncio.create_task(client.chat_async(messages=MESSAGES, model="anthropic::fixture", stream=True))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert_unsettled(isolated)
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            from ouroboros.transport_custody import _capture_on_chain
            assert _capture_on_chain(caught.value).state == "settled"
        finally:
            release.set()
    asyncio.run(call())
    assert response.closed and rows(isolated)[-1]["state"] == "settled"
