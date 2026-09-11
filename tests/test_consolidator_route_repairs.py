"""Cross-owner regressions: actual fit/cache, local wire and typed interruption."""
from copy import deepcopy
import json
from math import ceil
import queue
from types import SimpleNamespace

import pytest

from ouroboros import capability_evidence as ce, config, consolidator as c, context_fit
from ouroboros.llm import LLMClient
from ouroboros.llm_claudexor import ClaudexorModelError
from ouroboros.model_wait import ModelWaitInterrupted
from tests.test_consolidator_context_fit import _LLM, _paths, _source, _summary, _write_chat


MODEL = "claudexor::test-source=exact-model"


def _route(profile="account-a", fingerprint="identity-a"):
    return dict(source="test-source", model="exact-model",
                credentialProfileId=profile, accountFingerprint=fingerprint)


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    settings = {"OUROBOROS_MODEL": MODEL, "OUROBOROS_MODEL_LIGHT": MODEL,
                "OUROBOROS_MODEL_ACCOUNTS": {"main": "main-account", "light": ""},
                "OUROBOROS_MODEL_CONTEXT_WINDOWS": {}}
    state = SimpleNamespace(settings=settings, catalog_calls=[], resolutions=[], window=17000,
                            route=_route(), timestamp=ce.utc_now_iso())
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setenv("OUROBOROS_MODEL_ACCOUNTS", json.dumps(settings["OUROBOROS_MODEL_ACCOUNTS"]))
    monkeypatch.setattr(c, "_consolidation_route", lambda: (MODEL, False))
    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(ce, "_DENSITY_MEMO", {})

    def catalog(source, credential_profile_id=None, *, requested_model=None):
        state.catalog_calls.append((source, credential_profile_id, requested_model))
        return {**state.route, "observedAt": state.timestamp, "provenance": "fixture metadata transport",
                "models": [{"id": "exact-model", "contextWindow": state.window,
                            "maxContextWindow": state.window, "effectiveContextWindow": 1}]}

    monkeypatch.setattr(LLMClient, "claudexor_model_catalog", staticmethod(catalog))
    monkeypatch.setattr(ce, "_generative_probe_window", lambda *_a, **_k: pytest.fail("generation probe"))
    real_resolve = context_fit.resolve_context_fit_route

    def resolve(task, *, allow_fetch):
        resolved = real_resolve(task, allow_fetch=allow_fetch)
        state.resolutions.append((deepcopy(task), resolved[1]))
        return resolved

    monkeypatch.setattr(context_fit, "resolve_context_fit_route", resolve)
    return state


def _prime(capacity, *, observed=None):
    return context_fit.resolve_context_fit_route(
        {"model": MODEL, "model_role": "light", "use_local_model": False,
         "model_route": observed}, allow_fetch=True)[1]


