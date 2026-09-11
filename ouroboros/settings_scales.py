"""Ouroboros — the closed scales a settings value is clamped to.

Reasoning effort, prompt-cache tier, runtime mode and safety-supervisor coverage
are ordered or enumerated vocabularies. Each one is defined once here, with the
clamp that turns any caller-supplied or environment-supplied text into a member
of it, so an unknown value can never reach a consumer.
"""

from __future__ import annotations

from typing import Any

from ouroboros.settings_defaults import SETTINGS_DEFAULTS
from ouroboros.settings_integrity import runtime_setting

# v6.57.0 — EFFORT_SCALE: ORDERED reasoning-effort SSOT (low→high), the single place a tier is
# defined (settings, llm.py builder, switch_model enum, subagent lanes). `ultra` = the codex
# vendor tier above `max`; above-ceiling tiers adapt per route (API wire recovery / delegated).
EFFORT_SCALE: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def effort_rank(value: str) -> int:
    """Index of an effort in EFFORT_SCALE (−1 if unknown). Strength-ordering SSOT."""
    v = str(value or "").strip().lower()
    return EFFORT_SCALE.index(v) if v in EFFORT_SCALE else -1


def clamp_effort_to(value: str, ceiling: str) -> str:
    """Clamp ``value`` down to ``ceiling`` on EFFORT_SCALE; unknown inputs pass through."""
    vi, ci = effort_rank(value), effort_rank(ceiling)
    return ceiling if (vi >= 0 and ci >= 0 and vi > ci) else str(value or "").strip().lower()


def effort_one_step_down(value: str) -> str:
    """Next-lower effort on EFFORT_SCALE (reject-and-retry walk); floors at `none`."""
    idx = effort_rank(value)
    return EFFORT_SCALE[idx - 1] if idx > 0 else ("none" if idx == 0 else "medium")


def resolve_effort(task_type: str) -> str:
    """Return the configured reasoning effort for the given task type."""
    t = (task_type or "").lower().strip()

    if t == "evolution":
        key = "OUROBOROS_EFFORT_EVOLUTION"
        default = "high"
    elif t == "review":
        key = "OUROBOROS_EFFORT_REVIEW"
        default = "high"
    elif t == "deep_self_review":
        key = "OUROBOROS_EFFORT_DEEP_SELF_REVIEW"
        default = "high"
    elif t in ("scope_review", "scope-review"):
        key = "OUROBOROS_EFFORT_SCOPE_REVIEW"
        default = "high"
    elif t == "consciousness":
        key = "OUROBOROS_EFFORT_CONSCIOUSNESS"
        default = "high"
    else:
        # Legacy INITIAL_REASONING_EFFORT is retired; use EFFORT_TASK.
        key = "OUROBOROS_EFFORT_TASK"
        default = "medium"

    raw = runtime_setting(key, default)
    return raw if raw in EFFORT_SCALE else default


# Prompt-cache TTL scale (owner decision 2026-08-08): 'default' = bare markers (provider default tier), '5m'/'1h' =
# the two documented Anthropic ephemeral tiers. Deliberately NO 'auto' (dead until an adaptive design exists) and NO '24h' (Anthropic would clamp it — a value that mostly lies).
PROMPT_CACHE_TTL_SCALE: tuple[str, ...] = ("default", "5m", "1h")


def resolve_prompt_cache_ttl() -> str:
    """The owner-configured global prompt-cache TTL ('default' | '5m' | '1h').

    Validated like ``resolve_effort``: an unknown value falls back to the shipped default.
    Consumed ONLY by the finalizer (``llm.LLMClient._normalize_payload_cache_ttl``), by
    ``review_helpers.cached_prompt_blocks`` (its marker gets stamped to the same value anyway),
    and by ``usage_accounting._reservation_cost`` as the payload-free admission fallback
    (payload-carrying sites use the finalizer's applied TTL) — never by per-builder marking
    sites (docs/DEVELOPMENT.md cache-friendliness invariant)."""
    default = str(SETTINGS_DEFAULTS["OUROBOROS_PROMPT_CACHE_TTL"])
    raw = str(runtime_setting("OUROBOROS_PROMPT_CACHE_TTL", default) or "").strip().lower()
    return raw if raw in PROMPT_CACHE_TTL_SCALE else default


# Runtime mode and review enforcement are separate axes.  ``cyber_pro`` is the
# owner-selected high-power access level; it remains an ordinary member of the
# same closed scale so every consumer shares one vocabulary and rank.
VALID_RUNTIME_MODES = ("light", "advanced", "pro", "cyber_pro")

# Lower rank = stricter scope. ``save_settings`` refuses agent self-elevation.
_RUNTIME_MODE_RANK = {"light": 0, "advanced": 1, "pro": 2, "cyber_pro": 3}


def normalize_runtime_mode(value: Any) -> str:
    """Clamp caller-supplied runtime mode to the canonical closed enum."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"])
    text = str(value or "").strip().lower()
    return text if text in VALID_RUNTIME_MODES else default_val


VALID_SAFETY_MODES = ("full", "light", "off")


def normalize_safety_mode(value: Any) -> str:
    """Clamp caller-supplied safety mode to the closed enum (full / light / off)."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_SAFETY_MODE"])
    text = str(value or "").strip().lower()
    return text if text in VALID_SAFETY_MODES else default_val


_SAFETY_MODE_RANK = {"full": 2, "light": 1, "off": 0}


# Effect vocabulary shared by the owner gateway and task-local runtime readers.
IMMEDIATE_SETTINGS = frozenset({
    "TOTAL_BUDGET",
    # The OUTER per-call tool cap reads settings.json BEFORE env on every tool
    # call in every process (loop_tool_execution.py), so a saved change bites
    # the currently running task's next tool call. The inner shell subprocess
    # timeout still prefers the worker env (next task) — disclosed residual.
    "OUROBOROS_TOOL_TIMEOUT_SEC",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "OUROBOROS_UPDATE_CHANNEL",
    # The save handler hot-reconfigures MCP itself before responding
    # (_apply_settings_save_side_effects), and worker processes re-check the
    # settings mtime on their next tool-schema read; a reconfigure failure is
    # surfaced as a save warning instead of silently keeping the claim.
    "MCP_ENABLED",
    "MCP_SERVERS",
    "MCP_TOOL_TIMEOUT_SEC",
})

RESTART_REQUIRED_SETTINGS = frozenset({
    "OUROBOROS_MAX_WORKERS",
    "OUROBOROS_SERVER_HOST",
    # The host-service port is bound once at server startup.
    "OUROBOROS_HOST_SERVICE_PORT",
    # Pooled workers load the extension registry once at spawn and never
    # reload it per task; the save-time server reload keeps the skills UI
    # fresh, but agent tasks see the new repo only after a restart.
    "OUROBOROS_SKILLS_REPO_PATH",
    "LOCAL_MODEL_SOURCE",
    "LOCAL_MODEL_FILENAME",
    "LOCAL_MODEL_PORT",
    "LOCAL_MODEL_N_GPU_LAYERS",
    "LOCAL_MODEL_CONTEXT_LENGTH",
    "LOCAL_MODEL_CHAT_FORMAT",
    # Background cognition reads these at consciousness __init__, so a change
    # only takes effect after restart (Phase 4 Evolution settings group).
    "OUROBOROS_BG_WAKEUP_MIN",
    "OUROBOROS_BG_WAKEUP_MAX",
    "OUROBOROS_BG_MAX_ROUNDS",
})
