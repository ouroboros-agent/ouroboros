"""Host producers' operative views: the actionable facts that stay inline when a window is physically small.

Owner decision 1A (16.10): host service blocks shrink to actionable facts inline plus a pointer
to the retained full text; tier zero stays whole. Each producer builds its own view from the
facts it holds, never by parsing its rendered prose. The sizes here follow the composition
measured on the owner's data (``rework/consilium/PACKET_R2.md`` §0).
"""
from __future__ import annotations

import types

import ouroboros.context_health as context_health
from ouroboros.delegate_custody import RunCustody


def _env(tmp_path):
    env = types.SimpleNamespace()
    env.drive_root = tmp_path
    env.repo_path = lambda *parts: tmp_path / "repo-none" / "/".join(parts)
    env.drive_path = lambda *parts: tmp_path / "/".join(parts)
    return env


def _obligations(monkeypatch, *, available=119, unread=53, patches=35, faults=2):
    """Delegated-run obligations in the owner's measured composition (PACKET_R2 §0: ~111 KB of lines)."""
    import ouroboros.delegate_custody as custody

    runs = []
    for i in range(available + unread):
        run = RunCustody(run_id=f"{i:08x}-{i * 7:07x}", task_id=f"{i % 87:08x}")
        run.output_artifact = f"delegated/{run.task_id}/outputs/{'nested-segment/' * 4}{run.run_id}/final.json"
        run.terminal_source = {"kind": "engine"} if i < available else {}
        runs.append(run)
    held = []
    for i in range(patches):
        run = RunCustody(run_id=f"{i:08x}-p{i:06x}", task_id=f"{i % 31:08x}")
        run.patch_captured = bool(i % 2)
        run.target_root = f"/Users/someone/projects/{'deep/' * 8}target-{i}"
        held.append(run)
    open_faults = [{"run_id": f"live-{i:011x}", "task_id": f"{i:08x}", "reason": "stop_unverified",
                    "ts": "2026-10-16T10:00:00Z"} for i in range(faults)]
    monkeypatch.setattr(custody, "settled_unread_outputs", lambda *_a, **_k: runs)
    monkeypatch.setattr(custody, "undisposed_patches", lambda *_a, **_k: held)
    monkeypatch.setattr(custody, "open_containment_faults", lambda *_a, **_k: open_faults)
    return runs, held, open_faults


def test_health_view_keeps_one_short_line_per_obligation_at_the_owners_scale(tmp_path, monkeypatch):
    runs, held, faults = _obligations(monkeypatch)
    other = ["OK: passing check " + "with recorded details " * 20 for _ in range(12)]
    other += ["CRITICAL: VERSION DESYNC — VERSION=1.2.3, README=1.2.2", "WARNING: COST ACCOUNTING UNAVAILABLE — "
              "budget drift check skipped", "INFO: cache hit rate moderate (40%)"]
    monkeypatch.setattr(context_health, "_collect_log_analysis_checks", lambda _env, found: found.extend(other))
    section = context_health.build_health_invariants(_env(tmp_path), task_id="task-reader")
    full, view = section.full_text, section.operative_view
    assert len(full) > 90_000  # the full section keeps every ownership rule and address
    assert len(view) < len(full) / 4  # one short line per obligation under its kind, not its prose
    for run in [*runs, *held]:
        assert f"\n  - run {run.run_id}, owner task {run.task_id}" in view
    assert "- INFO: DELEGATED RESULT AVAILABLE — 119: its owner task reads each retained output.\n" in view
    assert "- WARNING: DELEGATED RESULT NEVER READ — 53: its owner task reads each to EOF.\n" in view
    assert "- WARNING: DELEGATED PATCH AWAITS DISPOSITION — 35: its owner task decides each while live" in view
    for fault in faults:
        assert (f"- CRITICAL: DELEGATED RUN MAY STILL BE LIVE — run {fault['run_id']} (stop_unverified), "
                f"owner task {fault['task_id']}, since {fault['ts']}\n") in view
    # Every other non-passing fact stays exact, unknown state included; passing checks are counted.
    for line in other[-3:]:
        assert f"- {line}\n" in view
    passing = sum(line.startswith("- OK:") for line in full.splitlines())
    assert passing >= 12 and other[0] not in view and view.endswith(f"- {passing} passing checks; details retained.")
    # The full section is unchanged: every exact address and rule is still there to read.
    for run in runs:
        assert run.output_artifact in full and run.output_artifact not in view
    assert "apply requires the caller's active Git root" in full and "apply requires" not in view


def test_health_view_names_the_reader_s_own_verb(tmp_path, monkeypatch):
    runs, held, _faults = _obligations(monkeypatch, available=0, unread=1, patches=1, faults=0)
    monkeypatch.setattr(context_health, "_collect_log_analysis_checks", lambda _env, found: None)
    own = context_health.build_health_invariants(_env(tmp_path), task_id=runs[0].task_id).operative_view
    assert f"\n  - run {runs[0].run_id}, owner task {runs[0].task_id}: this task reads it to EOF\n" in own
    assert (f"\n  - run {held[0].run_id}, owner task {held[0].task_id}, snapshot only, no patch captured: "
            "this task applies or rejects it\n") in own
    foreign = context_health.build_health_invariants(_env(tmp_path), task_id="task-reader").operative_view
    assert f"\n  - run {runs[0].run_id}, owner task {runs[0].task_id}\n" in foreign
    assert "this task" not in foreign
