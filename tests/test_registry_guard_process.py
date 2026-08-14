from __future__ import annotations

import inspect
import pathlib
from types import SimpleNamespace

import ouroboros.tools.registry as registry_module
import ouroboros.tools.registry_guard_process as process_guard
from ouroboros.artifacts import task_artifact_dir_path
from ouroboros.tools.registry import ToolContext, ToolRegistry


_FUNCTION_SIGNATURES = {
    "_detect_runtime_mode_elevation": "(text_lower: 'str') -> 'bool'",
    "_subagent_shell_targets_secret": "(cmd_path_lower: 'str') -> 'bool'",
    "_detect_mutative_toggle_self_change": "(text_lower: 'str') -> 'bool'",
    "_detect_evolution_owner_control_self_change": "(text_lower: 'str') -> 'bool'",
    "_detect_context_mode_self_lowering": "(text_lower: 'str') -> 'bool'",
    "_trusted_read_head": "(token: 'str') -> 'str'",
    "_denied_read_option": "(token: 'str', denied: 'frozenset') -> 'bool'",
    "_is_pure_read_inspection": "(text_lower: 'str') -> 'bool'",
    "_detect_scope_review_floor_self_lowering": "(text_lower: 'str', *, writeish: 'bool' = True) -> 'bool'",
    "_detect_safety_mode_self_lowering": "(text_lower: 'str') -> 'bool'",
    "_detect_owner_skill_attest_self_call": "(text_lower: 'str') -> 'bool'",
    "_mentions_skill_owner_state": "(text_lower: 'str') -> 'bool'",
    "_mentions_detached_process": "(text_lower: 'str') -> 'bool'",
    "_run_shell_safety_check": "(self, args: 'Dict[str, Any]', runtime_mode: 'str', binding: 'Any' = None) -> 'Optional[str]'",
}

_CONSTANT_CARDINALITIES = {
    "_SUBAGENT_SHELL_SECRET_MARKERS": 17,
    "_READ_ONLY_INSPECTION_COMMANDS": 41,
    "_COMMAND_HEAD_WRAPPERS": 11,
    "_READ_ONLY_GIT_SUBCOMMANDS": 11,
    "_SEARCH_TOOL_EXEC_OPTIONS": 4,
    "_DENIED_READ_OPTIONS": 11,
    "_TRUSTED_EXECUTABLE_DIRS": 6,
    "_NESTED_EXECUTION_MARKERS": 4,
    "_NESTED_EXECUTION_TOKENS": 6,
    "_SKILL_OWNER_STATE_STEMS": 12,
    "_DETACHED_PROCESS_MARKERS": 5,
}

_RETIRED_REGISTRY_DEPENDENCIES = frozenset({
    "LIGHT_SHELL_WRITER_COMMANDS",
    "SKILL_OWNER_STATE_STEMS",
    "build_resolved_resource_binding",
    "interpreter_family",
    "light_shell_repo_mutation",
    "protected_artifact_shell_block_reason",
    "runtime_data_guard_targets",
    "shell_command_string",
    "strip_leading_env_assignments",
    "sudo_noninteractive_violation",
    "unwrap_env_argv",
    "workspace_executor_state_write_block",
    "writer_target_tokens",
})


def test_process_guard_owner_surface_is_exact_and_retired_from_registry():
    for name, signature in _FUNCTION_SIGNATURES.items():
        assert str(inspect.signature(getattr(process_guard, name))) == signature
    for name, cardinality in _CONSTANT_CARDINALITIES.items():
        assert len(getattr(process_guard, name)) == cardinality

    moved_module_names = (set(_FUNCTION_SIGNATURES) - {"_run_shell_safety_check"}) | set(
        _CONSTANT_CARDINALITIES
    ) | _RETIRED_REGISTRY_DEPENDENCIES
    assert all(not hasattr(registry_module, name) for name in moved_module_names)
    assert not hasattr(ToolRegistry, "_run_shell_safety_check")


class _RegistryStub:
    def __init__(self, root: pathlib.Path):
        self.acting = False
        self._ctx = SimpleNamespace(
            drive_root=root,
            repo_dir=root,
            task_id="task-process-guard",
            is_workspace_mode=lambda: False,
            task_drive_root=lambda: root / "task_drive",
        )
        self.work_dir = root

    def _acting_self_worktree(self):
        return False

    def _is_acting_subagent(self):
        return self.acting

    def _is_local_readonly_subagent(self):
        return False

    def _resolved_shell_cwd(self, _args, _binding):
        return self.work_dir

    def _workspace_shell_write_block(self, *_args):
        return None

    def _protected_shell_block(self, *_args):
        return None

    def _shell_git_and_runtime_block(self, *_args):
        return None


