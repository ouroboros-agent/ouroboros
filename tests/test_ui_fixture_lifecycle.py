"""The shared UI fixture owns each spawned tree through readiness, restart and teardown.

The sleeper payload keeps process containment real without starting a server or
requiring browser binaries. Real UI consumers run in their existing marker lane.
"""

from __future__ import annotations

from contextlib import nullcontext, suppress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ouroboros import platform_layer as pl, process_containment as pc
from tests import test_ui_smoke_playwright as ui


pytestmark = pytest.mark.serial

_TREE_SCRIPT = r"""
import json, os, pathlib, subprocess, sys, time
receipt, entered, ready = map(pathlib.Path, sys.argv[1:4])
entered.write_text("entered", encoding="utf-8")
child = subprocess.Popen(
    [sys.executable, "-c",
     "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); time.sleep(90)",
     str(ready)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=os.name != "nt",
)
pending = receipt.with_suffix(".pending")
pending.write_text(json.dumps({"parent": os.getpid(), "child": child.pid}), encoding="utf-8")
pending.replace(receipt)
if sys.argv[4] == "exited":
    sys.exit(0)
time.sleep(90)
"""


def _wait(predicate, detail, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(detail)
        time.sleep(0.05)


def _gone(pid):
    # A reparented POSIX zombie cannot execute, even before init has waited it.
    return not pl.pid_is_alive(pid) or pc.pid_is_zombie(pid)


def _assert_stopped(probe, run):
    _wait(lambda: _gone(run.proc.pid) and _gone(run.child), "fixture-owned tree survived")
    assert run.proc.poll() is not None
    assert ("reap", run.index) in probe.events
    assert ("close", run.index) in probe.events
    assert probe.sentinel.poll() is None, "cleanup killed an unrelated process"


@pytest.fixture
def fixture_probe(tmp_path, monkeypatch):
    """Use a sleeper tree at the fixture's one spawn seam; keep containment real."""
    original_container = pc.ProcessContainer
    probe = SimpleNamespace(
        mode="alive", fail_at="", reap_error="", reap_exception=False,
        runs=[], containers=[], generators=[], events=[], prior_gone=[],
    )
    sentinel_ready = tmp_path / "sentinel-ready"
    probe.sentinel = subprocess.Popen(
        [sys.executable, "-c",
         "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8'); sys.stdin.read()",
         str(sentinel_ready)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **pl.subprocess_new_group_kwargs(),
    )

    class ObservedContainer(original_container):
        def __init__(self):
            super().__init__()
            self.index = len(probe.containers)
            self.initial_token = dict(self.containment_env())
            probe.containers.append(self)

        def spawn(self, argv, **kwargs):
            assert Path(argv[1]).name == "server.py", argv
            if probe.runs:
                previous = probe.runs[-1]
                probe.prior_gone.append(
                    _gone(previous.proc.pid) and _gone(previous.child)
                )
            run = SimpleNamespace(
                index=self.index, container=self, proc=None, child=0,
                receipt=tmp_path / f"tree-{len(probe.runs)}.json",
                entered=tmp_path / f"entered-{len(probe.runs)}",
                ready=tmp_path / f"ready-{len(probe.runs)}",
            )
            probe.runs.append(run)
            probe.events.append(("spawn", self.index))
            run.proc = super().spawn(
                [sys.executable, "-c", _TREE_SCRIPT,
                 str(run.receipt), str(run.entered), str(run.ready), probe.mode],
                **kwargs,
            )
            return run.proc

        def reap(self):
            probe.events.append(("reap", self.index))
            actual = super().reap()
            if actual:
                return actual
            if probe.reap_exception:
                raise RuntimeError("injected reap exception")
            return probe.reap_error

        def close(self):
            probe.events.append(("close", self.index))
            return super().close()

    def health(_url):
        # The OLD bare-Popen fixture must fail without accidentally starting a
        # real server. Its direct Popen is refused below; only this container's
        # script may start.
        assert probe.runs, "the UI fixture did not use ProcessContainer.spawn"
        run = probe.runs[-1]
        _wait(lambda: run.receipt.exists() and run.ready.exists(), "sleeper tree did not start")
        info = json.loads(run.receipt.read_text(encoding="utf-8"))
        assert info["parent"] == run.proc.pid
        run.child = info["child"]
        assert not _gone(run.child), "child must be live before teardown"
        if probe.mode == "exited":
            assert run.proc.wait(timeout=15) == 0
        else:
            assert run.proc.poll() is None
        if probe.fail_at == "health":
            raise RuntimeError("injected health failure")

    def supervisor(_url):
        if probe.fail_at == "supervisor":
            raise RuntimeError("injected supervisor failure")

    original_popen = subprocess.Popen

    def no_real_server(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and len(argv) > 1:
            assert Path(str(argv[1])).name != "server.py", "uncontained real server spawn"
        proc = original_popen(argv, *args, **kwargs)
        if isinstance(argv, list) and len(argv) > 2 and argv[2] == _TREE_SCRIPT:
            # Retain custody even if a later native assignment/resume step raises
            # before ProcessContainer.spawn has returned its Popen to the caller.
            probe.runs[-1].proc = proc
        return proc

    def open_fixture():
        gen = ui.direct_server_with_data.__wrapped__(tmp_path / f"fixture-{len(probe.generators)}")
        probe.generators.append(gen)
        return gen, next(gen)

    monkeypatch.setenv("OUROBOROS_RUN_UI_SMOKE", "1")
    monkeypatch.setattr(pc, "ProcessContainer", ObservedContainer)
    monkeypatch.setattr(ui, "MockLLMServer", lambda: nullcontext(
        SimpleNamespace(base_url="http://127.0.0.1:9/v1")
    ))
    monkeypatch.setattr(ui, "_free_port", lambda: 27991)  # no port is actually bound
    monkeypatch.setattr(ui, "_wait_health", health)
    monkeypatch.setattr(ui, "_wait_supervisor_ready", supervisor)
    monkeypatch.setattr(subprocess, "Popen", no_real_server)
    probe.open = open_fixture
    try:
        _wait(sentinel_ready.exists, "unrelated sentinel did not start")
        yield probe
    finally:
        # This runs only after test assertions. It cleans our own exact children
        # if a regression made the generator's cleanup fail; never sweeps argv,
        # filenames, global process lists, or another test's historical runs.
        try:
            for gen in probe.generators:
                with suppress(RuntimeError, pytest.fail.Exception):
                    gen.close()
            for run in probe.runs:
                if run.receipt.exists():
                    run.child = json.loads(run.receipt.read_text(encoding="utf-8"))["child"]
                try:
                    original_container.reap(run.container)
                finally:
                    original_container.close(run.container)
                    if run.child and not _gone(run.child):
                        pl.force_kill_pid(run.child)
                    if run.proc is not None:
                        if run.proc.poll() is None:
                            run.proc.kill()
                        run.proc.wait(timeout=15)
        finally:
            if probe.sentinel.stdin is not None:
                probe.sentinel.stdin.close()
            if probe.sentinel.poll() is None:
                probe.sentinel.terminate()
            probe.sentinel.wait(timeout=15)


@pytest.mark.parametrize("mode", ["exited", "alive"])
def test_ui_fixture_reaps_descendants_after_parent_exit_or_normal_stop(fixture_probe, mode):
    probe = fixture_probe
    probe.mode = mode
    gen, _ = probe.open()
    run = probe.runs[0]
    assert (run.proc.poll() is not None) == (mode == "exited")
    gen.close()
    _assert_stopped(probe, run)


@pytest.mark.parametrize("stage", ["health", "supervisor"])
def test_ui_fixture_reaps_after_readiness_failure(fixture_probe, stage):
    probe = fixture_probe
    probe.fail_at = stage
    with pytest.raises(RuntimeError, match=f"injected {stage} failure"):
        probe.open()
    assert len(probe.runs) == 1
    _assert_stopped(probe, probe.runs[0])


def test_ui_fixture_restart_retires_one_container_before_spawning_another(fixture_probe):
    probe = fixture_probe
    gen, fixture = probe.open()
    first = probe.runs[0]
    fixture["restart_server"]()
    assert len(probe.runs) == len(probe.containers) == 2
    second = probe.runs[1]
    assert first.container.initial_token != second.container.initial_token
    assert probe.prior_gone == [True]
    assert probe.events.index(("close", 0)) < probe.events.index(("spawn", 1))
    _assert_stopped(probe, first)
    assert second.proc.poll() is None and not _gone(second.child)
    gen.close()
    _assert_stopped(probe, second)


@pytest.mark.parametrize("raises", [False, True], ids=["returned-error", "raised-error"])
@pytest.mark.parametrize("operation", ["teardown", "restart"])
def test_ui_fixture_surfaces_reap_failure_and_still_closes(fixture_probe, raises, operation):
    probe = fixture_probe
    gen, fixture = probe.open()
    probe.reap_exception = raises
    probe.reap_error = "injected reap failure"
    with pytest.raises((RuntimeError, pytest.fail.Exception), match="injected reap"):
        if operation == "teardown":
            gen.close()
        else:
            fixture["restart_server"]()
    # A failed retirement cannot start a second generation or erase the error.
    assert len(probe.runs) == 1
    _assert_stopped(probe, probe.runs[0])
    # The generator still reaches its outer finally after restart failed.
    # Root may keep or clear active references; either way this cannot spawn.
    with suppress(RuntimeError, pytest.fail.Exception):
        gen.close()
    assert len(probe.runs) == 1


@pytest.mark.skipif(os.name != "nt", reason="requires actual Windows Job and suspended-start APIs")
def test_ui_fixture_native_windows_assigns_suspended_root_then_reaps_orphan(fixture_probe, monkeypatch):
    probe = fixture_probe
    probe.mode = "exited"
    native_popen = subprocess.Popen
    native_assign = pl.assign_pid_to_job
    native_resume = pl.resume_process
    ordering = []
    assigned = set()
    observations = []

    def popen(argv, **kwargs):
        if isinstance(argv, list) and len(argv) > 2 and argv[2] == _TREE_SCRIPT:
            assert int(kwargs.get("creationflags", 0)) & 0x4, "root was not CREATE_SUSPENDED"
            ordering.append("popen")
        return native_popen(argv, **kwargs)

    def assign(job, pid):
        observations.append(("before_assign", probe.runs[-1].entered.exists()))
        result = native_assign(job, pid)
        observations.append(("assigned", result))
        if result:
            assigned.add(pid)
        ordering.append("assign")
        return result

    def resume(pid):
        observations.append(("before_resume", pid in assigned, probe.runs[-1].entered.exists()))
        result = native_resume(pid)
        observations.append(("resumed", result))
        ordering.append("resume")
        return result

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(pl, "assign_pid_to_job", assign)
    monkeypatch.setattr(pl, "resume_process", resume)
    gen, _ = probe.open()
    run = probe.runs[0]
    assert ordering == ["popen", "assign", "resume"]
    assert observations == [
        ("before_assign", False), ("assigned", True),
        ("before_resume", True, False), ("resumed", True),
    ]
    assert run.entered.read_text(encoding="utf-8") == "entered"
    assert run.proc.poll() == 0 and not _gone(run.child)
    gen.close()
    _assert_stopped(probe, run)
