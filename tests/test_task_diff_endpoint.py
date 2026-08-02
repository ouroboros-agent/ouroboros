"""HTTP contract for GET /api/tasks/{task_id}/diff.

The endpoint answers ONE typed lifecycle for both task shapes:

- a WORKSPACE task projects its durable ``workspace.patch`` artifact (with its
  manifest as the base/head and blocker source) — a task whose artifacts are not
  finalized yet is ``pending``, never a 404 and never a fabricated "no changes";
- a SELF-REPO task has no historical patch, so it projects the paths the
  mutation-attribution authority attributed to the task window against the
  CURRENT repo, discloses baseline drift as a boolean, passes attribution
  blockers through, and refuses (typed ``projection_changed_during_read``)
  rather than serving a patch that does not belong to the disclosed baseline.

Every test is hermetic: a tmp drive root, a tmp git repo, no supervisor.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway import tasks as gateway_tasks
from ouroboros.gateway.tasks import api_task_diff
from ouroboros.headless import task_artifacts_dir
from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result


def _client(drive_root: pathlib.Path, repo_dir: pathlib.Path) -> TestClient:
    app = Starlette(routes=[Route("/api/tasks/{task_id}/diff", api_task_diff, methods=["GET"])])
    app.state.drive_root = drive_root
    app.state.repo_dir = repo_dir
    return TestClient(app)


def _git(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ.get("PATH", ""),
             "HOME": str(root), "LC_ALL": "C"},
    )
    return proc.stdout.strip()


def _init_repo(root: pathlib.Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "loop.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def _write_artifact(drive_root: pathlib.Path, task_id: str, name: str, body: str) -> pathlib.Path:
    path = task_artifacts_dir(drive_root, task_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _baseline_evidence(root: pathlib.Path, base_commit: str, **extra) -> dict:
    return {
        "effect_state": "observed_window",
        "baseline": {
            "baseline_hash": "hash-1",
            "surfaces": [{
                "surface_type": "system_repo",
                "canonical_root": str(root.resolve()),
                "git": {
                    "base_commit": base_commit,
                    "base_tree": "",
                    "dirty_paths": [],
                    "dirty_fingerprints": {},
                },
            }],
        },
        **extra,
    }


# --- workspace source -------------------------------------------------------

def test_workspace_ready_serves_full_patch_artifact_bytes(tmp_path):
    drive_root = tmp_path / "drive"
    patch_text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    patch_path = _write_artifact(drive_root, "ws-ready", "workspace.patch", patch_text)
    manifest_path = _write_artifact(drive_root, "ws-ready", "workspace_patch.json", json.dumps({
        "status": "ready_with_changes", "base_head": "aaa", "current_head": "aaa", "errors": [],
    }))
    write_task_result(
        drive_root, "ws-ready", STATUS_COMPLETED,
        workspace_root=str(tmp_path / "ws"),
        artifact_status="ready_with_changes",
        artifacts=[
            {"kind": "workspace_patch", "name": "workspace.patch", "path": str(patch_path)},
            {"kind": "workspace_patch_manifest", "name": "workspace_patch.json", "path": str(manifest_path)},
        ],
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/ws-ready/diff").json()
    assert payload["status"] == "ready"
    assert payload["source"] == "workspace_patch"
    assert payload["patch"] == patch_text
    assert payload["patch_sha256"] == hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    assert payload["base_commit"] == "aaa"
    assert payload["head_advanced"] is False
    assert payload["blockers"] == []


def test_workspace_running_without_finalized_artifacts_is_pending(tmp_path):
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "ws-pending", STATUS_RUNNING,
        workspace_root=str(tmp_path / "ws"), artifact_status="pending",
    )
    with _client(drive_root, tmp_path / "repo") as client:
        response = client.get("/api/tasks/ws-pending/diff")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["source"] == "workspace_patch"
    assert payload["patch"] == ""


def test_workspace_ready_no_changes_manifest_is_empty(tmp_path):
    drive_root = tmp_path / "drive"
    manifest_path = _write_artifact(drive_root, "ws-empty", "workspace_patch.json", json.dumps({
        "status": "ready_no_changes", "base_head": "bbb", "current_head": "bbb", "errors": [],
    }))
    write_task_result(
        drive_root, "ws-empty", STATUS_COMPLETED,
        workspace_root=str(tmp_path / "ws"), artifact_status="ready_no_changes",
        artifacts=[{"name": "workspace_patch.json", "path": str(manifest_path)}],
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/ws-empty/diff").json()
    assert payload["status"] == "empty"
    assert payload["patch"] == ""
    assert payload["base_commit"] == "bbb"


def test_workspace_head_change_is_blocked_with_manifest_blockers(tmp_path):
    drive_root = tmp_path / "drive"
    manifest_path = _write_artifact(drive_root, "ws-failed", "workspace_patch.json", json.dumps({
        "status": "failed", "base_head": "ccc", "current_head": "ddd",
        "errors": [{"type": "workspace_head_changed", "message": "HEAD moved"}],
    }))
    write_task_result(
        drive_root, "ws-failed", STATUS_COMPLETED,
        workspace_root=str(tmp_path / "ws"), artifact_status="failed",
        artifacts=[{"name": "workspace_patch.json", "path": str(manifest_path)}],
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/ws-failed/diff").json()
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["workspace_head_changed"]
    assert payload["head_advanced"] is True


def test_terminal_workspace_task_without_any_artifact_is_blocked_not_empty(tmp_path):
    """A finished workspace task with no patch artifact must not claim "no changes"."""
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "ws-none", STATUS_COMPLETED,
        workspace_root=str(tmp_path / "ws"), artifact_status="ready_with_changes",
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/ws-none/diff").json()
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["artifact_not_declared"]


def test_artifact_path_outside_task_dir_is_refused_as_a_typed_blocker(tmp_path):
    """The shared resolver's containment guard is what the diff path relies on."""
    drive_root = tmp_path / "drive"
    outside = tmp_path / "elsewhere" / "workspace.patch"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("diff --git a/x b/x\n", encoding="utf-8")
    write_task_result(
        drive_root, "ws-escape", STATUS_COMPLETED,
        workspace_root=str(tmp_path / "ws"), artifact_status="ready_with_changes",
        artifacts=[{"name": "workspace.patch", "path": str(outside)}],
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/ws-escape/diff").json()
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["artifact_outside_task_dir"]
    assert payload["patch"] == ""


# --- self-repo source -------------------------------------------------------

def test_self_repo_terminal_snapshot_drives_the_patch(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 3\n", encoding="utf-8")
    (repo / "new_file.py").write_text("fresh = True\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-terminal", STATUS_COMPLETED,
        mutation_evidence=_baseline_evidence(
            repo, base,
            effect_state="quiescent",
            terminal_candidate_snapshot={
                "captured_at": "now", "baseline_hash": "hash-1",
                "surfaces": [{
                    "surface_type": "system_repo",
                    "canonical_root": str(repo.resolve()),
                    "candidates": ["loop.py", "new_file.py"],
                    "excluded_preexisting_dirty": [],
                    "blockers": [],
                    "head_advanced": False,
                }],
            },
        ),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-terminal/diff").json()
    assert payload["status"] == "ready"
    assert payload["source"] == "mutation_baseline"
    assert payload["base_commit"] == base
    assert payload["head_advanced"] is False
    assert payload["blockers"] == []
    assert "-b = 2" in payload["patch"] and "+b = 3" in payload["patch"]
    # The untracked new file arrives through its own --no-index section.
    assert "new_file.py" in payload["patch"] and "+fresh = True" in payload["patch"]
    assert payload["patch_sha256"] == hashlib.sha256(payload["patch"].encode("utf-8")).hexdigest()


def test_terminal_drift_is_measured_at_READ_time_not_from_the_snapshot(tmp_path):
    """The patch is taken against the CURRENT repo, so drift must be read now.

    The snapshot recorded ``head_advanced: False`` when the task ended; HEAD moved
    afterwards. Trusting the recorded flag would describe a repo that has since
    moved on, and the owner would review a projection whose baseline silently
    disagrees with the disclosure.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 8\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-late-drift", STATUS_COMPLETED,
        mutation_evidence=_baseline_evidence(
            repo, base,
            effect_state="quiescent",
            terminal_candidate_snapshot={
                "captured_at": "now", "baseline_hash": "hash-1",
                "surfaces": [{
                    "surface_type": "system_repo",
                    "canonical_root": str(repo.resolve()),
                    "candidates": ["loop.py"],
                    "blockers": [],
                    "head_advanced": False,
                }],
            },
        ),
    )
    (repo / "unrelated.py").write_text("later = True\n", encoding="utf-8")
    _git(repo, "add", "unrelated.py")
    _git(repo, "commit", "-qm", "someone else moved HEAD")
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-late-drift/diff").json()
    assert payload["head_advanced"] is True
    assert payload["status"] == "ready"
    assert payload["base_commit"] == base
    assert "+b = 8" in payload["patch"]
    # Only the attributed path is projected; the unrelated commit is not claimed.
    assert "unrelated.py" not in payload["patch"]


def test_self_repo_running_uses_the_live_attribution_authority(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 9\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-running", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-running/diff").json()
    assert payload["status"] == "ready"
    assert payload["head_advanced"] is False
    assert payload["blockers"] == []
    assert "+b = 9" in payload["patch"]


def test_self_repo_head_drift_is_disclosed_as_a_boolean_not_a_refusal(tmp_path):
    """Decision 33: drift discloses a boolean AND still shows the projection.

    The task committed one attributed change and left another dirty. The live
    authority reports ``baseline_stale`` (HEAD moved), which this endpoint renders
    as ``head_advanced`` while still serving the current projection — a refusal
    here would hide real work behind an evidence footnote.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 4\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "task commit")
    (repo / "later.py").write_text("still_working = True\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-drift", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-drift/diff").json()
    assert payload["head_advanced"] is True
    assert payload["status"] == "ready"
    assert "baseline_stale" in payload["blockers"]
    assert "+still_working = True" in payload["patch"]


def test_running_task_whose_work_is_fully_committed_is_blocked_not_empty(tmp_path):
    """Honest limit of the LIVE authority (decision 33).

    ``attributed_git_candidates`` attributes working-tree-dirty paths only; a
    running task that already committed everything has no live candidate set. The
    answer is ``blocked`` with the drift disclosed — never ``empty``, which would
    claim the task changed nothing. The committed paths become visible once the
    task terminalizes and its ``terminal_candidate_snapshot`` is recorded.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 7\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "all committed")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-committed", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-committed/diff").json()
    assert payload["status"] == "blocked"
    assert payload["head_advanced"] is True
    assert payload["blockers"] == ["baseline_stale"]


def test_self_repo_without_a_baseline_is_blocked_never_empty(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    drive_root = tmp_path / "drive"
    write_task_result(drive_root, "self-nobaseline", STATUS_RUNNING)
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-nobaseline/diff").json()
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["baseline_missing"]
    assert payload["patch"] == ""


def test_terminal_self_repo_without_snapshot_is_blocked(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-nosnapshot", STATUS_COMPLETED,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-nosnapshot/diff").json()
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["terminal_snapshot_missing"]
    assert payload["base_commit"] == base


def test_self_repo_clean_projection_with_no_candidates_is_empty(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-clean", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-clean/diff").json()
    assert payload["status"] == "empty"
    assert payload["blockers"] == []


def test_projection_race_retries_once_then_refuses(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 5\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-race", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    seen = []
    monkeypatch.setattr(
        gateway_tasks, "_projection_fingerprint",
        lambda root, candidates: seen.append(1) or f"fp-{len(seen)}",
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-race/diff").json()
    assert payload["status"] == "blocked"
    assert "projection_changed_during_read" in payload["blockers"]
    assert payload["patch"] == ""
    # Exactly one retry: before/after for the first read, before/after for the retry.
    assert len(seen) == 4


def test_projection_race_that_settles_on_the_retry_answers_ready(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "loop.py").write_text("a = 1\nb = 6\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "self-settle", STATUS_RUNNING,
        mutation_evidence=_baseline_evidence(repo, base),
    )
    values = iter(["fp-a", "fp-b", "fp-c", "fp-c"])
    monkeypatch.setattr(
        gateway_tasks, "_projection_fingerprint", lambda root, candidates: next(values),
    )
    with _client(drive_root, repo) as client:
        payload = client.get("/api/tasks/self-settle/diff").json()
    assert payload["status"] == "ready"
    assert "projection_changed_during_read" not in payload["blockers"]
    assert "+b = 6" in payload["patch"]


# --- lifecycle --------------------------------------------------------------

def test_unknown_task_id_is_the_only_404(tmp_path):
    with _client(tmp_path / "drive", tmp_path / "repo") as client:
        response = client.get("/api/tasks/absent-task/diff")
    assert response.status_code == 404
    assert response.json() == {"error": "task not found", "task_id": "absent-task"}


def test_malformed_task_id_is_a_400(tmp_path):
    with _client(tmp_path / "drive", tmp_path / "repo") as client:
        response = client.get("/api/tasks/not a task id/diff")
    assert response.status_code == 400


def test_response_carries_exactly_the_declared_envelope(tmp_path):
    drive_root = tmp_path / "drive"
    write_task_result(
        drive_root, "envelope", STATUS_RUNNING,
        workspace_root=str(tmp_path / "ws"), artifact_status="pending",
    )
    with _client(drive_root, tmp_path / "repo") as client:
        payload = client.get("/api/tasks/envelope/diff").json()
    assert set(payload) == {
        "status", "source", "base_commit", "head_advanced", "blockers", "patch", "patch_sha256",
    }
