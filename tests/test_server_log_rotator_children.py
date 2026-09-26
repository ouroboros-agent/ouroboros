"""``server.py`` imported by a spawn/forkserver worker (``__mp_main__``) attaches a stream
handler only: two processes must never rotate ``logs/server.log`` against each other. A
module-level ``multiprocessing.parent_process()`` check would be None in such a child, so the
proof is a REAL child re-running the module under that name."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.serial
def test_a_spawn_child_importing_server_gets_a_stream_handler_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    code = (
        "import logging, runpy, sys\n"
        "runpy.run_path('server.py', run_name='__mp_main__')\n"
        "print('HANDLERS', sorted(type(h).__name__ for h in logging.getLogger().handlers))\n"
    )
    env = {**os.environ, "OUROBOROS_DATA_DIR": str(data), "PYTHONPATH": str(REPO)}
    env.pop("PYTEST_CURRENT_TEST", None)
    completed = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True,
                               text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    handlers = next(line for line in completed.stdout.splitlines() if line.startswith("HANDLERS"))
    assert "RotatingFileHandler" not in handlers and "StreamHandler" in handlers, handlers
    assert not (data / "logs" / "server.log").exists()
