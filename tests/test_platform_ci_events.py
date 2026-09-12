"""Ordinary desktop PR coverage at the existing CI matrix/event seam."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _value(expression, *, event, ref, base="", schedule=""):
    """Evaluate the workflow's small expression vocabulary against real event shapes."""
    expression = expression.strip()
    if expression.startswith("$" + "{{"):
        expression = expression[3:-2]
    expression = " ".join(expression.split()).replace("&&", " and ").replace("||", " or ")
    github = SimpleNamespace(
        event_name=event, ref=ref, base_ref=base,
        event=SimpleNamespace(
            schedule=schedule, inputs=SimpleNamespace(e2e_live="false"), before="previous-tip",
            pull_request=SimpleNamespace(base=SimpleNamespace(sha="pr-base")),
        ),
    )
    return eval(expression, {"__builtins__": {}}, {
        "github": github, "fromJSON": json.loads,
        "startsWith": lambda value, prefix: value.startswith(prefix),
    })


@pytest.mark.parametrize("event,ref,base,quick,platforms", [
    ("pull_request", "refs/pull/42/merge", "ouroboros", True, ["windows-latest", "macos-latest"]),
    ("pull_request", "refs/pull/42/merge", "main", False, []),
    ("push", "refs/heads/ouroboros", "", True, []),
    ("push", "refs/heads/main", "", False, []),
    ("push", "refs/heads/ouroboros-stable", "", False, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("push", "refs/tags/v7.0.0", "", False, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("workflow_dispatch", "refs/heads/candidate", "", True, ["ubuntu-latest", "windows-latest", "macos-latest"]),
    ("schedule", "refs/heads/main", "", False, []),
])
def test_ordinary_matrix_expands_for_prs_without_changing_existing_events(event, ref, base, quick, platforms):
    facts = {"event": event, "ref": ref, "base": base}
    assert bool(_value(WORKFLOW["jobs"]["quick-test"]["if"], **facts)) is quick
    job = WORKFLOW["jobs"]["full-test"]
    admitted = _value(job["if"], **facts)
    actual = _value(job["strategy"]["matrix"]["os"], **facts) if admitted else []
    assert actual == platforms
    assert job["strategy"]["fail-fast"] is False
    assert job["runs-on"] == "$" + "{{ matrix.os }}"


def test_desktop_pr_matrix_keeps_merge_checkout_and_pr_base_evidence_secret_free():
    triggers = WORKFLOW.get("on") or WORKFLOW[True]  # PyYAML's YAML 1.1 spelling of "on".
    assert triggers["pull_request"]["branches"] == ["ouroboros"]
    assert "pull_request_target" not in triggers
    assert WORKFLOW["permissions"] == {"contents": "read"}
    job = WORKFLOW["jobs"]["full-test"]
    assert "secrets." not in json.dumps(job)
    assert not job.get("permissions")
    checkout = job["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {"fetch-depth": 0}  # Default PR checkout tests the merge ref.
    base = next(step["env"]["OURO_SIZE_RATCHET_BASE_REF"] for step in job["steps"]
                if "OURO_SIZE_RATCHET_BASE_REF" in step.get("env", {}))
    assert _value(base, event="pull_request", ref="refs/pull/42/merge", base="ouroboros") == "pr-base"
    assert _value(base, event="push", ref="refs/heads/ouroboros-stable") == "previous-tip"


@pytest.mark.parametrize("cron", ["37 4 * * *", "17 3 * * *"])
def test_scheduled_main_runs_do_not_enter_the_ordinary_matrix(cron):
    for name in ("quick-test", "full-test"):
        assert not _value(
            WORKFLOW["jobs"][name]["if"], event="schedule", ref="refs/heads/main", schedule=cron,
        )


@pytest.mark.parametrize("name", [
    "integration-test", "skill-smoke", "system-e2e-mock", "e2e-live",
    "marker-guards", "docker-ui-smoke", "docker-portable-test",
    "release-preflight", "build", "release", "vendor-package-smoke",
])
def test_desktop_pr_coverage_does_not_admit_provider_or_release_jobs(name):
    job = WORKFLOW["jobs"][name]
    assert not _value(job["if"], event="pull_request", ref="refs/pull/42/merge", base="ouroboros")
