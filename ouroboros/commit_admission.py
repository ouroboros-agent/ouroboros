"""Deterministic commit-admission preflights (SSOT).

These checks decide whether a candidate tree may spend paid review budget at
all — release-metadata coherence (BIBLE P9), staged-Python syntax, and the
hermetic pytest run whose execution receipt can cover an equivalent later
preflight. They are ADMISSION policy, shared
by the advisory pre-review gate and the commit gate; the critic delivery
(which model reads the tree, over which transport) is a separate axis and
lives on the review substrate.

Extracted from ``ouroboros/tools/claude_advisory_review.py`` (owner decision
Q3=A, 2026-08-29): the advisory module keeps thin aliases as its monkeypatch
seams, but the single implementation lives here so the two gates can never
drift apart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
from typing import List, NamedTuple, Optional

from ouroboros.tools.registry import ToolContext
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger("ouroboros.commit_admission")


def changed_worktree_paths(
    repo_dir: pathlib.Path, paths: list[str] | None = None
) -> list[str]:
    """Changed paths from ``git status --porcelain`` (empty on any git error)."""
    from ouroboros.tools.review_helpers import parse_changed_paths_from_porcelain

    path_args = (["--"] + [str(p) for p in paths]) if paths else []
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"] + path_args,
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return parse_changed_paths_from_porcelain(result.stdout)


def auto_sync_release_metadata_if_needed(
    ctx: ToolContext,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    paths: list[str] | None,
) -> list[str]:
    """Sync VERSION-derived carriers before admission snapshot hashing."""
    selected = set(str(p) for p in (paths or []) if str(p).strip())
    touched = set(changed_worktree_paths(repo_dir))
    if "VERSION" not in selected and "VERSION" not in touched:
        return []
    try:
        from ouroboros.tools.release_sync import sync_release_metadata
        changed = list(sync_release_metadata(str(repo_dir)) or [])
        if changed:
            subprocess.run(
                ["git", "add", "--", *changed],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            append_jsonl(drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "release_metadata_auto_synced",
                "changed_files": changed,
                "task_id": str(getattr(ctx, "task_id", "") or ""),
            })
        return changed
    except Exception as exc:
        log.debug("release metadata auto-sync failed (non-fatal): %s", exc, exc_info=True)
        return []


def release_metadata_preflight(
    repo_dir: pathlib.Path,
    commit_message: str,
    paths: list[str] | None,
) -> Optional[str]:
    """Cheap deterministic P9/release checks before any paid review spend."""
    touched = set(str(p) for p in (paths or []) if str(p).strip()) | set(
        changed_worktree_paths(repo_dir, paths=paths))
    version_in_scope = "VERSION" in touched
    if touched and not version_in_scope:
        # Doc-only carve (finding W3A-F1). The commit gate ALREADY exempts a
        # doc-only diff from its compensating preflight; this admission blocked
        # the same diff outright, so on every install a doc-only change could
        # never obtain a fresh advisory verdict at all — the standard
        # preflight_review -> commit_reviewed flow degraded to the AUDITED
        # BYPASS for every doc-only change, and hardest for the two commit
        # classes BIBLE P9 exempts from the bump (a version-neutral external
        # contribution, a forensic recovery snapshot), which have no VERSION to
        # name by construction. Same classifier as the commit gate, read from
        # its owner module: one detector, so the two gates cannot drift. Narrow
        # on purpose, and NARROWER than those two classes — a code-bearing diff
        # without VERSION still blocks here whatever its provenance, and every
        # carrier-coherence check below still runs the moment VERSION IS in
        # scope.
        from ouroboros.tools.git_review_cycle import _diff_is_doc_only

        if _diff_is_doc_only(sorted(touched)):
            return None
        return (
            "⚠️ PREFLIGHT_BLOCKED: Changed files are present but VERSION is not in scope.\n"
            "  BIBLE.md P9 requires every commit to bump VERSION and sync release artifacts.\n"
            "  Stage or include VERSION plus pyproject.toml, web/package.json, README.md, and docs/ARCHITECTURE.md before advisory review.\n"
            f"  Currently changed/in-scope: {', '.join(sorted(touched)) or '(none)'}"
        )
    if not version_in_scope:
        return None
    try:
        from ouroboros.tools.release_sync import (
            check_history_limit,
            is_release_version,
            version_carrier_desyncs,
        )
        version_path = repo_dir / "VERSION"
        readme_path = repo_dir / "README.md"
        pyproject_path = repo_dir / "pyproject.toml"
        uv_lock_path = repo_dir / "uv.lock"
        web_package_path = repo_dir / "web" / "package.json"
        web_package_lock_path = repo_dir / "web" / "package-lock.json"
        arch_path = repo_dir / "docs" / "ARCHITECTURE.md"
        api_types_path = repo_dir / "web" / "modules" / "api_types.js"
        site_install_path = repo_dir / "site" / "install" / "index.html"
        docs_install_path = repo_dir / "docs" / "install" / "index.html"
        version_str = version_path.read_text(encoding="utf-8").strip()
        if not is_release_version(version_str):
            return None
        pyproject_text = pyproject_path.read_text(encoding="utf-8") if pyproject_path.exists() else ""
        uv_lock_text = uv_lock_path.read_text(encoding="utf-8") if uv_lock_path.exists() else ""
        web_package_text = web_package_path.read_text(encoding="utf-8") if web_package_path.exists() else ""
        web_package_lock_text = (
            web_package_lock_path.read_text(encoding="utf-8") if web_package_lock_path.exists() else ""
        )
        readme_text = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        arch_text = arch_path.read_text(encoding="utf-8") if arch_path.exists() else ""
        api_types_text = api_types_path.read_text(encoding="utf-8") if api_types_path.exists() else ""
        desync = version_carrier_desyncs(
            version_str,
            pyproject_text=pyproject_text,
            uv_lock_text=uv_lock_text,
            web_package_text=web_package_text,
            web_package_lock_text=web_package_lock_text,
            readme_text=readme_text,
            arch_text=arch_text,
            api_types_text=api_types_text,
            download_readme_text=readme_text,
            site_install_text=(site_install_path.read_text(encoding="utf-8") if site_install_path.exists() else ""),
            docs_install_text=(docs_install_path.read_text(encoding="utf-8") if docs_install_path.exists() else ""),
            detailed=True,
        )
        if readme_text:
            if not re.search(r'\|\s*' + re.escape(version_str) + r'\s*\|', readme_text):
                return (
                    f"⚠️ PREFLIGHT_BLOCKED: VERSION is {version_str} but README.md "
                    "changelog has no table row for this version.\n"
                    "  Add a changelog entry in the Version History table in README.md before advisory review."
                )
            limit_warnings = check_history_limit(readme_text)
            if limit_warnings:
                return (
                    "⚠️ PREFLIGHT_BLOCKED: README.md Version History exceeds BIBLE.md P9 limits.\n"
                    + "".join(f"  - {w}\n" for w in limit_warnings)
                    + "  Trim the oldest entry in the over-limit category before advisory review."
                )
        if desync:
            return (
                f"⚠️ PREFLIGHT_BLOCKED: VERSION file says {version_str} but "
                "the following worktree files have a different version value:\n"
                + "".join(f"  - {d}\n" for d in desync)
                + "Run release metadata sync before advisory review."
            )
    except Exception:
        return None
    return None


def syntax_preflight_staged_py_files(
    repo_dir: pathlib.Path,
    resolved_paths: List[str],
) -> Optional[str]:
    """Compile staged repo Python files before any paid review spend."""
    if not (repo_dir / "ouroboros" / "__init__.py").exists():
        return None

    errors: List[str] = []
    for rel in resolved_paths:
        if not rel.endswith(".py"):
            continue
        file_path = repo_dir / rel
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        try:
            compile(source, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            line = getattr(exc, "lineno", None) or "?"
            msg = getattr(exc, "msg", None) or str(exc)
            errors.append(f"{rel}:{line}: {msg}")
        except ValueError as exc:
            # Null bytes and tokenizer rejects are syntax preflight blockers too.
            errors.append(f"{rel}:?: {exc}")

    if not errors:
        return None

    return (
        "⚠️ PREFLIGHT_BLOCKED: syntax errors:\n"
        + "\n".join(f"- {err}" for err in errors)
        + "\n\nFix the syntax error(s) above and re-run preflight_review. "
        "The paid advisory episode was skipped to save budget."
    )


class PreflightTestProof(NamedTuple):
    """Process-held receipt of the runner's tested checkout and workload.

    Every workload binds HEAD as well as candidate files and the installed
    index: even an ordinary unmarked test can read committed Git content.
    No phase label, generated probe nonce or temporary pathname is a workload.
    """

    tree: str
    index_tree: str
    workload: tuple
    head: str

    def covers(self, candidate: PreflightTestProof | None) -> bool:
        return candidate is not None and self == candidate


def _executable_identity(executable: str) -> tuple:
    import shutil

    # Python locates pyvenv.cfg from the invocation path, before resolving the
    # binary symlink. Equal binary/stat facts need not mean the same environment.
    invocation = pathlib.Path(shutil.which(executable) or executable).absolute()
    path = invocation.resolve(strict=True)
    stat = path.stat()
    return str(invocation), str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def log_preflight_test_proof(ctx, proof: PreflightTestProof, *, reused: bool, phase: str,
                             passes: list[tuple[str, float]] | None = None) -> None:
    """Disclose the runner's actual proof on the existing event log, never read it as authority.

    ``passes`` are the executed passes' own `(label, seconds)`. A green run renders
    no pytest output at all, so this row is the only durable record of what the gate
    cost against `budget_sec`; a reused proof executed nothing and reports none.
    """
    event = {
        "ts": utc_now_iso(), "type": "preflight_test_proof",
        "action": "reused" if reused else "created", "phase": phase,
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "pass_seconds": dict(passes or ()), "budget_sec": proof.workload[1],
        "head": proof.head, "tree": proof.tree, "index_tree": proof.index_tree,
        "workload_fingerprint": hashlib.sha256(json.dumps(
            proof.workload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
    }
    metadata = getattr(ctx, "task_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    root = (metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", None)
            or getattr(ctx, "drive_root", None))
    try:
        if root and append_jsonl(pathlib.Path(root) / "logs" / "events.jsonl", event):
            return  # append_jsonl also forwards through the worker/server log sink.
    except Exception:
        log.warning("Preflight proof event could not be persisted", exc_info=True)
    # Diagnostics cannot turn completed tests into a failed gate. Keep the
    # binding visible even when no data root or durable log is available.
    log.warning("Preflight proof event (not persisted): %s", json.dumps(event, sort_keys=True))


def preflight_test_workload(
    repo, *, timeout=None, pytest_args=None, passes=None, agent_python=None, probe_module="",
) -> tuple:
    """Effective runner inputs, with generated paths and probe names normalized."""
    import sys
    import tempfile
    from ouroboros import preflight_runner as pr
    from ouroboros.preflight_node import candidate_node_tests, resolve_node

    base = pathlib.Path(tempfile.gettempdir()) / "ouroboros-preflight-contract"
    env = pr._preflight_env(base, base / "repo")
    environment = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()
    python = agent_python or os.environ.get("OUROBOROS_AGENT_PYTHON") or sys.executable or "python3"
    specs = pr._preflight_pass_specs(pytest_args) if passes is None else passes
    node_tests = tuple(candidate_node_tests(repo))
    return (
        tuple((p.label, tuple(pr._WORKER_PROBE_MODULE if probe_module and arg == probe_module else arg
                             for arg in p.args), p.parallel) for p in specs),
        pr._resolve_preflight_timeout(pr._DEFAULT_PREFLIGHT_TIMEOUT_SEC if timeout is None else timeout),
        environment, _executable_identity(python),
        (_executable_identity(resolve_node()), node_tests) if node_tests else (),
        pr._WORKER_PROBE_SOURCE,
    )


def capture_preflight_test_subject(
    repo, *, timeout=None, pytest_args=None, passes=None, agent_python=None, probe_module="",
) -> PreflightTestProof | None:
    """Describe the actual checkout before execution, or decline reuse.

    Reuse the candidate serializer, pass compiler and environment owner. This
    is not persisted authority and cannot turn a skipped/failed run into proof.
    """
    from ouroboros import preflight_runner as pr
    from supervisor.update_candidate import worktree_snapshot_tree

    repo = pathlib.Path(repo).resolve()
    try:
        index_tree, error = pr._capture_source_index_tree(repo, 8000)
        if error or not index_tree:
            return None
        tree, error = worktree_snapshot_tree("HEAD", cwd=str(repo))
        if error or not tree:
            return None
        head = pr._run_git(repo, ["rev-parse", "HEAD"])
        if head.returncode:
            return None
        head = head.stdout.strip()
        workload = preflight_test_workload(
            repo, timeout=timeout, pytest_args=pytest_args, passes=passes,
            agent_python=agent_python, probe_module=probe_module,
        )
        return PreflightTestProof(tree, index_tree, workload, head)
    except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, ValueError):
        log.debug("test workload could not be bound; no proof reuse", exc_info=True)
        return None


def preflight_test_proof_matches(ctx, repo) -> bool:
    proof = getattr(ctx, "_preflight_test_proof", None)
    return isinstance(proof, PreflightTestProof) and proof.covers(capture_preflight_test_subject(repo))


def preflight_test_workload_unchanged(proof, repo, *, timeout, pytest_args) -> bool:
    try:
        return proof.workload == preflight_test_workload(repo, timeout=timeout, pytest_args=pytest_args)
    except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, ValueError):
        return False  # inability to bind reuse is not a failed test


def run_tests_preflight_with_proof(ctx: ToolContext, *, runner) -> Optional[str]:
    """Run the caller's seam; only the hermetic runner can attest execution.

    None includes no-suite and policy skips. The runner stamps the ctx only
    after every applicable lane and containment check succeeded (or matched a
    process-held proof). Managed telemetry consumes that receipt, never a later
    live-tree snapshot.
    """
    from ouroboros.tools.registry import _authorized_managed_update_resolver

    force = _authorized_managed_update_resolver(ctx)
    ctx._preflight_tests_passed = False
    test_err = runner(ctx, force=True) if force else runner(ctx)
    if test_err:
        ctx._preflight_test_proof = None
        return str(test_err)
    if not ctx._preflight_tests_passed:
        ctx._preflight_test_proof = None
        return None
    try:
        from supervisor.update_merge import record_managed_tests_proof

        if force:
            record_managed_tests_proof(ctx, force=True)
        else:
            record_managed_tests_proof(ctx)
    except Exception:
        log.debug("managed tests evidence recording failed", exc_info=True)
    return None
