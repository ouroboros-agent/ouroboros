"""Bundled seeds by platform: which install offers, enables, readies and shows each one.

These run the launcher's real seeding, resync, trust stamp and dependency step
over the payloads in ``repo/skills`` (and, for updates, over a real earlier
Unix seed and the earlier launcher code from Git history). The host platform is
pinned through ``skill_manifest.host_platform``; nothing here touches a desktop,
a network or the live data root. Real Windows UI Automation is exercised only by
``tests/test_windows_computer_use_native.py``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile

import pytest

from ouroboros import launcher_bootstrap
from ouroboros.launcher_bootstrap import (
    _native_seed_dependency_pass,
    _per_skill_version_resync,
    _read_skill_manifest,
    _seed_skills_into,
    native_seed_dependencies,
)
from ouroboros.skill_dependencies import skill_deps_not_ready
from ouroboros.skill_loader import (
    discover_skills,
    find_skill,
    load_enabled,
    load_review_state,
    save_enabled,
    save_review_state,
    SkillReviewState,
)
from ouroboros.skill_peer_inventory import discover_skill_peers
from tests._shared import clean_extension_runtime_state

REPO = pathlib.Path(__file__).resolve().parents[1]
SEEDS = REPO / "skills"
UNIX = "unix_computer_use"
WINDOWS = "windows_computer_use"
BASE = "e4ab6cabbb579cfb62b9547ad039cf10e86bfad3"  # official history, available in upstream checkouts
UNIX_043 = "1e237c1bc89f0600a7362f69eddafdff4ddd1f2d"
log = logging.getLogger(__name__)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "true")
    root = tmp_path / "data"
    (root / "skills").mkdir(parents=True)
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", root / "settings.json")
    return root


def on(monkeypatch, platform: str) -> None:
    monkeypatch.setattr("ouroboros.contracts.skill_manifest.host_platform", lambda: platform)


def seed(data_root):
    return _seed_skills_into(SEEDS, data_root / "skills", log)


def resync(data_root):
    return _per_skill_version_resync(SEEDS, data_root / "skills" / "native", log, drive_root=data_root)


def names(data_root):
    return {skill.name for skill in discover_skills(data_root)}


def assert_peer_matches(data_root):
    full = {skill.name: (skill.enabled, tuple(skill.conflicts)) for skill in discover_skills(data_root)}
    peers = {skill.name: (skill.enabled, skill.conflicts) for skill in discover_skill_peers(data_root)}
    assert peers == full


def git_payload(source: str, path: str, dest: pathlib.Path) -> None:
    archived = subprocess.run(["git", "archive", f"{source}:{path}"], cwd=REPO,
                              capture_output=True, check=True, timeout=30)
    with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
        for member in archive:
            if member.isfile():
                target = dest / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())


def earlier_install(data_root, *, unix_marker=True) -> pathlib.Path:
    """An install bootstrapped by an earlier release: Unix 0.4.3 seeded and offered."""
    native = data_root / "skills" / "native"
    native.mkdir(parents=True, exist_ok=True)
    (native / ".bootstrap-seed-complete").write_text("earlier release\n", encoding="utf-8")
    for name in ("telegram", UNIX):
        (native / f".post-bootstrap-seed-{name}").write_text("offered\n", encoding="utf-8")
    # Telegram has its own release cadence; its current payload keeps the
    # resync counts below about the computer-use skills only.
    shutil.copytree(SEEDS / "telegram", native / "telegram",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (native / "telegram" / ".seed-origin").write_text(f"seeded_from={BASE}\n", encoding="utf-8")
    git_payload(UNIX_043, f"skills/{UNIX}", native / UNIX)
    if unix_marker:
        (native / UNIX / ".seed-origin").write_text(f"seeded_from={UNIX_043}\n", encoding="utf-8")
    state = data_root / "state" / "skills" / UNIX
    state.mkdir(parents=True, exist_ok=True)
    (state / "connections.json").write_text('{"active": "bench", "connections": {"bench": {"backend": '
                                            '"osworld_http", "target": "http://10.0.0.5:5000"}}}\n', encoding="utf-8")
    return native


def earlier_launcher(data_root, monkeypatch):
    path = data_root.parent / "earlier_launcher_bootstrap.py"
    path.write_bytes(subprocess.run(["git", "show", f"{BASE}:ouroboros/launcher_bootstrap.py"], cwd=REPO,
                                    capture_output=True, check=True, timeout=30).stdout)
    spec = importlib.util.spec_from_file_location("earlier_launcher_bootstrap", path)
    earlier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, earlier)
    spec.loader.exec_module(earlier)
    return earlier


def state_bytes(data_root, name):
    state = data_root / "state" / "skills" / name
    return {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}


@pytest.mark.parametrize("platform,own,other", [
    ("darwin", UNIX, WINDOWS), ("linux", UNIX, WINDOWS), ("win32", WINDOWS, UNIX),
])
def test_a_fresh_install_offers_and_enables_only_its_own_computer_use_skill(data, monkeypatch, platform, own, other):
    on(monkeypatch, platform)

    seed(data)

    native = data / "skills" / "native"
    assert (native / own / ".seed-origin").is_file() and not (native / other).exists()
    assert (native / f".post-bootstrap-seed-{own}").is_file()
    assert not (native / f".post-bootstrap-seed-{other}").exists()
    assert load_enabled(data, own) is True
    assert json.loads((data / "state" / "skills" / own / "enabled.json").read_text(encoding="utf-8"))["actor"] == "native_seed"
    assert load_review_state(data, own, skill_dir=native / own).review_profile == "native_seed"
    assert own in names(data) and other not in names(data)
    assert_peer_matches(data)
    # Telegram is unchanged: seeded everywhere, waiting for its owner grants.
    assert (native / "telegram").is_dir() and load_enabled(data, "telegram") is False
    if own == UNIX:
        assert "net" in _read_skill_manifest(native / UNIX).permissions


def test_markerless_payload_does_not_count_as_a_previously_offered_seed(data, monkeypatch):
    on(monkeypatch, "darwin")
    native = earlier_install(data, unix_marker=False)
    offered = native / f".post-bootstrap-seed-{UNIX}"
    offered.unlink()  # manually placed payload, never offered by this launcher
    before = state_bytes(data, UNIX)
    assert seed(data) == 0
    assert not offered.exists() and state_bytes(data, UNIX) == before
    assert find_skill(data, UNIX).source == "external"
    shutil.rmtree(native / UNIX)
    assert seed(data) == 1
    assert (native / UNIX / ".seed-origin").is_file() and offered.is_file()


def test_an_existing_windows_install_receives_the_windows_skill_on_update(data, monkeypatch):
    on(monkeypatch, "win32")
    native = earlier_install(data)
    connections = (data / "state" / "skills" / UNIX / "connections.json").read_bytes()
    assert UNIX in names(data)  # the earlier seed declares no platform

    copied, upgraded = seed(data), resync(data)

    assert (copied, upgraded) == (1, 1)
    assert (native / WINDOWS / ".seed-origin").is_file() and (native / f".post-bootstrap-seed-{WINDOWS}").is_file()
    assert load_enabled(data, WINDOWS) is True
    # The upgraded Unix copy now says where it applies: hidden here, never switched
    # on, its connections kept for an install that moves back to macOS/Linux.
    assert _read_skill_manifest(native / UNIX).version == "0.5.0"
    assert not (data / "state" / "skills" / UNIX / "enabled.json").exists()
    assert (data / "state" / "skills" / UNIX / "connections.json").read_bytes() == connections
    assert WINDOWS in names(data) and UNIX not in names(data)
    assert_peer_matches(data)


def test_the_earlier_launcher_never_auto_enabled_the_unrestricted_unix_net_seed(data, monkeypatch):
    on(monkeypatch, "win32")
    native = earlier_install(data)
    earlier_launcher(data, monkeypatch)._stamp_native_seed_trust(data, native / UNIX, log)

    assert _read_skill_manifest(native / UNIX).version == "0.4.3"
    assert _read_skill_manifest(native / UNIX).os == "any"
    assert "net" in _read_skill_manifest(native / UNIX).permissions
    assert load_review_state(data, UNIX, skill_dir=native / UNIX).review_profile == "native_seed"
    assert not (data / "state" / "skills" / UNIX / "enabled.json").exists()


@pytest.mark.parametrize("actor", ["owner_ui", None])
@pytest.mark.parametrize("backend", ["osworld_http", "ssh_macos"])
def test_a_legacy_unix_remote_choice_on_windows_survives_off_boot_and_on(data, monkeypatch, actor, backend):
    from ouroboros import extension_loader, skill_lifecycle_actions

    on(monkeypatch, "win32")
    native = earlier_install(data)
    state = data / "state" / "skills" / UNIX
    if actor is None:
        # Unknown provenance: manual ON and inherited early auto-ON look alike.
        (state / "enabled.json").write_text('{"enabled": true}\n', encoding="utf-8")
    else:
        save_enabled(data, UNIX, True, actor=actor)
    registry = {"active": "bench", "connections": {"bench": {
        "backend": backend, "target": "http://10.0.0.5:5000", "ssh_alias": "bench-mac", "enabled": True}}}
    (state / "connections.json").write_text(json.dumps(registry) + "\n", encoding="utf-8")
    (state / "active_connection.txt").write_text("bench\n", encoding="utf-8")
    retained = {name: (state / name).read_bytes() for name in ("connections.json", "active_connection.txt", "enabled.json")}
    assert (seed(data), resync(data)) == (1, 1)
    assert_peer_matches(data)
    assert {UNIX, WINDOWS} <= names(data)
    assert "legacy_remote_platform=win32" in (native / UNIX / ".seed-origin").read_text(encoding="utf-8").splitlines()
    assert all((state / name).read_bytes() == value for name, value in retained.items())

    # Real extension registration and resolved handlers; only the actual remote
    # transport is replaced. This never opens a desktop, SSH session or socket.
    calls = []

    def exercise_remote():
        extension_loader.reload_all(data, lambda: {})
        assert UNIX in extension_loader.snapshot()["extensions"]
        module = extension_loader._extension_modules[UNIX]

        def osworld(self, conn, argv, *, timeout):
            calls.append(("osworld_http", conn, argv, timeout))
            return {"status": 200, "result": {"status": "success", "returncode": 0, "output": "remote-ok"}}

        def ssh(self, conn, command, *, timeout):
            calls.append(("ssh_macos", conn, command, timeout))
            return 0, "remote-ok", ""

        monkeypatch.setattr(module._ComputerUse, "_osworld_execute", osworld)
        monkeypatch.setattr(module._ComputerUse, "_ssh_run", ssh)
        tool = extension_loader.get_tool(extension_loader.extension_surface_name(UNIX, "remote_exec"))
        assert tool is not None
        result = json.loads(tool["handler"](command="printf remote-ok"))
        assert result["ok"] and result["backend"] == backend
        assert calls[-1][0] == backend
        assert calls[-1][1] == registry["connections"]["bench"]
        assert calls[-1][2] == (["bash", "-lc", "printf remote-ok"] if backend == "osworld_http" else "printf remote-ok")

    monkeypatch.setattr(skill_lifecycle_actions, "_sync_schedules", lambda *args, **kwargs: None)
    clean_extension_runtime_state()
    try:
        exercise_remote()
        disabled = skill_lifecycle_actions._toggle(find_skill(data, UNIX), data, "", False, "owner_ui", "test OFF")
        assert disabled["ok"] and not disabled["enabled"]
        off = (state / "enabled.json").read_bytes()
        assert extension_loader.get_tool(extension_loader.extension_surface_name(UNIX, "remote_exec")) is None
        clean_extension_runtime_state()  # the next boot has no old module/registration
        assert (seed(data), resync(data)) == (0, 0)
        assert find_skill(data, UNIX) is not None and not find_skill(data, UNIX).enabled
        assert_peer_matches(data)
        extension_loader.reload_all(data, lambda: {})
        assert UNIX not in extension_loader.snapshot()["extensions"]
        # A subsequent version resync must preserve eligibility AND the newer OFF.
        installed = native / UNIX / "SKILL.md"
        installed.write_text(installed.read_text(encoding="utf-8").replace("version: 0.5.0", "version: 0.0.1", 1), encoding="utf-8")
        assert resync(data) == 1
        assert (state / "enabled.json").read_bytes() == off and not load_enabled(data, UNIX)
        assert find_skill(data, UNIX) is not None
        assert_peer_matches(data)
        enabled = skill_lifecycle_actions._toggle(find_skill(data, UNIX), data, "", True, "owner_ui", "intentional ON")
        assert enabled["ok"] and enabled["enabled"], enabled
        exercise_remote()
        assert all((state / name).read_bytes() == retained[name] for name in ("connections.json", "active_connection.txt"))
        clean_extension_runtime_state()
        shutil.rmtree(native / UNIX)
        assert (seed(data), resync(data)) == (0, 0) and find_skill(data, UNIX) is None
    finally:
        clean_extension_runtime_state()


@pytest.mark.parametrize("choice", [{"enabled": False, "actor": "owner_ui"},
                                    {"enabled": True, "actor": "native_seed"}, {"enabled": "true"}])
def test_an_old_unix_copy_without_a_real_on_choice_stays_hidden_on_windows(data, monkeypatch, choice):
    on(monkeypatch, "win32")
    native = earlier_install(data)
    state = data / "state" / "skills" / UNIX
    (state / "enabled.json").write_text(json.dumps(choice), encoding="utf-8")
    before = (state / "enabled.json").read_bytes()

    seed(data)
    assert resync(data) == 1

    assert UNIX not in names(data) and find_skill(data, UNIX) is None
    assert_peer_matches(data)
    assert "legacy_remote_platform" not in (native / UNIX / ".seed-origin").read_text(encoding="utf-8")
    assert (state / "enabled.json").read_bytes() == before


def test_legacy_visibility_confers_no_trust_when_native_trust_is_disabled(data, monkeypatch):
    from ouroboros import extension_loader

    on(monkeypatch, "win32")
    earlier_install(data)
    save_enabled(data, UNIX, True, actor="owner_ui")
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "false")
    seed(data)
    assert resync(data) == 1
    legacy = find_skill(data, UNIX)
    assert legacy is not None and legacy.enabled
    assert not legacy.review.gate_for(legacy.content_hash)["executable_review"]
    assert_peer_matches(data)
    clean_extension_runtime_state()
    try:
        assert extension_loader.load_extension(legacy, lambda: {}, drive_root=data) is not None
        assert UNIX not in extension_loader.snapshot()["extensions"]
        assert extension_loader.get_tool(extension_loader.extension_surface_name(UNIX, "remote_exec")) is None
    finally:
        clean_extension_runtime_state()


def test_update_turns_the_unix_skill_on_where_its_owner_never_chose(data, monkeypatch):
    on(monkeypatch, "darwin")
    native = earlier_install(data)
    connections = (data / "state" / "skills" / UNIX / "connections.json").read_bytes()

    copied, upgraded = seed(data), resync(data)

    assert (copied, upgraded) == (0, 1)
    assert not (native / WINDOWS).exists() and not (native / f".post-bootstrap-seed-{WINDOWS}").exists()
    manifest = _read_skill_manifest(native / UNIX)
    assert manifest.version == "0.5.0" and "net" in manifest.permissions
    assert load_enabled(data, UNIX) is True
    assert (data / "state" / "skills" / UNIX / "connections.json").read_bytes() == connections


@pytest.mark.parametrize("platform,name", [("darwin", UNIX), ("win32", WINDOWS)])
def test_an_explicit_off_survives_reseed_and_update(data, monkeypatch, platform, name):
    on(monkeypatch, platform)
    seed(data)
    save_enabled(data, name, False, actor="owner_ui")
    installed = data / "skills" / "native" / name / "SKILL.md"
    text = installed.read_text(encoding="utf-8")
    version = _read_skill_manifest(installed.parent).version
    installed.write_text(text.replace(f"version: {version}", "version: 0.0.1", 1), encoding="utf-8")

    assert seed(data) == 0 and resync(data) == 1

    assert _read_skill_manifest(installed.parent).version == version
    assert load_enabled(data, name) is False


def test_the_trust_opt_out_seeds_without_trust_enable_or_dependencies(data, monkeypatch):
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "false")
    on(monkeypatch, "win32")
    installs = []
    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages",
                        lambda packages, *args, **kwargs: installs.append(packages) or [])

    seed(data)

    assert (data / "skills" / "native" / WINDOWS).is_dir()
    assert load_review_state(data, WINDOWS).status != "clean"
    assert load_enabled(data, WINDOWS) is False
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [] and installs == []


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_markerless_native_payload_stays_its_owners(data, monkeypatch, platform):
    on(monkeypatch, platform)
    native = earlier_install(data, unix_marker=False)
    save_enabled(data, UNIX, True, actor="owner_ui")
    before = state_bytes(data, UNIX)

    assert resync(data) == 0

    assert _read_skill_manifest(native / UNIX).version == "0.4.3"
    assert not (data / "state" / "skills" / UNIX / "review.json").exists()
    assert find_skill(data, UNIX).source == "external"
    assert state_bytes(data, UNIX) == before
    assert_peer_matches(data)


@pytest.mark.parametrize("earlier_first_seed", [False, True])
def test_a_removed_seed_is_not_offered_again(data, monkeypatch, earlier_first_seed):
    on(monkeypatch, "win32")
    native = data / "skills" / "native"
    if earlier_first_seed:
        # First seeded by a release that wrote no per-skill offered marker.
        native.mkdir(parents=True)
        (native / ".bootstrap-seed-complete").write_text("earlier release\n", encoding="utf-8")
        git_payload(BASE, "skills/telegram", native / "telegram")
        (native / "telegram" / ".seed-origin").write_text("seeded_from=skills\n", encoding="utf-8")
    seed(data)
    removed = "telegram" if earlier_first_seed else WINDOWS
    assert (native / removed).is_dir()
    shutil.rmtree(native / removed)

    assert seed(data) == 0 and not (native / removed).exists()


def test_a_seed_from_an_earlier_launcher_for_another_platform_stays_hidden_and_inert(data, monkeypatch):
    """The earlier launcher's first seed copied every bundled seed and enabled the
    tool-only ones; on macOS that put an enabled Windows skill on disk."""
    earlier = earlier_launcher(data, monkeypatch)
    on(monkeypatch, "darwin")
    earlier._seed_skills_into(SEEDS, data / "skills", log)
    native = data / "skills" / "native"
    assert (native / WINDOWS / ".seed-origin").is_file() and load_enabled(data, WINDOWS) is True
    # The narrow migration field is not a generic foreign-seed exemption.
    with (native / WINDOWS / ".seed-origin").open("a", encoding="utf-8") as marker:
        marker.write("legacy_remote_platform=win32\n")
    before = state_bytes(data, WINDOWS)
    installs = []
    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages",
                        lambda packages, *args, **kwargs: installs.append(packages) or [])
    from ouroboros import extension_loader
    from ouroboros.skill_loader import summarize_skills

    clean_extension_runtime_state()
    try:
        results = extension_loader.reload_all(data, lambda: {})
        live = extension_loader.snapshot()["extensions"]
    finally:
        clean_extension_runtime_state()

    assert WINDOWS not in names(data) and find_skill(data, WINDOWS) is None
    assert_peer_matches(data)
    assert WINDOWS not in results and WINDOWS not in live
    assert WINDOWS not in json.dumps(summarize_skills(data))
    assert resync(data) == 0
    assert _native_seed_dependency_pass(data, [WINDOWS, UNIX], log) == [] and installs == []
    assert state_bytes(data, WINDOWS) == before


# ── the dependency step a launcher-trusted seed gets instead of a paid review ──

@pytest.fixture
def windows_seeded(data, monkeypatch):
    on(monkeypatch, "win32")
    seed(data)
    installs, reconciles = [], []
    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages",
                        lambda packages, *args, **kwargs: installs.append(list(packages)) or [])
    monkeypatch.setattr("ouroboros.extension_loader.reconcile_extension",
                        lambda name, *args, **kwargs: reconciles.append(name) or {"action": "extension_loaded"})
    return data, installs, reconciles


def test_an_enabled_windows_seed_gets_comtypes_without_a_review(windows_seeded):
    data, installs, reconciles = windows_seeded
    skill = find_skill(data, WINDOWS)
    assert skill_deps_not_ready(data, skill)[1] == "status"

    assert _native_seed_dependency_pass(data, sorted(p.name for p in SEEDS.iterdir() if p.is_dir()), log) == [WINDOWS]

    assert installs == [["comtypes==1.4.17"]] and reconciles == [WINDOWS]
    assert skill_deps_not_ready(data, find_skill(data, WINDOWS)) == ("", "")
    review = load_review_state(data, WINDOWS, skill_dir=data / "skills" / "native" / WINDOWS)
    assert review.review_profile == "native_seed" and review.reviewer_models == ["repo_commit_gate"]
    assert not (data / "state" / "skills" / WINDOWS / "review_job.json").exists()
    # Ready dependencies are not installed twice.
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [] and len(installs) == 1


def test_a_disabled_seed_waits_and_its_enable_installs_the_dependency(windows_seeded, monkeypatch):
    data, installs, _reconciles = windows_seeded
    save_enabled(data, WINDOWS, False, actor="owner_ui")
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [] and installs == []
    from ouroboros import skill_lifecycle_actions

    monkeypatch.setattr(skill_lifecycle_actions, "_reconcile", lambda *args, **kwargs: {"action": "extension_loaded"})
    monkeypatch.setattr(skill_lifecycle_actions, "_sync_schedules", lambda *args, **kwargs: None)

    result = skill_lifecycle_actions._toggle(find_skill(data, WINDOWS), data, "", True, "owner_ui", "test")

    assert result["ok"] is True and result["enabled"] is True
    assert installs == [["comtypes==1.4.17"]] and load_enabled(data, WINDOWS) is True


def test_a_failed_install_keeps_the_enable_and_names_the_failure(windows_seeded, monkeypatch):
    data, _installs, reconciles = windows_seeded

    def offline(*args, **kwargs):
        raise RuntimeError("pip install failed: no network")

    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages", offline)

    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [WINDOWS]

    from ouroboros.extension_liveness import _deps_block_reason

    assert load_enabled(data, WINDOWS) is True
    assert _deps_block_reason(data, find_skill(data, WINDOWS)) == "deps_failed"
    assert "no network" in json.loads((data / "state" / "skills" / WINDOWS / "deps.json").read_text(encoding="utf-8"))["error"]
    assert reconciles == [WINDOWS]


def test_the_seed_receipt_waits_until_a_boot_finds_every_dependency_ready(windows_seeded, monkeypatch):
    data, installs, _reconciles = windows_seeded
    receipt = data / "state" / "native_seed.json"
    identity = {"schema_version": 1, "seed_tree": "fixed"}
    reseeded, passes = [], []
    monkeypatch.setattr(launcher_bootstrap, "_native_seed_identity", lambda repo_dir: identity)
    monkeypatch.setattr(launcher_bootstrap, "_seed_skills_into", lambda *args, **kwargs: reseeded.append(1) or 0)
    monkeypatch.setattr(launcher_bootstrap, "_migrate_control_file_hashes", lambda *args: True)
    monkeypatch.setattr(launcher_bootstrap, "_per_skill_version_resync", lambda *args, **kwargs: 0)
    monkeypatch.setattr(launcher_bootstrap, "cleanup_orphaned_seed_markers", lambda *args: True)
    # the process dies before its background pass installs anything
    monkeypatch.setattr(launcher_bootstrap, "_start_native_seed_dependency_pass", lambda *args: passes.append(1))

    for _boot in range(2):
        launcher_bootstrap._ensure_native_seed_version(REPO, data, log, start_dependency_pass=True)
        assert not receipt.exists()
    assert len(reseeded) == 2 and len(passes) == 2 and installs == []

    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [WINDOWS] and len(installs) == 1
    assert not receipt.exists()  # the pass itself never certifies the seed
    launcher_bootstrap._ensure_native_seed_version(REPO, data, log)
    assert json.loads(receipt.read_text(encoding="utf-8")) == identity
    launcher_bootstrap._ensure_native_seed_version(REPO, data, log)
    assert len(reseeded) == 3  # fast path from here on



def test_the_launcher_leaves_dependency_installation_to_the_python_server(windows_seeded, monkeypatch):
    from types import SimpleNamespace
    from ouroboros import config

    data, installs, _reconciles = windows_seeded
    receipt = data / "state" / "native_seed.json"
    monkeypatch.setattr(config, "REPO_DIR", REPO)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(launcher_bootstrap, "_native_seed_identity", lambda repo: {"seed_tree": "fixed"})
    monkeypatch.setattr(launcher_bootstrap, "_seed_skills_into", lambda *a, **k: 0)
    monkeypatch.setattr(launcher_bootstrap, "_migrate_control_file_hashes", lambda *a: True)
    monkeypatch.setattr(launcher_bootstrap, "_per_skill_version_resync", lambda *a, **k: 0)
    monkeypatch.setattr(launcher_bootstrap, "cleanup_orphaned_seed_markers", lambda *a: True)
    started = []
    monkeypatch.setattr(launcher_bootstrap, "_start_native_seed_dependency_pass", lambda *a: started.append(1))

    launcher_bootstrap.bootstrap_native_skills(SimpleNamespace(repo_dir=REPO, data_dir=data, log=log))
    assert started == [] and installs == [] and not receipt.exists()
    launcher_bootstrap.ensure_data_skills_seeded()
    assert started == [1] and not receipt.exists()


def test_unknown_dependency_readiness_never_certifies_the_seed(windows_seeded, monkeypatch):
    data, installs, _reconciles = windows_seeded
    receipt = data / "state" / "native_seed.json"
    identity = {"schema_version": 1, "seed_tree": "fixed"}
    monkeypatch.setattr(launcher_bootstrap, "_native_seed_identity", lambda repo_dir: identity)
    monkeypatch.setattr(launcher_bootstrap, "_seed_skills_into", lambda *args, **kwargs: 0)
    monkeypatch.setattr(launcher_bootstrap, "_migrate_control_file_hashes", lambda *args: True)
    monkeypatch.setattr(launcher_bootstrap, "_per_skill_version_resync", lambda *args, **kwargs: 0)
    monkeypatch.setattr(launcher_bootstrap, "cleanup_orphaned_seed_markers", lambda *args: True)
    monkeypatch.setattr(launcher_bootstrap, "_start_native_seed_dependency_pass", lambda *args: None)
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [WINDOWS] and len(installs) == 1

    import ouroboros.skill_dependencies as deps

    def unreadable(*args, **kwargs):
        raise PermissionError("deps state unreadable")

    with monkeypatch.context() as broken:
        broken.setattr(deps, "auto_install_specs_for_skill", unreadable)
        assert deps.skill_deps_not_ready(data, find_skill(data, WINDOWS)) == ("", "")  # execution readers
        launcher_bootstrap._ensure_native_seed_version(REPO, data, log)
        assert not receipt.exists()
    with monkeypatch.context() as broken:
        broken.setattr(launcher_bootstrap, "_shipped_seed_names", lambda seed_dir: None)
        launcher_bootstrap._ensure_native_seed_version(REPO, data, log)
        assert not receipt.exists()
    assert launcher_bootstrap._shipped_seed_names(data / "no-such-seed-tree") is None
    launcher_bootstrap._ensure_native_seed_version(REPO, data, log)
    assert json.loads(receipt.read_text(encoding="utf-8")) == identity

def reviewed_external_skill(data_root):
    """An ordinary reviewed extension with one auto-installed dependency."""
    external = data_root / "skills" / "external" / "pinger"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text(
        "---\nname: pinger\ndescription: x\nversion: 0.1.0\ntype: extension\nentry: plugin.py\n"
        'plugin_api: "2.0"\npermissions: [tool]\ninstall:\n  - kind: pip\n    package: requests\n---\n',
        encoding="utf-8")
    (external / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    skill = find_skill(data_root, "pinger")
    save_review_state(data_root, "pinger", SkillReviewState(status="pass", content_hash=skill.content_hash))
    return find_skill(data_root, "pinger")


def test_a_reviewed_skill_that_is_not_a_seed_gets_nothing_from_the_seed_step(data, monkeypatch):
    assert native_seed_dependencies(data, reviewed_external_skill(data)) == ("", "")


def test_the_dependency_job_carries_a_kind_the_skills_card_labels(windows_seeded):
    from ouroboros.gateway.projects import _SKILL_LIFECYCLE_KINDS
    from ouroboros.skill_lifecycle_queue import queue_snapshot

    data, _installs, _reconciles = windows_seeded
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [WINDOWS]

    job = [event for event in queue_snapshot()["events"]
           if event["target"] == WINDOWS and event["source"] == "native_seed"][-1]
    card = (REPO / "web" / "modules" / "skill_card_renderer.js").read_text(encoding="utf-8")
    labels = re.search(r"const LIFECYCLE_PENDING_LABELS = \{(.*?)\};", card, re.S).group(1)
    assert job["kind"] in re.findall(r"^\s*(\w+):", labels, re.M)  # else the card says only "Working…"
    assert _SKILL_LIFECYCLE_KINDS[job["kind"]] == "Skill dependencies"


def dependency_actions(data_root, name):
    from ouroboros.skill_readiness import skill_readiness_for_execution

    readiness = skill_readiness_for_execution(data_root, find_skill(data_root, name))
    return readiness, [action for action in readiness.next_actions if action["phase"] == "dependencies"]


def test_readiness_sends_a_trusted_seed_to_its_enable_instead_of_a_paid_review(windows_seeded, monkeypatch):
    data, installs, _reconciles = windows_seeded

    from ouroboros.tools import skill_exec
    entry = next(tool for tool in skill_exec.get_tools() if tool.name == "toggle_skill")
    assert entry.timeout_sec == skill_exec._SKILL_REVIEW_TOOL_TIMEOUT_SEC
    assert "prepares its declared isolated dependencies" in entry.schema["description"]

    def offline(*args, **kwargs):
        raise RuntimeError("pip install failed: no network")

    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages", offline)
    assert _native_seed_dependency_pass(data, [WINDOWS], log) == [WINDOWS]

    readiness, deps = dependency_actions(data, WINDOWS)
    assert "deps_not_ready:failed" in readiness.blockers
    assert [action["tool"] for action in deps] == ["toggle_skill"]
    assert "paid panel" in deps[0]["reason"] and "never over an owner's OFF" in deps[0]["reason"]
    assert all(action["tool"] != "skill_review" for action in readiness.next_actions)

    # Following the hint reruns the same dependency step: no review job, the seed's verdict kept.
    from ouroboros import skill_lifecycle_actions

    monkeypatch.setattr("ouroboros.marketplace.isolated_deps._install_python_packages",
                        lambda packages, *args, **kwargs: installs.append(list(packages)) or [])
    monkeypatch.setattr(skill_lifecycle_actions, "_reconcile", lambda *args, **kwargs: {"action": "extension_loaded"})
    monkeypatch.setattr(skill_lifecycle_actions, "_sync_schedules", lambda *args, **kwargs: None)
    result = skill_lifecycle_actions._toggle(find_skill(data, WINDOWS), data, "", True, "agent_tool", "readiness hint")
    assert result["ok"] is True and installs == [["comtypes==1.4.17"]]
    assert not (data / "state" / "skills" / WINDOWS / "review_job.json").exists()
    readiness, deps = dependency_actions(data, WINDOWS)
    assert readiness.ready and not deps and find_skill(data, WINDOWS).review.review_profile == "native_seed"
    # A seed update that moves the pin replaces the payload and its environment: the same route.
    seeds = data.parent / "seeds"
    shutil.copytree(SEEDS / WINDOWS, seeds / WINDOWS)
    manifest = seeds / WINDOWS / "SKILL.md"
    version = _read_skill_manifest(seeds / WINDOWS).version
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(f"version: {version}", "version: 9.9.9", 1)
                        .replace("comtypes==1.4.17", "comtypes==1.4.18", 1), encoding="utf-8")
    assert _per_skill_version_resync(seeds, data / "skills" / "native", log, drive_root=data) == 1
    readiness, deps = dependency_actions(data, WINDOWS)
    assert "deps_not_ready:missing" in readiness.blockers and [action["tool"] for action in deps] == ["toggle_skill"]


def test_readiness_names_the_owner_off_and_switches_nothing_on(windows_seeded):
    data, installs, _reconciles = windows_seeded
    save_enabled(data, WINDOWS, False, actor="owner_ui")

    readiness, deps = dependency_actions(data, WINDOWS)

    assert "skill_disabled" in readiness.owner_action_blockers
    assert [action["tool"] for action in deps] == ["toggle_skill"] and "owner's OFF" in deps[0]["reason"]
    assert load_enabled(data, WINDOWS) is False and installs == []


def test_readiness_keeps_the_review_route_where_no_current_seed_verdict_holds(windows_seeded):
    data, _installs, _reconciles = windows_seeded
    installed = data / "skills" / "native" / WINDOWS / "SKILL.md"
    installed.write_text(installed.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    edited, edited_deps = dependency_actions(data, WINDOWS)
    reviewed_external_skill(data)
    _external, external_deps = dependency_actions(data, "pinger")

    assert "review_stale" in edited.blockers
    assert [action["tool"] for action in edited_deps] == ["skill_review"]
    assert [action["tool"] for action in external_deps] == ["skill_review"]


def test_boot_seeding_starts_the_dependency_step_off_the_boot_path(data, monkeypatch):
    import threading

    on(monkeypatch, "win32")
    started = threading.Event()
    seen = {}

    def pass_(drive_root, seed_names, log_obj):
        seen.update(drive_root=drive_root, names=list(seed_names))
        started.set()

    monkeypatch.setattr(launcher_bootstrap, "_native_seed_dependency_pass", pass_)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", data)
    monkeypatch.setattr("ouroboros.config.REPO_DIR", REPO)

    launcher_bootstrap.ensure_data_skills_seeded()

    assert started.wait(10)
    assert seen["drive_root"] == data and {UNIX, WINDOWS, "telegram"} <= set(seen["names"])


def test_os_declarations_parse_as_platform_lists():
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

    def manifest(os_line):
        return parse_skill_manifest_text(f"---\nname: x\ndescription: x\nversion: 1\ntype: instruction\n{os_line}---\n")

    assert manifest("").os == "any" and manifest("").applies_to_platform("win32")
    listed = manifest("os: [darwin, Linux]\n")
    assert listed.os == "darwin,linux"
    assert listed.applies_to_platform("linux") and not listed.applies_to_platform("win32")
    assert manifest("os: windows\n").applies_to_platform("win32")
    assert manifest("os: macos, win32\n").applies_to_platform("darwin")
    assert not manifest("os: win32\n").applies_to_platform("darwin")
    unix = _read_skill_manifest(SEEDS / UNIX)
    windows = _read_skill_manifest(SEEDS / WINDOWS)
    assert unix.os == "darwin,linux" and windows.os == "win32"
    assert windows.permissions == ["tool"] and windows.raw_extra["install"] == [{"kind": "pip", "package": "comtypes==1.4.17"}]
