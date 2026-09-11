"""Main-loop subscription cache-affinity regressions."""

import queue

from ouroboros.llm_claudexor import _request
from ouroboros.loop_llm_call import call_llm_with_retry


def test_claudexor_request_projects_explicit_affinity_to_cache_key():
    payload = _request(
        {"source": "codex", "resolved_model": "model"},
        [{"role": "user", "content": "work"}],
        None,
        {"cache_affinity": "execution-7", "model_account_override": ""},
    )
    assert payload["options"]["cacheKey"] == "execution-7"


def test_main_loop_scopes_execution_affinity_to_claudexor(tmp_path):
    captured = []

    class LLM:
        def chat(self, **kwargs):
            captured.append(kwargs)
            return ({"content": "done", "tool_calls": [], "finish_reason": "stop"}, {
                "provider": "fixture", "resolved_model": kwargs["model"], "cost": 0.0,
                "prompt_tokens": 1, "completion_tokens": 1,
            })

    logs = tmp_path / "logs"
    logs.mkdir()
    for model in ("claudexor::codex=model", "openrouter::openai/model"):
        usage = {"execution_id": "execution-7"}
        message, _cost = call_llm_with_retry(
            LLM(), [{"role": "user", "content": "work"}], model, None, "medium", 1,
            logs, "task", 1, queue.Queue(), usage,
        )
        assert message["content"] == "done"

    assert captured[0]["cache_affinity"] == "execution-7"
    assert captured[1]["cache_affinity"] == ""


def test_main_loop_projects_claudexor_options_outside_the_route(tmp_path):
    class LLM:
        def chat(self, **_kwargs):
            return ({"content": "done", "tool_calls": [], "finish_reason": "stop"}, {
                "provider": "claudexor", "resolved_model": "claudexor::codex=model",
                "cost": 0.0, "prompt_tokens": 1, "completion_tokens": 1,
                "claudexor": {"route": {"credentialProfileId": "account-a"},
                               "requested_options": {"reasoningEffort": "high"},
                               "applied_options": {"reasoningEffort": "medium"},
                               "options_honored": "mismatch"},
            })

    logs = tmp_path / "logs"
    logs.mkdir()
    usage = {"execution_id": "execution-7"}
    call_llm_with_retry(LLM(), [{"role": "user", "content": "work"}],
                        "claudexor::codex=model", None, "high", 1,
                        logs, "task", 1, queue.Queue(), usage)

    assert usage["_model_route"] == {"credentialProfileId": "account-a"}
    assert usage["_options"] == {
        "requested_options": {"reasoningEffort": "high"},
        "applied_options": {"reasoningEffort": "medium"},
        "options_honored": "mismatch",
    }
