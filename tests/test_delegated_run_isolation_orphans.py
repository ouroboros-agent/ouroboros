"""Orphan disposition across NESTED host-minted trees (В7-A) and the orphan
capture read.

Both existing target-mismatch tests use DISJOINT roots, so the containment
change would otherwise land with no failing test. The incident: a swarm fanned
into ``<project>/contributions/<track>`` clones, its children died, and the
recovery root -- whose active root was the PARENT project -- got
``INTEGRATE_DELEGATED_TARGET_MISMATCH`` for a target that was a strict
descendant of its own root, while the capture it was authorized to dispose
read back as "outside selected root=artifact_store".
"""

from __future__ import annotations

import pathlib

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import provision_execution_snapshot
from ouroboros.task_results import STATUS_FAILED, write_task_result
from ouroboros.tools.delegate import _capture_terminal_patch
from ouroboros.tools.subagent_integration import _integrate_delegated_patch
from tests.test_delegated_run_isolation import _git, _isolated_entry, _nanny_ctx, _seed_target


def _nested_clone(tmp_path, monkeypatch, *, under_projects_root: bool):
    """A parent tree with a nested git clone, optionally inside the host-minted
    subagent-projects root, plus a nanny ctx whose active root IS the clone."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(projects_root))
    parent_root = (projects_root if under_projects_root else tmp_path / "attached") / "P"
    parent_root.mkdir(parents=True)
    target = _seed_target(parent_root)  # <parent>/target: a real git tree
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    return parent_root, target, ctx


def _captured_orphan(tmp_path, monkeypatch, *, under_projects_root=True, terminal=True):
    parent_root, target, ctx = _nested_clone(
        tmp_path, monkeypatch, under_projects_root=under_projects_root)
    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nanny", snapshot_id="snapNested")
    custody._CUSTODY.clear()
    (pathlib.Path(handle.path) / "newfile.py").write_text("print('hi')\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    assert _capture_terminal_patch(ctx, entry)["status"] == "ready_with_changes"
    if terminal:
        write_task_result(custody.custody_root(ctx), "t-nanny", STATUS_FAILED)
    return parent_root, target, ctx, entry


def _disposer(tmp_path, monkeypatch, parent_root, target, *, task_id="t-second"):
    disposer = _nanny_ctx(tmp_path, target, monkeypatch)
    disposer.task_id = task_id
    disposer.workspace_root = str(parent_root)
    return disposer


class TestNestedOrphanApply:
    def test_orphan_applies_from_the_parent_of_a_host_minted_clone(self, tmp_path, monkeypatch):
        parent_root, target, _ctx, entry = _captured_orphan(tmp_path, monkeypatch)
        disposer = _disposer(tmp_path, monkeypatch, parent_root, target)

        out = _integrate_delegated_patch(disposer, "run-1", "apply", "adopted the dead child")
        assert "✅ Integrated" in out, out
        assert "orphan of terminal task t-nanny" in out, out
        assert (target / "newfile.py").read_text(encoding="utf-8") == "print('hi')\n"
        assert "newfile.py" in _git(target, "diff", "--cached", "--name-only").stdout
        assert entry.patch_disposed == "applied"
        rows = [row for row in custody._iter_rows(
            custody.event_log_path(custody.custody_root(disposer)))
            if str(row.get("type") or "") == custody.PATCH_DISPOSED]
        assert [row["disposed_by_task_id"] for row in rows] == ["t-second"], rows
        custody._CUSTODY.clear()

    def test_nested_target_outside_the_projects_root_stays_a_mismatch(self, tmp_path, monkeypatch):
        """Containment alone is not authority: an owner-attached folder is never
        a host-minted tree, so the same geometry keeps refusing there."""
        parent_root, target, _ctx, entry = _captured_orphan(
            tmp_path, monkeypatch, under_projects_root=False)
        disposer = _disposer(tmp_path, monkeypatch, parent_root, target)

        out = _integrate_delegated_patch(disposer, "run-1", "apply", "")
        assert "INTEGRATE_DELEGATED_TARGET_MISMATCH" in out, out
        assert "subagent-projects" in out
        assert not (target / "newfile.py").exists()
        assert entry.patch_disposed == ""
        custody._CUSTODY.clear()

    def test_a_live_owner_keeps_exact_equality_from_its_own_nested_root(self, tmp_path, monkeypatch):
        """The relaxation is the ORPHAN rule, not a looser gate: with the owner
        task still live the same nested geometry is refused for the owner too."""
        parent_root, target, _ctx, entry = _captured_orphan(
            tmp_path, monkeypatch, terminal=False)
        owner = _disposer(tmp_path, monkeypatch, parent_root, target, task_id="t-nanny")

        out = _integrate_delegated_patch(owner, "run-1", "apply", "")
        assert "INTEGRATE_DELEGATED_TARGET_MISMATCH" in out, out
        assert not (target / "newfile.py").exists()
        assert entry.patch_disposed == ""
        custody._CUSTODY.clear()

    def test_a_live_owner_still_refuses_a_foreign_disposer(self, tmp_path, monkeypatch):
        parent_root, target, _ctx, entry = _captured_orphan(
            tmp_path, monkeypatch, terminal=False)
        disposer = _disposer(tmp_path, monkeypatch, parent_root, target)

        out = _integrate_delegated_patch(disposer, "run-1", "apply", "")
        assert "INTEGRATE_DELEGATED_NOT_OWNED" in out, out
        assert not (target / "newfile.py").exists()
        assert entry.patch_disposed == ""
        custody._CUSTODY.clear()
