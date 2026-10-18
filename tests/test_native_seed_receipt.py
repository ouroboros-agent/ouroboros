"""Seed completion survives processes; changed versions and failed passes do not reuse it."""

import logging
import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ouroboros import config, launcher_bootstrap as bootstrap, skill_loader
from tests._git_ops_recovery_shared import _git
from tests.test_per_skill_version_resync import _write_skill


@pytest.fixture
def seed_repo(tmp_path, monkeypatch):
    repo, drive = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@ouroboros")
    for name in ("first", "second"):
        _write_skill(repo / "skills", name)
    _commit(repo)
    monkeypatch.setattr(config, "REPO_DIR", repo)
    monkeypatch.setattr(config, "DATA_DIR", drive)
    monkeypatch.setattr(config, "SETTINGS_PATH", drive / "settings.json")
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "false")
    return repo, drive


def _commit(repo):
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed fixture")


def _receipt(drive):
    return drive / "state" / "native_seed.json"


def test_unchanged_launcher_and_server_read_one_fact_without_payload_work(seed_repo, monkeypatch):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    receipt = _receipt(drive)
    before = receipt.read_bytes()
    context = SimpleNamespace(repo_dir=repo, data_dir=drive, log=logging.getLogger(__name__))
    reads, read = [], bootstrap._read_json_file

    def observe(path):
        reads.append(path)
        return read(path)

    def forbidden(*args, **kwargs):
        pytest.fail("unchanged seed walked or read payloads")

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "_read_json_file", observe)
        for name in ("_seed_skills_into", "_per_skill_version_resync", "_migrate_control_file_hashes",
                     "cleanup_orphaned_seed_markers"):
            patch.setattr(bootstrap, name, forbidden)
        patch.setattr(skill_loader, "compute_content_hash", forbidden)
        patch.setattr(skill_loader, "discover_skills", forbidden)
        patch.setattr(pathlib.Path, "iterdir", forbidden)
        patch.setattr(shutil, "copytree", forbidden)
        patch.setattr(shutil, "rmtree", forbidden)
        assert bootstrap.ensure_data_skills_seeded() == 0
        bootstrap.bootstrap_native_skills(context)
        assert reads == [receipt, receipt]
        # Unrelated commits do not change the seed version.
        (repo / "unrelated.txt").write_text("other code\n", encoding="utf-8")
        _commit(repo)
        assert bootstrap.ensure_data_skills_seeded() == 0
        assert reads == [receipt] * 3
    assert receipt.read_bytes() == before

    script = (
        "import pathlib,sys\n"
        "from ouroboros import config,launcher_bootstrap as b\n"
        "config.REPO_DIR=pathlib.Path(sys.argv[1]);config.DATA_DIR=pathlib.Path(sys.argv[2])\n"
        "def forbidden(*a,**k): raise AssertionError('fresh process repeated seeding')\n"
        "b._seed_skills_into=forbidden\n"
        "assert b.ensure_data_skills_seeded()==0\n"
    )
    subprocess.run([sys.executable, "-c", script, str(repo), str(drive)], check=True, timeout=30)


def test_upgrade_and_rollback_reseed_only_the_changed_manifest_version(seed_repo, monkeypatch):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    original = _git(repo, "rev-parse", "HEAD")
    copied, copy = [], shutil.copytree
    monkeypatch.setattr(shutil, "copytree", lambda src, dst, *a, **kw:
                        (copied.append(pathlib.Path(src).name), copy(src, dst, *a, **kw))[1])
    _write_skill(repo / "skills", "first", "2.0.0")
    _commit(repo)
    assert bootstrap.ensure_data_skills_seeded() == 1
    assert copied == ["first"]
    assert bootstrap._read_skill_manifest_version(drive / "skills/native/first") == "2.0.0"
    assert bootstrap.ensure_data_skills_seeded() == 0
    assert copied == ["first"]
    _git(repo, "reset", "--hard", original)
    assert bootstrap.ensure_data_skills_seeded() == 1
    assert bootstrap._read_skill_manifest_version(drive / "skills/native/first") == "1.0.0"
    assert copied == ["first", "first"]


def test_dirty_seed_is_reconciled_without_certifying_it_and_deletions_stay_deleted(seed_repo):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    shutil.rmtree(drive / "skills/native/second")
    _write_skill(repo / "skills", "first", "2.0.0")
    assert bootstrap.ensure_data_skills_seeded() == 1
    assert not _receipt(drive).exists()
    assert bootstrap.ensure_data_skills_seeded() == 0
    assert not _receipt(drive).exists()  # another dirty pass is not a committed version
    _commit(repo)
    assert bootstrap.ensure_data_skills_seeded() == 0
    assert _receipt(drive).exists()
    assert not (drive / "skills/native/second").exists()


@pytest.mark.parametrize("phase", ["discovery", "migration", "resync", "cleanup"])
def test_failed_pass_is_retried_and_cannot_publish_done(seed_repo, monkeypatch, phase):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    _write_skill(repo / "skills", "first", "2.0.0")
    _commit(repo)
    with monkeypatch.context() as patch:
        if phase == "discovery":
            def failed(*args, **kwargs):
                raise OSError("fixture read unavailable")
            patch.setattr(skill_loader, "discover_skills", failed)
        else:
            name = {"migration": "_migrate_control_file_hashes", "resync": "_reseed_native_skill_in_place",
                    "cleanup": "cleanup_orphaned_seed_markers"}[phase]
            patch.setattr(bootstrap, name, lambda *args, **kwargs: False)
        bootstrap.ensure_data_skills_seeded()
    assert not _receipt(drive).exists()
    assert bootstrap.ensure_data_skills_seeded() == (1 if phase == "resync" else 0)
    assert _receipt(drive).exists()
    assert bootstrap.ensure_data_skills_seeded() == 0


def test_a_partly_applied_version_cannot_reuse_an_older_receipt_on_rollback(seed_repo, monkeypatch):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    original = _git(repo, "rev-parse", "HEAD")
    for name in ("first", "second"):
        _write_skill(repo / "skills", name, "2.0.0")
    _commit(repo)
    reseed = bootstrap._reseed_native_skill_in_place
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "_reseed_native_skill_in_place", lambda src, *args, **kwargs:
                      False if src.name == "second" else reseed(src, *args, **kwargs))
        assert bootstrap.ensure_data_skills_seeded() == 1
    assert not _receipt(drive).exists()
    _git(repo, "reset", "--hard", original)
    assert bootstrap.ensure_data_skills_seeded() == 1
    assert bootstrap._read_skill_manifest_version(drive / "skills/native/first") == "1.0.0"
    assert _receipt(drive).exists()


def test_new_seed_copy_failure_and_name_collision_are_not_done(seed_repo, monkeypatch):
    repo, drive = seed_repo
    assert bootstrap.ensure_data_skills_seeded() == 2
    _write_skill(repo / "skills", "telegram")
    _commit(repo)
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise OSError("fixture copy unavailable")
        patch.setattr(shutil, "copytree", unavailable)
        assert bootstrap.ensure_data_skills_seeded() == 0
    assert not _receipt(drive).exists()
    collision = _write_skill(drive / "skills/external", "telegram")
    assert bootstrap.ensure_data_skills_seeded() == 0
    assert not _receipt(drive).exists()
    shutil.rmtree(collision)
    assert bootstrap.ensure_data_skills_seeded() == 1
    assert _receipt(drive).exists()
    shutil.rmtree(drive / "skills/native/telegram")
    assert bootstrap.ensure_data_skills_seeded() == 0
    assert not (drive / "skills/native/telegram").exists()
