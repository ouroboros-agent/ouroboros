"""Focused owner/facade contracts for the ToolRegistry extraction."""

from __future__ import annotations

import inspect


def test_registry_core_extraction_preserves_only_proven_facades():
    """Execution moves once; the compatibility module exports only proven ABI."""
    import ouroboros.tools as tools_package
    from ouroboros.tools import (
        registry,
        registry_core,
        registry_guards,
        tool_catalog,
        tool_context,
        tool_resolution,
        tool_result,
    )

    resolution_names = {
        "_GENERIC_VCS_TARGET_TOOLS",
        "_PATH_NORMALIZED_TOOLS",
        "_PROCESS_TARGET_TOOLS",
        "_SKILL_LIFECYCLE_TARGET_TOOLS",
        "_TARGET_BINDING_OPERATIONS",
        "_VERIFY_RUN_KINDS",
        "_binding_items",
        "_binding_set_is_light_restricted",
        "_binding_set_targets_system_repo",
        "_binding_state_drive_root",
        "_build_builtin_target_binding",
        "_coerce_real_path",
        "_normalize_dispatch_path_args",
        "_target_binding_operation",
        "active_repo_dir_for",
        "system_repo_dir_for",
    }
    guard_names = {
        "_EPHEMERAL_ALLOWED_TOOLS",
        "_GITHUB_TOKEN_TOOLS",
        "_HEAL_MODE_ALLOWED_TOOLS",
        "_WEB_TOOLS",
        "_authorized_managed_update_resolver",
        "_builtin_tool_availability",
        "_disabled_tools",
        "_heal_protected_payload_sidecar",
        "_managed_update_code_tool_block",
        "_resource_allowed",
        "_task_constraint_path_allowed",
    }
    proven = {
        "BrowserState",
        "ToolContext",
        "ToolEntry",
        "ToolRegistry",
        "_compose_execute_result",
        *resolution_names,
        *guard_names,
    }

    assert len(proven) == 32
    assert {
        name for name in vars(registry)
        if not name.startswith("__") and name != "annotations"
    } == proven
    assert registry.ToolRegistry is registry_core.ToolRegistry is tools_package.ToolRegistry
    assert registry.ToolContext is tool_context.ToolContext is tools_package.ToolContext
    assert registry.BrowserState is tool_context.BrowserState
    assert registry.ToolEntry is tool_catalog.ToolEntry is tools_package.ToolEntry
    assert registry._compose_execute_result is tool_result._compose_execute_result
    assert all(getattr(registry, name) is getattr(tool_resolution, name) for name in resolution_names)
    assert all(getattr(registry, name) is getattr(registry_guards, name) for name in guard_names)
    assert registry.ToolRegistry.__module__ == "ouroboros.tools.registry_core"
    assert str(inspect.signature(registry.ToolRegistry.execute)) == (
        "(self, name: 'str', args: 'Dict[str, Any]') -> 'str'"
    )
    assert str(inspect.signature(registry.ToolRegistry.execute_result)) == (
        "(self, name: 'str', args: 'Dict[str, Any]') -> 'ToolResult'"
    )
    assert not hasattr(registry_core, "get_tools")
    assert not hasattr(registry_core, "_HEAL_PROTECTED_PAYLOAD_FILENAMES")


def test_registry_core_uses_canonical_managed_update_resolver(tmp_path, monkeypatch):
    import ouroboros.config as config
    import ouroboros.safety as safety
    from ouroboros.tools import registry, registry_guards

    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    tools = registry.ToolRegistry(repo_dir=repo, drive_root=drive)

    def handler(_ctx, *, _resolved_binding=None, **_kwargs):
        assert _resolved_binding is not None
        return "OK"

    tools.override_handler("write_file", handler)

    monkeypatch.setattr(config, "get_runtime_mode", lambda: "light")
    monkeypatch.setattr(safety, "check_safety", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(
        registry_guards,
        "_authorized_managed_update_resolver",
        lambda _ctx: True,
    )

    def facade_call_is_not_execution_authority(_ctx):
        raise AssertionError("registry facade was consulted as execution authority")

    monkeypatch.setattr(
        registry,
        "_authorized_managed_update_resolver",
        facade_call_is_not_execution_authority,
    )

    result = tools.execute_result(
        "write_file",
        {"root": "system_repo", "path": "BIBLE.md", "content": "unchanged"},
    )

    assert result.status == "ok"
    assert result.text == "OK"


def test_active_workspace_root_redirect_is_native_with_legacy_loop_projection(tmp_path, monkeypatch):
    import json

    import ouroboros.safety as safety
    from ouroboros.loop_tool_execution import _execute_single_tool
    from ouroboros.tools import tool_resolution
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.tool_result import LegacyTextResultAdapter, ToolResult

    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    logs = drive / "logs"
    repo.mkdir()
    logs.mkdir(parents=True)
    tools = ToolRegistry(repo_dir=repo, drive_root=drive)
    calls = []

    def forbidden(label):
        def fail(*_args, **_kwargs):
            calls.append(label)
            raise AssertionError(f"root redirect reached {label}")

        return fail

    tools.override_handler("write_file", forbidden("handler"))
    monkeypatch.setattr(safety, "check_safety", forbidden("safety"))
    monkeypatch.setattr(
        tool_resolution,
        "build_resolved_resource_binding",
        forbidden("binding"),
    )
    monkeypatch.setattr(
        LegacyTextResultAdapter,
        "from_text",
        forbidden("legacy adapter"),
    )

    target = str(repo / "result.txt")
    args = {"root": "user_files", "path": target, "content": "result\n"}
    expected_text = (
        "⚠️ ROOT_REQUIRED_ACTIVE_WORKSPACE: absolute path "
        f"{target!r} is under the active workspace, but root='user_files' does not "
        "write there. Retry the same call with root='active_workspace' (the same path is accepted)."
    )
    expected = ToolResult(
        status="blocked",
        code="ROOT_REQUIRED",
        text=expected_text,
        meta={"required_root": "active_workspace"},
    )

    result = tools.execute_result("write_file", dict(args))
    assert result == expected
    assert tools.execute("write_file", dict(args)) == expected_text
    assert not (repo / "result.txt").exists()

    row = _execute_single_tool(
        tools,
        {
            "id": "root-redirect",
            "function": {"name": "write_file", "arguments": json.dumps(args)},
        },
        logs,
    )
    assert row["tool_result"] == expected
    assert row["result"] == expected_text
    assert row["is_error"] is True
    assert row["result_meta"]["status"] == "root_required_active_workspace"
    assert row["result_meta"]["tool_result_status"] == "blocked"
    assert row["result_meta"]["tool_result_code"] == "ROOT_REQUIRED"
    assert row["result_meta"]["tool_result_meta"] == {"required_root": "active_workspace"}
    assert calls == []
