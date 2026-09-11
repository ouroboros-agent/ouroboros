"""Subscription model calls retain ordinary LLM, physical ledger and result custody."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from ouroboros import llm_claudexor as transport
from ouroboros import usage_accounting as ua
from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable
from ouroboros.llm import LLMClient
from ouroboros.model_slots import MODEL_ACCOUNTS_KEY
from ouroboros.transport_custody import is_pre_dispatch_transport_failure, is_retryable_transport_death


MODEL = "claudexor::codex=exact-model"
ROUTE = {"source": "codex", "credentialProfileId": "account-a", "accountFingerprint": "fingerprint-a", "model": "exact-model"}
REF = {"resourceId": "res-one", "sha256": "sha256:" + "a" * 64, "sizeBytes": 99}


@pytest.mark.parametrize("unknown", [False, True])
def test_display_diagnostics_preserve_exception_and_private_problem(unknown):
    problem = {"code": "invalid_request", "message": "Codex model request was refused (HTTP 400).",
               "retryable": True, "context": {"httpStatus": 400, "resetsAt": "2099-01-01T00:00:00Z",
                   "vendorCode": "string_above_max_length", "parameter": "instructions",
                   "providerMessage": "private provider body", "requestId": "private-request"}}
    original = deepcopy(problem)
    error = transport.ClaudexorModelError(problem, model_role="main", operation_id="op-1", route=ROUTE, unknown=unknown)
    code = "model_outcome_unknown" if unknown else "invalid_request"
    generic = f"{code}: {problem['message']}"
    assert error.args == (generic,) and str(error) == generic
    assert repr(error) == f"ClaudexorModelError({generic!r})"
    assert error.code == code and error.body == ({"code": code} if unknown else original)
    assert error.status_code == (0 if unknown else 400) and error.retryable is (not unknown)
    assert error.reset_at == original["context"]["resetsAt"]
    assert error.model_role == "main" and error.operation_id == "op-1" and error.route == ROUTE
    display = error.display_message
    assert ("provider_code=string_above_max_length, parameter=instructions" in display[:220]) is (not unknown)
    assert "private provider body" not in display and "private-request" not in display
    if unknown:
        assert display == generic
    problem["context"]["parameter"] = "changed after construction"
    assert error.problem == original and error.display_message == display


@pytest.mark.parametrize("context", [{}, {"vendorCode": None, "parameter": 42},
                                     {"vendorCode": "  ", "parameter": ["private provider data"]}])
def test_display_ignores_absent_or_nontext_details(context):
    error = transport.ClaudexorModelError({"code": "invalid_request", "message": "Controlled refusal", "context": context})
    assert error.display_message == str(error)


def test_display_redacts_typed_details_without_mutating_custody():
    token = "sk-" + "secretfixture" * 4
    context = {"vendorCode": token, "parameter": "https://user:private-password@example.test/input"}
    error = transport.ClaudexorModelError({"code": "invalid_request", "message": "Controlled refusal", "context": context})
    assert token not in error.display_message and "private-password" not in error.display_message
    assert "REDACTED" in error.display_message and error.problem["context"] == context


@pytest.mark.parametrize("code,status,unknown,kind,retry,wait", [
    ("invalid_request", 400, False, "bad_request", False, ""),
    ("auth_required", 401, False, "auth_error", False, "auth"),
    ("subscription_window_exhausted", 429, False, "subscription_window_exhausted", True, "quota"),
    ("invalid_request", 400, True, "provider_outcome_unknown", False, ""),
])
@pytest.mark.parametrize("vendor,parameter", [("string_above_max_length", "instructions"),
    ("string_above_max_length", "max_tokens"), ("rate_limit_exceeded", "input"), ("context_length_exceeded", "input")])
def test_display_never_changes_classification_wait_or_compaction(code, status, unknown, kind, retry, wait, vendor, parameter):
    from ouroboros.context_compaction import _typed_context_overflow
    from ouroboros.loop_llm_call import classify_llm_exception
    from ouroboros.model_wait import model_wait_reason

    error = transport.ClaudexorModelError({"code": code, "message": "Controlled model refusal", "retryable": True,
        "context": {"httpStatus": status, "vendorCode": vendor, "parameter": parameter}}, unknown=unknown)
    assert error.display_message  # Computing a human view must not mutate behavioral readers.
    classified = classify_llm_exception(error)
    assert classified.kind == kind and classified.retry_same_request is retry
    assert model_wait_reason(error) == wait
    assert not _typed_context_overflow(error)
    typed = transport.ClaudexorModelError({"code": "context_length_exceeded", "message": "Controlled refusal"})
    assert _typed_context_overflow(typed) and classify_llm_exception(typed).kind == "context_overflow"


def result(*, outcome="completed", cash=None, knowledge="unknown", route=None, problem=None):
    route = dict(ROUTE if route is None else route)
    return {"outcome": outcome, "message": {"role": "assistant", "content": "Ответ 🐍", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "read", "arguments": '{ "path": "a" }'}},
        {"id": "b", "type": "function", "function": {"name": "read", "arguments": '{"path":"b"}'}},
    ], "nativeContinuation": {"route": route, "format": "codex.responses.v1", "payload": [
        {"type": "reasoning", "encrypted_content": "opaque+==\r\n", "_context_capsule": "not host metadata"},
    ]}}, "route": route, "usage": {"input_tokens": 20, "output_tokens": 7, "cached_input_tokens": 12,
                                    "cache_write_tokens": None, "reasoning_tokens": 3},
        "cost": {"knowledge": knowledge, "cashUsd": cash, "estimatedUsd": None, "valuationUsd": 12.5,
                 "valuationKnowledge": "exact", "billing": "unknown", "source": "provider", "provenance": ["fixture"]},
        "appliedOptions": {"reasoningEffort": "high"}, "problem": problem}


class Gateway:
    def __init__(self, results=None, dispatch=None):
        self.results = results or [result()]
        self.dispatch = dispatch or ["response_received"] * len(self.results)
        self.uploads = []
        self.operations = {}
        self.creates = []
        self.reads = []
        self.acks = []
        self.cancels = []
        self.closed = 0
        self.lose_create = False
        self.ack_error = None
        self.pending = False
        self.read_error = False
        self.raw_result = None

    def upload_model_request(self, payload, *, idempotency_key):
        self.uploads.append((deepcopy(payload), idempotency_key))
        return REF

    def create_model_operation(self, ref, *, idempotency_key):
        assert ref == REF
        self.creates.append(idempotency_key)
        if idempotency_key not in self.operations:
            self.operations[idempotency_key] = len(self.operations)
        index = self.operations[idempotency_key]
        if self.lose_create:
            self.lose_create = False
            raise ClaudexorUnavailable("daemon_unreachable", "lost create reply")
        return self.detail(index)

    def get_model_operation(self, operation_id, *, timeout_sec=None):
        self.reads.append(operation_id)
        if self.read_error:
            raise ClaudexorUnavailable("daemon_unreachable", "control failed") from httpx.ConnectError("local dial failed")
        return self.detail(int(operation_id.removeprefix("op-")))

    def detail(self, index):
        value = self.results[index]
        return {"id": f"op-{index}", "state": "running" if self.pending else "succeeded" if value["outcome"] == "completed" else "failed",
                "dispatch": {"state": "started" if self.pending else self.dispatch[index], "route": value["route"]},
                "response": {"state": "absent"} if self.pending else {"state": "ready", "ref": REF},
                "problem": value["problem"]}

    def get_model_result(self, operation_id, *, expected_ref, timeout_sec=None, raw_bytes=False):
        assert expected_ref == REF
        value = deepcopy(self.results[int(operation_id.removeprefix("op-"))])
        if raw_bytes:
            return self.raw_result or json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value

    def acknowledge_model_result(self, operation_id, sha256):
        self.acks.append((operation_id, sha256))
        if self.ack_error:
            raise self.ack_error
        return {"id": operation_id, "response": {"state": "acknowledged", "ref": REF}}

    def cancel_model_operation(self, operation_id, *, reason_code):
        self.cancels.append((operation_id, reason_code))
        return {"id": operation_id, "state": "running"}

    def close(self):
        self.closed += 1


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    monkeypatch.delenv(MODEL_ACCOUNTS_KEY, raising=False)
    monkeypatch.setattr(transport.config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.001)
    from ouroboros import pricing
    monkeypatch.setattr(pricing, "_fetch_live_rows", lambda *_: pytest.fail("Claudexor must not fetch API pricing"))
    gateway = Gateway()
    monkeypatch.setattr(transport, "ensure_owned_gateway", lambda: gateway)
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id="task-one", root_task_id="task-one")):
        yield root, gateway, LLMClient()


def ledger(root):
    path = root / ua.LEDGER_REL
    return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


def retained(root, suffix="response", *, raw=False):
    files = list((root / "observability" / "calls" / "task-one").glob(f"*_model_{suffix}.json"))
    assert files
    manifest = json.loads(files[-1].read_text())
    assert manifest["full_payload_redacted"] is False
    ref = manifest["full_payload_ref"]
    with gzip.open(ref["path"], "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if suffix != "response":
        return payload
    return payload["result_json_utf8"].encode("utf-8") if raw else json.loads(payload["result_json_utf8"])


def test_sync_preserves_system_images_tool_cycle_and_native_custody(setup, monkeypatch):
    root, gateway, client = setup
    initial = [{"role": "system", "content": "Own SYSTEM\r\nBIBLE 🐍"}, {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "read"}}
    monkeypatch.setenv(MODEL_ACCOUNTS_KEY, json.dumps({"main": "account-a"}))
    original = deepcopy(initial)
    answer, usage = client.chat(initial, MODEL, tools, tool_choice=choice, model_role="main", max_tokens=65432)
    assert initial == original
    payload = gateway.uploads[0][0]
    assert payload["messages"] == original and payload["tools"] == tools and payload["toolChoice"] == choice
    assert payload["account"] == {"mode": "pin", "profileId": "account-a"}
    assert "maxOutputTokens" not in payload["options"]
    assert answer == result()["message"]
    assert usage["claudexor"]["output_reserve_tokens"] == 65432 and usage["claudexor"]["output_cap_applied"] is False
    assert usage["cached_tokens"] == 12 and usage["prompt_tokens"] == 20
    assert usage["cost"] is None and usage["cost_final"] is False
    assert usage["claudexor"]["cost_evidence"]["valuationUsd"] == 12.5
    assert retained(root) == result()
    assert retained(root, "request") == payload
    followup = initial + [answer, {"role": "tool", "tool_call_id": "a", "content": "first"},
                          {"role": "tool", "tool_call_id": "b", "content": "second"}]
    gateway.results.append(result())
    gateway.dispatch.append("response_received")
    client.chat(followup, MODEL, tools)
    assert gateway.uploads[-1][0]["messages"] == followup
    assert gateway.uploads[-1][0]["account"] == {"mode": "auto", "preferredProfileId": "account-a"}
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "settled"] * 2
    assert len(usage["ledger_attempt_ids"]) == 1


@pytest.mark.parametrize("cash,knowledge,expected,final", [(0, "exact", 0, True), (0.25, "exact", 0.25, True),
                                                        (0.4, "estimated", 0.4, False), (0, "unknown", None, False)])
def test_cash_is_independent_from_valuation_and_credentials(setup, cash, knowledge, expected, final):
    root, gateway, client = setup
    gateway.results = [result(cash=cash, knowledge=knowledge)]
    _, usage = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert usage["cost"] == expected and usage["cost_final"] is final
    row = ledger(root)[-1]
    assert row["cost_usd"] == expected and row["cost_final"] is final


def test_same_model_roles_bind_separate_accounts_and_unlabelled_is_auto(setup, monkeypatch):
    _, gateway, client = setup
    gateway.results *= 4
    gateway.dispatch *= 4
    monkeypatch.setenv(MODEL_ACCOUNTS_KEY, json.dumps({"main": "first", "light": "second", "fallback": ["third"]}))
    for role in ("main", "light", "fallback:0", ""):
        client.chat([{"role": "user", "content": "hi"}], MODEL, model_role=role)
    assert [payload["account"] for payload, _ in gateway.uploads] == [
        {"mode": "pin", "profileId": "first"}, {"mode": "pin", "profileId": "second"},
        {"mode": "pin", "profileId": "third"}, {"mode": "auto"},
    ]


@pytest.mark.parametrize("method", ["chat", "chat_async", "vision_query", "remote"])
@pytest.mark.parametrize("override", [None, "", "account-b"])
def test_explicit_account_override_keeps_role_and_auto_semantics(setup, monkeypatch, method, override):
    _, gateway, client = setup
    monkeypatch.setenv(MODEL_ACCOUNTS_KEY, json.dumps({"main": "main-pin", "vision": "vision-pin"}))
    control = lambda: None
    options = {"model_poll_control": control, "model_account_override": override}
    if method == "vision_query":
        text, usage = client.vision_query("describe", [{"url": "data:image/png;base64,AAAA"}], model=MODEL, **options)
        assert text == result()["message"]["content"]
    elif method == "remote":
        _, usage = client._chat_remote(client._resolve_remote_target(MODEL), [], None, "medium", 10,
                                       "auto", None, model_role="main", **options)
    else:
        value = getattr(client, method)([{"role": "user", "content": "hi"}], MODEL, model_role="main", **options)
        _, usage = asyncio.run(value) if method == "chat_async" else value
    role = "vision" if method == "vision_query" else "main"
    expected = f"{role}-pin" if override is None else override
    assert gateway.uploads[0][0]["account"] == ({"mode": "pin", "profileId": expected} if expected else {"mode": "auto"})
    assert usage["claudexor"]["model_role"] == role


def test_lost_create_reply_rejoins_without_second_physical_attempt(setup):
    root, gateway, client = setup
    gateway.lose_create = True
    answer, usage = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert answer["content"] == "Ответ 🐍"
    assert len(gateway.creates) == 2 and gateway.creates[0] == gateway.creates[1]
    assert len(gateway.operations) == 1 and len(usage["ledger_attempt_ids"]) == 1
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "settled"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("content", [None, "Original answer\r\n", [{"type": "text", "text": "Original block 🐍"}]])
def test_direct_provider_history_keeps_refusal_text_and_omits_stop_metadata(setup, content, asynchronous):
    _, gateway, client = setup
    refusal = "Cannot comply with this request. 🐍\r\n"
    messages = [{"role": "assistant", "content": content, "refusal": refusal, "stop_reason": "end_turn"}]
    original = deepcopy(messages)
    if asynchronous:
        asyncio.run(client.chat_async(messages, MODEL))
    else:
        client.chat(messages, MODEL)
    sent = gateway.uploads[0][0]["messages"][0]
    assert set(sent) == {"role", "content"}
    expected = (refusal if content is None else
                [*([{"type": "text", "text": content}] if isinstance(content, str) else content),
                 {"type": "text", "text": refusal}])
    assert sent["content"] == expected
    assert messages == original


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("phase,code,status,not_sent", [
    ("create", "model_request_invalid", 400, True),
    ("get", "model_request_invalid", 400, False),
    ("create", "http_400", 400, False),
    ("create", "auth_required", 401, False),
    ("get", "subscription_window_exhausted", 429, False),
])
def test_only_exact_pre_admission_create_refusal_releases_the_attempt(setup, monkeypatch, phase, code, status, not_sent, asynchronous):
    root, gateway, client = setup
    calls = []

    def refuse(*_args, **_kwargs):
        calls.append(phase)
        raise ClaudexorUnavailable(code, "Controlled control-plane refusal", status_code=status)

    if phase == "get":
        gateway.pending = True
        monkeypatch.setattr(gateway, "get_model_operation", refuse)
    else:
        monkeypatch.setattr(gateway, "create_model_operation", refuse)
    with pytest.raises(transport.ClaudexorModelError) as caught:
        if asynchronous:
            asyncio.run(client.chat_async([], MODEL))
        else:
            client.chat([], MODEL)
    assert isinstance(caught.value, transport.ClaudexorModelNotDispatched) is not_sent
    assert caught.value.code == (code if not_sent else "model_outcome_unknown")
    assert ledger(root)[-1]["state"] == ("released" if not_sent else "unresolved")
    assert calls == [phase]


@pytest.mark.parametrize("code", ["rate_limited", "provider_failed", "invalid_request", "catalog_unavailable"])
def test_confirmed_ordinary_model_failure_keeps_helper_fallback_policy(code):
    error = transport.ClaudexorModelError({"code": code, "message": "Controlled ordinary refusal"})
    for state in ("released", "settled"):
        error.physical_attempt_capture = SimpleNamespace(state=state)
        assert transport.propagate_model_error(error) is None
    error.physical_attempt_capture = SimpleNamespace(state="unresolved")
    with pytest.raises(transport.ClaudexorModelError) as caught:
        transport.propagate_model_error(error)
    assert caught.value is error


@pytest.mark.parametrize("code", ["auth_required", "subscription_window_exhausted", "model_outcome_unknown", "model_operation_interrupted"])
def test_helper_fallback_never_swallows_resource_control_or_unknown(code):
    error = transport.ClaudexorModelError({"code": code, "message": "Controlled retained outcome"})
    with pytest.raises(transport.ClaudexorModelError) as caught:
        transport.propagate_model_error(error)
    assert caught.value is error


def test_control_connect_failure_after_acceptance_stays_unknown(setup):
    root, gateway, client = setup
    gateway.pending = gateway.read_error = True
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL, timeout=0.01)
    error = raised.value
    assert error.code == "model_outcome_unknown" and error.operation_id == "op-0"
    assert not is_pre_dispatch_transport_failure(error) and not is_retryable_transport_death(error)
    assert ledger(root)[-1]["state"] == "unresolved" and len(gateway.operations) == 1
    assert not gateway.acks


def test_proven_never_started_quota_attempts_do_not_spend_generation_limit(setup):
    root, gateway, client = setup
    refused = result(outcome="failed", problem={"code": "credential_pool_exhausted", "message": "quota"})
    gateway.results, gateway.dispatch = [refused] * 3 + [result()], ["not_started"] * 3 + ["response_received"]
    with ua.physical_attempt_limit(1):
        for _ in range(3):
            with pytest.raises(transport.ClaudexorModelNotDispatched):
                client.chat([], MODEL)
        client.chat([], MODEL)
        with pytest.raises(ua.PhysicalAttemptLimitExceeded):
            client.chat([], MODEL)
    assert len(gateway.operations) == 4
    assert [r['state'] for r in ledger(root)].count('settled') == 1


def test_unknown_outcome_keeps_its_generation_limit_claim(setup):
    _, gateway, client = setup
    gateway.results, gateway.dispatch = [result(outcome="unknown")], ["unknown"]
    with ua.physical_attempt_limit(1):
        with pytest.raises(transport.ClaudexorModelError):
            client.chat([], MODEL)
        with pytest.raises(ua.PhysicalAttemptLimitExceeded):
            client.chat([], MODEL)
    assert len(gateway.operations) == 1


def test_confirmed_provider_failure_settles_real_usage_before_raising(setup):
    root, gateway, client = setup
    gateway.results = [result(outcome="failed", cash=0.13, knowledge="exact", problem={
        "code": "subscription_window_exhausted", "message": "window exhausted", "retryable": True,
        "context": {"resetsAt": "2099-01-01T00:00:00Z", "httpStatus": 429},
    })]
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL, model_role="vision")
    error = raised.value
    assert error.code == "subscription_window_exhausted" and error.model_role == "vision"
    assert error.reset_at == "2099-01-01T00:00:00Z"
    assert error.physical_attempt_capture.state == "settled"
    assert ledger(root)[-1]["cost_usd"] == 0.13 and ledger(root)[-1]["prompt_tokens"] == 20
    assert retained(root) == gateway.results[0]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_field_refusal_retains_then_acknowledges_once_with_display(setup, asynchronous):
    root, gateway, client = setup
    problem = {"code": "invalid_request", "message": "Codex model request was refused (HTTP 400).", "retryable": False,
               "context": {"httpStatus": 400, "vendorCode": "string_above_max_length", "parameter": "instructions"}}
    gateway.results = [result(outcome="failed", problem=problem)]
    acknowledge = gateway.acknowledge_model_result

    def after_retention(*args):
        assert retained(root) == gateway.results[0]
        return acknowledge(*args)

    gateway.acknowledge_model_result = after_retention
    with pytest.raises(transport.ClaudexorModelError) as caught:
        if asynchronous:
            asyncio.run(client.chat_async([], MODEL, model_role="main"))
        else:
            client.chat([], MODEL, model_role="main")
    error = caught.value
    assert error.problem == problem and error.body == problem and error.code == "invalid_request"
    assert error.status_code == 400 and error.retryable is False
    assert error.operation_id == "op-0" and error.model_role == "main" and error.route == ROUTE
    assert "provider_code=string_above_max_length, parameter=instructions" in error.display_message[:220]
    assert error.physical_attempt_capture.state == "settled"
    assert error.usage["claudexor"]["result_custody"]["state"] == "acknowledged"
    assert len(gateway.operations) == len(gateway.creates) == len(gateway.acks) == 1
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "settled"]


def test_proven_not_started_releases_and_never_fabricates_provider_usage(setup):
    root, gateway, client = setup
    gateway.results = [result(outcome="failed", problem={"code": "unsupported_parameter", "message": "temperature unsupported"})]
    gateway.dispatch = ["not_started"]
    with pytest.raises(transport.ClaudexorModelNotDispatched) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL, temperature=0.2)
    assert raised.value.physical_attempt_capture.state == "released"
    assert gateway.uploads[0][0]["options"]["temperature"] == 0.2
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "released"]
    assert len(gateway.operations) == 1


@pytest.mark.parametrize("change", [{"credentialProfileId": "account-b", "accountFingerprint": "fingerprint-b"},
                                     {"model": None}, {"source": "different-source"}])
def test_native_reset_requires_actual_account_change_and_keeps_canonical_tools(setup, change):
    root, gateway, client = setup
    old = result()["message"]
    messages = [old, {"role": "tool", "tool_call_id": "a", "content": "result-a"},
                {"role": "tool", "tool_call_id": "b", "content": "result-b"}]
    original = deepcopy(messages)
    changed = {**ROUTE, **change}
    gateway.results = [result(outcome="failed", route=changed, problem={"code": "invalid_continuation", "message": "different account"}), result(route=changed)]
    gateway.dispatch = ["not_started", "response_received"]
    if not set(change) & {"credentialProfileId", "accountFingerprint"}:
        with pytest.raises(transport.ClaudexorModelNotDispatched):
            client.chat(messages, MODEL)
        assert len(gateway.operations) == 1
    else:
        _, usage = client.chat(messages, MODEL)
        assert len(usage["ledger_attempt_ids"]) == 2
        assert gateway.creates[0] != gateway.creates[1]
        resent = gateway.uploads[1][0]["messages"]
        assert "nativeContinuation" not in resent[0]
        assert resent[0]["tool_calls"] == original[0]["tool_calls"] and resent[1:] == original[1:]
        assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "released", "reserved", "dispatched", "settled"]
        event = json.loads((root / "logs/events.jsonl").read_text().splitlines()[-1])
        assert event["type"] == "native_continuation_reset" and "payload" not in json.dumps(event)
    assert messages == original


@pytest.mark.parametrize("error", [ClaudexorUnavailable("daemon_unreachable", "ACK reply lost"), RuntimeError("ACK parser failed")])
def test_ack_failure_preserves_paid_result_and_does_not_repeat(setup, error):
    root, gateway, client = setup
    gateway.ack_error = error
    answer, usage = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert answer == result()["message"] and retained(root) == result()
    assert usage["claudexor"]["result_custody"]["state"] == "pending"
    assert len(gateway.operations) == 1 and ledger(root)[-1]["state"] == "settled"


def test_failed_local_result_retention_withholds_ack_but_keeps_answer(setup, monkeypatch):
    _, gateway, client = setup
    persist = transport.persist_call

    def failing(*args, **kwargs):
        if kwargs["call_type"] == "llm_claudexor_response":
            raise OSError("disk unavailable")
        return persist(*args, **kwargs)

    monkeypatch.setattr(transport, "persist_call", failing)
    answer, usage = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert answer == result()["message"] and not gateway.acks
    assert usage["claudexor"]["result_custody"]["reason"] == "result_retention_failed:OSError"


@pytest.mark.parametrize("option", [{"response_format": {"type": "json_object"}}, {"response_format": {}},
                                     {"allow_server_web_search": True}, {"bypass_response_cache": True}])
def test_unsupported_request_intent_is_not_silently_ignored(setup, option):
    root, gateway, client = setup
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL, **option)
    assert raised.value.code == "unsupported_parameter" and not gateway.creates and not ledger(root)


@pytest.mark.parametrize("model,expected", [(MODEL, False), ("anthropic::some-model", False),
                                            ("gigachat::some-model", False), ("openai::some-model", True),
                                            ("openai-compatible::some-model", True), ("provider/router-model", True)])
def test_optional_response_format_is_a_transport_capability(model, expected):
    assert LLMClient.supports_response_format(model) is expected
    assert LLMClient.supports_response_format(model, use_local=True) is False


def test_async_tools_and_capture_remain_in_callers_context(setup):
    root, gateway, client = setup

    async def run():
        answer, usage = await client.chat_async([{"role": "user", "content": "hi"}], MODEL,
                                               tools=[{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}])
        capture = ua.last_physical_attempt_capture()
        assert capture.state == "settled" and capture.attempt_id in usage["ledger_attempt_ids"]
        assert answer == result()["message"]

    asyncio.run(run())
    assert len(gateway.operations) == 1 and ledger(root)[-1]["state"] == "settled"


def test_gigachat_async_tools_still_refuse_before_provider_io(setup, monkeypatch):
    _, gateway, client = setup
    monkeypatch.setattr(client, "_resolve_remote_target", lambda _: {"provider": "gigachat"})
    with pytest.raises(ValueError, match="does not support GigaChat tool calls"):
        asyncio.run(client.chat_async([], "gigachat::some-model", tools=[{"type": "function"}]))
    assert not gateway.creates


@pytest.mark.parametrize("profile", [None, "account-b"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("requested_model", [None, "exact-model"])
def test_catalog_metadata_uses_exact_optional_profile_and_closes(setup, monkeypatch, profile, fails, requested_model):
    root, gateway, client = setup
    monkeypatch.setattr(transport, "read_owned_gateway", lambda: gateway)
    catalog = {"source": "opaque-source", "route": {"credentialProfileId": profile}, "models": []}

    def read(source, credential_profile_id, **kwargs):
        assert source == "opaque-source" and credential_profile_id == profile
        assert kwargs == ({"requested_model": requested_model} if requested_model else {})
        if fails:
            raise ClaudexorUnavailable("catalog_unavailable", "No catalog evidence")
        return catalog

    gateway.list_source_models = read
    if fails:
        with pytest.raises(ClaudexorUnavailable):
            client.claudexor_model_catalog("opaque-source", profile, requested_model=requested_model)
    else:
        assert client.claudexor_model_catalog("opaque-source", profile, requested_model=requested_model) is catalog
    assert gateway.closed == 1 and not gateway.creates and not ledger(root)


def test_cold_actual_model_call_still_ensures_engine_after_unknown_metadata(setup, monkeypatch):
    from ouroboros import capability_evidence as ce, claudexor_daemon as owned
    from ouroboros.gateways import claudexor as wire

    root, gateway, client = setup
    monkeypatch.setattr(owned, "owned_config_dir", lambda: root / "cold-owned")
    evidence = ce.probe(root, provider="claudexor", model=MODEL,
        allow_fetch=True, allow_generative=False)
    assert evidence.status == ce.STATUS_FAILED and evidence.window_tokens == 0
    assert not ledger(root) and not gateway.creates
    startup = []
    endpoint = wire.DaemonEndpoint("127.0.0.1", 1, "fixture-owned-token")

    def start():
        startup.append("ensure_running")
        return endpoint

    def connect(actual_endpoint):
        assert actual_endpoint is endpoint
        return gateway

    gateway.handshake = lambda **_kwargs: startup.append("handshake") or {}
    monkeypatch.setattr(owned, "get_owned_daemon", lambda: SimpleNamespace(
        ensure_running=start, reconcile_rotation=lambda _gateway: startup.append("reconcile")))
    monkeypatch.setattr(wire, "ClaudexorGateway", connect)
    monkeypatch.setattr(transport, "ensure_owned_gateway", owned.ensure_owned_gateway)
    answer, usage = client.chat([{"role": "system", "content": "Own SYSTEM"},
        {"role": "user", "content": "hello"}], MODEL, model_role="main")
    assert answer == result()["message"]
    assert startup == ["ensure_running", "handshake", "reconcile"]
    assert len(gateway.creates) == 1 and len(usage["ledger_attempt_ids"]) == 1
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "settled"]
    assert gateway.closed == 1


@pytest.mark.parametrize("method", ["chat", "chat_async", "vision_query"])
def test_operation_observer_has_recovery_identity_without_provider_content(setup, method):
    root, gateway, client = setup
    events = []
    kwargs = {"model": MODEL, "model_operation_observer": lambda value: events.append(deepcopy(value))}
    if method == "vision_query":
        client.vision_query("describe", [{"url": "data:image/png;base64,AAAA"}], **kwargs)
    else:
        call = getattr(client, method)([], **kwargs)
        if method == "chat_async":
            asyncio.run(call)
    assert [value["operation_id"] for value in events] == ["", "op-0"]
    assert events[0]["invocation_id"] == events[1]["invocation_id"]
    assert events[1]["request_ref"] == REF
    assert events[1]["request_manifest_ref"]["path"]
    assert events[1]["request_manifest_ref"]["sha256"]
    assert set(events[1]) == {"operation_id", "invocation_id", "request_ref", "request_manifest_ref"}
    assert "model_operation_observer" not in gateway.uploads[0][0]
    assert retained(root, "request") == gateway.uploads[0][0]


def test_observer_failure_does_not_lose_response_or_repeat_generation(setup):
    root, gateway, client = setup

    def failed(_value):
        raise OSError("IPC recipient gone")

    answer, _ = client.chat([], MODEL, model_operation_observer=failed)
    assert answer == result()["message"] and retained(root) == result()
    assert len(gateway.operations) == 1 and ledger(root)[-1]["state"] == "settled"


def test_async_local_switch_projects_its_new_capture_to_the_caller(setup, monkeypatch):
    root, _gateway, client = setup

    def local(*_args, **_kwargs):
        request = ua.AttemptRequest(model="local-fixture", provider="local", drive_root=root)
        msg, usage = ua.execute_physical_attempt(
            request, lambda: ({"content": "local answer"}, {"prompt_tokens": 1, "completion_tokens": 1}),
            extractor=lambda value: (value[1], 0.0, True))
        return msg, usage

    monkeypatch.setattr(client, "_chat_local", local)

    async def run():
        answer, usage = await client.chat_async([], MODEL, use_local=True)
        assert answer["content"] == "local answer"
        assert ua.last_physical_attempt_capture().provider == "local"
        assert ua.last_physical_attempt_capture().attempt_id in usage["ledger_attempt_ids"]

    asyncio.run(run())


def test_lost_result_read_recovers_same_operation(setup):
    root, gateway, client = setup
    original = gateway.get_model_result
    reads = []

    def read(operation_id, **kwargs):
        reads.append(operation_id)
        if len(reads) == 1:
            raise ClaudexorUnavailable("daemon_unreachable", "result reply lost")
        return original(operation_id, **kwargs)

    gateway.get_model_result = read
    answer, usage = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert answer == result()["message"]
    assert reads == ["op-0", "op-0"] and len(gateway.creates) == 1
    assert len(usage["ledger_attempt_ids"]) == 1 and ledger(root)[-1]["state"] == "settled"


def test_pending_operation_has_no_whole_generation_http_deadline(setup, monkeypatch):
    root, gateway, client = setup
    original = gateway.detail
    observed = 0
    # Every poll advances the transport clock far beyond the HTTP bound. A
    # healthy typed pending response never spends an outage's clock.
    def detail(index):
        nonlocal observed
        observed += 1
        gateway.pending = observed < 5
        return original(index)

    gateway.detail = detail
    ticks = iter(range(100, 10000, 100))
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _seconds: None))
    answer, _ = client.chat([{"role": "user", "content": "hi"}], MODEL, timeout=0.001)
    assert observed == 5 and answer == result()["message"]
    assert not gateway.cancels and ledger(root)[-1]["state"] == "settled"


def test_unknown_engine_outcome_retains_response_without_resend_or_false_zero(setup):
    root, gateway, client = setup
    gateway.results = [result(outcome="unknown", cash=0, knowledge="unknown", problem={
        "code": "engine_died", "message": "Provider outcome is unknown"})]
    gateway.dispatch = ["unknown"]
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert raised.value.code == "model_outcome_unknown"
    assert retained(root) == gateway.results[0] and not gateway.acks
    assert len(gateway.operations) == 1 and ledger(root)[-1]["state"] == "unresolved"


def test_continuation_repair_is_bounded_to_one_unstarted_operation(setup):
    root, gateway, client = setup
    changed = {**ROUTE, "credentialProfileId": "account-b"}
    failure = result(outcome="failed", route=changed, problem={"code": "invalid_continuation", "message": "different account"})
    gateway.results = [failure, failure]
    gateway.dispatch = ["not_started", "not_started"]
    with pytest.raises(transport.ClaudexorModelNotDispatched):
        client.chat([result()["message"]], MODEL)
    assert len(gateway.operations) == 2
    assert [row["state"] for row in ledger(root)] == ["reserved", "dispatched", "released"] * 2


def test_unreleased_attempt_cannot_authorize_continuation_repair(setup, monkeypatch):
    root, gateway, client = setup
    changed = {**ROUTE, "credentialProfileId": "account-b"}
    gateway.results = [result(outcome="failed", route=changed, problem={"code": "invalid_continuation", "message": "different account"})]
    gateway.dispatch = ["not_started"]
    monkeypatch.setattr(ua, "release_pre_dispatch_attempt", lambda *_: False)
    with pytest.raises(transport.ClaudexorModelNotDispatched) as raised:
        client.chat([result()["message"]], MODEL)
    assert raised.value.physical_attempt_capture.state == "unresolved"
    assert len(gateway.operations) == 1 and ledger(root)[-1]["state"] == "unresolved"


def test_model_switch_strips_native_envelope_but_not_tool_results():
    messages = [result()["message"], {"role": "tool", "tool_call_id": "a", "content": "unchanged"}]
    original = deepcopy(messages)
    assert LLMClient.sanitize_reasoning_on_model_switch(messages, MODEL, MODEL) is messages
    for destination in ("claudexor::codex=other-model", "openai::some-model"):
        switched = LLMClient.sanitize_reasoning_on_model_switch(messages, MODEL, destination)
        assert "nativeContinuation" not in switched[0]
        assert switched[0]["tool_calls"] == original[0]["tool_calls"] and switched[1] == original[1]
    assert messages == original


def test_caller_control_interrupts_pending_operation_without_false_success(setup):
    root, gateway, client = setup
    gateway.pending = True
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL,
                    model_poll_control=lambda: "deadline_exceeded" if gateway.operations else None)
    assert raised.value.control_reason == "deadline_exceeded"
    assert gateway.cancels == [("op-0", "host_cancelled")]
    assert ledger(root)[-1]["state"] == "unresolved" and not gateway.acks


def test_control_after_response_preserves_cas_and_never_returns_success(setup):
    root, gateway, client = setup
    received = False
    original = gateway.get_model_result

    def read(*args, **kwargs):
        nonlocal received
        received = True
        return original(*args, **kwargs)

    gateway.get_model_result = read
    with pytest.raises(transport.ClaudexorModelError) as raised:
        client.chat([{"role": "user", "content": "hi"}], MODEL,
                    model_poll_control=lambda: "owner_cancelled" if received else None)
    assert raised.value.control_reason == "owner_cancelled"
    assert retained(root) == result() and not gateway.acks
    assert raised.value.model_result == result()
    assert raised.value.usage["claudexor"]["result_custody"]["retained_manifest_ref"]
    assert ledger(root)[-1]["state"] == "settled"


def test_exact_response_bytes_are_retained_before_ack(setup):
    root, gateway, client = setup
    # Noncanonical whitespace, escaped Unicode and exponent spelling all survive.
    gateway.raw_result = (" \n" + json.dumps(result(), ensure_ascii=True, indent=3).replace("12.5", "1.25e1") + "\r\n").encode("utf-8")
    original = gateway.acknowledge_model_result

    def acknowledge(*args):
        assert retained(root, raw=True) == gateway.raw_result
        return original(*args)

    gateway.acknowledge_model_result = acknowledge
    answer, _ = client.chat([{"role": "user", "content": "hi"}], MODEL)
    assert answer == result()["message"]
    assert gateway.acks and retained(root, raw=True) == gateway.raw_result


@pytest.mark.parametrize("raw", [b"[]", b'{"value": NaN}', b'{"value": "\xff"}'])
def test_gateway_raw_result_still_validates_json_object_and_utf8(raw):
    gateway = object.__new__(ClaudexorGateway)
    gateway._request = lambda *_args, **_kwargs: raw
    ref = {"resourceId": "model-result", "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "sizeBytes": len(raw)}
    with pytest.raises(ClaudexorUnavailable, match="Model"):
        gateway.get_model_result("op-0", expected_ref=ref, raw_bytes=True)


def test_gateway_raw_and_default_result_share_integrity_validation():
    gateway = object.__new__(ClaudexorGateway)
    raw = b' { "message" : "ok" }\r\n'
    gateway._request = lambda *_args, **_kwargs: raw
    ref = {"resourceId": "model-result", "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "sizeBytes": len(raw)}
    assert gateway.get_model_result("op-0", expected_ref=ref) == {"message": "ok"}
    assert gateway.get_model_result("op-0", expected_ref=ref, raw_bytes=True) == raw
    with pytest.raises(ClaudexorUnavailable, match="size and SHA-256"):
        gateway.get_model_result("op-0", expected_ref={**ref, "sizeBytes": len(raw) + 1}, raw_bytes=True)


def test_missing_old_account_does_not_authorize_native_reset(setup):
    _, gateway, client = setup
    message = result()["message"]
    message["nativeContinuation"]["route"] = {"source": "codex", "model": "exact-model"}
    gateway.results = [result(outcome="failed", problem={"code": "invalid_continuation", "message": "missing binding"})]
    gateway.dispatch = ["not_started"]
    with pytest.raises(transport.ClaudexorModelNotDispatched):
        client.chat([message], MODEL)
    assert len(gateway.operations) == 1


def test_caller_control_before_create_proves_no_dispatch(setup):
    root, gateway, client = setup
    with pytest.raises(transport.ClaudexorModelNotDispatched) as raised:
        client.chat([], MODEL, model_poll_control=lambda: "owner_cancelled")
    assert not gateway.uploads and not gateway.creates
    assert raised.value.physical_attempt_capture.state == "released"
    assert [row["state"] for row in ledger(root)] == ["reserved", "released"]


@pytest.mark.parametrize("phase", ["upload", "ack"])
def test_async_cancellation_preserves_io_ownership_outside_polling(setup, phase):
    root, gateway, client = setup
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    name = "upload_model_request" if phase == "upload" else "acknowledge_model_result"
    original = getattr(gateway, name)
    original_close = gateway.close

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        assert not closed.is_set()
        return original(*args, **kwargs)

    def close():
        original_close()
        closed.set()

    setattr(gateway, name, blocked)
    gateway.close = close

    async def run():
        task = asyncio.create_task(client.chat_async([], MODEL))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert gateway.closed == 1
    if phase == "upload":
        assert not gateway.creates and ledger(root)[-1]["state"] == "released"
        assert ua.usage_projection(root)["integrity_degraded"] is False
    else:
        assert retained(root) == result() and ledger(root)[-1]["state"] == "settled"


def test_async_cancellation_leaves_io_owner_to_cancel_and_close(setup):
    root, gateway, client = setup
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    gateway.pending = True
    original = gateway.get_model_operation
    original_close = gateway.close

    def read(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    def close():
        original_close()
        closed.set()

    gateway.get_model_operation, gateway.close = read, close

    async def run():
        task = asyncio.create_task(client.chat_async([{"role": "user", "content": "hi"}], MODEL))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert gateway.cancels == [("op-0", "host_cancelled")]
    assert ledger(root)[-1]["state"] == "unresolved" and not gateway.acks


TURN = {"route": ROUTE, "format": "codex.turn.v1", "payload": {"turnState": "opaque-turn-state"}}
EARLIER = {"route": ROUTE, "format": "codex.turn.v1", "payload": {"turnState": "earlier-turn"}}


@pytest.fixture
def turn_engine(monkeypatch):
    """A serving engine whose strict request schema accepts the active-turn field."""
    monkeypatch.setattr(transport, "owned_engine_version",
                        lambda: transport.config.CLAUDEXOR_MODEL_TURN_STATE_MIN_VERSION)


@pytest.mark.parametrize("version,opted", [("3.10.4", True), ("4.0.0", True), ("3.10.3", False), ("", False)])
def test_active_turn_is_offered_only_where_the_request_schema_accepts_it(setup, monkeypatch, version, opted):
    _, gateway, client = setup
    monkeypatch.setattr(transport, "owned_engine_version", lambda: version)
    slot = transport.ModelTurnState()
    client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    payload = gateway.uploads[-1][0]
    # Absent is the legacy stateless shape; explicit null opts into an empty turn.
    assert ("nativeContinuation" in payload) is opted
    assert payload.get("nativeContinuation") is None and slot.envelope is None


def test_request_carries_a_copy_of_the_slot_value(turn_engine):
    slot = transport.ModelTurnState(deepcopy(TURN))
    payload = transport._request({"source": "codex", "resolved_model": "exact-model"}, [], None,
                                 {"model_turn_state": slot})
    assert payload["nativeContinuation"] == TURN
    payload["nativeContinuation"]["payload"]["turnState"] = "mutated inside the frozen request"
    assert slot.envelope == TURN


def test_dispatched_result_replaces_the_slot_and_the_next_send_replays_it(setup, turn_engine):
    _, gateway, client = setup
    gateway.results = [{**result(), "nativeContinuation": deepcopy(TURN)} for _ in range(2)]
    gateway.dispatch = ["response_received"] * 2
    slot = transport.ModelTurnState()
    client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert slot.envelope == TURN and gateway.uploads[0][0]["nativeContinuation"] is None
    client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert gateway.uploads[-1][0]["nativeContinuation"] == TURN


def test_a_result_without_an_envelope_leaves_the_turn_stateless(setup, turn_engine):
    _, gateway, client = setup
    slot = transport.ModelTurnState(deepcopy(EARLIER))
    client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert gateway.uploads[-1][0]["nativeContinuation"] == EARLIER and slot.envelope is None


@pytest.mark.parametrize("dispatch,outcome", [("not_started", "completed"), ("unknown", "unknown")])
def test_a_not_dispatched_or_unknown_outcome_never_touches_the_slot(setup, turn_engine, dispatch, outcome):
    _, gateway, client = setup
    gateway.results = [{**result(outcome=outcome), "nativeContinuation": deepcopy(TURN)}]
    gateway.dispatch = [dispatch]
    slot = transport.ModelTurnState(deepcopy(EARLIER))
    with pytest.raises(transport.ClaudexorModelError):
        client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert slot.envelope == EARLIER


def test_the_active_turn_token_never_reaches_usage_the_ledger_or_ordinary_logs(setup, turn_engine):
    root, gateway, client = setup
    gateway.results = [{**result(), "nativeContinuation": deepcopy(TURN)}]
    slot = transport.ModelTurnState()
    _, usage = client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert slot.envelope == TURN and repr(slot) == "ModelTurnState(active=True)"
    token = TURN["payload"]["turnState"]
    assert token not in json.dumps(usage, default=str) and token not in json.dumps(ledger(root))
    assert all(token not in path.read_text(encoding="utf-8") for path in (root / "logs").glob("*.jsonl"))
