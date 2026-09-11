"""Cyber capability remains usable through real schemas and retained review controls."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("mode", ["pro", "cyber_pro"])
def test_protected_atomic_rewrite_and_unrelated_cleanup_remain_usable(tmp_path, monkeypatch, mode):
    from tests.test_runtime_mode_core import _registry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    reg = _registry(tmp_path)
    (tmp_path / "BIBLE.md").write_text("old")
    (tmp_path / "BIBLE.tmp").write_text("reformulated")
    (tmp_path / "scratch.txt").write_text("scratch")
    nested = tmp_path / "docs"
    nested.mkdir()
    (nested / "BIBLE.md").write_text("unrelated")
    out = reg.execute("run_command", {"cmd": "mv BIBLE.tmp BIBLE.md"})
    assert "DELETE_BLOCKED" not in out
    assert (tmp_path / "BIBLE.md").read_text() == "reformulated"
    out = reg.execute("run_command", {"cmd": ["sh", "-c", "python3 -c \"from pathlib import Path; Path('BIBLE.md').write_text('updated')\" && rm scratch.txt docs/BIBLE.md"]})
    assert "DELETE_BLOCKED" not in out
    assert (tmp_path / "BIBLE.md").read_text() == "updated"
    assert not (tmp_path / "scratch.txt").exists()
    assert not (nested / "BIBLE.md").exists()
    out = reg.execute("run_command", {"cmd": "mv BIBLE.md BIBLE.old"})
    assert "BIBLE_DELETE_BLOCKED" in out
    assert (tmp_path / "BIBLE.md").exists()


def test_cyber_acting_schema_and_scheduling_share_execution_authority(tmp_path, monkeypatch):
    from ouroboros.tool_access import subagent_profile_satisfies
    from tests.test_acting_subagents import _acting_registry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    reg, ctx, worktree = _acting_registry(tmp_path)
    assert subagent_profile_satisfies("acting_subagent", ["review"]) == (True, [])
    assert not subagent_profile_satisfies("local_readonly_subagent", ["review"])[0]
    for tool in ("read_file", "write_file", "edit_text", "search_code"):
        schema = reg.get_schema_by_name(tool)
        schema = schema.get("function", schema)
        assert "system_repo" in schema["parameters"]["properties"]["root"]["enum"]
    result = reg.execute("write_file", {"root": "system_repo", "path": "example.txt", "content": "host effect"})
    assert (ctx.repo_dir / "example.txt").read_text() == "host effect", result
    assert not (worktree / "example.txt").exists()
    result = reg.execute("write_file", {"root": "system_repo", "path": "BIBLE.md", "content": "# Preserved constitutional core\n"})
    assert (ctx.repo_dir / "BIBLE.md").read_text() == "# Preserved constitutional core\n", result
    assert "CORE_PATCH_NOTICE" in result
    assert "commit_reviewed" not in reg.available_tools()


def test_cyber_external_root_keeps_credential_shell_access(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    repo, data, project = (tmp_path / name for name in ("repo", "data", "project"))
    for path in (repo, data, project):
        path.mkdir()
    settings = data / "settings.json"
    settings.write_text('{"fixture": "owner-selected material"}')
    reg = ToolRegistry(repo_dir=repo, drive_root=data)
    reg._ctx = ToolContext(repo_dir=repo, drive_root=data, workspace_root=str(project), workspace_mode="external")
    out = reg.execute("run_command", {"cmd": ["cat", str(settings)]})
    assert "WORKSPACE_SHELL_BLOCKED" not in out
    assert "owner-selected material" in out


def test_cyber_keeps_owner_review_controls_without_removing_supervisor_or_key_use(tmp_path, monkeypatch):
    from ouroboros import browser_policy as policy
    from tests.test_runtime_mode_core import _registry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    reg = _registry(tmp_path)
    # Pre-execution checks only: no live endpoint or account is contacted.
    for command, code in (
        ("curl -X POST http://localhost/api/owner/context-mode -d '{\"mode\":\"low\"}'", "CONTEXT_MODE_SELF_LOWERING_BLOCKED"),
        ("curl -X POST http://localhost/api/settings -d '{\"OUROBOROS_REVIEW_ENFORCEMENT\":\"advisory\"}'", "ELEVATION_BLOCKED"),
    ):
        out = reg.execute("run_command", {"cmd": command})
        assert code in out
    monkeypatch.setattr(policy, "runtime_service_kind", lambda *_a: "main")
    request = lambda path, body: SimpleNamespace(url="http://localhost" + path, method="POST", post_data=body)
    assert policy.browser_request_block_reason(request("/api/settings", '{"OUROBOROS_REVIEW_ENFORCEMENT":"advisory"}'), None, restricted=False, runtime_mode="cyber_pro")
    assert policy.browser_request_block_reason(request("/api/owner/context-mode", '{"mode":"low"}'), None, restricted=False, runtime_mode="cyber_pro")
    for path, body in (("/api/owner/safety-mode", '{"mode":"off"}'), ("/api/settings", '{"SERVICE_API_KEY":"synthetic"}')):
        assert policy.browser_request_block_reason(request(path, body), None, restricted=False, runtime_mode="cyber_pro") == ""
    assert policy.browser_evaluate_block_reason("http://localhost", "fetch('/api/settings',{body:JSON.stringify({OUROBOROS_REVIEW_ENFORCEMENT:'advisory'})})", runtime_mode="cyber_pro")
    assert policy.browser_evaluate_block_reason("http://localhost", "fetch('/api/owner/safety-mode',{body:JSON.stringify({mode:'off'})})", runtime_mode="cyber_pro") == ""
