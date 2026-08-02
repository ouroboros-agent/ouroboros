from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_proof", REPO / "scripts" / "release_proof.py"
)
assert SPEC and SPEC.loader
release_proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_proof)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_release(tmp_path: Path, version: str = "6.87.5") -> tuple[Path, Path, Path]:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    version_file = tmp_path / "VERSION"
    version_file.write_text(f"{version}\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        "## Version History\n\n"
        "| Version | Date | Description |\n"
        "|---|---|---|\n"
        f"| {version} | 2026-08-01 | **A clear release note.** |\n",
        encoding="utf-8",
    )
    for proof_id, name_factory in release_proof.PROOF_IDS.items():
        artifact = release_dir / name_factory(version)
        artifact.write_bytes(f"archive:{proof_id}".encode())
        receipt = {
            "schemaVersion": 1,
            "kind": "packaged_artifact_smoke",
            "status": "passed",
            "proofId": proof_id,
            "artifact": artifact.name,
            "sha256": _digest(artifact),
            "sourceCommit": "a" * 40,
            "releaseTag": f"v{version}",
            "checks": sorted(release_proof.REQUIRED_SMOKE_CHECKS[proof_id]),
        }
        (release_dir / f"release-smoke-{proof_id}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        (release_dir / f"sbom-{proof_id}.cdx.json").write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.6",
                    "serialNumber": f"urn:uuid:{proof_id}",
                }
            ),
            encoding="utf-8",
        )
    return release_dir, version_file, readme


def test_locate_artifact_requires_exactly_one_archive(tmp_path: Path):
    one = tmp_path / "Ouroboros-1.0.0.dmg"
    one.write_bytes(b"one")
    assert release_proof.locate_artifact(tmp_path) == one
    (tmp_path / "Ouroboros-1.0.0.zip").write_bytes(b"two")
    with pytest.raises(ValueError, match="exactly one"):
        release_proof.locate_artifact(tmp_path)


def test_assemble_binds_three_archives_smokes_and_sboms(tmp_path: Path):
    release_dir, version_file, readme = _fixture_release(tmp_path)
    notes = tmp_path / "notes.md"
    args = argparse.Namespace(
        directory=release_dir,
        version_file=version_file,
        readme=readme,
        repository="razzant/ouroboros",
        tag="v6.87.5",
        commit="a" * 40,
        run_url="https://github.com/razzant/ouroboros/actions/runs/1",
        previous_tag="v6.87.4",
        generated_at="2026-08-02T00:00:00+00:00",
        notes_output=notes,
    )
    release_proof.command_assemble(args)

    evidence = json.loads((release_dir / "release-evidence.json").read_text())
    assert evidence["source"]["commit"] == "a" * 40
    assert len(evidence["artifacts"]) == 3
    assert {row["proofId"] for row in evidence["artifacts"]} == set(
        release_proof.PROOF_IDS
    )
    checksum_lines = (release_dir / "SHA256SUMS").read_text().splitlines()
    assert len(checksum_lines) == 9
    assert checksum_lines == sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1])
    assert "A clear release note." in notes.read_text()
    assert "v6.87.4...v6.87.5" in notes.read_text()
    commands = evidence["verification"]["attestationCommands"]
    assert len(commands) == 2
    assert all("--source-digest " + "a" * 40 in command for command in commands)
    assert all("--source-ref refs/tags/v6.87.5" in command for command in commands)
    assert "--predicate-type https://cyclonedx.org/bom" in commands[1]


