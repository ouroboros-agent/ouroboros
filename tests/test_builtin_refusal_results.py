"""Real builtin refusals keep producer facts through the string handler ABI."""

from types import SimpleNamespace

import pytest

from ouroboros.tools.tool_result import (
    LegacyTextResultAdapter,
    ToolResult,
    _install_tool_result_sidecar,
    _published_tool_result,
    _restore_tool_result_sidecar,
)


def _call(ctx, function, *args, **kwargs):
    sentinel = object()
    token = _install_tool_result_sidecar(ctx, sentinel)
    try:
        text = function(ctx, *args, **kwargs)
        result = _published_tool_result(ctx, sentinel)
        assert isinstance(result, ToolResult)
        assert result.text == (text["message"] if isinstance(text, dict) else text)
        return result
    finally:
        _restore_tool_result_sidecar(token)


@pytest.mark.parametrize("producer", ["commit", "review_only"])
def test_empty_commit_message_is_an_argument_refusal_before_git(producer, monkeypatch):
    from ouroboros.tools import git, git_review_cycle

    monkeypatch.setattr(git, "_reset_commit_review_state", lambda _ctx: None)
    ctx = SimpleNamespace()
    function = git._repo_commit_push if producer == "commit" else git_review_cycle._run_non_committing_review_cycle
    result = _call(ctx, function, "")
    assert result.status == "error"
    assert result.code == "TOOL_ARG_ERROR"
    assert result.text == "⚠️ ERROR: commit_message must be non-empty."


@pytest.mark.parametrize("stored,code", [
    ({}, "LEGACY_UNAVAILABLE"),
    ({"status": "completed"}, "LEGACY_BLOCKED"),
    ({"status": "pending"}, "LEGACY_BLOCKED"),
])
def test_forwarding_refuses_unaddressable_tasks_before_mailbox_write(tmp_path, monkeypatch, stored, code):
    from ouroboros.tools import core
    import ouroboros.owner_mailbox as mailbox
    import ouroboros.task_status as task_status

    writes = []
    monkeypatch.setattr(core, "canonical_data_root", lambda _ctx: tmp_path)
    monkeypatch.setattr(task_status, "load_effective_task_result", lambda *_: stored)
    monkeypatch.setattr(mailbox, "write_task_message", lambda *_a, **_k: writes.append(1))
    result = _call(SimpleNamespace(drive_root=tmp_path), core._forward_to_worker, "missing-fixture", "hello")
    assert result.status != "ok"
    assert result.code == code
    assert writes == []


@pytest.mark.parametrize("function,arguments", [
    ("_update_scratchpad", {"content": ""}),
    ("_update_identity", {"content": "short"}),
    ("_send_user_message", {"text": ""}),
])
def test_invalid_cognitive_or_message_arguments_are_not_success(function, arguments):
    from ouroboros.tools import control_runtime

    result = _call(SimpleNamespace(current_chat_id=1), getattr(control_runtime, function), **arguments)
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")


@pytest.mark.parametrize("arguments,code", [
    ({}, "TOOL_ARG_ERROR"),
    ({"run_at": "not-a-date", "objective": "check"}, "TOOL_ARG_ERROR"),
    ({"run_at": "2099-01-01T00:00:00Z"}, "TOOL_ARG_ERROR"),
])
def test_followup_rejections_never_schedule(tmp_path, monkeypatch, arguments, code):
    from ouroboros.tools import followup
    from supervisor import queue

    writes = []
    monkeypatch.setattr(queue, "upsert_scheduled_task", lambda *_a, **_k: writes.append(1))
    ctx = SimpleNamespace(task_id="root", drive_root=tmp_path, task_metadata={})
    result = _call(ctx, followup._handle_schedule_followup, **arguments)
    assert (result.status, result.code) == ("error", code)
    assert writes == []


def test_knowledge_and_registry_argument_refusals_leave_files_untouched(tmp_path):
    from ouroboros.tools import knowledge, memory_tools

    ctx = SimpleNamespace(drive_root=tmp_path)
    for function, args in (
        (knowledge._knowledge_read, ("../private",)),
        (knowledge._knowledge_write, ("valid", "content", "invalid-mode")),
        (memory_tools._memory_update_registry, ("../private", "content")),
    ):
        result = _call(ctx, function, *args)
        assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")
    assert list(tmp_path.iterdir()) == []


def test_presence_contract_refusal_and_valid_completion_remain_distinct():
    from ouroboros.tools.presence import _finish_presence

    ctx = SimpleNamespace(task_contract={})
    result = _call(ctx, _finish_presence, "message", "hello")
    assert result.status == "unavailable"
    assert not hasattr(ctx, "_presence_completion")
    ctx.task_contract = {"capability_ceiling": {}}
    assert _finish_presence(ctx, "message", "hello").startswith("PRESENCE_COMPLETION_RECORDED")
    assert ctx._presence_completion == {"outcome": "message", "message": "hello"}


def test_real_producer_failure_survives_registry_dispatch(tmp_path, monkeypatch):
    from ouroboros.tools import control_runtime
    from ouroboros.tools.registry import ToolEntry, ToolRegistry
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *_a, **_k: (True, ""))
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.current_chat_id = 1
    registry.register(ToolEntry("fixture_builtin", {
        "name": "fixture_builtin", "description": "fixture",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }, lambda ctx: control_runtime._send_user_message(ctx, "")))
    result = registry.execute_result("fixture_builtin", {})
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")
    assert result.text == "⚠️ Empty message."


@pytest.mark.parametrize("text,code", [
    ("⚠️ WARNING: untracked files remain", "LEGACY_WARNING"),
    ("⚠️ REVIEW_BLOCKED: address findings", "REVIEW_BLOCKED"),
    ("⚠️ GIT_ERROR: inspect the refusal", "GIT_ERROR"),
])
def test_existing_warning_and_review_policy_are_not_blanket_reclassified(text, code):
    result = LegacyTextResultAdapter.from_text("fixture", text)
    assert (result.status, result.code, result.text) == ("ok", code, text)
