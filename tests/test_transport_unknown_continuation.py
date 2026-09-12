"""Owner-approved managed continuation after upstream recovery, with old custody."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from ouroboros import loop as loop_mod, loop_transport as transport
from ouroboros.loop import run_llm_loop
from ouroboros.tools.registry import ToolRegistry
from tests.test_loop_transport_wait import _loop_kwargs


def test_managed_unknown_waits_for_upstream_then_adds_new_input(tmp_path, monkeypatch):
    sends, probes, sleeps, notes = [], [], [], []
    previous = {"physical_attempt_id": "old-paid-attempt", "outcome": "unknown", "model": "test-model"}
    def send(_llm, messages, *args, **kwargs):
        usage = args[8]  # model, tools, effort, retries, logs, task, round, event, usage
        sends.append([dict(row) for row in messages])
        if len(sends) == 1:
            usage.update(_last_llm_error_kind="provider_outcome_unknown", _pending_transport_outcome=dict(previous))
            return None, 0.0
        usage.pop("_last_llm_error_kind", None)
        return {"role": "assistant", "content": "continued answer"}, 0.0
    def reachable(*args, **kwargs):
        probes.append(kwargs)
        assert len(sends) == 1
        return {"kind": "upstream_http", "status_code": 200} if len(probes) == 3 else {}
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", send)
    monkeypatch.setattr(transport, "upstream_transport_reachable", reachable)
    monkeypatch.setattr(transport, "interruptible_wait_sleep", lambda seconds, wake: sleeps.append(seconds) or False)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "1")
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result, usage, trace = run_llm_loop(**_loop_kwargs(tmp_path, registry, notes))
    assert result == "continued answer" and len(sends) == 2 and len(probes) == 3
    assert len(sleeps) == 3  # No cognition or compaction was sent between observations.
    assert sends[0] != sends[1]
    assert any("NEW physical model attempt" in str(row.get("content")) and "old-paid-attempt" in str(row.get("content")) for row in sends[1])
    assert usage["transport_recovery"]["previous_attempt"] == previous
    assert usage["transport_recovery"]["old_outcome"] == "unknown"
    assert any("another charge is possible" in text for text in notes)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    recovered = [row for row in events if row.get("detail") == "new_attempt_after_unknown_outcome"]
    assert recovered[0]["outcome_custody"] == previous


@pytest.mark.parametrize("flag", ["is_direct_chat"])
def test_unknown_policy_does_not_expand_other_execution_classes(tmp_path, flag):
    ctx = SimpleNamespace(task_id="t", **{flag: True})
    assert transport.reconcile_transport_wait(None, ctx, msg_present=False,
        error_kind="provider_outcome_unknown", drive_logs=tmp_path, task_id="t", model="m",
        emit_progress=lambda *a, **kw: pytest.fail("unexpected automatic continuation")) is None


def test_deadline_closes_unknown_wait_without_a_probe_or_send(tmp_path, monkeypatch):
    ctx = SimpleNamespace(task_id="t", task_metadata={"deadline_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
    episode = transport.TransportWaitEpisode(wait_cause="provider_outcome_unknown")
    monkeypatch.setattr(transport, "upstream_transport_reachable", lambda *a, **k: pytest.fail("probe after deadline"))
    assert not transport.continue_unknown_transport(episode, llm=None, tools=SimpleNamespace(_ctx=ctx),
        messages=[], accumulated_usage={}, drive_logs=tmp_path, task_id="t", model="m", emit_progress=lambda *a, **k: None)


@pytest.mark.parametrize("status,ready", [(200, True), (401, True), (405, True), (503, False)])
def test_upstream_probe_is_non_generating_and_uses_resolved_route(monkeypatch, status, ready):
    from ouroboros.llm import LLMClient

    seen = []
    original = httpx.Client
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        def respond(request):
            seen.append(request)
            return httpx.Response(status)
        return original(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", client)
    llm = LLMClient()
    monkeypatch.setattr(llm, "_resolve_remote_target",
                        lambda model: {"base_url": "https://exact-provider.invalid/api"})
    observed = transport.upstream_transport_reachable(llm, "vendor/model", timeout=3)
    assert bool(observed) is ready
    assert len(seen) == 1 and seen[0].method == "HEAD"
    assert str(seen[0].url) == "https://exact-provider.invalid/api"
    assert not seen[0].content and "authorization" not in seen[0].headers


def test_loopback_response_cannot_prove_upstream_recovery(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("loopback is not upstream"))
    llm = SimpleNamespace(_resolve_remote_target=lambda model: {"base_url": "http://127.0.0.1:1234/v1"})
    assert not transport.upstream_transport_reachable(llm, "vendor/model", timeout=3)


@pytest.mark.parametrize("fact", ["absent", "stale", "local", "upstream"])
def test_subscription_requires_fresh_typed_upstream_observation(monkeypatch, fact):
    from ouroboros import llm_claudexor
    now = datetime.now(timezone.utc)
    observed = (now + timedelta(seconds=1)).isoformat() if fact != "stale" else "2020-01-01T00:00:00Z"
    catalog = {"source": "codex", "observedAt": observed, "credentialProfileId": "profile-a",
               "models": [{"id": "test"}], "provenance": "fixture"}
    if fact != "absent":
        catalog["provenance"] = "local_cache" if fact == "local" else "provider_http"
    monkeypatch.setattr(llm_claudexor, "model_catalog", lambda *a, **kw: catalog)
    monkeypatch.setattr("ouroboros.model_slots.model_role_option", lambda *a: "profile-a")
    assert bool(transport.upstream_transport_reachable(None, "claudexor::codex=test", timeout=3)) is (fact == "upstream")


def test_claudexor_control_loss_keeps_same_operation_past_read_window(tmp_path, monkeypatch):
    from ouroboros import llm_claudexor
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    inv = llm_claudexor._ModelInvocation({"usage_model": "claudexor::codex=test"}, {}, {"timeout": 1})
    inv.operation_id, inv.invocation_id, inv.task_id, inv.root = "same-op", "same-attempt", "t", tmp_path
    inv.create_attempted = True
    now, reads = [0.0], []
    ctx = SimpleNamespace(task_id="t")
    waiter = SimpleNamespace(tool_context=ctx, control_reason=lambda: None)
    monkeypatch.setattr(llm_claudexor, "current_model_wait", lambda: waiter)
    monkeypatch.setattr(llm_claudexor, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda t: now.__setitem__(0, now[0]+t)))
    class Gateway:
        def get_model_operation(self, operation, **kwargs):
            reads.append(operation)
            if len(reads) <= 3: raise ClaudexorUnavailable("daemon_unreachable", "offline")
            return {"id": operation, "state": "succeeded", "dispatch": {"state": "response_received"},
                    "response": {"state": "ready", "ref": {"sha256": "exact"}}}
        def get_model_result(self, operation, **kwargs):
            assert operation == "same-op"
            return b'{"outcome":"completed","message":{"content":"same paid answer"}}'
        def create_model_operation(self, *a, **kw): pytest.fail("second model operation")
        def close(self): pass
    gateway = inv.gateway = Gateway()
    monkeypatch.setattr(llm_claudexor, "read_owned_gateway", lambda: gateway)
    monkeypatch.setattr(inv, "retain", lambda raw: None)
    answer = inv.receive()
    assert answer["message"]["content"] == "same paid answer"
    assert reads == ["same-op"] * 4 and now[0] > inv.timeout
    assert inv.operation_id == "same-op" and inv.invocation_id == "same-attempt"
    events = [json.loads(line) for line in (tmp_path / "logs/events.jsonl").read_text().splitlines()]
    assert events[-1]["detail"] == "same_model_operation_rejoined"


def test_managed_continuation_keeps_old_money_and_mints_one_new_attempt(tmp_path, monkeypatch):
    from ouroboros import usage_accounting as ua
    from tests.test_transport_death_retry import _LedgerLLM, _ledger, _loop_kwargs as ledger_kwargs
    llm = _LedgerLLM(tmp_path, lambda: httpx.ReadError("lost after dispatch"))
    observations = []
    def reachable(*args, **kwargs):
        observations.append(llm.calls)
        return {"kind": "upstream_http", "status_code": 200}
    monkeypatch.setattr(transport, "upstream_transport_reachable", reachable)
    monkeypatch.setattr(transport, "interruptible_wait_sleep", lambda *args: False)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id="t-death", root_task_id="t-death", global_limit_usd=100)):
        text, usage, _ = run_llm_loop(**ledger_kwargs(tmp_path, llm, []))
    assert text == "done" and llm.calls == 2 and observations == [1]
    rows = _ledger(tmp_path)
    assert [row["state"] for row in rows] == ["reserved", "dispatched", "unresolved", "reserved", "dispatched", "settled"]
    old, new = rows[0]["attempt_id"], rows[3]["attempt_id"]
    assert old != new
    assert usage["transport_recovery"]["previous_attempt"]["physical_attempt_id"] == old
    assert ua.usage_projection(tmp_path)["unresolved_upper_bound_usd"] == 1.0


@pytest.mark.parametrize("reported_model", ["absent", None, "test"])
@pytest.mark.parametrize("axis", ["matching", "empty", "before_wait", "source", "profile", "fingerprint", "model", "local"])
def test_catalog_reachability_binds_effective_account_and_wait_start(monkeypatch, axis, reported_model):
    import time
    from ouroboros import llm_claudexor
    started = time.time() - 10
    catalog = dict(source="codex", credentialProfileId="effective-profile", accountFingerprint="account-a",
                   observedAt=datetime.fromtimestamp(started + 1, timezone.utc).isoformat(),
                   provenance="provider_http", models=[{"id": "test"}])
    if axis == "before_wait": catalog["observedAt"] = datetime.fromtimestamp(started - 1, timezone.utc).isoformat()
    if axis == "source": catalog["source"] = "foreign"
    if axis == "profile": catalog["credentialProfileId"] = "foreign"
    if axis == "fingerprint": catalog["accountFingerprint"] = "foreign"
    if axis == "model": catalog["models"] = [{"id": "foreign"}]
    if axis == "local": catalog["provenance"] = "local_cache"
    if axis == "empty": catalog = {}
    route = {"source": "codex", "credentialProfileId": "effective-profile", "accountFingerprint": "account-a"}
    if reported_model != "absent": route["model"] = reported_model
    inv = llm_claudexor._ModelInvocation({}, {}, {})
    inv.operation_id = "old-unknown-operation"
    error = inv.error({"code": "transport_unknown", "retryable": False},
                      {"dispatch": {"state": "unknown", "route": route}}, unknown=True)
    reads = []
    def read(source, account, **kwargs):
        assert (source, account, kwargs["requested_model"]) == ("codex", "effective-profile", "test")
        reads.append(kwargs)
        return catalog
    monkeypatch.setattr(llm_claudexor, "model_catalog", read)
    result = transport.upstream_transport_reachable(None, "claudexor::codex=test", timeout=3,
        account_override="", observed_after=started,
        expected_route=error.route)
    assert bool(result) is (axis == "matching")
    assert len(reads) == 1
    assert error.route == route and error.code == "model_outcome_unknown" and not error.retryable
    assert error.operation_id == "old-unknown-operation"


@pytest.mark.parametrize("axis", ["source", "model", "profile"])
def test_explicit_unknown_operation_route_mismatch_refuses_before_catalog(monkeypatch, axis):
    from ouroboros import llm_claudexor
    route = {"source": "codex", "model": None, "credentialProfileId": "effective-profile"}
    route[{"source": "source", "model": "model", "profile": "credentialProfileId"}[axis]] = "foreign"
    monkeypatch.setattr(llm_claudexor, "model_catalog", lambda *a, **kw: pytest.fail("mismatched route was probed"))
    assert not transport.upstream_transport_reachable(None, "claudexor::codex=test", timeout=3,
        account_override="effective-profile", expected_route=route)


def test_null_reported_model_recovers_without_rewriting_unknown_custody(tmp_path, monkeypatch):
    from ouroboros import llm_claudexor
    previous = {"physical_attempt_id": "old-paid-attempt", "operation_id": "old-operation", "outcome": "unknown",
                "route": {"source": "codex", "model": None, "credentialProfileId": "profile-a",
                          "accountFingerprint": "account-a"}}
    episode = transport.TransportWaitEpisode(wait_cause="provider_outcome_unknown", outcome_custody=previous)
    def catalog(source, profile, **kwargs):
        assert (source, profile, kwargs["requested_model"]) == ("codex", "profile-a", "test")
        return {"source": source, "credentialProfileId": profile, "accountFingerprint": "account-a",
                "provenance": "provider_http",
                "observedAt": datetime.fromtimestamp(episode.started_at + 1, timezone.utc).isoformat(),
                "models": [{"id": "test"}]}
    monkeypatch.setattr(llm_claudexor, "model_catalog", catalog)
    monkeypatch.setattr("ouroboros.model_slots.task_model_binding", lambda *a, **kw: ("main", "profile-a"))
    monkeypatch.setattr(llm_claudexor, "chat_claudexor", lambda *a, **kw: pytest.fail("metadata must not generate"))
    ctx = SimpleNamespace(task_id="t", task_metadata={})
    usage, messages = {}, []
    assert transport.continue_unknown_transport(episode, llm=None, tools=SimpleNamespace(_ctx=ctx),
        messages=messages, accumulated_usage=usage, drive_logs=tmp_path, task_id="t",
        model="claudexor::codex=test", emit_progress=lambda *a, **kw: None)
    assert messages[0]["role"] == "user" and "NEW physical model attempt" in messages[0]["content"]
    assert usage["transport_recovery"]["previous_attempt"] == previous
    assert usage["transport_recovery"]["old_outcome"] == "unknown"
    assert previous["route"]["model"] is None and episode.outcome_custody == previous


def test_stop_before_unknown_probe_preserves_no_send_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "transport_repeat_stop_requested", lambda *_: True)
    monkeypatch.setattr(transport, "upstream_transport_reachable", lambda *a, **kw: pytest.fail("probe after Stop"))
    assert not transport.continue_unknown_transport(transport.TransportWaitEpisode(), llm=None,
        tools=SimpleNamespace(_ctx=SimpleNamespace(task_id="t")), messages=[], accumulated_usage={},
        drive_logs=tmp_path, task_id="t", model="m", emit_progress=lambda *a, **kw: None)


@pytest.mark.parametrize("remaining", [2700.0, 3.0])
def test_upstream_head_uses_connection_window_in_every_socket_phase(monkeypatch, remaining):
    """A metadata HEAD cannot hold an unleased task for the cognition read window."""
    from ouroboros.llm import LLMClient

    requests = []
    original = httpx.Client

    def client(**kwargs):
        def respond(request):
            requests.append(request)
            return httpx.Response(200)
        return original(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "Client", client)
    llm = LLMClient()
    monkeypatch.setattr(llm, "_resolve_remote_target",
                        lambda model: {"base_url": "https://exact-provider.invalid/api"})
    observed = transport.upstream_transport_reachable(llm, "vendor/model", timeout=remaining)
    assert observed["kind"] == "upstream_http" and len(requests) == 1
    assert requests[0].method == "HEAD" and not requests[0].content
    # Compare the actual HTTPX request with the established ordinary connection
    # allowance, retaining a shorter owner remainder on every socket phase.
    bound = min(remaining, llm._no_proxy_timeout(remaining).connect)
    assert requests[0].extensions["timeout"] == dict(connect=bound, read=bound, write=bound, pool=bound)


def test_unknown_policy_keeps_configured_session_nanny_out_of_managed_continuation(tmp_path):
    ctx = SimpleNamespace(task_id="t", exact_model_route=True, _configured_subagent_route_kind="agent_session")
    from ouroboros import loop_transport
    assert loop_transport.reconcile_transport_wait(None, ctx, msg_present=False, error_kind="provider_outcome_unknown", drive_logs=tmp_path, task_id="t", model="m", emit_progress=lambda *a, **kw: pytest.fail("unexpected automatic continuation")) is None