def test_process_guard_denials_preserve_exact_text(tmp_path, monkeypatch):
    stub = _RegistryStub(tmp_path)
    monkeypatch.setattr(process_guard, "protected_artifact_shell_block_reason", lambda *args, **kwargs: None)
    monkeypatch.setattr(process_guard, "workspace_executor_state_write_block", lambda *args, **kwargs: None)

    def check(command, mode="advanced"):
        return process_guard._run_shell_safety_check(stub, {"cmd": command}, mode, ())

    assert check(["sudo", "true"]) == (
        "⚠️ SUDO_INTERACTIVE_BLOCKED: sudo must be noninteractive. Use sudo -n for commands "
        "that can run without a password; if sudo -n fails, report validation/install blocked "
        "by environment."
    )

    stub.acting = True
    assert check("cat data/settings.json") == (
        "⚠️ SUBAGENT_SECRET_READ_BLOCKED: subagents may not read Ouroboros secrets, "
        "credentials, or owner-control state via shell. Use the gated read_file tool "
        "(which denies secrets) for any inspection you actually need."
    )
    stub.acting = False

    cases = (
        (
            'save_settings({"ouroboros_runtime_mode":"pro"})',
            "⚠️ ELEVATION_BLOCKED: shell command pattern looks like an OUROBOROS_RUNTIME_MODE "
            "elevation attempt (mentions ``save_settings`` together with ``OUROBOROS_RUNTIME_MODE``, "
            "or invokes ``ouroboros.config.save_settings`` directly). Runtime mode is "
            "owner-controlled — change it by stopping the agent and editing settings.json "
            "directly, then restart.",
        ),
        (
            'save_settings({"ouroboros_context_mode":"low"})',
            "⚠️ CONTEXT_MODE_SELF_LOWERING_BLOCKED: shell command pattern looks like an attempt "
            "to lower OUROBOROS_CONTEXT_MODE to low through settings.json or /api/owner/context-mode. "
            "Context mode is owner-controlled — ask the owner to change the Low/Max toggle or edit "
            "settings while the agent is stopped.",
        ),
        (
            "python -c 'post /api/owner/scope-review-floor'",
            "⚠️ SCOPE_REVIEW_FLOOR_SELF_LOWERING_BLOCKED: shell command pattern reaches "
            "OUROBOROS_SCOPE_REVIEW_FLOOR through settings.json, /api/settings, or "
            "/api/owner/scope-review-floor from something other than a pure read. The floor is a "
            "deprecated, enforcement-inert owner setting (BIBLE P3 scope-review applicability "
            "follows the owner context mode) — it stays owner-only, and the agent must not write "
            "owner settings through any channel. Ask the owner to change it via the dedicated "
            "/api/owner/scope-review-floor endpoint, or stop the agent and edit settings.json "
            "directly. Pure source inspection (grep/rg/cat/jq/git grep) is allowed; an interpreter "
            "or HTTP client naming the endpoint is not, whatever verb it spells.",
        ),
        (
            'save_settings({"ouroboros_safety_mode":"off"})',
            "⚠️ SAFETY_MODE_SELF_LOWERING_BLOCKED: shell command pattern looks like an attempt to "
            "change OUROBOROS_SAFETY_MODE (e.g. to ``light``/``off``) through settings.json, "
            "/api/settings, or /api/owner/safety-mode. LLM-safety coverage is owner-controlled "
            "(BIBLE P3) — the agent must not reduce its own supervision. Ask the owner to change "
            "it via the dedicated /api/owner/safety-mode endpoint, or stop the agent and edit "
            "settings.json directly.",
        ),
        (
            "curl -x post /api/owner/skills/alpha/attest-review",
            "⚠️ OWNER_SKILL_ATTESTATION_SELF_CALL_BLOCKED: shell command pattern looks like an "
            "attempt to loopback-POST /api/owner/skills/<skill>/attest-review. Owner-attestation "
            "skips the expensive LLM skill review and is OWNER-ONLY — the agent must not "
            "self-attest its own skill to bypass the immune system's review. Ask the owner to "
            "attest it from the Skills UI.",
        ),
        (
            'save_settings({"ouroboros_allow_mutative_subagents":"true"})',
            "⚠️ ELEVATION_BLOCKED: OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS is owner-controlled (it "
            "grants subagents write power against the live body). Change it by stopping the agent "
            "and editing settings.json directly, then restart — the agent must not self-enable "
            "mutative subagents.",
        ),
        (
            'save_settings({"ouroboros_post_task_evolution":"true"})',
            "⚠️ ELEVATION_BLOCKED: the self-evolution controls (OUROBOROS_POST_TASK_EVOLUTION and "
            "OUROBOROS_EVOLUTION_PERSISTENT_OBJECTIVE) are owner-controlled — they enable or "
            "steer self-modification cycles. Change them via the owner Settings UI, or stop the "
            "agent and edit settings.json directly — the agent must not self-set evolution controls.",
        ),
        (
            "echo state/skills/alpha/review.json",
            "⚠️ SKILL_STATE_WRITE_BLOCKED: skill review, enablement, grants, and marketplace "
            "provenance are owner/review controlled state. Use skill_review, toggle_skill/the "
            "Skills UI, or the desktop launcher confirmation flow.",
        ),
        (
            "nohup echo state/skills/alpha/unknown.json",
            "⚠️ SKILL_STATE_WRITE_BLOCKED: detached shell processes must not target skill state "
            "directories. Use the reviewed skill lifecycle tools instead.",
        ),
        (
            "gh repo create example",
            "⚠️ SAFETY_VIOLATION: Creating/deleting GitHub repositories requires admin approval.",
        ),
        (
            "gh auth login",
            "⚠️ SAFETY_VIOLATION: Modifying GitHub authentication is not permitted.",
        ),
    )
    for command, expected in cases:
        assert check(command) == expected

    monkeypatch.setattr(process_guard, "light_shell_repo_mutation", lambda *args, **kwargs: True)
    assert check("echo ok", "light") == (
        "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses shell commands that mutate the "
        "Ouroboros repository. For external deliverables, run with cwd under user_files "
        "(for example /Users/<you>/Desktop), root=artifact_store, or root=task_drive. Switch "
        "to advanced/pro only for reviewed Ouroboros self-modification."
    )

    monkeypatch.setattr(process_guard, "light_shell_repo_mutation", lambda *args, **kwargs: False)
    monkeypatch.setattr(process_guard, "shell_has_write_indicator", lambda _command: True)
    monkeypatch.setattr(process_guard, "runtime_data_guard_targets", lambda *args, **kwargs: ["/blocked"])
    task_drive = tmp_path / "task_drive"
    artifact_dir = task_artifact_dir_path(tmp_path, "task-process-guard", create=False)
    assert check("echo ok", "light") == (
        "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light blocks process commands that write under "
        "runtime_data paths outside this task's own roots. This task's real roots are: "
        f"artifact_store={artifact_dir}, task_drive={task_drive} — staged attachments live "
        f"under {artifact_dir / 'attachments'}. Use those absolute paths in scripts, or "
        "root=artifact_store / root=task_drive / root=user_files in file tools. Blocked paths: "
        "/blocked"
    )