def test_assemble_rejects_smoke_digest_drift(tmp_path: Path):
    release_dir, version_file, readme = _fixture_release(tmp_path)
    receipt_path = release_dir / "release-smoke-macos-arm64.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    args = argparse.Namespace(
        directory=release_dir,
        version_file=version_file,
        readme=readme,
        repository="razzant/ouroboros",
        tag="v6.87.5",
        commit="a" * 40,
        run_url="https://example.test/run",
        previous_tag=None,
        generated_at="2026-08-02T00:00:00+00:00",
        notes_output=tmp_path / "notes.md",
    )
    with pytest.raises(ValueError, match="not bound"):
        release_proof.command_assemble(args)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("proofId", "linux-x86_64", "identity"),
        ("sourceCommit", "b" * 40, "identity"),
        ("releaseTag", "v6.87.4", "identity"),
        ("checks", ["packaged_cli_help"], "missing required checks"),
    ],
)
def test_assemble_rejects_unbound_or_incomplete_smoke_receipt(
    tmp_path: Path, field: str, value: object, message: str
):
    release_dir, version_file, readme = _fixture_release(tmp_path)
    receipt_path = release_dir / "release-smoke-macos-arm64.json"
    receipt = json.loads(receipt_path.read_text())
    receipt[field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    args = argparse.Namespace(
        directory=release_dir,
        version_file=version_file,
        readme=readme,
        repository="razzant/ouroboros",
        tag="v6.87.5",
        commit="a" * 40,
        run_url="https://example.test/run",
        previous_tag=None,
        generated_at="2026-08-02T00:00:00+00:00",
        notes_output=tmp_path / "notes.md",
    )
    with pytest.raises(ValueError, match=message):
        release_proof.command_assemble(args)


def test_assemble_rejects_tag_version_mismatch(tmp_path: Path):
    release_dir, version_file, readme = _fixture_release(tmp_path)
    args = argparse.Namespace(
        directory=release_dir,
        version_file=version_file,
        readme=readme,
        repository="razzant/ouboros",
        tag="v6.87.6",
        commit="a" * 40,
        run_url="https://example.test/run",
        previous_tag=None,
        generated_at=None,
        notes_output=tmp_path / "notes.md",
    )
    with pytest.raises(ValueError, match="tag/version mismatch"):
        release_proof.command_assemble(args)


def test_verify_uploaded_requires_exact_names_sizes_and_digests(tmp_path: Path):
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    asset = release_dir / "Ouroboros-1.0.0.dmg"
    asset.write_bytes(b"artifact")
    metadata = tmp_path / "remote.json"
    metadata.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "name": asset.name,
                        "size": asset.stat().st_size,
                        "digest": f"sha256:{_digest(asset)}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    release_proof.command_verify_uploaded(
        argparse.Namespace(directory=release_dir, metadata=metadata)
    )
    asset.write_bytes(b"ARTIFACT")
    with pytest.raises(ValueError, match="digest mismatch"):
        release_proof.command_verify_uploaded(
            argparse.Namespace(directory=release_dir, metadata=metadata)
        )


def test_release_workflow_orders_smoke_sbom_attestation_and_draft_verification():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    markers = [
        "- name: Locate final release archive",
        "- name: Smoke final macOS DMG",
        "- name: Record packaged artifact smoke",
        "- name: Install digest-pinned Syft",
        "- name: Generate CycloneDX SBOM from packaged payload",
        "- name: Attest build provenance",
        "- name: Attest SBOM",
        "- name: Upload build artifact",
        "- name: Assemble release proof capsule and notes",
        "- name: Verify artifact attestations",
        "- name: Require an unpublished release slot",
        "- name: Verify remote release tag before draft",
        "- name: Create draft GitHub Release",
        "- name: Verify uploaded draft",
        "- name: Verify remote release tag before publish",
        "- name: Publish verified GitHub Release",
    ]
    positions = [workflow.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d" in workflow
    assert "anchore/sbom-action@" not in workflow
    assert "SYFT_VERSION: 1.50.0" in workflow
    assert "syft_1.50.0_darwin_arm64.tar.gz" in workflow
    assert "e32fdb9d47823fa633748a1efca2528fd77c37469ea93c9e40ab835da44e4cce" in workflow
    assert "if-no-files-found: error" in workflow
    assert "draft: true" in workflow
    assert "files: release-artifacts/*" not in workflow
    assert "matrix.sbom_path" not in workflow
    assert "steps.smoke_macos.outputs.sbom_path" in workflow
    assert 'test -x "$MOUNT/Install CLI.command"' in workflow
    assert "lipo -archs" in workflow
    assert "Refusing to modify the published release" in workflow
    assert "group: release-${{ github.ref }}" in workflow
    assert workflow.count('git ls-remote --exit-code origin "$TAG_REF" "$PEELED_REF"') == 2
    assert workflow.count('test "$(git cat-file -t "$TAG_REF")" = "tag"') == 2
    assert workflow.count('[ "$PEELED_SHA" != "$GITHUB_SHA" ]') == 2
    assert 'target_commitish: ${{ github.sha }}' in workflow
    assert '--source-digest "$GITHUB_SHA"' in workflow
    assert '--source-ref "$GITHUB_REF"' in workflow
    assert '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/ci.yml"' in workflow
    assert "--predicate-type https://cyclonedx.org/bom" in workflow
    assert "$env:USERPROFILE = $HomeDir" in workflow
    assert "$env:LOCALAPPDATA = Join-Path $HomeDir" in workflow
    assert "$env:APPDATA = Join-Path $HomeDir" in workflow
    assert "$env:HOMEDRIVE = Split-Path -Qualifier $HomeDir" in workflow
    assert "$env:HOMEPATH = $HomeDir.Substring" in workflow
    build_job = workflow[workflow.index("  build:") : workflow.index("  release:")]
    job_env = build_job[build_job.index("    env:") : build_job.index("    steps:")]
    assert "BUILD_CERTIFICATE_BASE64:" not in job_env
    assert "P12_PASSWORD:" not in job_env
    assert "KEYCHAIN_PASSWORD:" not in job_env