def test_local_preflight_matches_actual_wire_normalization(capacity, monkeypatch):
    from ouroboros import local_model

    capacity.settings["OUROBOROS_MODEL"] = "local-fixture"
    monkeypatch.setattr(c, "_consolidation_route", lambda: ("local-fixture", True))
    monkeypatch.setattr(local_model, "get_manager", lambda: SimpleNamespace(get_context_length=lambda: 16384))
    client = LLMClient(api_key="unused")
    sent = []

    def create(**kwargs):
        sent.append(deepcopy(kwargs))
        return SimpleNamespace(model_dump=lambda: {
            "choices": [{"message": {"role": "assistant", "content": "local summary"}}],
            "usage": {"prompt_tokens": 250, "completion_tokens": 20, "total_tokens": 270}})

    monkeypatch.setattr(client, "_get_local_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    # Establish what the real wire owner does, before asking consolidation to fit.
    client.chat(messages=[{"role": "user", "content": "source"}], model="local-fixture",
                max_tokens=16384, use_local=True)
    assert sent.pop()["max_tokens"] == 4096
    evidence = context_fit.resolve_context_fit_route(
        {"model": "local-fixture", "model_role": "light", "use_local_model": True}, allow_fetch=True)[1]
    assert evidence.window_tokens == 16384

    content, usage = _summary(client)

    assert content == "local summary"
    assert len(sent) == 1 and sent[0]["max_tokens"] == 4096
    assert "identity" in sent[0]["messages"][0]["content"]
    assert not usage.get("_consolidation_errors")


@pytest.mark.parametrize("pin", ["", "account-a"])
def test_auto_and_pin_recover_fresh_exact_account_capacity(capacity, monkeypatch, pin):
    capacity.settings["OUROBOROS_MODEL_ACCOUNTS"]["light"] = pin
    monkeypatch.setenv("OUROBOROS_MODEL_ACCOUNTS", json.dumps(capacity.settings["OUROBOROS_MODEL_ACCOUNTS"]))
    expected = _prime(capacity)
    assert expected.credential_profile_id == "account-a" and ce.is_known(expected, require_fresh=True)
    capacity.resolutions.clear()
    llm = _LLM()
    source = "whole source Ж🙂 " * 2000

    content, _ = _summary(llm, source)

    assert content and len(llm.calls) > 1
    assert _source(llm.accepted) == source
    assert all(call["model_account_override"] == pin and call["model_role"] == "light" for call in llm.calls)
    assert all(context_fit.estimate_context_prompt_tokens(call["messages"]) + 16384 <= 17000 for call in llm.calls)
    assert all(ev.route_fp == expected.route_fp for _, ev in capacity.resolutions)
    # Discovery is carried into subsequent parts; exact identity hits the real cache.
    assert len(capacity.catalog_calls) == 2


@pytest.mark.parametrize("change", ["stale", "missing_identity", "missing_window"])
def test_catalog_without_fresh_complete_evidence_stays_unknown(capacity, change):
    if change == "stale":
        capacity.timestamp = "2020-01-01T00:00:00Z"
    elif change == "missing_identity":
        capacity.route["accountFingerprint"] = ""
    else:
        capacity.window = None
    llm = _LLM()
    assert _summary(llm)[0]
    assert len(llm.calls) == 1
    assert not ce.is_known(capacity.resolutions[-1][1], require_fresh=True)


@pytest.mark.parametrize("receipt", ["success", "refusal"])
@pytest.mark.parametrize("profile", ["account-a", "account-b"])
def test_actual_rotated_account_rebinds_next_part_to_its_cache(capacity, receipt, profile):
    capacity.route, capacity.window = _route(profile, "identity-b"), 16800
    expected = _prime(capacity)
    capacity.route, capacity.window = _route(), 18000
    _prime(capacity)
    capacity.resolutions.clear()

    def rotate(llm, _prompt):
        if len(llm.calls) == 1:
            actual = _route(profile, "identity-b")
            if receipt == "refusal":
                error = ClaudexorModelError({"code": "context_length_exceeded", "message": "too long"}, route=actual)
                error.physical_attempt_capture = SimpleNamespace(state="settled")
                raise error
            llm.accepted.append(_prompt)
            return {"content": "first summary"}, {"cost": None, "claudexor": {"route": actual}}

    llm = _LLM(effect=rotate)
    source = "complete entry Ж🙂 " * 1500
    assert _summary(llm, source)[0]
    assert _source(llm.accepted) == source
    observed = [(task, ev) for task, ev in capacity.resolutions
                if (task.get("model_route") or {}).get("accountFingerprint") == "identity-b"]
    assert observed and all(ev.route_fp == expected.route_fp for _, ev in observed)
    assert all(call["model_account_override"] == "" for call in llm.calls)
    assert all(context_fit.estimate_context_prompt_tokens(call["messages"]) + 16384 <= 16800 for call in llm.calls[1:])


def test_exact_account_density_is_read_from_the_existing_evidence_store(tmp_path, capacity):
    capacity.window = 18000
    evidence = _prime(capacity)
    ce.record_token_density(tmp_path, MODEL, route_fp=evidence.route_fp,
                            prompt_chars=400000, prompt_tokens=200000, basis="bounded_proxy")
    density = context_fit._route_calibration_ratio(None, evidence.route_fp, MODEL)
    assert density == 2.0
    source = "full dense source Ж🙂 " * 2000
    llm = _LLM()
    content, usage = _summary(llm, source)
    assert content and len(llm.calls) > 1 and _source(llm.accepted) == source
    assert all(ceil(context_fit.estimate_context_prompt_tokens(call["messages"]) * density) + 16384 <= 18000
               for call in llm.calls)
    assert all(failure["measurement_density"] == density for failure in usage["_consolidation_errors"])


def test_quota_wait_reprepares_auto_with_the_new_accounts_capacity(tmp_path, capacity, monkeypatch):
    from ouroboros import model_wait

    capacity.window = 100000
    client = LLMClient(api_key="unused")
    calls, accepted = [], []
    monkeypatch.setattr(client, "claudexor_model_sources", lambda: {
        "sources": [{"id": "test-source", "credentialHarness": "fixture"}]})

    def remote(_target, messages, tools, _effort, _max_tokens, _choice, _temperature, **kwargs):
        calls.append(deepcopy(messages))
        assert kwargs["model_role"] == "light" and kwargs["model_account_override"] == ""
        if len(calls) == 1:
            capacity.route, capacity.window = _route("account-b", "identity-b"), 17000
            error = ClaudexorModelError({"code": "subscription_window_exhausted", "message": "quota"}, route=_route())
            error.physical_attempt_capture = SimpleNamespace(state="settled")
            raise error
        assert context_fit.estimate_context_prompt_tokens(messages, tools) + 16384 <= 17000
        accepted.append(messages[0]["content"])
        return {"content": "summary"}, {"cost": None, "claudexor": {"route": capacity.route}}

    monkeypatch.setattr(client, "_chat_remote", remote)
    source = "all source Ж🙂 " * 1500
    with model_wait.task_model_wait_scope(
        task={"id": "consolidation-fixture"}, drive_root=tmp_path, event_queue=queue.Queue(),
        worker_slot_held=False, owner_control=lambda: None,
    ) as waiter:
        content, usage = _summary(client, source)
    assert content and len(calls) > 2 and usage["cost"] is None
    assert _source(accepted) == source
    assert all(row["resolution"] == "resource_available" for row in waiter.waits.values())
    assert any(ev.credential_profile_id == "account-b" and ev.window_tokens == 17000
               for _, ev in capacity.resolutions)


def test_unavailable_route_metadata_keeps_an_ordinary_call(capacity, monkeypatch):
    def unavailable():
        raise OSError("settings read unavailable")

    monkeypatch.setattr(config, "load_settings", unavailable)
    llm = _LLM()
    assert _summary(llm)[0]
    assert len(llm.calls) == 1


def test_oversized_era_keeps_all_original_blocks(tmp_path, capacity):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, text_size=0)
    originals = [{"range": "2025-01-01", "type": "summary", "message_count": 100,
                  "content": f"old-{index} " * 3000} for index in range(10)]
    c.atomic_write_json(blocks, originals)
    llm = _LLM()
    usage = c.consolidate(chat, blocks, meta, llm)
    assert json.loads(blocks.read_text())[:10] == originals
    assert len(json.loads(blocks.read_text())) == 11
    assert json.loads(meta.read_text())["last_consolidated_offset"] == 100
    assert usage["_consolidation_errors"][-1]["kind"] == "context_overflow"
    assert usage["_consolidation_errors"][-1]["preflight_only"]
    assert all("Compress these older memory blocks" not in prompt for prompt in llm.accepted)


