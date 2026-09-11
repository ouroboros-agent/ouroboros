"""The live Main call rebinds routes without replaying its completed work."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros import loop, model_wait, usage_accounting as ua
from ouroboros.llm_attempt import _attempt_request, _candidate_before_dispatch
from ouroboros.loop_model_call import _reprepare_waiting_main
from ouroboros.model_slots import MODEL_ACCOUNTS_KEY
from tests.test_context_fit_integration import _plan
from tests.test_llm_claudexor import MODEL, ROUTE, result, ledger, setup as gateway_fixture
from tests.test_model_wait import live_wait as wait_fixture

setup = gateway_fixture
live_wait = wait_fixture
ROUTE_B = {**ROUTE, "credentialProfileId": "account-b", "accountFingerprint": "fingerprint-b"}


@pytest.fixture
def main_call(live_wait, monkeypatch):
    root, gateway, client, controller, events, decide = live_wait
    plan = replace(_plan(preferred="max", window=900_000), model=MODEL,
                   provider="claudexor", model_role="main", model_route=ROUTE,
                   route_fp="capacity-account-a")
    messages = [*plan.messages_for("max"), result()["message"],
                {"role": "tool", "tool_call_id": "a", "content": "verified read A"},
                {"role": "tool", "tool_call_id": "b", "content": "completed review B"}]
    inner = SimpleNamespace(task_id="task-one", task_attempt=1, task_metadata={},
                            active_model=MODEL, active_context_mode="max", messages=messages,
                            context_fit_plan=plan, event_queue=events, drive_logs=lambda: root / "logs")
    controller.tool_context = inner
    ctx = loop._RoundModelCallContext(
        llm=client, messages=messages, tools=SimpleNamespace(_ctx=inner), context_fit_plan=plan,
        active_model=MODEL, tool_schemas=[], active_effort="medium", max_retries=3,
        drive_logs=root / "logs", task_id="task-one", round_idx=1, event_queue=events,
        accumulated_usage={}, task_type="task", active_use_local=False,
        active_context_mode="max", drive_root=root)
    observations = []

    def route_evidence(task, **_kwargs):
        observations.append(deepcopy(task))
        observed = task.get("model_route") or {}
        account = task.get("credential_profile_id") or observed.get("credentialProfileId") or "account-b"
        subscription = task["model"] == MODEL and not task["use_local_model"]
        return ({"model": task["model"], "provider": "claudexor" if subscription else "local" if task["use_local_model"] else "openai"},
                SimpleNamespace(route_fp=f"capacity-{account}" if subscription else "other-route", status="confirmed",
                                stale=False, window_tokens=240_000 if subscription else 300_000,
                                source_id="codex" if subscription else "", source="advertised",
                                credential_profile_id=account if subscription else "",
                                account_fingerprint="fingerprint-b" if subscription else ""))

    from ouroboros import context
    monkeypatch.setattr(context, "_context_fit_route", route_evidence)
    monkeypatch.setattr(loop, "_server_web_allowed_by_task", lambda _ctx: False)
    monkeypatch.setattr(loop, "_run_main_reclaim", lambda *_a, **_kw: pytest.fail("No completed work may be replayed"))
    return ctx, gateway, controller, events, decide, observations


def _failed(code, route=ROUTE):
    return result(outcome="failed", route=route, problem={"code": code, "message": code})


def _dispatch(ctx):
    return loop._call_round_model(ctx)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_account_repair_rebinds_real_physical_candidate_before_send(main_call, asynchronous):
    ctx, gateway, controller, _events, _decide, observations = main_call
    original = deepcopy(ctx.messages)
    gateway.results = [_failed("invalid_continuation", ROUTE_B), result(route=ROUTE_B)]
    gateway.dispatch = ["not_started", "response_received"]
    if asynchronous:
        disposition = loop._measure_round_main_fit(ctx, automatic_pass_used=False)
        with controller.register_reprepare("main", lambda values: _reprepare_waiting_main(ctx, values)):
            with ua.bind_physical_attempt_context(loop._physical_context_for_fit(disposition)):
                asyncio.run(ctx.llm.chat_async(ctx.messages, MODEL, model_role="main"))
    else:
        answer, _cost, _mode = _dispatch(ctx)
        assert answer["content"] == result()["message"]["content"]
    rows = ledger(ctx.drive_root)
    assert [row["state"] for row in rows] == ["reserved", "dispatched", "released", "reserved", "dispatched", "settled"]
    dispatched = [row for row in rows if row["state"] == "dispatched"]
    assert dispatched[0]["physical_context"]["route_fp"] == "capacity-account-a"
    assert dispatched[1]["physical_context"]["route_fp"] == "capacity-account-b"
    assert dispatched[1]["physical_context"]["capacity_total_tokens"] == 240_000
    assert observations[0]["model_route"] == ROUTE_B
    assert len(gateway.operations) == 2 and gateway.creates[0] != gateway.creates[1]
    resent = gateway.uploads[1][0]["messages"]
    assert "nativeContinuation" not in resent[2]
    assert resent[2]["tool_calls"] == original[2]["tool_calls"] and resent[3:] == original[3:]
    assert "nativeContinuation" not in ctx.messages[2]
    assert ctx.context_fit_plan.core_sha256 == "a" * 64


def test_quota_auto_wait_rejoins_same_round_then_repairs_changed_account(main_call):
    ctx, gateway, _controller, events, _decide, observations = main_call
    gateway.results = [_failed("subscription_window_exhausted"),
                       _failed("invalid_continuation", ROUTE_B), result(route=ROUTE_B)]
    gateway.dispatch = ["not_started", "not_started", "response_received"]
    answer, cost, mode = _dispatch(ctx)
    assert answer and cost is None and mode == "max"
    assert len(gateway.operations) == 3
    assert ctx.accumulated_usage["rounds"] == 1
    assert ctx.accumulated_usage["_model_route"] == ROUTE_B
    assert ctx.accumulated_usage["_context_route_fp"] == "capacity-account-b"
    assert any(item["model_route"] == ROUTE_B for item in observations)
    waits = [event for event in list(events.queue) if event.get("type") == "task_model_wait"]
    assert [event["state"] for event in waits] == ["waiting", "waiting", "resolved"]
    assert waits[-1]["resolution"] == "resource_available"
    assert [row["state"] for row in ledger(ctx.drive_root)].count("settled") == 1


@pytest.mark.parametrize("destination,use_local,pin", [
    ("openai::alternate", False, ""), ("local-model", True, ""), (MODEL, False, "account-b"),
])
def test_manual_switch_updates_only_waiting_role_and_continues_current_main_call(
    main_call, monkeypatch, destination, use_local, pin,
):
    ctx, gateway, controller, events, decide, _observations = main_call
    monkeypatch.setenv(MODEL_ACCOUNTS_KEY, json.dumps({"main": "account-a", "light": "light-account"}))
    gateway.results = [_failed("subscription_window_exhausted"), result(route=ROUTE_B), result(route=ROUTE_B)]
    gateway.dispatch = ["not_started", "response_received", "response_received"]
    if destination == MODEL:
        gateway.results.insert(1, _failed("invalid_continuation", ROUTE_B))
        gateway.dispatch.insert(1, "not_started")
    decisions = []

    def catalog(*_args, **_kwargs):
        wait = next(event for event in reversed(list(events.queue)) if event.get("type") == "task_model_wait")
        response = decide({"request_id": "switch-once", "decision_id": f"model_wait:task-one:{wait['wait_id']}",
                           "revision": wait["revision"], "action": "switch", "model": destination,
                           "credential_profile_id": pin, "use_local": use_local, "persist_role": False})
        assert response.status_code == 202
        decisions.append(json.loads(response.body))
        return {}

    monkeypatch.setattr(ctx.llm, "claudexor_model_catalog", catalog)
    remote = ctx.llm._chat_remote

    def direct(messages, model, tools):
        target = {"provider": "local" if use_local else "openai", "usage_model": model}
        payload = {"messages": deepcopy(messages), "tools": tools or [], "model": model}
        request = _attempt_request(target, payload)
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0 if use_local else 0.2,
                 "provider": target["provider"], "resolved_model": model, "cost_final": True}
        return ua.execute_physical_attempt(
            request, lambda: ({"role": "assistant", "content": "switched answer"}, usage),
            extractor=lambda value: (value[1], value[1]["cost"], True),
            before_dispatch=_candidate_before_dispatch(payload, request))

    def remote_call(target, messages, tools, *args, **kwargs):
        if target["provider"] == "claudexor":
            return remote(target, messages, tools, *args, **kwargs)
        return direct(messages, destination, tools)

    monkeypatch.setattr(ctx.llm, "_chat_remote", remote_call)
    monkeypatch.setattr(ctx.llm, "_chat_local", lambda messages, tools, *_args, **_kwargs: direct(messages, destination, tools))
    answer, cost, _mode = _dispatch(ctx)
    assert answer and len(decisions) == 1 and decisions[0]["saved"] is False
    assert ctx.active_model == destination and ctx.active_use_local is use_local
    assert controller.overrides == {"main": {"model": destination, "use_local": use_local, "model_account_override": pin}}
    assert json.loads(__import__("os").environ[MODEL_ACCOUNTS_KEY])["light"] == "light-account"
    assert cost == (None if destination == MODEL else 0 if use_local else 0.2)
    assert ctx.messages[-2:][0]["content"] == "verified read A"
    assert ctx.messages[-1]["content"] == "completed review B"
    # The same model used by Light remains pinned to its own account after Main switches.
    ctx.llm.chat([], MODEL, model_role="light")
    assert gateway.uploads[-1][0]["account"] == {"mode": "pin", "profileId": "light-account"}


def test_main_control_interrupt_is_typed_no_retry_and_keeps_operation_custody(main_call, monkeypatch):
    ctx, gateway, controller, _events, _decide, _observations = main_call
    gateway.pending = True
    monkeypatch.setattr(controller, "control_reason", lambda: "cancelled" if gateway.operations else None)
    with pytest.raises(model_wait.ModelWaitInterrupted) as raised:
        _dispatch(ctx)
    error = raised.value
    assert error.control_reason == "cancelled" and error.operation_id == "op-0"
    assert error.physical_attempt_capture.state == "unresolved"
    assert error.model_role_route["role"] == "main"
    assert len(gateway.operations) == 1 and gateway.cancels == [("op-0", "host_cancelled")]
    assert not controller.waits and not ctx.accumulated_usage.get("_last_llm_retry_same_request")


def test_cancel_after_result_keeps_settled_usage_and_exact_result(main_call, monkeypatch):
    ctx, gateway, controller, _events, _decide, _observations = main_call
    monkeypatch.setattr(controller, "control_reason", lambda: "cancelled" if gateway.acks else None)
    with pytest.raises(model_wait.ModelWaitInterrupted) as raised:
        _dispatch(ctx)
    assert raised.value.model_result == result()
    assert raised.value.usage["prompt_tokens"] == 20
    assert raised.value.physical_attempt_capture.state == "settled"
    assert raised.value.route == ROUTE
    assert len(gateway.operations) == 1


def test_native_repair_without_main_callback_refuses_stale_physical_fit(setup):
    _root, gateway, client = setup
    gateway.results = [_failed("invalid_continuation", ROUTE_B), result(route=ROUTE_B)]
    gateway.dispatch = ["not_started", "response_received"]
    physical = ua.PhysicalAttemptContext(
        profile="owner_max", route_fp="old-account", rendered_mode="max",
        measurement_basis="cold_estimate", round_id="round-one", target_total_tokens=None,
        capacity_total_tokens=900_000, context_target_miss=False, automatic_pass_used=False)
    with ua.bind_physical_attempt_context(physical):
        with pytest.raises(model_wait.ModelWaitInterrupted, match="model_wait_reprepare_required"):
            client.chat([result()["message"]], MODEL, model_role="main")
    assert len(gateway.operations) == 1


def test_unknown_main_outcome_never_retries_or_waits_for_quota(main_call):
    ctx, gateway, controller, _events, _decide, _observations = main_call
    gateway.results, gateway.dispatch = [result(outcome="unknown")], ["unknown"]
    answer, _cost, _mode = _dispatch(ctx)
    assert answer is None and ctx.accumulated_usage["_last_llm_error_kind"] == "provider_outcome_unknown"
    assert len(gateway.operations) == 1 and not controller.waits
    assert ledger(ctx.drive_root)[-1]["state"] == "unresolved"


def test_original_fallback_ordinal_keeps_account_after_filters(tmp_path, monkeypatch):
    from tests.test_loop_compaction import _ctx

    ctx = _ctx(tmp_path)
    configured = [ctx.active_model, MODEL, MODEL, "openai::third"]
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", ",".join(configured))
    seen = []
    monkeypatch.setattr(loop, "_rebind_context_fit_plan", lambda _plan, *_args, **kwargs: (seen.append(kwargs["model_role"]) or _plan, "max"))
    monkeypatch.setattr(loop, "_call_round_model", lambda call: (None, 0, "max"))
    from ouroboros import fallback_cooldown
    monkeypatch.setattr(fallback_cooldown, "is_cooling_down", lambda *_args: False)
    loop._run_cross_model_fallback_chain(
        llm=ctx.llm, ctx=ctx.tools._ctx, tools=ctx.tools, messages=ctx.messages,
        active_model=ctx.active_model, active_use_local=False, tool_schemas=[], active_effort="medium",
        max_retries=1, drive_logs=ctx.drive_logs, task_id=ctx.task_id, round_idx=1,
        event_queue=None, accumulated_usage={}, task_type="task",
        emit_progress=lambda _text, *, incident=None: None,
        context_fit_plan=ctx.context_fit_plan, active_context_mode="max")
    assert seen == ["fallback:1", "fallback:3"]


def test_same_round_delivery_uses_the_route_changed_inside_model_call(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry

    class Client:
        def default_model(self):
            return MODEL

    def switched(call):
        call.active_model, call.active_use_local = "local-destination", True
        return {"role": "assistant", "content": "finished"}, 0, "max"

    def final(_content, limit, trace, tools, *_args):
        assert limit.active_model == tools._ctx.active_model == "local-destination"
        assert limit.active_use_local is tools._ctx.active_use_local is True
        return "finished", limit.accumulated_usage, trace

    monkeypatch.setattr(loop, "_call_round_model", switched)
    monkeypatch.setattr(loop, "_no_tool_final_answer", final)
    value, _usage, _trace = loop.run_llm_loop(
        messages=[{"role": "user", "content": "go"}],
        tools=ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path), llm=Client(),
        drive_logs=tmp_path / "logs", emit_progress=lambda _text, **_kwargs: None,
        incoming_messages=queue.Queue(), task_id="route-delivery", drive_root=tmp_path)
    assert value == "finished"


TURN = {"route": ROUTE, "format": "codex.turn.v1", "payload": {"turnState": "live-turn"}}


@pytest.fixture
def turn_engine(monkeypatch):
    """A serving engine whose strict request schema accepts the active-turn field."""
    from ouroboros import config, llm_claudexor

    monkeypatch.setattr(llm_claudexor, "owned_engine_version",
                        lambda: config.CLAUDEXOR_MODEL_TURN_STATE_MIN_VERSION)


def _slot(envelope=None):
    from ouroboros.llm_claudexor import ModelTurnState

    return ModelTurnState(deepcopy(envelope) if envelope else None)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_same_route_wait_and_reprepare_update_the_original_slot(main_call, turn_engine, asynchronous):
    """Both real reprepare routes deep-copy their kwargs and must keep ONE owner.

    The synchronous case goes through the live quota wait, the asynchronous one
    through the proven-un-sent account repair and its thread offload.
    """
    ctx, gateway, controller, _events, _decide, _observations = main_call
    landed = {**result(route=ROUTE_B if asynchronous else ROUTE), "nativeContinuation": deepcopy(TURN)}
    gateway.results = [_failed("invalid_continuation", ROUTE_B) if asynchronous
                       else _failed("subscription_window_exhausted"), landed]
    gateway.dispatch = ["not_started", "response_received"]
    slot = _slot()
    ctx.tools._ctx.model_turn_state = slot
    if asynchronous:
        disposition = loop._measure_round_main_fit(ctx, automatic_pass_used=False)
        with controller.register_reprepare("main", lambda values: _reprepare_waiting_main(ctx, values)):
            with ua.bind_physical_attempt_context(loop._physical_context_for_fit(disposition)):
                asyncio.run(ctx.llm.chat_async(ctx.messages, MODEL, model_role="main", model_turn_state=slot))
    else:
        assert _dispatch(ctx)[0]
    # The slot the loop still owns is the one the durable result must have replaced.
    assert ctx.tools._ctx.model_turn_state is slot and slot.envelope == TURN
    assert len(gateway.operations) == 2 and gateway.uploads[-1][0]["nativeContinuation"] is None


def test_a_helper_call_cannot_overwrite_the_running_loop_slot(setup, turn_engine):
    _root, gateway, client = setup
    gateway.results = [{**result(), "nativeContinuation": deepcopy(TURN)}]
    slot = _slot({"route": ROUTE, "format": "codex.turn.v1", "payload": {"turnState": "main-turn"}})
    client.chat([{"role": "user", "content": "compact this"}], MODEL, model_role="light")
    assert slot.envelope["payload"]["turnState"] == "main-turn"
    assert "nativeContinuation" not in gateway.uploads[-1][0]


@pytest.mark.parametrize("destination,use_local", [("openai::alternate", False), ("local-model", True)])
def test_leaving_this_transport_ends_the_turn_and_returning_does_not_revive_it(
    setup, turn_engine, monkeypatch, destination, use_local,
):
    _root, gateway, client = setup
    answer = ({"role": "assistant", "content": "elsewhere"}, {"cost": None, "provider": "other"})
    subscription, remote = {"on": False}, client._chat_remote
    monkeypatch.setattr(client, "_chat_remote",
                        lambda *args, **kwargs: remote(*args, **kwargs) if subscription["on"] else deepcopy(answer))
    monkeypatch.setattr(client, "_chat_local", lambda *_args, **_kwargs: deepcopy(answer))
    slot = _slot(TURN)
    client.chat([{"role": "user", "content": "hi"}], destination, use_local=use_local, model_turn_state=slot)
    assert slot.envelope is None
    subscription["on"] = True
    client.chat([{"role": "user", "content": "hi"}], MODEL, model_turn_state=slot)
    assert gateway.uploads[-1][0]["nativeContinuation"] is None


def _run_loop(tmp_path, monkeypatch, rounds, registry=None):
    """Drive one real run_llm_loop invocation, recording the slot every round saw."""
    from ouroboros.tools.registry import ToolRegistry

    seen, replies = [], iter(rounds)

    def call_round(call):
        slot = call.tools._ctx.model_turn_state
        seen.append((slot, deepcopy(slot.envelope)))
        reply = next(replies)
        return reply(call) if callable(reply) else reply

    def tools_then_steering(calls, _tools, _logs, _task, _executor, messages, *_args):
        messages.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": "done"})
        # Owner steering and host notices arrive as user turns INSIDE one loop;
        # they must never be read as the start of a new transport turn (P5).
        messages.append({"role": "user", "content": "[SYSTEM NOTICE]\nkeep going"})

    monkeypatch.setattr(loop, "_call_round_model", call_round)
    monkeypatch.setattr(loop, "_no_tool_final_answer",
                        lambda _content, limit, trace, *_args: ("finished", limit.accumulated_usage, trace))
    monkeypatch.setattr(loop, "handle_tool_calls", tools_then_steering)
    registry = registry if registry is not None else ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    loop.run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=registry,
        llm=SimpleNamespace(default_model=lambda: MODEL), drive_logs=tmp_path / "logs",
        emit_progress=lambda _text, **_kwargs: None, incoming_messages=queue.Queue(),
        task_id="turn-loop", drive_root=tmp_path)
    return registry, seen


def test_every_round_of_one_loop_shares_its_slot_and_a_next_loop_starts_empty(tmp_path, monkeypatch):
    def tool_round(call):
        call.tools._ctx.model_turn_state.envelope = deepcopy(TURN)  # the engine answered
        return ({"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, 0, "max")

    final_round = ({"role": "assistant", "content": "finished"}, 0, "max")
    registry, seen = _run_loop(tmp_path, monkeypatch, [tool_round, final_round])
    (first_slot, first_value), (second_slot, second_value) = seen
    assert first_slot is second_slot and first_value is None and second_value == TURN
    # A next loop over the SAME context and history — the shape a cold restart
    # takes — opens a new turn instead of replaying the finished one.
    _registry, again = _run_loop(tmp_path, monkeypatch, [final_round], registry=registry)
    assert again[0][0] is not first_slot and again[0][1] is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_legacy_shaped_exchange_leaves_a_live_turn_untouched(setup, monkeypatch, asynchronous):
    """A legacy result is SILENCE about the turn, not a disclaimer (BIBLE P1).

    The version floor can be closed while the caller already holds a token: an
    engine this process has never proven, a slot armed by an earlier proven one.
    Such a request asks nothing about the turn and its result answers nothing,
    so adopting its absent field would discard a token the engine never dropped.
    """
    from ouroboros import llm_claudexor

    _root, gateway, client = setup
    monkeypatch.setattr(llm_claudexor, "owned_engine_version", lambda: "")
    slot, messages = _slot(TURN), [{"role": "user", "content": "hi"}]
    if asynchronous:
        asyncio.run(client.chat_async(messages, MODEL, model_turn_state=slot))
    else:
        client.chat(messages, MODEL, model_turn_state=slot)
    assert "nativeContinuation" not in gateway.uploads[-1][0]
    assert slot.envelope == TURN


@pytest.mark.parametrize("asynchronous", [False, True])
def test_an_opted_in_exchange_still_adopts_and_then_clears_the_turn(setup, turn_engine, asynchronous):
    """Both entrypoints keep the opt-in contract the legacy guard sits beside."""
    _root, gateway, client = setup
    gateway.results = [{**result(), "nativeContinuation": deepcopy(TURN)}, result()]
    gateway.dispatch = ["response_received"] * 2
    slot, messages = _slot(), [{"role": "user", "content": "hi"}]

    def send():
        if asynchronous:
            asyncio.run(client.chat_async(messages, MODEL, model_turn_state=slot))
        else:
            client.chat(messages, MODEL, model_turn_state=slot)

    send()
    assert slot.envelope == TURN and gateway.uploads[0][0]["nativeContinuation"] is None
    send()
    # The engine answered without an envelope: the turn is stateless, not stale.
    assert gateway.uploads[-1][0]["nativeContinuation"] == TURN and slot.envelope is None