def test_registry_dispatch_calls_process_owner_once_before_safety_and_handler(
    tmp_path, monkeypatch,
):
    from ouroboros import safety

    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    ctx = ToolContext(repo_dir=repo, drive_root=data, task_id="task-process-dispatch")
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry.set_context(ctx)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")

    calls: list[str] = []
    denied = {"value": True}

    def guard(owner, args, runtime_mode, binding=None):
        assert owner is registry
        assert args["cmd"] == ["echo", "ok"]
        assert runtime_mode == "advanced"
        assert binding is not None
        calls.append("guard")
        return "⚠️ TEST_PROCESS_BLOCKED" if denied["value"] else None

    def check_safety(*_args, **_kwargs):
        calls.append("safety")
        return True, ""

    def handler(_ctx, contract_kind, check, _resolved_binding=None, **_kwargs):
        assert contract_kind == "explicit_command"
        assert check == ["echo", "ok"]
        assert _resolved_binding is not None
        calls.append("handler")
        return "OK"

    monkeypatch.setattr(process_guard, "_run_shell_safety_check", guard)
    monkeypatch.setattr(safety, "check_safety", check_safety)
    registry.override_handler("verify_and_record", handler)
    args = {"contract_kind": "explicit_command", "check": ["echo", "ok"]}

    assert registry.execute("verify_and_record", dict(args)) == "⚠️ TEST_PROCESS_BLOCKED"
    assert calls == ["guard"]

    calls.clear()
    denied["value"] = False
    assert registry.execute("verify_and_record", dict(args)) == "OK"
    assert calls == ["guard", "safety", "handler"]