@pytest.mark.parametrize("interrupt", ["quota", "owner", "deadline"])
def test_learned_refusal_survives_typed_interruption_before_next_cycle(tmp_path, capacity, interrupt):
    capacity.window = None
    chat, blocks, meta = _paths(tmp_path)
    rows = _write_chat(chat, text_size=30)
    original = chat.read_bytes()
    error = (ClaudexorModelError({"code": "subscription_window_exhausted", "message": "quota"}, route=_route())
             if interrupt == "quota" else ModelWaitInterrupted(
                 "finalize_requested" if interrupt == "owner" else "deadline", role="light"))

    def fail(llm, _prompt):
        if len(llm.calls) == 1:
            refusal = ClaudexorModelError({"code": "context_length_exceeded", "message": "too long"}, route=_route())
            refusal.physical_attempt_capture = SimpleNamespace(state="settled")
            raise refusal
        raise error

    first = _LLM(effect=fail)
    with pytest.raises(type(error)) as caught:
        c.consolidate(chat, blocks, meta, first)
    assert caught.value is error and len(first.calls) == 2
    assert not blocks.exists() and chat.read_bytes() == original
    saved = json.loads(meta.read_text())
    assert saved.get("last_consolidated_offset", 0) == 0
    original_size = len(first.calls[0]["messages"][0]["content"].encode("utf-8"))
    assert saved["consolidation_retry"]["input_limit"]["input_bytes"] == original_size - 1

    second = _LLM()
    c.consolidate(chat, blocks, meta, second)
    assert len(second.calls[0]["messages"][0]["content"].encode("utf-8")) < original_size
    assert _source(second.accepted) == c._format_entries_for_block(rows)
    assert json.loads(meta.read_text())["last_consolidated_offset"] == 100
    assert chat.read_bytes() == original


@pytest.mark.parametrize("change", ["source", "route"])
def test_interrupted_refusal_bound_invalidates_for_changed_source_or_route(tmp_path, capacity, change):
    capacity.window = None
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, text_size=0)

    def fail(llm, _prompt):
        if len(llm.calls) == 1:
            raise ClaudexorModelError({"code": "context_length_exceeded", "message": "too long"}, route=_route())
        raise ModelWaitInterrupted("deadline", role="light")

    first = _LLM(effect=fail)
    with pytest.raises(ModelWaitInterrupted):
        c.consolidate(chat, blocks, meta, first)
    assert meta.exists()
    if change == "route":
        capacity.route = _route("account-b", "identity-b")
    second = _LLM()
    c.consolidate(chat, blocks, meta, second, "changed identity" if change == "source" else "")
    assert len(second.calls) == 1
    assert json.loads(meta.read_text())["last_consolidated_offset"] == 100
