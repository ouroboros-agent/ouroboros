"""Observability blobs are never deleted and never counted (CPL4-C22, owner 7A; TZ-1 A).

The retention knob that deleted nothing is retired (absent from the module, listed in
``RETIRED_SETTING_KEYS`` so stored ghosts drop on settings load), and the startup census
that walked every manifest and blob to count them is gone with it: startup neither lists
nor touches the store.
"""

from __future__ import annotations

import os
import time


def _seed_store(tmp_path):
    calls = tmp_path / "observability" / "calls" / "t1"
    blobs = tmp_path / "observability" / "blobs"
    calls.mkdir(parents=True)
    blobs.mkdir(parents=True)
    aged = time.time() - 4000 * 86400
    paths = [calls / "a.json", calls / "b.json", blobs / "x.gz", blobs / "y.gz", blobs / "z.gz"]
    for path in paths:
        path.write_bytes(b"data")
        os.utime(path, (aged, aged))
    return paths


def test_startup_neither_counts_nor_deletes_observability_blobs(tmp_path, monkeypatch):
    import ouroboros.observability as observability
    from ouroboros import server_maintenance as sm

    monkeypatch.setattr(sm, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OUROBOROS_OBSERVABILITY_RETENTION_DAYS", "1")  # inert: retired
    paths = _seed_store(tmp_path)
    touched = []
    real_stat = os.stat

    def spy(path, *args, **kwargs):
        if "observability" in str(path):
            touched.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    sm._startup_prune_sweeps()
    assert all(path.exists() for path in paths)
    assert touched == [], touched
    assert not hasattr(observability, "prune_observability_blobs")


def test_retention_knob_is_retired_everywhere():
    import inspect

    import ouroboros.observability as observability
    from ouroboros.settings_defaults import RETIRED_SETTING_KEYS

    source = inspect.getsource(observability)
    # The docstring may still NAME the retired knob; nothing may READ it.
    assert 'environ.get("OUROBOROS_OBSERVABILITY_RETENTION_DAYS"' not in source
    assert "OUROBOROS_OBSERVABILITY_RETENTION_DAYS" in RETIRED_SETTING_KEYS
