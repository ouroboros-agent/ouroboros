"""Tool registry SSOT: load tool modules, expose schemas, execute safely."""

from __future__ import annotations

import copy
import hashlib
import inspect
import logging
import os
import pathlib
import re
import subprocess
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from ouroboros.runtime_mode_policy import (
    PROTECTED_RUNTIME_PATHS,
    mode_allows_protected_write,
    protected_paths_in,
    protected_write_block_message,
)
from ouroboros.tool_capabilities import (
    ACTING_SUBAGENT_MODE,
    ACTING_SUBAGENT_TOOL_NAMES,
    CORE_TOOL_NAMES,
    LOCAL_READONLY_SUBAGENT_MODE,
    LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
    META_TOOL_NAMES,
)
from ouroboros.tool_module_inventory import tool_modules_for_runtime
from ouroboros.shell_parse import (
    is_absolute_path_text,
    path_text_is_inside,
    shell_argv,
    shell_argv_with_path_tokens,
    shell_command_string,
    strip_leading_env_assignments,
    sudo_noninteractive_violation,
    unwrap_env_argv,
)
from ouroboros.tools.shell_guards import (
    LIGHT_SHELL_WRITER_COMMANDS,
    PROTECTED_RUNTIME_PATHS_LOWER,
    interpreter_family,
    light_shell_repo_mutation,
    parse_porcelain_paths,
    process_shell_guard_args,
    shell_has_write_indicator,
    runtime_data_guard_targets,
    shell_writer_targets_protected,
    workspace_executor_state_write_block,
    writer_target_tokens,
)
from ouroboros.artifacts import task_artifact_dir_path, task_id_for_artifacts
from ouroboros.protected_artifacts import shell_block_reason as protected_artifact_shell_block_reason
from ouroboros.git_shell_policy import run_shell_git_block_reason, workspace_git_safety_violation
from ouroboros.tool_access import (
    binding_targets_system_repo,  # noqa: F401 -- public compatibility re-export
    build_resolved_resource_binding,
    canonical_repo_relative_path,
    is_external_workspace,
    light_cognitive_or_root_redirect,
    normalize_root,
    normalize_root_relative,  # noqa: F401 -- public compatibility re-export
    resolve_shell_cwd,
    shell_cwd_block_message,
    UserFilesPathBlockedError,
    workspace_mode_block_reason,
)
from ouroboros.tools.tool_catalog import (
    DuplicateToolNameError as _DuplicateToolNameError,
    ToolCatalog as _ToolCatalog,
    ToolEntry,
    partition_shadowed_tools as _partition_shadowed_tools,
)
from ouroboros.tools.tool_context import BrowserState, ToolContext  # noqa: F401 -- public compatibility re-export
from ouroboros.tools.tool_resolution import (
    _GENERIC_VCS_TARGET_TOOLS,
    _PATH_NORMALIZED_TOOLS,  # noqa: F401 -- public compatibility re-export
    _PROCESS_TARGET_TOOLS,  # noqa: F401 -- public compatibility re-export
    _SKILL_LIFECYCLE_TARGET_TOOLS,  # noqa: F401 -- public compatibility re-export
    _TARGET_BINDING_OPERATIONS,  # noqa: F401 -- public compatibility re-export
    _VERIFY_RUN_KINDS,  # noqa: F401 -- public compatibility re-export
    _binding_items,
    _binding_set_is_light_restricted,
    _binding_set_targets_system_repo,
    _binding_state_drive_root,
    _build_builtin_target_binding,
    _coerce_real_path,  # noqa: F401 -- public compatibility re-export
    _normalize_dispatch_path_args,
    _target_binding_operation,
    active_repo_dir_for,
    system_repo_dir_for,
)
from ouroboros.tools.tool_result import (  # noqa: F401 -- public compatibility re-export
    LegacyTextResultAdapter,
    ToolResult,
    _compose_execute_result,
)
from ouroboros.tools.registry_guards import (
    _EPHEMERAL_ALLOWED_TOOLS,
    _GITHUB_TOKEN_TOOLS,  # noqa: F401 -- public compatibility re-export
    _HEAL_MODE_ALLOWED_TOOLS,  # noqa: F401 -- public compatibility re-export
    _WEB_TOOLS,  # noqa: F401 -- public compatibility re-export
    _builtin_tool_availability,
    _capability_resource_guard_result,
    _disabled_tools,
    _ephemeral_block_result,
    _heal_mode_guard_result,
    _heal_protected_payload_sidecar,  # noqa: F401 -- public compatibility re-export
    _managed_update_code_tool_block as _managed_update_code_tool_block,
    _resource_allowed,
    _subagent_and_update_guard_result,
    _task_constraint_path_allowed,  # noqa: F401 -- public compatibility re-export
)
from ouroboros.python_interpreter import record_python_resolution, resolve_process_python
from ouroboros.utils import safe_relpath
from ouroboros.contracts.task_constraint import TaskConstraint, VALID_WRITE_SURFACES, normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import (
    SKILL_OWNER_STATE_FILENAMES,
    SKILL_OWNER_STATE_STEMS,
    SKILL_PAYLOAD_CONTROL_DIRNAMES,
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    constraint_bucket_skill,  # noqa: F401 -- public compatibility re-export
    cross_skill_redirect_error,
    decide_payload_short_form,
    is_skill_payload_control_filename,  # noqa: F401 -- public compatibility re-export
    is_skill_payload_path,
    resolve_skill_payload_target,
    synthesize_payload_constraint,
)

log = logging.getLogger(__name__)
_FROZEN_TOOL_MANIFEST_PATH: pathlib.Path | None = None


def _executor_backend_candidate_allowed(ctx: Any, candidate: str, allowed_roots: List[pathlib.Path]) -> bool:
    try:
        from ouroboros.workspace_executor import executor_ref_from_ctx as _executor_ref_from_ctx
        from ouroboros.workspace_executor import map_backend_path as _executor_map_backend_path

        executor_ref = _executor_ref_from_ctx(ctx)
        if executor_ref is None:
            return False
        resolved = _executor_map_backend_path(executor_ref, candidate)
        return any(resolved.is_relative_to(root) for root in allowed_roots)
    except Exception:
        return False


def _detect_runtime_mode_elevation(text_lower: str) -> bool:
    """Detect shell/script attempts to change ``OUROBOROS_RUNTIME_MODE``."""
    has_save = "save_settings" in text_lower
    has_mode_key = "ouroboros_runtime_mode" in text_lower
    has_dotted_path = "ouroboros.config.save_settings" in text_lower
    return (has_save and has_mode_key) or has_dotted_path


_SUBAGENT_SHELL_SECRET_MARKERS = (
    # Ouroboros owner secrets/control state. The relative form (no leading slash)
    # closes the interpreter-string bypass (CW4, v6.34.0): the whole-command
    # substring scan already catches "/data/settings.json" and "../../data/..",
    # but a bare "data/settings.json" (e.g. python -c "open('data/settings.json')"
    # from a workspace cwd) needs the slash-less marker too.
    "/data/settings.json", "data/settings.json", "ouroboros/data/settings", "file1.txt",
    # Universal credential/secret/control files (relative or absolute).
    ".env", ".git/config", ".git/credentials", "credentials.json", "tokens.json",
    "/.ssh/", ".ssh/", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pgpass", ".aws/",
)


def _subagent_shell_targets_secret(cmd_path_lower: str) -> bool:
    """Deterministic guard: a shell command referencing Ouroboros secrets/credentials
    or owner-control state (settings.json, ssh keys, token/credential files)."""
    return any(marker in cmd_path_lower for marker in _SUBAGENT_SHELL_SECRET_MARKERS)


def _command_mentions_protected_root(cmd_path_lower: str, root_text: str) -> bool:
    """Boundary-aware path containment for the workspace shell guard.

    True only when ``root_text`` (a normalised, lower-cased protected root path)
    appears in the command as a whole path or a parent prefix at a real path
    boundary — NOT as an incidental substring of an unrelated path that merely
    shares the prefix (e.g. protected ``/x/data`` must not match ``/x/database``).
    Used as a coarse catch-all for runtime paths embedded in non-tokenised text
    (e.g. inside a ``python -c`` string); the precise per-token containment loop
    still does the authoritative active/protected classification.
    """
    if not root_text:
        return False
    norm = root_text.rstrip("/")
    if not norm:
        return False
    span = len(norm)
    limit = len(cmd_path_lower)
    start = 0
    while True:
        idx = cmd_path_lower.find(norm, start)
        if idx < 0:
            return False
        end = idx + span
        nxt = cmd_path_lower[end] if end < limit else ""
        # Boundary = end-of-string, a path separator (child path), or a shell
        # token delimiter (the exact path). A trailing path char (letter/digit/
        # ``.``/``-``/``_``) means a DIFFERENT sibling path → keep scanning.
        if nxt == "" or nxt == "/" or nxt in " \t\"')(;:,&|<>":
            return True
        start = end


def _stray_skill_payload_failsoft(root_arg: str, workspace_mode: bool, task_constraint: Any) -> bool:
    """Whether stray bucket/skill_name on a write tool should be DROPPED rather than
    surfaced as SKILL_PAYLOAD_ARG_ERROR. Fail-soft ONLY for a WORKSPACE edit that is
    NOT skill-authoring: there bucket/skill_name are model noise (the B2 footgun —
    reflexive bucket="external" on an /app edit). In light/advanced non-workspace
    skill-authoring (or an explicit root=skill_payload / skill_repair) the specific
    error is the intended helpful signal."""
    skill_payload_intent = root_arg == "skill_payload" or bool(
        task_constraint and getattr(task_constraint, "mode", "") == "skill_repair"
    )
    return bool(workspace_mode and not skill_payload_intent)


def _detect_mutative_toggle_self_change(text_lower: str) -> bool:
    """Detect shell/script/CLI attempts to change the owner-only mutative-subagents toggle."""
    has_key = "ouroboros_allow_mutative_subagents" in text_lower
    has_write = (
        "save_settings" in text_lower
        or "settings.json" in text_lower
        or "/api/settings" in text_lower
        or "settings set" in text_lower  # `ouroboros settings set <key> <value>` CLI path
        or "ouroboros.cli" in text_lower
    )
    return has_key and has_write


def _authorized_managed_update_resolver(ctx: Any) -> bool:
    """Whether this task is the durable tx-authorized assisted resolver."""
    try:
        from supervisor.update_merge import authorized_assisted_task

        return bool(authorized_assisted_task(
            getattr(ctx, "task_id", ""),
            getattr(ctx, "task_metadata", None),
        ))
    except Exception:
        return False


def _detect_evolution_owner_control_self_change(text_lower: str) -> bool:
    """Detect shell/script/CLI attempts to set the owner-only self-evolution controls:
    the post-task evolution toggle OR the persistent evolution-objective steer (which
    biases every evolution campaign, so it is owner-only like the toggle)."""
    has_key = (
        "ouroboros_post_task_evolution" in text_lower
        or "ouroboros_evolution_persistent_objective" in text_lower
    )
    has_write = (
        "save_settings" in text_lower
        or "settings.json" in text_lower
        or "/api/settings" in text_lower
        or "settings set" in text_lower
        or "ouroboros.cli" in text_lower
    )
    return has_key and has_write


def _detect_context_mode_self_lowering(text_lower: str) -> bool:
    """Detect shell/script attempts to lower the owner-controlled context mode."""
    mentions_context_key = "ouroboros_context_mode" in text_lower
    mentions_owner_endpoint = "/api/owner/context-mode" in text_lower
    mentions_context_endpoint = "context-mode" in text_lower and "/api/owner" in text_lower
    mentions_context_cli = "context-mode" in text_lower and (
        "ouroboros settings" in text_lower
        or "ouroboros.cli" in text_lower
    )
    mentions_save = "save_settings" in text_lower or "settings.json" in text_lower
    mentions_owner_lowering_flag = "allow_context_lowering" in text_lower
    return (
        mentions_owner_endpoint
        or mentions_context_endpoint
        or mentions_context_cli
        or mentions_owner_lowering_flag
        or (mentions_context_key and mentions_save)
    )


# Commands that can only READ. This is an ALLOWLIST on purpose: an unrecognised
# command head is treated as executable access, so the enumeration fails CLOSED.
# (A denylist of "write markers" fails OPEN — every new spelling of a POST walks
# around it, which is exactly the keyword-gate antipattern BIBLE P5 forbids.)
_READ_ONLY_INSPECTION_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "ripgrep",
    "cat", "bat", "head", "tail", "less", "more", "nl", "strings",
    "ls", "find", "fd", "stat", "file", "wc", "sort", "uniq", "cut", "tr", "column",
    "basename", "dirname", "realpath", "readlink", "diff", "cmp", "jq", "yq",
    "echo", "printf", "true", "pwd", "date", "tree",
})
# Wrappers that do not themselves act: the real command head follows them.
_COMMAND_HEAD_WRAPPERS = frozenset({
    "sudo", "env", "command", "builtin", "exec", "nohup", "time", "nice", "ionice",
    "stdbuf", "\\",
})
# ``git`` reads only through these subcommands.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "grep", "log", "show", "diff", "blame", "cat-file", "ls-files", "ls-tree",
    "rev-parse", "status", "describe",
})
# Allowlist MEMBERSHIP IS NOT ENOUGH: several read heads execute or write through their
# own options. Per command, because short flags are not portable — ``grep -o`` prints
# matches, ``sort -o`` writes a file. Text reaching here is lowercased, so an upper-case
# spelling (``git grep -O``, ``fd -X``) collapses onto the same entry.
_SEARCH_TOOL_EXEC_OPTIONS = frozenset({"--pre", "--pre-glob", "--hostname-bin", "--pager"})
_DENIED_READ_OPTIONS: dict = {
    # find/fd run and delete: -exec/-execdir/-ok/-okdir/-x, -delete, and the -f* writers.
    "find": frozenset({
        "-exec", "-execdir", "-ok", "-okdir", "-delete",
        "-fls", "-fprint", "-fprint0", "-fprintf",
    }),
    "fd": frozenset({"-x", "--exec", "--exec-batch"}),
    "rg": _SEARCH_TOOL_EXEC_OPTIONS,
    "ripgrep": _SEARCH_TOOL_EXEC_OPTIONS,
    "ag": _SEARCH_TOOL_EXEC_OPTIONS,
    "ack": _SEARCH_TOOL_EXEC_OPTIONS,
    "sort": frozenset({"-o", "--output", "--compress-program"}),
    "less": frozenset({"-o", "--log-file", "-k", "--lesskey-file"}),
    "more": frozenset({"-o"}),
    "file": frozenset({"-c", "--compile"}),
    # git: external diff/textconv helpers execute a configured program, -o/--output and
    # git grep -O write or spawn a pager, --exec-path relocates the git binaries.
    "git": frozenset({
        "-c", "--config-env", "--exec-path", "--ext-diff", "--textconv",
        "-o", "--output", "--open-files-in-pager",
    }),
}
# The executable itself must be a bare name or live in a system bin: ``/tmp/evil/grep``
# and ``./grep`` are shadowing, not inspection.
_TRUSTED_EXECUTABLE_DIRS = frozenset({
    "/bin", "/usr/bin", "/usr/local/bin", "/sbin", "/usr/sbin", "/opt/homebrew/bin",
})


def _trusted_read_head(token: str) -> str:
    """The allowlist-comparable command name, or "" when the executable is untrusted."""
    if "\\" in token:
        return ""  # a windows/escaped path is not a form we can resolve — fail closed
    directory, sep, name = token.rpartition("/")
    if sep and directory not in _TRUSTED_EXECUTABLE_DIRS:
        return ""
    return name.removesuffix(".exe")


def _denied_read_option(token: str, denied: frozenset) -> bool:
    """True when an argument spells an execution/mutation option of its command."""
    if not token.startswith("-") or token in {"-", "--"}:
        return False
    name = token.split("=", 1)[0]
    if name in denied:
        return True
    if name.startswith("--"):
        return False
    return any(f"-{letter}" in denied for letter in name[1:])  # bundled short cluster


# Spellings that make a shell run a command NESTED inside another one. The read exemption
# fails closed on all of them: the head-allowlist can only vouch for heads it actually sees,
# and a nested command's head is not one of them ("echo" vouching for the "curl -X POST" it
# interpolates). Refusing the CONSTRUCT rather than enumerating the payloads inside it is the
# point — no list of "what a write looks like" is ever complete (BIBLE P5).
_NESTED_EXECUTION_MARKERS = ("$(", "`", "<(", ">(")
# Bare tokens the lexer emits for the same constructs (and for a plain subshell). These used to
# be STRIPPED from the token list before the head was taken, which is precisely how the nested
# command escaped validation; they are refused instead.
_NESTED_EXECUTION_TOKENS = frozenset({"$", "(", ")", "<(", ">(", "$("})


def _is_pure_read_inspection(text_lower: str) -> bool:
    """True when EVERY command in a shell line is a read-only source inspection.

    Structural, not keyword-based: the line is split into per-command segments with
    the shared lexer (``shell_parse.shell_segments``) and each segment's HEAD is
    matched against an allowlist. An unknown head — any interpreter, HTTP client,
    or shell — is not an inspection, whatever flags or payload spelling it carries.

    Head membership is NECESSARY, NOT SUFFICIENT (review round 2): an allowed head can
    still execute through its own options (``find -exec``, ``rg --pre``, git's external
    diff/textconv) or through what precedes it. So the options are validated per command
    (``_DENIED_READ_OPTIONS``), a leading environment assignment is REFUSED rather than
    dropped (``PATH=``/``LD_PRELOAD=``/``GIT_EXTERNAL_DIFF=`` change what actually runs),
    wrappers may not carry their own flags (``env -i``, ``sudo -e``), and the executable
    must resolve to a bare name or a system bin. Anything unrecognised stays fail-closed.

    NESTED EXECUTION IS REFUSED BEFORE ANY OF THAT (review round 3). Only the heads the lexer
    actually surfaces get validated, so a command substitution hid its command from every check
    above: ``echo "$(curl -X POST .../api/owner/scope-review-floor)"`` presented the allowlisted
    ``echo``, and the write-shape detector does not recognise an HTTP POST, so the exemption was
    granted to a line that existed to reach the owner-only endpoint. A quoted substitution is
    one opaque argument token to the lexer, which is why this is a check on the TEXT and on the
    tokens, not something the per-segment head walk could have caught.
    """
    from ouroboros.shell_parse import shell_segments

    if any(marker in text_lower for marker in _NESTED_EXECUTION_MARKERS):
        return False
    segments = shell_segments(text_lower)
    if not segments:
        return False
    for segment in segments:
        if any(token in _NESTED_EXECUTION_TOKENS for token in segment):
            return False
        tokens = [token for token in segment if token]
        while tokens and tokens[0] in _COMMAND_HEAD_WRAPPERS:
            tokens = tokens[1:]
            if tokens and tokens[0].startswith("-"):
                return False  # a wrapper's own options can rebuild the environment
        if not tokens:
            continue  # a bare wrapper executes nothing
        if "=" in tokens[0] and not tokens[0].startswith(("-", "=")):
            return False  # leading env assignment: never silently discarded
        head = _trusted_read_head(tokens[0])
        if head == "git":
            if len(tokens) < 2 or tokens[1] not in _READ_ONLY_GIT_SUBCOMMANDS:
                return False
        elif not head or head not in _READ_ONLY_INSPECTION_COMMANDS:
            return False
        denied = _DENIED_READ_OPTIONS.get(head)
        if denied and any(_denied_read_option(token, denied) for token in tokens[1:]):
            return False
    return True


def _detect_scope_review_floor_self_lowering(text_lower: str, *, writeish: bool = True) -> bool:
    """Detect shell/script attempts to REACH the owner-controlled scope-review floor
    (CW1, v6.34.0). ``OUROBOROS_SCOPE_REVIEW_FLOOR`` is deprecated and enforcement-inert
    since v6.80.0 (scope-review applicability follows the owner context mode), but it is
    still an owner-only stored setting behind its dedicated audited endpoint, so the agent
    must not write it through any channel. Mirrors the context-mode guard.

    POLARITY (v6.80.0): naming the owner endpoint or the floor key in a settings context
    is blocked UNLESS the whole command line is demonstrably read-only inspection
    (``_is_pure_read_inspection``). The earlier shape — block only on a listed HTTP write
    marker — failed OPEN: ``python -c "httpx.request('POST', '.../api/owner/
    scope-review-floor', ...)"`` names the endpoint, matches no marker, and mutated the
    setting. No substring enumeration of "what a write looks like" is ever complete
    (BIBLE P5), so the enumeration was inverted to "what a read looks like", where an
    unrecognised entry is refused rather than admitted.

    Pure source inspection stays allowed: ``grep OUROBOROS_SCOPE_REVIEW_FLOOR
    data/settings.json`` and ``rg '/api/owner/scope-review-floor' ouroboros/gateway``
    read and do not act. ``writeish`` is the shell guard's own already-computed
    write-shape fact (redirects, writer commands, write-mode ``open()``); it disqualifies
    the read exemption, and its default ``True`` keeps a caller that cannot supply the
    fact fail-closed."""
    import urllib.parse

    decoded = urllib.parse.unquote(urllib.parse.unquote(text_lower)).lower()
    text = f"{text_lower} {decoded}"
    mentions_floor_key = "ouroboros_scope_review_floor" in text
    mentions_owner_endpoint = "/api/owner/scope-review-floor" in text
    mentions_floor_endpoint = "scope-review-floor" in text and "/api/owner" in text
    mentions_floor_cli = "scope-review-floor" in text and (
        "ouroboros settings" in text
        or "ouroboros.cli" in text
    )
    mentions_save = "save_settings" in text or "settings.json" in text or "/api/settings" in text
    reaches_floor = (
        mentions_owner_endpoint
        or mentions_floor_endpoint
        or mentions_floor_cli
        or (mentions_floor_key and mentions_save)
    )
    if not reaches_floor:
        return False
    return writeish or not _is_pure_read_inspection(text_lower)


def _detect_safety_mode_self_lowering(text_lower: str) -> bool:
    """Detect shell/script attempts to lower the owner-controlled LLM-safety coverage
    (v6.54.3). OUROBOROS_SAFETY_MODE gates the LLM safety supervisor layer; the agent
    must not switch it to ``light``/``off`` to remove its own supervision friction
    (BIBLE P3). Mirrors the context-mode/scope-floor self-lowering guards."""
    import urllib.parse

    # URL-decode so a percent-encoded endpoint (curl .../safety%2Dmode) cannot
    # slip the literal scan (review round 6; mirrors the attest-endpoint guard).
    decoded = urllib.parse.unquote(urllib.parse.unquote(text_lower)).lower()
    text_lower = f"{text_lower} {decoded}"
    mentions_mode_key = "ouroboros_safety_mode" in text_lower
    mentions_owner_endpoint = "/api/owner/safety-mode" in text_lower
    mentions_mode_endpoint = "safety-mode" in text_lower and "/api/owner" in text_lower
    mentions_mode_cli = "safety-mode" in text_lower and (
        "ouroboros settings" in text_lower
        or "ouroboros.cli" in text_lower
    )
    mentions_save = "save_settings" in text_lower or "settings.json" in text_lower or "/api/settings" in text_lower
    return (
        mentions_owner_endpoint
        or mentions_mode_endpoint
        or mentions_mode_cli
        or (mentions_mode_key and mentions_save)
    )


def _detect_owner_skill_attest_self_call(text_lower: str) -> bool:
    """Detect agent attempts to loopback-call the OWNER-ONLY skill owner-attestation endpoint
    (C1, v6.39). Owner-attestation skips the expensive LLM skill review; it MUST be
    owner-issued, never agent self-callable — otherwise the agent could self-bypass the
    immune system's review for its own skill. Mirrors the context-mode/scope-floor guards.

    URL-DECODE first so a percent-encoded path (e.g. ``%61ttest-review`` / ``attest%2Dreview``)
    — which Starlette decodes back to ``attest-review`` before routing — cannot slip past the
    literal match (decode twice to catch double-encoding)."""
    import urllib.parse
    decoded = urllib.parse.unquote(urllib.parse.unquote(text_lower)).lower()
    text = f"{text_lower} {decoded}"
    return "/api/owner/skills/" in text and "attest-review" in text


def _light_mode_payload_mutation_allowed(
    *,
    ctx: Any,
    tool_name: str,
    args: Dict[str, Any],
    runtime_mode: str,
    effective_constraint: Optional[TaskConstraint],
    implicit_skill_cwd_allowed: bool,
    allow_short_relative: bool,
) -> bool:
    """Return True for light-mode data skill payload edits that do not touch repo files."""

    # apply_patch/edit_batch are DELIBERATELY absent: they refuse data-plane roots
    # entirely (repo lanes only), so they can never be a payload edit — in light
    # mode they stay under the generic repo-mutation block like any repo write.
    if runtime_mode != "light" or tool_name not in {"edit_text", "write_file"}:
        return False
    requested_root = str(args.get("root", "") or "active_workspace")
    try:
        requested_root = normalize_root(requested_root)
    except Exception:
        requested_root = str(args.get("root", "") or "active_workspace")
    if requested_root in {"task_drive", "artifact_store", "user_files"}:
        return True
    legacy_data_skill_edit = False
    if tool_name == "edit_text" and requested_root == "active_workspace":
        try:
            legacy_target = resolve_skill_payload_target(
                pathlib.Path(ctx.drive_root),
                str(args.get("path", "") or ""),
            )
            legacy_data_skill_edit = legacy_target.target_path.exists() and not legacy_target.control_plane
        except Exception:
            legacy_data_skill_edit = False
    if requested_root not in {"runtime_data", "skill_payload"} and not legacy_data_skill_edit:
        return False
    return is_skill_payload_path(
        pathlib.Path(ctx.drive_root),
        str(args.get("path", "") or ""),
        constraint=effective_constraint,
        allow_short_relative=allow_short_relative,
        allow_control_plane=False,
    )


_HEAL_PROTECTED_PAYLOAD_FILENAMES = SKILL_PAYLOAD_CONTROL_FILENAMES


_SKILL_OWNER_STATE_STEMS = SKILL_OWNER_STATE_STEMS
_DETACHED_PROCESS_MARKERS = ("start_new_session", "new_session", "setsid", "preexec_fn", "nohup")


def _mentions_skill_owner_state(text_lower: str) -> bool:
    if "state" not in text_lower or "skills" not in text_lower:
        return False
    for stem in _SKILL_OWNER_STATE_STEMS:
        if f"{stem}.json" in text_lower:
            return True
        if stem in text_lower and ".json" in text_lower:
            return True
    return False


def _mentions_detached_process(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _DETACHED_PROCESS_MARKERS)


_PROCESS_COMMAND_TOOLS = frozenset({"run_command", "run_script", "start_service"})
# verify_and_record runs the agent's declared `check` like a command, so it must clear the
# same PRE-EXECUTION shell guards (subagent-secret read, protected-artifact read, sudo,
# protected-root / workspace-state / light-mode writes) — that pre-exec filter is the
# security boundary and blocks a forbidden mutation BEFORE the handler runs, so a guarded
# check cannot mutate protected state and then leave a host-attested PASS receipt. It is
# deliberately NOT in _PROCESS_COMMAND_TOOLS: those POST-execution checks (owner-file
# restore, light-repo diff, git-ref tripwire) run AFTER the handler has already written the
# receipt, so they would only annotate the returned text, not gate the durable receipt —
# adding them would give false assurance while the pre-exec guards already do the gating.
_SHELL_GUARDED_TOOLS = _PROCESS_COMMAND_TOOLS | {"verify_and_record"}
# Repo-lane write tools that take a top-level `root` arg. Every gate keyed to
# "a write that lands in the repo working tree" must judge the whole set, not
# the historical write_file/edit_text pair — a new editing primitive that misses
# one of these gates is a silently weaker lane, not a new capability.
_ROOT_ARG_REPO_WRITE_TOOLS = frozenset({"write_file", "edit_text", "apply_patch", "edit_batch"})


def _payload_write_paths(name: str, args: Dict[str, Any]) -> List[str]:
    """Repo paths a write tool will touch, in the spelling its guards must judge.

    write_file/edit_text carry `path`/`files[]` and were already canonicalized by
    `_normalize_dispatch_path_args`. apply_patch addresses files inside the patch
    text (`*** Update File: <path>`) and edit_batch inside `edits[]`, so their
    paths reach this point RAW and are canonicalized here — otherwise a
    protected-path gate reads `repo/BIBLE.md` (not a protected-table member)
    while the write lands on `BIBLE.md`.
    """

    paths: List[str] = []
    if name == "write_file":
        if isinstance(args.get("path"), str) and args["path"]:
            paths.append(args["path"])
        for entry in args.get("files") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
    elif name == "edit_text":
        if isinstance(args.get("path"), str):
            paths.append(args["path"])
    elif name == "edit_batch":
        for entry in args.get("edits") or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
    elif name == "apply_patch":
        # Derived from the REAL parser (lazy import: edit_ops imports this
        # module), so the gate can never drift from what apply_patch will do.
        # An unparseable patch yields no paths and is refused by the handler
        # before any write, so the gate has nothing to miss.
        from ouroboros.tools.edit_ops import patch_target_paths

        paths.extend(patch_target_paths(str(args.get("patch") or "")))
    return [p for p in paths if str(p or "").strip()]


_REPO_MUTATION_TOOLS = frozenset({
    "write_file",
    "commit_reviewed",
    "vcs_commit_reviewed",
    "edit_text",
    "apply_patch",
    "edit_batch",
    "vcs_revert",
    "vcs_pull_ff",
    "vcs_restore",
    "vcs_rollback",
    "promote_to_stable",
    # PR integration tools mutate the local worktree/refs.
    "fetch_pr_ref",
    "create_integration_branch",
    "cherry_pick_pr_commits",
    "stage_adaptations",
    "stage_pr_merge",
})
_SYSTEM_INTRINSIC_REPO_MUTATION_TOOLS = frozenset({
    "commit_reviewed",
    "vcs_commit_reviewed",
    "vcs_rollback",
    "promote_to_stable",
    "fetch_pr_ref",
    "create_integration_branch",
    "cherry_pick_pr_commits",
    "stage_adaptations",
    "stage_pr_merge",
})


_TOOL_ARG_ALIASES: dict[str, dict[str, str]] = {
    "*": {"max_entries": "max_results"},
}
_IGNORE_ROOT_ARG_TOOLS = frozenset({
    "commit_reviewed",
    "vcs_commit_reviewed",
})
def _handler_public_params(handler: Callable[..., Any]) -> list[str]:
    try:
        params = list(inspect.signature(handler).parameters)
    except (TypeError, ValueError):
        return []
    return [name for name in params if name not in {"ctx", "_resolved_binding"}]


def _entry_public_params(entry: "ToolEntry") -> list[str]:
    try:
        params = entry.schema.get("parameters") or {}
        props = params.get("properties")
        if isinstance(props, dict):
            return [str(name) for name in props]
    except Exception:
        pass
    return _handler_public_params(entry.handler)


def _entry_has_public_param_schema(entry: "ToolEntry") -> bool:
    try:
        params = entry.schema.get("parameters") or {}
        return isinstance(params.get("properties"), dict)
    except Exception:
        return False


def _normalize_tool_call_args(entry: "ToolEntry", args: dict[str, Any]) -> None:
    tool_name = entry.name
    accepted = set(_entry_public_params(entry))
    aliases: dict[str, str] = {}
    aliases.update(_TOOL_ARG_ALIASES.get("*", {}))
    aliases.update(_TOOL_ARG_ALIASES.get(tool_name, {}))
    for alias, canonical in aliases.items():
        if alias in args and canonical in accepted and alias not in accepted and canonical not in args:
            args[canonical] = args.pop(alias)
    if tool_name in _IGNORE_ROOT_ARG_TOOLS and "root" in args and "root" not in accepted:
        args.pop("root", None)


def _prepare_public_builtin_args(entry: "ToolEntry", args: dict[str, Any]) -> str:
    """Normalize and validate only the model-visible builtin argument surface.

    This runs after capability/lineage availability checks but before path
    normalization, target selection, Python predispatch, or target-sensitive
    guards. Private dispatch carriers therefore cannot be supplied by the model
    and invalid public calls cannot trigger target work before rejection.
    """

    _normalize_tool_call_args(entry, args)
    public_params = set(_entry_public_params(entry))
    if _entry_has_public_param_schema(entry) and any(key not in public_params for key in args):
        return _format_tool_arg_error(entry)
    try:
        inspect.signature(entry.handler).bind(object(), **args)
    except TypeError:
        return _format_tool_arg_error(entry)
    return ""


def _light_binding_failure_redirect(name: str, args: dict[str, Any]) -> str:
    """Project an existing light-mode UX redirect after a failed target bind."""

    try:
        from ouroboros.config import get_runtime_mode

        if get_runtime_mode() == "light":
            return light_cognitive_or_root_redirect(name, args) or ""
    except Exception:
        pass
    return ""


def _binding_error_text(name: str, root: str, exc: Exception) -> str:
    detail = str(exc)
    if detail.startswith("SKILL_REDIRECT_BLOCKED:"):
        return f"⚠️ {detail}"
    if detail.startswith("profile=") and " cannot " in detail:
        return f"⚠️ TOOL_ACCESS_BLOCKED: {detail.rstrip('.')}."
    if isinstance(exc, UserFilesPathBlockedError) and name in {
        "read_file", "list_files", "search_code",
    }:
        return f"⚠️ USER_FILES_PATH_BLOCKED: {detail}"
    if root == "skill_payload" and name in {"write_file", "edit_text"}:
        return f"⚠️ SKILL_PAYLOAD_ARG_ERROR: {detail}"
    prefixes = {
        "read_file": "READ_FILE_ERROR",
        "list_files": "LIST_FILES_ERROR",
        "search_code": "SEARCH_ERROR",
        "query_code": "TOOL_ARG_ERROR (query_code)",
        "write_file": "WRITE_FILE_ERROR",
        "edit_text": "EDIT_TEXT_ERROR",
        "vcs_status": "GIT_ERROR",
        "vcs_diff": "GIT_ERROR",
        "vcs_pull_ff": "PULL_ERROR",
        "vcs_restore": "RESTORE_ERROR",
        "vcs_revert": "REVERT_ERROR",
        "skill_review": "SKILL_REVIEW_ERROR",
        "skill_preflight": "SKILL_PREFLIGHT_ERROR",
        "submit_skill_to_hub": "SUBMIT_BLOCKED",
        "run_command": "SHELL_CWD_BLOCKED",
        "run_script": "SCRIPT_CWD_BLOCKED",
        "start_service": "SHELL_CWD_BLOCKED",
        "verify_and_record": "VERIFY_ERROR",
    }
    return f"⚠️ {prefixes.get(name, 'TOOL_ERROR')}: {type(exc).__name__}: {detail}"


def _payload_dispatch_constraint(
    ctx: Any,
    *,
    name: str,
    args: dict[str, Any],
    task_constraint: Optional[TaskConstraint],
    workspace_mode: bool,
) -> tuple[Optional[TaskConstraint], ToolResult | None]:
    """Preserve repair selectors without letting stray selectors retarget work."""

    raw_bucket = str(args.get("bucket", "") or "")
    raw_skill_name = str(args.get("skill_name", "") or "")
    explicit_skill_root = str(args.get("root", "") or "").strip().lower() == "skill_payload"
    short_form_decision = None if explicit_skill_root else decide_payload_short_form(
        bucket=raw_bucket,
        skill_name=raw_skill_name,
        path_text=str(args.get("path", "") or "."),
        repo_dir=pathlib.Path(ctx.repo_dir),
        drive_root=pathlib.Path(ctx.drive_root),
    )
    if explicit_skill_root:
        # Binding selection already handled the explicit target. This legacy
        # constraint exists only for the light-mode data-payload carve-out.
        synthesized = synthesize_payload_constraint(raw_bucket, raw_skill_name)
    else:
        synthesized = (
            short_form_decision.constraint
            if short_form_decision is not None
            and task_constraint
            and task_constraint.mode == "skill_repair"
            else None
        )

    if (
        (raw_bucket or raw_skill_name)
        and short_form_decision is not None
        and short_form_decision.error
        and name in {"write_file", "edit_text"}
    ):
        root_arg = str(args.get("root", "") or "").strip().lower()
        if _stray_skill_payload_failsoft(root_arg, workspace_mode, task_constraint):
            log.info(
                "Ignoring stray bucket/skill_name on %s (workspace edit, root=%s): %s",
                name,
                root_arg or "active_workspace",
                short_form_decision.error[:80],
            )
            args.pop("bucket", None)
            args.pop("skill_name", None)
            synthesized = None
        else:
            return None, ToolResult(
                status="error",
                code="TOOL_ARG_ERROR",
                text=f"⚠️ SKILL_PAYLOAD_ARG_ERROR: {short_form_decision.error}",
            )

    redirect_err = cross_skill_redirect_error(task_constraint, synthesized)
    if redirect_err and name in {"write_file", "edit_text"}:
        return None, ToolResult(
            status="blocked",
            code="HEAL_MODE_BLOCKED",
            text=f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}",
        )
    if task_constraint and task_constraint.mode == "skill_repair":
        return task_constraint, None
    return synthesized or task_constraint, None


def _format_tool_arg_error(entry: "ToolEntry") -> str:
    params = _entry_public_params(entry)
    accepted = ", ".join(params) if params else "none"
    return (
        f"⚠️ TOOL_ARG_ERROR ({entry.name}): invalid arguments for {entry.name}. "
        f"Accepted parameters: {accepted}."
    )


def _light_repo_snapshot(repo_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Worktree tripwire for light-mode shell writes, not rollback machinery."""
    try:
        repo = pathlib.Path(repo_dir)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        if status.returncode != 0:
            return None
        unstaged = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff"],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        paths = parse_porcelain_paths(status.stdout)
        digest = hashlib.sha256()
        digest.update((status.stdout or "").encode("utf-8", errors="replace"))
        digest.update((unstaged.stdout if unstaged.returncode == 0 else "").encode("utf-8", errors="replace"))
        digest.update((staged.stdout if staged.returncode == 0 else "").encode("utf-8", errors="replace"))
        for rel in paths:
            try:
                target = (repo / safe_relpath(rel)).resolve(strict=False)
                target.relative_to(repo.resolve(strict=False))
                if target.is_file() and rel in (status.stdout or ""):
                    stat = target.stat()
                    digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8"))
            except Exception:
                continue
        return {"digest": digest.hexdigest(), "paths": paths}
    except Exception:
        return None


def _format_light_repo_write_block(before: Dict[str, Any], after: Dict[str, Any], result: str, tool_name: str = "run_command") -> str:
    before_paths = set(before.get("paths") or [])
    after_paths = set(after.get("paths") or [])
    touched = sorted(after_paths | before_paths)
    listed = ", ".join(touched[:30]) if touched else "(status changed; no paths parsed)"
    if len(touched) > 30:
        listed += f", ... (+{len(touched) - 30} more)"
    return (
        "⚠️ LIGHT_MODE_REPO_WRITE_BLOCKED: runtime_mode=light detected "
        f"a mutation of the Ouroboros repository after {tool_name}. "
        "The command result is blocked and no automatic rollback was attempted "
        "to avoid overwriting concurrent human edits. "
        f"Affected/dirty paths: {listed}. Switch to advanced/pro for repo writes.\n\n"
        "Original command output:\n"
        f"{result}"
    )


def _git_ref_snapshot(repo_dir: pathlib.Path) -> Optional[Dict[str, str]]:
    try:
        repo = pathlib.Path(repo_dir)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        refs = subprocess.run(
            ["git", "show-ref", "--head", "--dereference"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        if head.returncode != 0 or refs.returncode not in (0, 1):
            return None
        digest = hashlib.sha256()
        digest.update((head.stdout or "").encode("utf-8", errors="replace"))
        digest.update((refs.stdout or "").encode("utf-8", errors="replace"))
        return {"head": (head.stdout or "").strip(), "digest": digest.hexdigest()}
    except Exception:
        return None


def _extension_dispatch_candidate(
    ctx: ToolContext,
    name: str,
) -> tuple[Optional[Dict[str, Any]], bool]:
    """Return a live descriptor or a host-attested unavailable marker."""
    try:
        from ouroboros.extension_loader import (
            get_tool as _ext_get_tool,
            is_extension_live as _ext_is_live,
            parse_extension_surface_name as _ext_parse_name,
        )
    except Exception:
        return None, False
    if not _ext_parse_name(name):
        return None, False
    try:
        ext_tool = _ext_get_tool(name)
        meta = getattr(ctx, "task_metadata", {})
        budget_root = meta.get("budget_drive_root") if isinstance(meta, dict) else ""
        capability_root = pathlib.Path(
            budget_root
            or getattr(ctx, "budget_drive_root", "")
            or getattr(ctx, "drive_root", "")
            or "."
        ).resolve(strict=False)
        if ext_tool and not _ext_is_live(
            str(ext_tool.get("skill") or ""),
            capability_root,
            repo_path=str(ext_tool.get("skills_repo_path") or "") or None,
        ):
            return None, True
        return ext_tool, False
    except Exception:
        return None, False


class ToolRegistry:
    """Tool registry; modules export ``get_tools()``."""

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self._entries: Dict[str, ToolEntry] = {}
        self._ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
        self._capability_omissions: List[Dict[str, Any]] = []
        self._base_catalog = self._load_modules()
        self._entries.update(self._base_catalog.entries)
        self._entry_origins = dict(self._base_catalog.origins)
        self._scoped_entries: Dict[str, ToolEntry] = {}
        self._handler_overrides: Dict[str, Callable] = {}

    _FROZEN_TOOL_MODULES: List[str] = []

    def _load_modules(self) -> _ToolCatalog:
        """Load frozen or package-discovered tool modules."""
        import importlib
        import logging
        module_names, inventory_errors = tool_modules_for_runtime(
            pathlib.Path(__file__).resolve().parent,
            _FROZEN_TOOL_MANIFEST_PATH,
        )
        type(self)._FROZEN_TOOL_MODULES = list(module_names)
        for error in inventory_errors:
            logging.getLogger(__name__).warning("Failed to inspect tool module: %s", error)

        catalog_entries = []
        for modname in module_names:
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for index, entry in enumerate(mod.get_tools()):
                        catalog_entries.append(
                            (f"ouroboros.tools.{modname}.get_tools[{index}]", entry)
                        )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to load tool module %s", modname, exc_info=True)
        # Duplicate detection deliberately happens outside the import-degrade
        # boundary: a first-party name collision is a broken catalog, not an
        # optional module import failure that startup may silently omit.
        return _ToolCatalog(catalog_entries)

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def register(self, entry: ToolEntry, *, origin: str = "") -> None:
        """Register one task-scoped entry without mutating the base catalog."""
        scoped_origin = str(origin or "").strip()
        if not scoped_origin:
            handler = entry.handler
            handler_module = str(getattr(handler, "__module__", "") or "unknown")
            handler_name = str(
                getattr(handler, "__qualname__", "")
                or getattr(handler, "__name__", "")
                or type(handler).__qualname__
            )
            scoped_origin = f"{handler_module}.{handler_name}"
        if entry.name in self._entries:
            raise _DuplicateToolNameError(
                entry.name,
                self._entry_origins.get(entry.name, "unknown"),
                scoped_origin,
            )
        self._scoped_entries[entry.name] = entry
        self._entries[entry.name] = entry
        self._entry_origins[entry.name] = scoped_origin

    # Contract.

    def _ctx_is_delegated_subagent(self) -> bool:
        for attr in ("task_metadata", "task_contract"):
            data = getattr(self._ctx, attr, None)
            if isinstance(data, dict) and str(data.get("delegation_role") or "").strip() == "subagent":
                return True
        return False

    def _is_local_readonly_subagent(self) -> bool:
        tc = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        if tc and tc.mode == LOCAL_READONLY_SUBAGENT_MODE:
            return True
        # Fail-closed (mirror active_tool_profile): a valid acting constraint is
        # acting; a malformed acting constraint, or any delegated subagent without
        # a valid acting constraint (incl. a missing constraint), resolves read-only.
        if self._is_acting_subagent():
            return False
        if tc and tc.mode == ACTING_SUBAGENT_MODE:
            return True
        return self._ctx_is_delegated_subagent()

    def _is_acting_subagent(self) -> bool:
        tc = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        return bool(
            tc and tc.mode == ACTING_SUBAGENT_MODE
            and str(getattr(tc, "surface", "") or "") in VALID_WRITE_SURFACES
        )

    def _acting_self_worktree(self) -> bool:
        tc = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        return bool(
            tc and getattr(tc, "mode", "") == ACTING_SUBAGENT_MODE
            and str(getattr(tc, "surface", "") or "") == "self_worktree"
        )

    def _acting_tool_grants(self) -> set:
        tc = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        return set(getattr(tc, "external_tool_grants", ()) or ()) if tc else set()

    def initial_tool_names(self) -> frozenset[str]:
        if self._is_local_readonly_subagent():
            return LOCAL_READONLY_SUBAGENT_TOOL_NAMES
        if self._is_acting_subagent():
            return ACTING_SUBAGENT_TOOL_NAMES
        return frozenset(set(self.available_tools()) | set(META_TOOL_NAMES))

    def available_tools(self) -> List[str]:
        acting_subagent = self._is_acting_subagent()
        local_readonly_subagent = self._is_local_readonly_subagent()
        disabled = _disabled_tools(self._ctx)
        return [
            e.name
            for e in self._entries.values()
            if e.name not in disabled  # declarative tool policy (task_contract.disabled_tools)
            if _builtin_tool_availability(e.name, self._ctx)[0]
            if not local_readonly_subagent or e.name in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
            if not acting_subagent or e.name in ACTING_SUBAGENT_TOOL_NAMES
        ]

    def _schema_for_entry(self, entry: ToolEntry) -> Dict[str, Any]:
        schema = entry.schema
        if self._is_local_readonly_subagent():
            if entry.name in {"read_file", "list_files", "search_code", "query_code"}:
                schema = copy.deepcopy(schema)
                root_schema = schema.get("parameters", {}).get("properties", {}).get("root", {})
                if entry.name == "search_code":
                    allowed = {"active_workspace", "system_repo", "skill_payload"}
                elif entry.name == "query_code":
                    # query_code itself rejects non-repo roots — do not advertise more.
                    allowed = {"active_workspace", "system_repo"}
                else:
                    allowed = {"active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store"}
                if isinstance(root_schema.get("enum"), list): root_schema["enum"] = [root for root in root_schema["enum"] if root in allowed]
            elif entry.name in {"browse_page", "browser_action"}:
                schema = copy.deepcopy(entry.schema)
                if entry.name == "browse_page":
                    schema["description"] = "Open an HTTP(S) URL (external, or localhost on non-Ouroboros ports) or a file:// path under your workspace in a headless browser. Returns page content as text, html, markdown, or screenshot (base64 PNG) — use it with analyze_screenshot to visually verify your own built apps. The Ouroboros API ports, private/link-local IPs, and other URL schemes are blocked for subagents. Use viewport to test mobile layouts (e.g. '375x812')."
                if entry.name == "browser_action":
                    schema["description"] = "Perform action on the current browser page (external HTTP(S), localhost on non-Ouroboros ports, or a file:// page under your workspace). Actions: click (selector), fill (selector + value), select (selector + value), screenshot (base64 PNG), scroll (value: up/down/top/bottom). JavaScript evaluate is unavailable to local-readonly subagents."
                    props = schema.get("parameters", {}).get("properties", {})
                    action_schema = props.get("action", {})
                    if isinstance((action_enum := action_schema.get("enum")), list):
                        action_schema["enum"] = [name for name in action_enum if name != "evaluate"]
                    if isinstance((value_schema := props.get("value", {})), dict): value_schema["description"] = "Value for fill/select or direction for scroll"
            elif entry.name == "schedule_subagent":
                # A read-only subagent may delegate read-only children only — hide the
                # acting (mutative) fields so it cannot spawn an acting grandchild.
                schema = copy.deepcopy(schema)
                props = schema.get("parameters", {}).get("properties", {})
                for field in ("write_surface", "write_root", "protected_paths_grant", "external_tool_grants"):
                    props.pop(field, None)
        elif self._is_acting_subagent():
            # Advertise only what the acting profile can actually execute: writes go
            # ONLY to the isolated surface (active_workspace); reads use the read roots;
            # browser evaluate is unavailable (rejected at execute time).
            if entry.name in _ROOT_ARG_REPO_WRITE_TOOLS or entry.name in _GENERIC_VCS_TARGET_TOOLS:
                schema = copy.deepcopy(schema)
                root_schema = schema.get("parameters", {}).get("properties", {}).get("root", {})
                if isinstance(root_schema.get("enum"), list):
                    root_schema["enum"] = [root for root in root_schema["enum"] if root == "active_workspace"]
            elif entry.name in {"read_file", "list_files", "search_code", "query_code"}:
                # Acting profile reads its own surface + data roots, NOT the live
                # system_repo (no system_repo in _POLICY['acting_subagent']).
                schema = copy.deepcopy(schema)
                root_schema = schema.get("parameters", {}).get("properties", {}).get("root", {})
                allowed = {"active_workspace"} if entry.name in {"search_code", "query_code"} else {"active_workspace", "runtime_data", "task_drive", "artifact_store"}
                if isinstance(root_schema.get("enum"), list):
                    root_schema["enum"] = [root for root in root_schema["enum"] if root in allowed]
            elif entry.name == "browser_action":
                schema = copy.deepcopy(entry.schema)
                props = schema.get("parameters", {}).get("properties", {})
                action_schema = props.get("action", {})
                if isinstance((action_enum := action_schema.get("enum")), list):
                    action_schema["enum"] = [name for name in action_enum if name != "evaluate"]
        return {"type": "function", "function": schema}

    def _schemas_for_entry(self, entry: ToolEntry) -> List[Dict[str, Any]]:
        return [self._schema_for_entry(entry)]

    def _visible_dynamic_tools(
        self, surface: str, tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        visible, shadowed = _partition_shadowed_tools(tools, self._entries)
        if not shadowed:
            return visible
        collisions = []
        for tool in shadowed:
            name = str(tool.get("name") or "")
            if surface == "extensions":
                dynamic_origin = str(tool.get("skill") or "unknown extension")
            else:
                server_id = str(tool.get("server_id") or "unknown server")
                raw_name = str(tool.get("raw_name") or "unknown tool")
                dynamic_origin = f"{server_id}:{raw_name}"
            collisions.append({
                "name": name,
                "authoritative_origin": self._entry_origins.get(name, "unknown"),
                "dynamic_origin": dynamic_origin,
            })
        collisions.sort(key=lambda item: (item["name"], item["dynamic_origin"]))
        names = sorted({item["name"] for item in collisions})
        log.error(
            "%s tool name collision omitted; authoritative catalog wins: %s",
            surface,
            ", ".join(names),
        )
        self._capability_omissions.append({
            "surface": surface,
            "reason": "name_collision",
            "kind": "registry_shadow",
            "tools": names,
            "collisions": collisions,
        })
        return visible

    def _record_mcp_slug_collisions(self, collisions: List[Dict[str, Any]]) -> None:
        if not collisions:
            return
        rows = [dict(item) for item in collisions]
        rows.sort(key=lambda item: (
            str(item.get("prefixed_name") or ""),
            str(item.get("dropped_raw_name") or ""),
        ))
        names = sorted({str(item.get("prefixed_name") or "") for item in rows})
        self._capability_omissions.append({
            "surface": "mcp",
            "reason": "name_collision",
            "kind": "provider_slug",
            "tools": [name for name in names if name],
            "collisions": rows,
        })

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        acting_subagent = self._is_acting_subagent()
        acting_grants = self._acting_tool_grants() if acting_subagent else set()
        local_readonly_subagent = self._is_local_readonly_subagent()
        ephemeral_turn = bool(getattr(self._ctx, "is_ephemeral_turn", False))
        disabled_tools = _disabled_tools(self._ctx)
        self._capability_omissions = []
        unavailable_tools = {
            entry.name: detail
            for entry in self._entries.values()
            for available, reason, detail in [_builtin_tool_availability(entry.name, self._ctx)]
            if not available and reason == "missing_credential" and entry.name not in disabled_tools
        }
        built_in = [
            schema
            for entry in self._entries.values()
            if entry.name not in disabled_tools  # declarative tool policy (task_contract.disabled_tools)
            if entry.name not in unavailable_tools
            if not local_readonly_subagent or entry.name in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
            if not acting_subagent or entry.name in ACTING_SUBAGENT_TOOL_NAMES
            if not ephemeral_turn or entry.name in _EPHEMERAL_ALLOWED_TOOLS  # CW3: default-deny allowlist
            for schema in self._schemas_for_entry(entry)
        ]
        if disabled_tools:
            self._capability_omissions.append({"surface": "tools", "reason": "disabled_by_contract", "tools": sorted(disabled_tools)})
        if unavailable_tools:
            self._capability_omissions.append({
                "surface": "tools",
                "reason": "missing_credential",
                "tools": sorted(unavailable_tools),
                "details": {name: unavailable_tools[name] for name in sorted(unavailable_tools)},
            })
        # Include live extension tool schemas in normal tool discovery.
        extension_schemas: List[Dict[str, Any]] = []
        if ephemeral_turn:
            # CW3: a short decision turn answers/routes/spawns/steers only — it gets no
            # extension surfaces, which can have durable/reviewed side effects.
            self._capability_omissions.append({"surface": "extensions", "reason": "ephemeral_turn"})
        elif not _resource_allowed(self._ctx, "network"):
            self._capability_omissions.append({"surface": "extensions", "reason": "resource_blocked", "resource": "network=false"})
        else:
            try:
                from ouroboros.extension_loader import (
                    _tools as _ext_tools,
                    _lock as _ext_lock,
                    is_extension_live as _ext_is_live,
                )
                meta = getattr(self._ctx, "task_metadata", {})
                capability_root = pathlib.Path((meta.get("budget_drive_root") if isinstance(meta, dict) else "") or getattr(self._ctx, "budget_drive_root", "") or getattr(self._ctx, "drive_root", "") or ".").resolve(strict=False)
                with _ext_lock:
                    extension_tools = [
                        dict(tool)
                        for tool in _ext_tools.values()
                        if _ext_is_live(str(tool.get("skill") or ""), capability_root, repo_path=str(tool.get("skills_repo_path") or "") or None)
                        if not acting_subagent or tool["name"] in acting_grants
                    ]
                extension_tools = self._visible_dynamic_tools("extensions", extension_tools)
                extension_schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("schema", {"type": "object", "properties": {}}),
                        },
                    }
                    for tool in extension_tools
                ]
            except Exception as exc:
                self._capability_omissions.append({"surface": "extensions", "reason": "discovery_error", "error": f"{type(exc).__name__}: {exc}"})

        if not core_only:
            mcp_schemas = []
            if ephemeral_turn:
                # CW3: MCP tools can have durable side effects — not for a decision turn.
                self._capability_omissions.append({"surface": "mcp", "reason": "ephemeral_turn"})
            elif not _resource_allowed(self._ctx, "network"):
                self._capability_omissions.append({"surface": "mcp", "reason": "resource_blocked", "resource": "network=false"})
            else:
                try:
                    from ouroboros.mcp_client import ensure_configured_from_settings as _mcp_ensure_configured, get_manager as _mcp_get_manager
                    _mcp_ensure_configured(refresh=True)
                    _mgr = _mcp_get_manager()
                    mcp_tools = [
                        tool
                        for tool in _mgr.list_tools_for_registry()
                        if not acting_subagent or tool["name"] in acting_grants
                    ]
                    mcp_tools = self._visible_dynamic_tools(
                        "mcp", mcp_tools,
                    )
                    mcp_schemas = [
                        {
                            "type": "function",
                            "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("schema", {"type": "object", "properties": {}})},
                        }
                        for tool in mcp_tools
                    ]
                    slug_collisions = getattr(
                        _mgr, "tool_name_collisions", lambda: []
                    )()
                    if acting_subagent:
                        slug_collisions = [
                            item
                            for item in slug_collisions
                            if str(item.get("prefixed_name") or "") in acting_grants
                        ]
                    self._record_mcp_slug_collisions(
                        slug_collisions
                    )
                    # D1: an enabled+configured server returning zero tools WITHOUT
                    # raising (unreachable/slow/auth-failed) is otherwise silent. Make
                    # the reason visible so the model/owner learns WHY an expected MCP
                    # server produced no tools, instead of "the agent can't see MCP".
                    # Checked unconditionally so a broken server is surfaced even when a
                    # co-located healthy server contributed tools (does not mask it).
                    _empty = _mgr.enabled_servers_without_tools()
                    if _empty:
                        self._capability_omissions.append({"surface": "mcp", "reason": "server_no_tools", "servers": _empty})
                except Exception as exc:
                    self._capability_omissions.append({"surface": "mcp", "reason": "discovery_error", "error": f"{type(exc).__name__}: {exc}"})
            combined = built_in + extension_schemas + mcp_schemas
            if disabled_tools:
                # Apply the declarative tool policy to dynamic extension/MCP schemas too, not just
                # built-ins, so a disabled name can never surface from any discovery source.
                combined = [
                    s for s in combined
                    if (s.get("function", {}) or {}).get("name") not in disabled_tools
                ]
            return combined
        # Core tools plus meta-tools for enabling extended tools.
        result = []
        for e in self._entries.values():
            if e.name in disabled_tools:  # declarative tool policy (task_contract.disabled_tools)
                continue
            if e.name in unavailable_tools:
                continue
            if local_readonly_subagent and e.name not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES:
                continue
            if acting_subagent and e.name not in ACTING_SUBAGENT_TOOL_NAMES:
                continue
            if ephemeral_turn and e.name not in _EPHEMERAL_ALLOWED_TOOLS:
                continue  # CW3: the core/initial envelope is allowlisted too, not just schemas(core_only=False)
            if (
                (local_readonly_subagent and e.name in LOCAL_READONLY_SUBAGENT_TOOL_NAMES)
                or (acting_subagent and e.name in ACTING_SUBAGENT_TOOL_NAMES)
                or e.name in CORE_TOOL_NAMES
                or e.name in ("list_available_tools", "enable_tools")
            ):
                result.extend(self._schemas_for_entry(e))
        ext = extension_schemas
        if disabled_tools:
            ext = [s for s in ext if (s.get("function", {}) or {}).get("name") not in disabled_tools]
        return result + ext

    def capability_omissions(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._capability_omissions]

    def policy_hidden_reason(self, name: str) -> Optional[str]:
        """Why a REGISTERED built-in tool is invisible to THIS task, or None.

        Read-only companion to get_schema_by_name (same predicates, same order):
        it distinguishes "hidden by policy" from "does not exist" so discovery
        answers can stop reporting a policy-filtered tool as nonexistent (F3,
        2026-08-10 saga). None means visible OR unknown name — callers that got
        no schema and no reason may honestly say "not found".
        """
        requested = str(name or "").strip()
        if not requested:
            return None
        # BEFORE the registration check: the declarative contract policy applies
        # across ALL discovery sources (get_schema_by_name checks it first for the
        # same reason), so a contract-disabled extension/MCP name answers with its
        # reason instead of "not found" (2026-08-10 amendments). Deeper extension/
        # MCP policy reasons (grants, network) would need new plumbing — disclosed
        # residual, not built.
        if requested in _disabled_tools(self._ctx):
            return "disabled by this task's contract (disabled_tools)"
        if requested not in self._entries:
            return None
        available, reason, _detail = _builtin_tool_availability(requested, self._ctx)
        if not available:
            return f"unavailable ({reason})"
        if getattr(self._ctx, "is_ephemeral_turn", False) and requested not in _EPHEMERAL_ALLOWED_TOOLS:
            return "hidden on this ephemeral decision turn (allowlist)"
        acting_subagent = self._is_acting_subagent()
        if self._is_local_readonly_subagent() and requested not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES:
            return "hidden by the read-only subagent profile"
        if acting_subagent and requested not in ACTING_SUBAGENT_TOOL_NAMES:
            return "hidden by the acting subagent profile"
        return None

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        requested = str(name or "").strip()
        acting_subagent = self._is_acting_subagent()
        acting_grants = self._acting_tool_grants() if acting_subagent else set()
        local_readonly_subagent = self._is_local_readonly_subagent()
        # Declarative tool policy applies across ALL discovery sources (built-in, extension, MCP),
        # so enable_tools/discovery can never surface a disabled name — consistent with schemas()/execute().
        if requested in _disabled_tools(self._ctx):
            return None
        entry = self._entries.get(requested)
        if entry:
            available, reason, detail = _builtin_tool_availability(requested, self._ctx)
            if not available:
                if reason == "missing_credential":
                    self._capability_omissions.append({
                        "surface": "tools",
                        "reason": reason,
                        "tools": [requested],
                        "details": {requested: detail},
                    })
                return None
            if getattr(self._ctx, "is_ephemeral_turn", False) and requested not in _EPHEMERAL_ALLOWED_TOOLS:
                return None  # CW3: allowlist-consistent with schemas()/execute() (so enable_tools can't surface a denied tool)
            if local_readonly_subagent and requested not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES:
                return None
            if acting_subagent and requested not in ACTING_SUBAGENT_TOOL_NAMES:
                return None
            return self._schema_for_entry(entry)
        try:
            from ouroboros.extension_loader import parse_extension_surface_name as _ext_parse_name
        except Exception:
            _ext_parse_name = None
        if _ext_parse_name and _ext_parse_name(name):
            if acting_subagent and requested not in acting_grants:
                return None
            if not _resource_allowed(self._ctx, "network"):
                self._capability_omissions.append({"surface": "extensions", "reason": "resource_blocked", "resource": "network=false"})
                return None
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool, is_extension_live as _ext_is_live
                ext_tool = _ext_get_tool(name)
                meta = getattr(self._ctx, "task_metadata", {})
                capability_root = pathlib.Path((meta.get("budget_drive_root") if isinstance(meta, dict) else "") or getattr(self._ctx, "budget_drive_root", "") or getattr(self._ctx, "drive_root", "") or ".").resolve(strict=False)
            except Exception:
                ext_tool = None
            if (
                ext_tool
                and _ext_is_live(str(ext_tool.get("skill") or ""), capability_root, repo_path=str(ext_tool.get("skills_repo_path") or "") or None)
            ):
                return {
                    "type": "function",
                    "function": {
                        "name": ext_tool["name"],
                        "description": ext_tool.get("description", ""),
                        "parameters": ext_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        try:
            from ouroboros.mcp_client import (
                ensure_configured_from_settings as _mcp_ensure_configured,
                get_manager as _mcp_get_manager,
                is_mcp_tool_name as _mcp_is_name,
            )
            _mcp_ensure_configured(refresh=False)
        except Exception:
            _mcp_get_manager = None
            _mcp_is_name = None
        if _mcp_get_manager and _mcp_is_name and _mcp_is_name(requested):
            if acting_subagent and requested not in acting_grants:
                return None
            if not _resource_allowed(self._ctx, "network"):
                self._capability_omissions.append({"surface": "mcp", "reason": "resource_blocked", "resource": "network=false"})
                return None
            mcp_tool = _mcp_get_manager().get_tool(requested)
            if mcp_tool:
                return {
                    "type": "function",
                    "function": {
                        "name": mcp_tool["name"],
                        "description": mcp_tool.get("description", ""),
                        "parameters": mcp_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 360)."""
        entry = self._entries.get(str(name or "").strip())
        if entry is not None:
            return entry.timeout_sec
        # Extension tools carry timeout_sec in the loader descriptor.
        try:
            from ouroboros.extension_loader import parse_extension_surface_name as _ext_parse_name
        except Exception:
            _ext_parse_name = None
        if _ext_parse_name and _ext_parse_name(name):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None
            if ext_tool:
                # Add cleanup grace around the inner async wait_for.
                return int(ext_tool.get("timeout_sec") or 60) + 3
        try:
            from ouroboros.mcp_client import (
                ensure_configured_from_settings as _mcp_ensure_configured,
                get_manager as _mcp_get_manager,
                is_mcp_tool_name as _mcp_is_name,
            )
            _mcp_ensure_configured(refresh=False)
        except Exception:
            _mcp_get_manager = None
            _mcp_is_name = None
        if _mcp_get_manager and _mcp_is_name and _mcp_is_name(name):
            try:
                return int(_mcp_get_manager().tool_timeout_sec()) + 3
            except Exception:
                return 63
        return 360

    def _dispatch_extension_tool(self, name: str, ext_tool: Dict[str, Any], args: Optional[Dict[str, Any]]) -> ToolResult:
        """Dispatch live extension tools through the registry's typed seam."""
        from ouroboros.tools.extension_dispatch import _dispatch_extension_tool_result

        return _dispatch_extension_tool_result(self._ctx, name, ext_tool, args)

    def _dispatch_mcp_tool(self, name: str, args: Dict[str, Any]) -> ToolResult:
        """Run one MCP tool while preserving provider-owned result facts."""
        from ouroboros.safety import check_safety as _mcp_check_safety
        is_safe, safety_msg = _mcp_check_safety(
            name,
            args,
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
        )
        if not is_safe:
            return ToolResult(status="blocked", code="SAFETY_VIOLATION", text=safety_msg)
        try:
            from ouroboros.mcp_client import _call_mcp_tool_result as _mcp_call

            result = _mcp_call(name, args or {})
        except Exception as exc:
            text = f"⚠️ TOOL_ERROR ({name}): {exc}"
            return ToolResult(status="error", code="TOOL_ERROR", text=text)
        if not safety_msg:
            return result
        text = _compose_execute_result(result.text, "", safety_msg)
        meta = {**dict(result.meta), "safety_warning": True}
        if result.code == "OK":
            return ToolResult(status="ok", code="SAFETY_WARNING", text=text, meta=meta)
        return ToolResult(status=result.status, code=result.code, text=text, meta=meta)

    def _protected_shell_block(
        self, raw_cmd, cmd_path_lower, binding, acting_self_worktree,
    ) -> Optional[str]:
        """Apply payload/core write guards to the selected physical target."""
        items = _binding_items(binding)
        targets_skill = bool(items) and all(item.root == "skill_payload" for item in items)
        targets_system = (
            _binding_set_targets_system_repo(self._ctx, binding)
            or acting_self_worktree
        )
        if (targets_skill or targets_system) and any(
            name in cmd_path_lower
            for name in (
                *SKILL_PAYLOAD_CONTROL_FILENAMES,
                *(SKILL_PAYLOAD_CONTROL_DIRNAMES - {"__pycache__"}),
            )
        ) and shell_has_write_indicator(raw_cmd):
            return (
                "⚠️ SAFETY_VIOLATION: Shell command would modify a skill "
                "provenance / launcher seed / dependency marker (.clawhub.json, "
                ".ouroboroshub.json, .self_authored.json, SKILL.openclaw.md, .seed-origin, "
                ".ouroboros_env, node_modules). "
                "Use marketplace lifecycle flows or edit user-authored "
                "payload files instead."
            )
        if _authorized_managed_update_resolver(self._ctx):
            return None
        if targets_system and shell_writer_targets_protected(raw_cmd):
            return (
                "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                "a protected core/contract/release file. Protected: "
                + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
            )
        if targets_system:
            for cf in PROTECTED_RUNTIME_PATHS_LOWER:
                if cf in cmd_path_lower and shell_has_write_indicator(raw_cmd):
                    return (
                        "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                        "a protected core/contract/release file. Protected: "
                        + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
                    )
        return None

    def _git_protected_roots(self) -> list:
        """Ouroboros runtime roots the target-aware git resolver protects, by
        enumeration: the system repo + EVERY data drive the task touches (parent
        drive plus any child / budget drive in task_metadata). Missing a child
        drive here would let git escape into the control plane. ONE enumeration
        for the external-workspace lane and the default (non-workspace) lane."""
        git_protected_roots = [
            pathlib.Path(getattr(self._ctx, "system_repo_dir", None) or self._ctx.repo_dir),
            pathlib.Path(self._ctx.repo_dir),
            pathlib.Path(self._ctx.drive_root),
        ]
        _meta = getattr(self._ctx, "task_metadata", {})
        if isinstance(_meta, dict):
            for _k in ("drive_root", "child_drive_root", "headless_child_drive_root", "budget_drive_root"):
                if _meta.get(_k):
                    git_protected_roots.append(pathlib.Path(str(_meta.get(_k))))
        return git_protected_roots

    def _resolved_shell_cwd(self, args: Dict[str, Any], binding: Any = None) -> Any:
        """The command's working directory, resolved ONCE through the cwd SSOT.

        Returns a ``pathlib.Path``, or the typed cwd-block MESSAGE (a ``str``) when
        resolution fails. Every guard downstream takes this canonical path instead
        of re-resolving — or, worse, string-joining the raw cwd label onto a root,
        which is the D1 regression class (v6.74.0)."""
        items = _binding_items(binding)
        if items:
            return pathlib.Path(items[0].target_path)
        raw_cwd = str(args.get("cwd") or "")
        operation = "service" if str(args.get("__tool_name") or "") == "start_service" else "shell"
        try:
            work_dir, _cwd_root, _allowed = resolve_shell_cwd(self._ctx, raw_cwd, operation=operation)
        except Exception as exc:
            return shell_cwd_block_message(self._ctx, raw_cwd, operation=operation, error=exc)
        return pathlib.Path(work_dir)

    def _external_workspace_git_block(self, raw_cmd: Any, work_dir: pathlib.Path) -> Optional[str]:
        from ouroboros.git_shell_policy import external_workspace_git_violation

        # External-workspace git is no longer confined to the active workspace
        # (host scratch is legitimate); only the enumerated runtime roots are
        # protected. ``work_dir`` is the ALREADY-RESOLVED cwd from the one
        # resolve_shell_cwd call in _shell_git_and_runtime_block — passing it as
        # the base with cwd="" keeps the D1 rule (resolve once, through the SSOT,
        # never re-join a raw cwd label onto a root).
        git_violation = external_workspace_git_violation(
            raw_cmd,
            active_root=work_dir,
            cwd="",
            protected_roots=self._git_protected_roots(),
            allow_network=_resource_allowed(self._ctx, "network"),
        )
        if not git_violation:
            return None
        if git_violation.startswith("task_contract.allowed_resources"):
            return f"⚠️ RESOURCE_CONSTRAINT_BLOCKED: {git_violation}."
        return f"⚠️ WORKSPACE_GIT_BLOCKED: {git_violation}."

    def _external_runtime_protected_paths(
        self, binding: Any = None,
    ) -> tuple[list, list, list, list]:
        """Ouroboros runtime roots that an EXTERNAL-workspace task must not touch via
        shell (system repo + EVERY data drive incl child/budget + owner credential
        locations) plus the task's own exempt task_drive/artifact_store roots. Returns
        (protected_texts, allowed_texts, protected_paths, allowed_paths): the *_texts
        feed the embedded-string boundary check; the *_paths feed token resolution
        (relative->cwd, ~->home, symlink canonicalization) so relative/symlink bypasses
        are closed. SSOT for the read + write guards."""
        meta = getattr(self._ctx, "task_metadata", {}) if isinstance(getattr(self._ctx, "task_metadata", {}), dict) else {}
        protected_values = [getattr(self._ctx, "system_repo_dir", None) or getattr(self._ctx, "repo_dir", None),
                            getattr(self._ctx, "drive_root", None)]
        try:
            from ouroboros.config import DATA_DIR as _PARENT_DATA_DIR
            protected_values.append(_PARENT_DATA_DIR)
        except Exception:
            pass
        for _dk in ("drive_root", "child_drive_root", "headless_child_drive_root", "budget_drive_root"):
            if meta.get(_dk):
                protected_values.append(meta.get(_dk))
        # Owner/runtime credential locations, as ABSOLUTE paths. Blocking by
        # absolute containment (not a substring marker) means the OWNER's personal
        # secrets (~/.ssh/id_rsa, ~/.aws, ~/file1.txt) are off-limits while a
        # project-relative file merely NAMED like a credential (site/.ssh/config, a
        # project .env) stays the task's own — and a non-path token like
        # "os.environ" can never spuriously match.
        try:
            _home = pathlib.Path.home()
            for _rel in (".ssh", ".aws", ".gnupg", ".netrc", ".pgpass", ".config/gcloud",
                         ".docker/config.json", ".kube/config", ".npmrc", "file1.txt"):
                protected_values.append(_home / _rel)
        except Exception:
            pass
        def _text_forms(value: Any) -> list:
            # Both the as-given and the symlink-resolved form, so a command using
            # /var/... matches a root resolved to /private/var/... (macOS) and vice
            # versa. In production ($HOME paths) the two coincide.
            out = []
            for variant in (value, None):
                try:
                    p = pathlib.Path(value)
                    if variant is None:
                        p = p.resolve(strict=False)
                    t = str(p).replace("\\", "/").lower().rstrip("/")
                    if t and t not in out:
                        out.append(t)
                except Exception:
                    continue
            return out

        def _resolved(value: Any):
            try:
                return pathlib.Path(value).resolve(strict=False)
            except Exception:
                return None

        protected_texts: list = []
        protected_paths: list = []
        for v in protected_values:
            if not v:
                continue
            for t in _text_forms(v):
                if t not in protected_texts:
                    protected_texts.append(t)
            rp = _resolved(v)
            if rp is not None and rp not in protected_paths:
                protected_paths.append(rp)
        allowed_texts: list = []
        allowed_paths: list = []
        task_id = task_id_for_artifacts(self._ctx)
        for data_root in (getattr(self._ctx, "drive_root", None), meta.get("drive_root"), meta.get("budget_drive_root")):
            if not data_root:
                continue
            for rp_src in (pathlib.Path(data_root) / "task_drives" / task_id, task_artifact_dir_path(pathlib.Path(data_root), task_id, create=False)):
                for t in _text_forms(rp_src):
                    if t not in allowed_texts:
                        allowed_texts.append(t)
                rp = _resolved(rp_src)
                if rp is not None and rp not in allowed_paths:
                    allowed_paths.append(rp)
        # An explicitly selected system repo or exact skill payload is an
        # authorized process target. Keep every other runtime/credential root
        # protected, but do not re-block that exact binding merely because the
        # task also has an external workspace focus.
        for item in _binding_items(binding):
            if item.root not in {"system_repo", "skill_payload"}:
                continue
            selected = pathlib.Path(item.base_path)
            for t in _text_forms(selected):
                if t not in allowed_texts:
                    allowed_texts.append(t)
            rp = _resolved(selected)
            if rp is not None and rp not in allowed_paths:
                allowed_paths.append(rp)
        return protected_texts, allowed_texts, protected_paths, allowed_paths

    def _external_shell_runtime_or_secret_block(
        self, raw_cmd: Any, cmd_path_lower: str, args: Dict[str, Any],
        work_dir: Optional[pathlib.Path] = None,
        binding: Any = None,
    ) -> Optional[str]:
        """External-workspace shell guard for READ and write commands alike: block any
        command that targets the Ouroboros runtime (system repo / any data drive) or an
        owner credential path. read_file/user_files already enforce this; raw shell
        (cat, python -c open(...), etc.) would otherwise bypass it. Two layers, because
        string matching alone is bypassable by relative paths and symlinks:
          (1) embedded-string boundary match of ABSOLUTE protected roots (catches a path
              literal inside e.g. python -c "open('/abs/data/settings.json')");
          (2) path-token RESOLUTION — every path-like arg is expanduser'd, joined to the
              command cwd when relative, and resolve()'d (canonicalizing symlinks + ..),
              then containment-checked. This closes a relative path passed as its own
              argv token (`cat ../../data/settings.json`) and a workspace-internal symlink
              to the data drive (round-2 review).
        Both layers are best-effort DEFENSE-IN-DEPTH, not the primary control: a relative
        path hidden INSIDE an interpreter one-liner string (e.g. node -e
        "readFileSync('../../data/settings.json')") is not a standalone token, so it is
        not extracted here — and that residual is deliberately NOT chased with a regex
        over code strings (an unwinnable arms race; BIBLE P5 / no-string-gate doctrine).
        The PRIMARY control is the gated read_file/user_files path, which fully resolves
        and containment-checks every read against the protected drives, plus the LLM
        safety supervisor judging intent on each shell call."""
        _BLOCK = (
            "⚠️ WORKSPACE_SHELL_BLOCKED: shell command targets the Ouroboros runtime "
            "(system repo / data drive) or an owner credential path. External-workspace "
            "tasks may not read or write those; use the gated read_file tool for any "
            "inspection you need. Run your command against the task's own surfaces "
            "instead: the active workspace root (e.g. /app) or scratch such as /tmp."
        )
        protected_texts, allowed_texts, protected_paths, allowed_paths = (
            self._external_runtime_protected_paths(binding)
        )
        # (1) embedded-string boundary match (absolute roots only — no substring secret
        # markers, which would false-block the task's own project files / "os.environ").
        for pt in protected_texts:
            if _command_mentions_protected_root(cmd_path_lower, pt) and not any(
                _command_mentions_protected_root(cmd_path_lower, t) for t in allowed_texts
            ):
                return _BLOCK
        # (2) path-token resolution (relative -> cwd, ~ -> home, symlinks canonicalized).
        # The cwd is resolved ONCE per safety check by the caller (D1); resolve here
        # only when this guard is used standalone.
        if work_dir is None:
            resolved_cwd = self._resolved_shell_cwd(args, binding)
            if isinstance(resolved_cwd, str):
                return resolved_cwd
            work_dir = pathlib.Path(resolved_cwd)
        work_dir = pathlib.Path(work_dir)

        def _within(child: pathlib.Path, parent: pathlib.Path) -> bool:
            try:
                child.relative_to(parent)
                return True
            except ValueError:
                return False

        for tok in shell_argv_with_path_tokens(raw_cmd):
            tok_text = str(tok or "").strip()
            if not tok_text or tok_text.startswith("-") or tok_text in {"|", "&&", "||", ";", ">", ">>", "<", "<<", "&"}:
                continue
            try:
                p = pathlib.Path(tok_text).expanduser()
                resolved = p.resolve(strict=False) if p.is_absolute() else (work_dir / p).resolve(strict=False)
            except Exception:
                continue
            if any(_within(resolved, ap) for ap in allowed_paths):
                continue
            if any(_within(resolved, pp) for pp in protected_paths):
                return _BLOCK
        return None

    def _workspace_shell_write_block(
        self,
        args: Dict[str, Any],
        raw_cmd: Any,
        cmd_path_lower: str,
        explicit_write_targets: list[str],
        executable_path_tokens: set[str],
        runtime_mode: str,
        acting_subagent: bool,
        binding: Any,
    ) -> Optional[str]:
        """Keep workspace writes inside the selected target plus task custody roots."""

        items = _binding_items(binding)
        if not items:
            return "⚠️ WORKSPACE_SHELL_BLOCKED: process target was not resolved."
        selected = items[0]
        work_dir = pathlib.Path(selected.target_path).resolve(strict=False)
        selected_base = pathlib.Path(selected.base_path).resolve(strict=False)
        allowed_relative_roots = list(dict.fromkeys((selected_base, work_dir)))
        allowed_data_roots: list[pathlib.Path] = []
        meta = (
            getattr(self._ctx, "task_metadata", {})
            if isinstance(getattr(self._ctx, "task_metadata", {}), dict)
            else {}
        )
        for data_root in (getattr(self._ctx, "drive_root", None), meta.get("budget_drive_root")):
            if not data_root:
                continue
            task_id = task_id_for_artifacts(self._ctx)
            for root_path in (
                pathlib.Path(data_root) / "task_drives" / task_id,
                task_artifact_dir_path(pathlib.Path(data_root), task_id, create=False),
            ):
                resolved_root = pathlib.Path(root_path).resolve(strict=False)
                if resolved_root not in allowed_data_roots:
                    allowed_data_roots.append(resolved_root)
        if selected.root in {"task_drive", "artifact_store"}:
            allowed_data_roots.append(selected_base)
        # Acting subagents must write ONLY inside their isolated surface, so pro
        # mode does NOT grant them the outside-workspace absolute-path passthrough.
        pro_workspace_passthrough = (
            str(runtime_mode or "").strip().lower() == "pro" and not acting_subagent
        )
        protected_roots = [
            getattr(self._ctx, "system_repo_dir", None) or getattr(self._ctx, "repo_dir", None),
            getattr(self._ctx, "drive_root", None),
        ]
        try:
            from ouroboros.config import DATA_DIR as parent_data_dir

            protected_roots.append(parent_data_dir)
        except Exception:
            pass
        for key in ("drive_root", "child_drive_root", "headless_child_drive_root", "budget_drive_root"):
            if meta.get(key):
                protected_roots.append(meta.get(key))
        allowed_texts = [
            str(root).replace("\\", "/").lower().rstrip("/")
            for root in [*allowed_relative_roots, *allowed_data_roots]
        ]
        protected_paths = []
        for root_value in protected_roots:
            try:
                root_path = pathlib.Path(root_value).resolve(strict=False)
            except Exception:
                continue
            protected_paths.append(root_path)
            if any(root_path.is_relative_to(root) for root in allowed_relative_roots):
                continue
            root_text = str(root_path).replace("\\", "/").lower()
            if _command_mentions_protected_root(cmd_path_lower, root_text) and not any(
                _command_mentions_protected_root(cmd_path_lower, text)
                for text in allowed_texts
            ):
                return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell command mentions Ouroboros system/data paths."
        path_tokens = list(shell_argv_with_path_tokens(raw_cmd))
        path_tokens.extend(
            token
            for token in explicit_write_targets
            if token and token not in path_tokens
        )
        for token in path_tokens:
            token_text = str(token)
            if token_text in executable_path_tokens and token_text not in explicit_write_targets:
                continue
            candidates = [token_text] if is_absolute_path_text(token_text) else []
            if token_text.startswith(("./", "../")):
                candidates.append(token_text)
            elif (
                token_text
                and not token_text.startswith("-")
                and token_text not in {"|", "&&", "||", ";", ">", ">>", "<", "<<"}
                and (
                    token_text in explicit_write_targets
                    or "/" in token_text
                    or "\\" in token_text
                )
            ):
                candidates.append(token_text)
            for candidate in candidates:
                if candidate == "/dev/null":
                    continue
                if is_absolute_path_text(candidate):
                    if _executor_backend_candidate_allowed(
                        self._ctx,
                        candidate,
                        [*allowed_relative_roots, *allowed_data_roots],
                    ):
                        continue
                    windows_drive_path = bool(re.match(r"^[A-Za-z]:[\\/]", candidate))
                    unc_path = candidate.startswith("\\\\")
                    # On the native Windows host, resolve drive paths exactly as
                    # POSIX paths are resolved below. This canonicalizes directory
                    # symlinks/junctions before containment: a workspace alias stays
                    # allowed, while an in-workspace spelling whose nested link exits
                    # the root is blocked. Keep lexical handling for foreign Windows
                    # spellings seen on POSIX and for UNC paths (which may require a
                    # network lookup merely to evaluate the guard).
                    if (not windows_drive_path and not unc_path) or (
                        os.name == "nt" and windows_drive_path
                    ):
                        try:
                            resolved = pathlib.Path(candidate).resolve(strict=False)
                        except Exception:
                            continue
                        if any(resolved.is_relative_to(root) for root in allowed_relative_roots):
                            continue
                        if any(resolved.is_relative_to(root) for root in allowed_data_roots):
                            continue
                        for protected_path in protected_paths:
                            try:
                                resolved.relative_to(protected_path)
                                return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell command mentions Ouroboros system/data paths."
                            except Exception:
                                pass
                        if not pro_workspace_passthrough:
                            return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell commands may not target paths outside the selected process root."
                        continue
                    if any(path_text_is_inside(candidate, root) for root in allowed_relative_roots):
                        continue
                    if any(path_text_is_inside(candidate, root) for root in allowed_data_roots):
                        continue
                    for protected_path in protected_paths:
                        if path_text_is_inside(candidate, protected_path):
                            return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell command mentions Ouroboros system/data paths."
                    if not pro_workspace_passthrough:
                        return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell commands may not target paths outside the selected process root."
                    continue
                resolved = (work_dir / pathlib.Path(candidate)).resolve(strict=False)
                if any(resolved.is_relative_to(root) for root in allowed_relative_roots):
                    continue
                if any(resolved.is_relative_to(root) for root in allowed_data_roots):
                    continue
                for protected_path in protected_paths:
                    try:
                        resolved.relative_to(protected_path)
                        return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell command mentions Ouroboros system/data paths."
                    except Exception:
                        pass
                if not pro_workspace_passthrough:
                    return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell commands may not target paths outside the selected process root."
        return None

    def _run_shell_safety_check(
        self, args: Dict[str, Any], runtime_mode: str, binding: Any = None,
    ) -> Optional[str]:
        """Pre-execution run_command filter; returns a block message or ``None``."""
        raw_cmd = args.get("cmd", args.get("command", ""))
        if binding is None:
            operation = (
                "service"
                if str(args.get("__tool_name") or "") == "start_service"
                else "shell"
            )
            try:
                binding = build_resolved_resource_binding(
                    self._ctx,
                    operation=operation,
                    process_cwd=str(args.get("cwd") or ""),
                    bucket=str(args.get("bucket") or ""),
                    skill_name=str(args.get("skill_name") or ""),
                )
            except Exception as exc:
                return shell_cwd_block_message(
                    self._ctx,
                    str(args.get("cwd") or ""),
                    operation=operation,
                    error=exc,
                )
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        # self_worktree is a checkout of the system repo, so protected shell-write
        # guards must stay active for it even in workspace mode (acting children
        # must use write_file/edit_text, which apply the pro+grant gate).
        acting_self_worktree = self._acting_self_worktree()
        acting_subagent = self._is_acting_subagent()
        argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
        if sudo_noninteractive_violation(argv):
            return (
                "⚠️ SUDO_INTERACTIVE_BLOCKED: sudo must be noninteractive. Use sudo -n for commands that can run without a password; if sudo -n fails, report validation/install blocked by environment."
            )
        cmd_lower = (" ".join(str(x) for x in raw_cmd) if isinstance(raw_cmd, list) else str(raw_cmd)).lower()
        cmd_path_lower = cmd_lower.replace("\\", "/")
        while "//" in cmd_path_lower: cmd_path_lower = cmd_path_lower.replace("//", "/")
        # Subagents must not read owner secrets/credentials/control state via shell
        # (read_file already denies these). read_file is the gated inspection path.
        if (acting_subagent or self._is_local_readonly_subagent()) and _subagent_shell_targets_secret(cmd_path_lower):
            return (
                "⚠️ SUBAGENT_SECRET_READ_BLOCKED: subagents may not read Ouroboros secrets, "
                "credentials, or owner-control state via shell. Use the gated read_file tool "
                "(which denies secrets) for any inspection you actually need."
            )
        argv_for_write = argv
        argv_executable = pathlib.PurePath(argv_for_write[0]).name.lower().removesuffix(".exe") if argv_for_write else ""
        write_target_argvs = [argv_for_write] if argv_for_write else []
        if argv_executable in {"sh", "bash", "zsh"}:
            inline_cmd = next((str(argv_for_write[idx + 1] or "") for idx, token in enumerate(argv_for_write[1:], start=1) if str(token or "") in {"-c", "--command"} and idx + 1 < len(argv_for_write)), "")
            if not inline_cmd:
                inline_cmd = shell_command_string(argv_for_write)
            inline_argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(inline_cmd)))
            if inline_argv:
                write_target_argvs.append(inline_argv)
        explicit_write_targets = list(dict.fromkeys(str(token) for target_argv in write_target_argvs for token in writer_target_tokens(target_argv) if str(token or "").strip()))
        executable_path_tokens = {str(target_argv[0]) for target_argv in write_target_argvs if target_argv}
        # Writer-command membership canonicalizes versioned interpreter spellings to
        # their family (`ruby3.2` is `ruby`), so a versioned basename is exactly as
        # write-suspect as the unversioned one (XG-2R.2).
        writeish = shell_has_write_indicator(raw_cmd) or (bool(argv_for_write) and (interpreter_family(argv_executable) or argv_executable) in LIGHT_SHELL_WRITER_COMMANDS) or bool(explicit_write_targets)
        work_dir = self._resolved_shell_cwd(args, binding)
        if isinstance(work_dir, str):
            return work_dir
        if protected_artifact_block := protected_artifact_shell_block_reason(
            self._ctx,
            raw_cmd,
            cwd=str(work_dir),
            default_cwd=pathlib.Path(work_dir),
            binding=_binding_items(binding)[0] if _binding_items(binding) else None,
        ):
            return protected_artifact_block
        if writeish and (executor_state_block := workspace_executor_state_write_block(
            raw_cmd,
            drive_root=pathlib.Path(self._ctx.drive_root),
            cwd=str(work_dir),
            default_cwd=pathlib.Path(work_dir),
        )):
            return executor_state_block
        if workspace_mode and writeish:
            workspace_write_block = self._workspace_shell_write_block(
                args,
                raw_cmd,
                cmd_path_lower,
                explicit_write_targets,
                executable_path_tokens,
                runtime_mode,
                acting_subagent,
                binding,
            )
            if workspace_write_block:
                return workspace_write_block

        # Elevation pattern: blocked in all modes.
        if _detect_runtime_mode_elevation(cmd_lower):
            return "⚠️ ELEVATION_BLOCKED: shell command pattern looks like an OUROBOROS_RUNTIME_MODE elevation attempt (mentions ``save_settings`` together with ``OUROBOROS_RUNTIME_MODE``, or invokes ``ouroboros.config.save_settings`` directly). Runtime mode is owner-controlled — change it by stopping the agent and editing settings.json directly, then restart."
        if _detect_context_mode_self_lowering(cmd_lower):
            return "⚠️ CONTEXT_MODE_SELF_LOWERING_BLOCKED: shell command pattern looks like an attempt to lower OUROBOROS_CONTEXT_MODE to low through settings.json or /api/owner/context-mode. Context mode is owner-controlled — ask the owner to change the Low/Max toggle or edit settings while the agent is stopped."
        if _detect_scope_review_floor_self_lowering(cmd_lower, writeish=writeish):
            return "⚠️ SCOPE_REVIEW_FLOOR_SELF_LOWERING_BLOCKED: shell command pattern reaches OUROBOROS_SCOPE_REVIEW_FLOOR through settings.json, /api/settings, or /api/owner/scope-review-floor from something other than a pure read. The floor is a deprecated, enforcement-inert owner setting (BIBLE P3 scope-review applicability follows the owner context mode) — it stays owner-only, and the agent must not write owner settings through any channel. Ask the owner to change it via the dedicated /api/owner/scope-review-floor endpoint, or stop the agent and edit settings.json directly. Pure source inspection (grep/rg/cat/jq/git grep) is allowed; an interpreter or HTTP client naming the endpoint is not, whatever verb it spells."
        if _detect_safety_mode_self_lowering(cmd_lower):
            return "⚠️ SAFETY_MODE_SELF_LOWERING_BLOCKED: shell command pattern looks like an attempt to change OUROBOROS_SAFETY_MODE (e.g. to ``light``/``off``) through settings.json, /api/settings, or /api/owner/safety-mode. LLM-safety coverage is owner-controlled (BIBLE P3) — the agent must not reduce its own supervision. Ask the owner to change it via the dedicated /api/owner/safety-mode endpoint, or stop the agent and edit settings.json directly."
        if _detect_owner_skill_attest_self_call(cmd_lower):
            return "⚠️ OWNER_SKILL_ATTESTATION_SELF_CALL_BLOCKED: shell command pattern looks like an attempt to loopback-POST /api/owner/skills/<skill>/attest-review. Owner-attestation skips the expensive LLM skill review and is OWNER-ONLY — the agent must not self-attest its own skill to bypass the immune system's review. Ask the owner to attest it from the Skills UI."
        if _detect_mutative_toggle_self_change(cmd_lower):
            return "⚠️ ELEVATION_BLOCKED: OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS is owner-controlled (it grants subagents write power against the live body). Change it by stopping the agent and editing settings.json directly, then restart — the agent must not self-enable mutative subagents."
        if _detect_evolution_owner_control_self_change(cmd_lower):
            return "⚠️ ELEVATION_BLOCKED: the self-evolution controls (OUROBOROS_POST_TASK_EVOLUTION and OUROBOROS_EVOLUTION_PERSISTENT_OBJECTIVE) are owner-controlled — they enable or steer self-modification cycles. Change them via the owner Settings UI, or stop the agent and edit settings.json directly — the agent must not self-set evolution controls."
        if _mentions_skill_owner_state(cmd_lower):
            return (
                "⚠️ SKILL_STATE_WRITE_BLOCKED: skill review, enablement, "
                "grants, and marketplace provenance are owner/review "
                "controlled state. Use skill_review, toggle_skill/the Skills "
                "UI, or the desktop launcher confirmation flow."
            )
        if "state" in cmd_lower and "skills" in cmd_lower and _mentions_detached_process(cmd_lower):
            return (
                "⚠️ SKILL_STATE_WRITE_BLOCKED: detached shell processes must "
                "not target skill state directories. Use the reviewed skill "
                "lifecycle tools instead."
            )

        # Light-mode checks follow the selected physical target, not whether a
        # project workspace happens to be attached.
        if runtime_mode == "light":
            if light_shell_repo_mutation(
                raw_cmd,
                repo_dir=system_repo_dir_for(self._ctx),
                cwd=str(args.get("cwd") or ""),
                work_dir=pathlib.Path(work_dir),
                # Inline-code inspection now reaches EVERY surface this check guards
                # (it defaults ON in the fence) — scoping it to `__tool_name ==
                # "run_script"` let run_command mutate the repo first (XG-7B3.1).
            ):
                return (
                    "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses "
                    "shell commands that mutate the Ouroboros repository. "
                    "For external deliverables, run with cwd under user_files "
                    "(for example /Users/<you>/Desktop), root=artifact_store, "
                    "or root=task_drive. Switch to advanced/pro only for "
                    "reviewed Ouroboros self-modification."
                )
            runtime_data_executable = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe") if argv else ""
            # Versioned interpreter basenames (python3.11, ruby3.2, php8.3,
            # perl5.38, node18) must trigger the runtime_data scan exactly like
            # their unversioned spellings. Classification is the shared structural
            # `interpreter_family` — the exact-set + `startswith("python")` pair
            # recognized versions of ONE family and let every other family's
            # versioned spelling bypass the guard (XG-2R.2).
            runtime_data_scan = (
                writeish
                or runtime_data_executable in {"sh", "bash", "zsh"}
                or bool(interpreter_family(runtime_data_executable))
            )
            if runtime_data_scan:
                own_task_drive = pathlib.Path(self._ctx.task_drive_root())
                own_artifact_dir = task_artifact_dir_path(
                    pathlib.Path(self._ctx.drive_root),
                    task_id_for_artifacts(self._ctx),
                    create=False,
                )
                allowed_runtime_roots = [own_task_drive, own_artifact_dir]
                for item in _binding_items(binding):
                    if item.root == "skill_payload" and item.source != "native":
                        allowed_runtime_roots.append(pathlib.Path(item.base_path))
                runtime_data_targets = runtime_data_guard_targets(
                    raw_cmd,
                    writeish=writeish,
                    drive_root=pathlib.Path(self._ctx.drive_root),
                    work_dir=pathlib.Path(work_dir),
                    allowed_roots=allowed_runtime_roots,
                )
                if runtime_data_targets:
                    action = "write under" if writeish else "write-indicating commands that mention"
                    # Name the REAL task roots: a mis-guessed absolute path used to
                    # produce this block with no way to self-correct (v6.54.3).
                    return (
                        "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light blocks process commands "
                        f"that {action} runtime_data paths outside this task's own roots. "
                        f"This task's real roots are: artifact_store={own_artifact_dir}, "
                        f"task_drive={own_task_drive} — staged attachments live under "
                        f"{own_artifact_dir / 'attachments'}. Use those absolute paths in scripts, "
                        "or root=artifact_store / root=task_drive / root=user_files in file tools. "
                        "Blocked paths: " + ", ".join(runtime_data_targets[:5])
                    )

        if protected_shell := self._protected_shell_block(
            raw_cmd, cmd_path_lower, binding, acting_self_worktree,
        ):
            return protected_shell

        # GitHub repo create/delete/auth.
        cmd_words = re.sub(r"\s+", " ", cmd_lower)
        if "gh repo create" in cmd_words or "gh repo delete" in cmd_words:
            return "⚠️ SAFETY_VIOLATION: Creating/deleting GitHub repositories requires admin approval."
        if "gh auth" in cmd_words:
            return "⚠️ SAFETY_VIOLATION: Modifying GitHub authentication is not permitted."

        return self._shell_git_and_runtime_block(
            raw_cmd, args, cmd_path_lower, workspace_mode,
            acting_self_worktree, binding,
        )

    def _shell_git_and_runtime_block(
        self, raw_cmd: Any, args: Dict[str, Any], cmd_path_lower: str,
        workspace_mode: bool, acting_self_worktree: bool, binding: Any,
    ) -> Optional[str]:
        """Direct-git-via-shell policy + the external-workspace runtime/secret read
        guard. External workspaces AND the default (non-workspace) lane get full
        task-local git through ONE target-aware resolver — only the Ouroboros
        runtime is protected (Q4=A unwind, 2026-08-08) — while raw non-git shell
        in external workspaces still cannot read the runtime/secrets;
        self_worktree keeps the strict read-only git policy."""
        from ouroboros.git_shell_policy import is_readonly_git_command

        if not shell_argv(raw_cmd):
            return None
        if workspace_mode and not acting_self_worktree:
            work_dir = self._resolved_shell_cwd(args, binding)
            if isinstance(work_dir, str):  # a cwd block message, not a path
                return work_dir
            if git_block := self._external_workspace_git_block(raw_cmd, work_dir):
                return git_block
            # Even READ-only, non-git shell (cat/head/grep/python -c open(...)) must
            # not reach the runtime or secrets — close the raw-shell bypass of the
            # user_files path guard (scoped to top-level external tasks).
            #
            # READ-ONLY GIT IS EXEMPT (owner contract, Q4=A: "read-only everywhere",
            # and the f14baf8f false-block class). `git -C <system repo> status|log|
            # diff|show|rev-parse` is the vcs_status-equivalent inspection lane; the
            # runtime-read guard was catching it by path token and refusing it with a
            # WORKSPACE_SHELL_BLOCKED that named the wrong reason. The marginal
            # escalation is nil — the same history is already readable through the
            # gated read_file this very message points the agent at — while the
            # SECRET/credential surface stays closed because the exemption is
            # ALL-or-nothing per segment (`git status && cat <data>/settings.json`
            # is not exempt; every non-git shell still meets the full guard) AND
            # write-aware: `is_readonly_git_command` refuses the key to a read-only
            # subcommand carrying the file-truncating `--output=<file>` diff option
            # or `--no-index` (which reads arbitrary host files), so neither a
            # runtime write nor a settings.json dump can ride "read-only git".
            if is_external_workspace(self._ctx) and not is_readonly_git_command(raw_cmd):
                if ext_block := self._external_shell_runtime_or_secret_block(
                    raw_cmd, cmd_path_lower, args, work_dir=work_dir,
                    binding=binding,
                ):
                    return ext_block
            return None
        if workspace_mode:
            # Acting self_worktree: a checkout of the Ouroboros repo itself; the
            # acting-child contract (no commits anywhere — a moved HEAD fails patch
            # capture closed; patch integration) keeps the strict read-only git
            # policy, UNWEAKENED by the target-aware default lane below: both the
            # workspace-escape check and the blanket mutating-git text classifier
            # keep running for this lane.
            work_dir = self._resolved_shell_cwd(args, binding)
            if isinstance(work_dir, str):
                return work_dir
            binding_item = _binding_items(binding)[0]
            active_root = pathlib.Path(binding_item.base_path)
            try:
                binding_cwd = pathlib.Path(work_dir).relative_to(active_root).as_posix()
            except ValueError:
                binding_cwd = ""
            git_violation = workspace_git_safety_violation(
                raw_cmd,
                active_root=active_root,
                cwd=binding_cwd,
                allow_network=_resource_allowed(self._ctx, "network"),
            )
            if git_violation:
                if git_violation.startswith("task_contract.allowed_resources"):
                    return f"⚠️ RESOURCE_CONSTRAINT_BLOCKED: {git_violation}."
                return (
                    "⚠️ WORKSPACE_GIT_BLOCKED: run_command may only use read-only git "
                    f"operations inside the active workspace; blocked {git_violation}."
                )
            git_violation = run_shell_git_block_reason(
                raw_cmd,
                allow_network=_resource_allowed(self._ctx, "network"),
            )
            if git_violation:
                if git_violation.startswith("task_contract.allowed_resources"):
                    return f"⚠️ RESOURCE_CONSTRAINT_BLOCKED: {git_violation}."
                subcmd = git_violation.removeprefix("git ").strip() or git_violation
                return (
                    f"⚠️ GIT_VIA_SHELL_BLOCKED: `git {subcmd}` is blocked for acting "
                    "self_worktree children (no commits; the parent integrates the "
                    "returned patch and is the sole committer). For read-only git: "
                    "vcs_status, vcs_diff tools, or run_command with git "
                    "log/show/diff/status/rev-list/show-ref/for-each-ref/listing branch-tag forms."
                )
            return None
        # DEFAULT (non-workspace) lane — direct chat, light mode, self_modification-
        # profile tasks. Q4=A (owner, 2026-08-08): mutating git is free EVERYWHERE
        # outside the Ouroboros runtime, in every runtime mode and lane. The
        # argv-text blanket (blocked ANY mutating git with a commit_reviewed remedy
        # that is false for non-repo trees) is replaced by the SAME target-aware
        # resolver the external lane has run since v6.27: read-only git stays
        # allowed even at a runtime target, mutating git is blocked only when it
        # TARGETS the runtime (bidirectional/casefold/symlink-resolved containment),
        # and the contract network fence rides along. The cwd resolves EXACTLY ONCE
        # through the shared resolver and is passed as a canonical path — never
        # re-join a raw label onto a root (the v6.74.0 D1 regression class).
        # Disclosed residual (proportionality; no shell-parser arms race): git via
        # a transparent wrapper (nice/xargs) or interpreter code is not classified
        # here — the pre-flip text classifier never saw the interpreter form either,
        # and the LLM safety layer still reviews intent. The light-mode post-exec
        # system-repo dirtiness tripwire stays as the backstop.
        if "git" not in cmd_path_lower:
            return None
        work_dir = self._resolved_shell_cwd(args, binding)
        if isinstance(work_dir, str):  # a cwd block message, not a path
            return work_dir
        from ouroboros.git_shell_policy import external_workspace_git_violation

        git_violation = external_workspace_git_violation(
            raw_cmd,
            active_root=work_dir,
            cwd="",
            protected_roots=self._git_protected_roots(),
            allow_network=_resource_allowed(self._ctx, "network"),
        )
        if not git_violation:
            return None
        if git_violation.startswith("task_contract.allowed_resources"):
            return f"⚠️ RESOURCE_CONSTRAINT_BLOCKED: {git_violation}."
        return (
            f"⚠️ GIT_VIA_SHELL_BLOCKED: {git_violation}. Mutating git may not target "
            "the Ouroboros runtime (system repo / data drives): self-repo changes go "
            "through commit_reviewed, which enforces pre-commit checks and review. "
            "Read-only git (status/log/diff/show/rev-parse/branch- and tag-listing, "
            "or the vcs_status/vcs_diff tools) works everywhere, and mutating git is "
            "free in any tree OUTSIDE the runtime (e.g. ~/projects, /tmp, an attached "
            "project folder)."
        )

    def _snapshot_owner_files(
        self, state_drive_root: pathlib.Path | None = None,
    ) -> Dict[pathlib.Path, Optional[str]]:
        from ouroboros import config as _cfg
        out: Dict[pathlib.Path, Optional[str]] = {}
        settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
        try:
            out[settings_path] = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
        except OSError:
            out[settings_path] = None
        root = pathlib.Path(state_drive_root or self._ctx.drive_root) / "state" / "skills"
        if not root.is_dir():
            return out
        for path in root.glob("*/*"):
            if path.name.lower() not in SKILL_OWNER_STATE_FILENAMES:
                continue
            try:
                out[path] = path.read_text(encoding="utf-8")
            except OSError:
                out[path] = None
        return out

    def _restore_owner_files(
        self,
        before: Dict[pathlib.Path, Optional[str]],
        state_drive_root: pathlib.Path | None = None,
    ) -> bool:
        from ouroboros import config as _cfg
        root = pathlib.Path(state_drive_root or self._ctx.drive_root) / "state" / "skills"
        current = set()
        if root.is_dir():
            current.update(
                path for path in root.glob("*/*")
                if path.name.lower() in SKILL_OWNER_STATE_FILENAMES
            )
        settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
        current.add(settings_path)
        changed = False
        for path in current - set(before):
            try:
                path.unlink()
                changed = True
            except OSError:
                pass
        for path, content in before.items():
            try:
                if content is None:
                    if path.exists():
                        path.unlink()
                        changed = True
                    continue
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    changed = True
            except OSError:
                pass
        return changed

    def _run_shell_post_checks(
        self,
        result: str,
        *,
        owner_snapshot: Dict[pathlib.Path, Optional[str]],
        state_drive_root: pathlib.Path,
        light_repo_before: Optional[Dict[str, Any]],
        workspace_refs_before: Optional[Dict[str, str]],
        tool_name: str = "run_command",
    ) -> str:
        import time

        restored_owner_state = False
        for _ in range(4):
            time.sleep(0.3)
            restored_owner_state = (
                self._restore_owner_files(owner_snapshot, state_drive_root)
                or restored_owner_state
            )
        if restored_owner_state:
            result = (
                f"{result}\n\n⚠️ OWNER_STATE_RESTORED: run_command attempted to "
                "change owner-only settings or skill trust state; protected files were restored."
            )
        if light_repo_before is not None:
            light_repo_after = _light_repo_snapshot(system_repo_dir_for(self._ctx))
            if (
                light_repo_after is not None
                and light_repo_after.get("digest") != light_repo_before.get("digest")
            ):
                result = _format_light_repo_write_block(light_repo_before, light_repo_after, result, tool_name=tool_name)
        if workspace_refs_before is not None:
            workspace_refs_after = _git_ref_snapshot(active_repo_dir_for(self._ctx))
            if (
                workspace_refs_after is not None
                and workspace_refs_after.get("digest") != workspace_refs_before.get("digest")
            ):
                result = (
                    "⚠️ WORKSPACE_GIT_REF_CHANGED: run_command changed git HEAD or refs "
                    "inside the external workspace. External workspace runs must leave "
                    "changes as files/patch artifacts, not commits/tags/resets.\n\n"
                    "Original command output:\n"
                    f"{result}"
                )
        return result

    def _resolve_python_predispatch(
        self,
        name: str,
        args: Dict[str, Any],
        runtime_mode: str,
        effective_constraint: Any,
        resolved_binding: Any = None,
    ) -> tuple[Dict[str, Any], Any, str]:
        """Resolve an exact python/python3 request ONCE, before the shell guard.

        Every downstream guard and the handler therefore see byte-identical
        argv; launchers must not select an interpreter after this boundary.
        """
        args, python_resolution = resolve_process_python(
            self._ctx,
            name,
            args,
            runtime_mode=runtime_mode,
            effective_constraint=effective_constraint,
            resolved_binding=resolved_binding,
        )
        record_python_resolution(self._ctx, python_resolution)
        if python_resolution is not None and python_resolution.error_reason:
            if python_resolution.error_reason == "cwd_resolution_failed":
                # The failure is the CWD CONFINEMENT policy, not interpreter
                # provenance: python argv is resolved pre-dispatch, so without
                # this the same bad cwd that gets the self-healing
                # SHELL_CWD_BLOCKED root list from a non-python command got an
                # opaque interpreter message naming nothing (submarine waves
                # 1/3, `python3 -m http.server` in a coop tree). Emit the ONE
                # canonical cwd message (label=path root list); the
                # python_interpreter_resolution trace above keeps the true
                # reason, and the typed SHELL_CWD_BLOCKED status lands in the
                # policy-denial family instead of degrading execution.
                return args, python_resolution, shell_cwd_block_message(
                    self._ctx,
                    str((args or {}).get("cwd") or ""),
                    operation="service" if name == "start_service" else "shell",
                )
            return args, python_resolution, (
                "⚠️ PYTHON_INTERPRETER_UNAVAILABLE: Ouroboros could not prove "
                "the target interpreter for this launch surface "
                f"({python_resolution.error_reason}). The process was not started."
            )
        return args, python_resolution, ""

    def _invoke_builtin_handler(
        self,
        name: str,
        entry: Any,
        args: Dict[str, Any],
        resolved_binding: Any,
        python_resolution: Any,
        worktree_before: Any,
    ) -> tuple[str | None, Any]:
        """Run one builtin handler; returns (early_error_text, result).

        The launcher attestation lives exactly as long as the handler call:
        run_script consults it to accept the resolver-chosen interpreter.
        """
        missing = object()
        prior = getattr(self._ctx, "_active_python_resolution", missing)
        self._ctx._active_python_resolution = python_resolution
        try:
            try:
                handler_args = dict(args)
                if resolved_binding is not None:
                    parameters = inspect.signature(entry.handler).parameters
                    if "_resolved_binding" not in parameters:
                        return (
                            f"⚠️ TOOL_INTERNAL_ERROR ({name}): target-sensitive handler "
                            "does not declare the private _resolved_binding keyword.",
                            None,
                        )
                    handler_args["_resolved_binding"] = resolved_binding
                try:
                    inspect.signature(entry.handler).bind(self._ctx, **handler_args)
                except TypeError:
                    return _format_tool_arg_error(entry), None
                return None, entry.handler(self._ctx, **handler_args)
            except TypeError as e:
                return f"⚠️ TOOL_ERROR ({name}): {e}", None
            except Exception as e:
                return f"⚠️ TOOL_ERROR ({name}): {e}", None
        finally:
            if prior is missing:
                try:
                    delattr(self._ctx, "_active_python_resolution")
                except AttributeError:
                    pass
            else:
                self._ctx._active_python_resolution = prior
            # Central advisory invalidation by OBSERVED worktree diff: runs on
            # success, tool error, and exception paths alike (the per-tool
            # manual calls missed early-return/error paths), and skips
            # invalidation when a flagged tool ran read-only.
            if worktree_before is not None:
                self._invalidate_advisory_if_worktree_changed(name, worktree_before)

    def _execute_legacy_text(self, name: str, args: Dict[str, Any]) -> str | ToolResult:
        name = str(name or "").strip()
        args = dict(args or {})
        _route_note = ""
        task_constraint = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        local_readonly_subagent = self._is_local_readonly_subagent()
        acting_subagent = self._is_acting_subagent()
        acting_self_worktree = acting_subagent and str(getattr(task_constraint, "surface", "") or "") == "self_worktree"
        acting_protected_grant = acting_subagent and bool(getattr(task_constraint, "protected_paths_grant", False))
        acting_tool_grants = set(getattr(task_constraint, "external_tool_grants", ()) or ()) if acting_subagent else set()
        entry = self._entries.get(name)
        ext_tool, extension_unavailable = _extension_dispatch_candidate(
            self._ctx,
            name,
        ) if entry is None else (None, False)

        _mcp_is_name = None
        if entry is None and ext_tool is None:
            try:
                from ouroboros.mcp_client import (
                    ensure_configured_from_settings as _mcp_ensure_configured,
                    is_mcp_tool_name as _mcp_is_name,
                )
                _mcp_ensure_configured(refresh=False)
            except Exception:
                _mcp_is_name = None
        is_mcp = bool(_mcp_is_name and _mcp_is_name(name))
        _eph = _ephemeral_block_result(self._ctx, name, ext_tool, is_mcp)
        if _eph is not None:
            return _eph
        _resource_gate = _capability_resource_guard_result(
            self._ctx,
            name,
            args,
            ext_tool,
            is_mcp,
        )
        if _resource_gate is not None:
            return _resource_gate
        _gate = _subagent_and_update_guard_result(
            self._ctx, name, entry, ext_tool, is_mcp, local_readonly_subagent,
            acting_subagent, acting_tool_grants,
            entry is not None and (name in self.CODE_TOOLS or name in _REPO_MUTATION_TOOLS),
        )
        if _gate is not None:
            return _gate
        workspace_block_reason = ""
        try:
            workspace_block_reason = workspace_mode_block_reason(self._ctx)
        except Exception as exc:
            workspace_block_reason = f"workspace metadata validation failed: {type(exc).__name__}: {exc}"
        if workspace_block_reason:
            return (
                "⚠️ WORKSPACE_MODE_BLOCKED: invalid external workspace metadata: "
                f"{workspace_block_reason}. Workspace tasks must not overlap the "
                "Ouroboros repo, runtime data, or control plane."
            )
        if entry is not None:
            public_arg_error = _prepare_public_builtin_args(entry, args)
            if public_arg_error:
                return public_arg_error
            _route_note = _normalize_dispatch_path_args(self._ctx, name, args)
            if _route_note.startswith("⚠️ ROOT_REQUIRED_ACTIVE_WORKSPACE"):
                return _route_note
        heal_no_enable = bool(task_constraint and task_constraint.mode == "skill_repair")
        if heal_no_enable:
            heal_block = _heal_mode_guard_result(
                self._ctx,
                name,
                args,
                task_constraint,
                ext_tool,
                is_mcp,
            )
            if heal_block is not None:
                return heal_block
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        effective_constraint = task_constraint
        if entry is not None:
            effective_constraint, payload_result = _payload_dispatch_constraint(
                self._ctx,
                name=name,
                args=args,
                task_constraint=task_constraint,
                workspace_mode=workspace_mode,
            )
            if payload_result is not None:
                return payload_result
        resolved_binding = None
        if entry is not None and _target_binding_operation(name, args) is not None:
            try:
                resolved_binding = _build_builtin_target_binding(self._ctx, name, args)
            except Exception as exc:
                redirect = _light_binding_failure_redirect(name, args)
                if redirect:
                    return redirect
                operation = _target_binding_operation(name, args)
                if operation in {"shell", "service"}:
                    return shell_cwd_block_message(
                        self._ctx,
                        str(args.get("cwd") or ""),
                        operation=operation,
                        error=exc,
                    )
                return _binding_error_text(
                    name,
                    str(args.get("root") or "active_workspace"),
                    exc,
                )
        # Fail-closed: an acting child WITHOUT a resolved isolated workspace would
        # have active_workspace/system_repo fall back to the LIVE repo. Confine it
        # to data roots and block shell/coding/service (whose default target is the repo).
        if acting_subagent and not workspace_mode:
            if name in _ROOT_ARG_REPO_WRITE_TOOLS and str(args.get("root", "") or "active_workspace") in ("active_workspace", "system_repo"):
                return (
                    "⚠️ ACTING_NO_WORKSPACE_BLOCKED: this acting subagent has no resolved isolated "
                    "workspace; write only to root=task_drive, root=artifact_store, or root=user_files. "
                    "active_workspace/system_repo map to the live Ouroboros repo and are blocked."
                )
            if name in ("run_command", "run_script", "start_service",
                        "integrate_subagent_patch", "integrate_delegated_patch"):
                return (
                    "⚠️ ACTING_NO_WORKSPACE_BLOCKED: shell/coding/service/integration tools need an "
                    "isolated workspace (their default target is the live repo). Schedule a self_worktree "
                    "/ external_workspace child for that work."
                )
        # Hardcoded sandbox: light blocks repo mutation; advanced protects
        # core/contracts/release; pro still relies on commit review.
        try:
            from ouroboros.config import get_runtime_mode as _get_runtime_mode
            _runtime_mode = _get_runtime_mode()
        except Exception:
            _runtime_mode = "advanced"

        if is_mcp:
            return self._dispatch_mcp_tool(name, args)
        if entry is None:
            if ext_tool and callable(ext_tool.get("handler")):
                return self._dispatch_extension_tool(name, ext_tool, args)
            text = f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"
            if extension_unavailable:
                return ToolResult(
                    status="unavailable",
                    code="EXTENSION_UNAVAILABLE",
                    text=text,
                    meta={"dynamic_provider": True},
                )
            return text
        args, python_resolution, python_block = self._resolve_python_predispatch(
            name, args, _runtime_mode, effective_constraint, resolved_binding,
        )
        if python_block:
            return python_block
        allow_short_relative = bool(
            effective_constraint and effective_constraint.mode == "skill_repair"
        )
        light_skill_scoped_str_replace = resolved_binding is None and (
            _light_mode_payload_mutation_allowed(
                ctx=self._ctx,
                tool_name=name,
                args=args,
                runtime_mode=_runtime_mode,
                effective_constraint=effective_constraint,
                implicit_skill_cwd_allowed=bool(
                    task_constraint and task_constraint.mode == "skill_repair"
                ),
                allow_short_relative=allow_short_relative,
            )
        )
        if resolved_binding is not None and name not in _SYSTEM_INTRINSIC_REPO_MUTATION_TOOLS:
            light_targets_system = (
                _binding_set_is_light_restricted(self._ctx, resolved_binding)
                or acting_self_worktree
            )
        elif name in _SYSTEM_INTRINSIC_REPO_MUTATION_TOOLS:
            light_targets_system = True
        else:
            light_targets_system = not workspace_mode or acting_self_worktree
        if (
            _runtime_mode == "light"
            and name in _REPO_MUTATION_TOOLS
            and light_targets_system
            and not light_skill_scoped_str_replace
            and not _authorized_managed_update_resolver(self._ctx)
        ):
            return light_cognitive_or_root_redirect(name, args) or (
                "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light blocks Ouroboros "
                f"self-repo/control-plane mutation via {name!r}. For user-visible "
                "deliverables use root=user_files (for example Desktop/file.html), "
                "root=artifact_store for the canonical task artifact, or root=task_drive "
                "for scratch. Skill payload edits remain allowed only through "
                "root=skill_payload with bucket and skill_name "
                "(data/skills/<bucket>/<skill>/) or skill_repair constraints. "
                "Switch to advanced/pro only for reviewed Ouroboros self-modification."
            )

        protected_write_paths = []
        if name in _ROOT_ARG_REPO_WRITE_TOOLS:
            root_name = str(args.get("root", "") or "active_workspace")
            protected_write_paths = [
                canonical_repo_relative_path(self._ctx, root_name, p)
                for p in _payload_write_paths(name, args)
            ]
            if resolved_binding is not None:
                protected_target = (
                    _binding_set_targets_system_repo(self._ctx, resolved_binding)
                    or acting_self_worktree
                )
            else:
                protected_root = root_name in {"active_workspace", "system_repo"}
                protected_target = (
                    (not workspace_mode or acting_self_worktree) and protected_root
                )
            protected_matches = (
                protected_paths_in(protected_write_paths) if protected_target else []
            )
            allow_protected = _authorized_managed_update_resolver(self._ctx) or (
                mode_allows_protected_write(_runtime_mode)
                and (acting_protected_grant or not acting_subagent)
            )
            if protected_matches and not allow_protected:
                first = protected_matches[0]
                return protected_write_block_message(
                    path=first.path,
                    runtime_mode=_runtime_mode,
                    action=f"run tool {name!r} against",
                )

        if name in _SHELL_GUARDED_TOOLS:
            if (
                name == "start_service"
                and _runtime_mode == "light"
                and (
                    _binding_set_targets_system_repo(self._ctx, resolved_binding)
                    or acting_self_worktree
                )
            ):
                return ("⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses start_service against the Ouroboros repository because long-running services can mutate after initial tool checks. For external services, set cwd under user_files, task_drive, or artifact_store; switch to advanced/pro only for reviewed Ouroboros self-modification.")
            block_msg = self._run_shell_safety_check(
                process_shell_guard_args(name, args, ctx=self._ctx, runtime_mode=_runtime_mode),
                _runtime_mode,
                resolved_binding,
            )
            if block_msg:
                return block_msg

        # LLM safety supervisor.
        from ouroboros.safety import check_safety
        is_safe, safety_msg = check_safety(
            name,
            args,
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
            python_resolution=python_resolution,
        )
        if not is_safe:
            return ToolResult(status="blocked", code="SAFETY_VIOLATION", text=safety_msg)
        state_drive_root = _binding_state_drive_root(self._ctx, resolved_binding)
        owner_snapshot = (
            self._snapshot_owner_files(state_drive_root)
            if name in _PROCESS_COMMAND_TOOLS else {}
        )
        light_repo_before = (
            _light_repo_snapshot(system_repo_dir_for(self._ctx))
            if (
                name in _PROCESS_COMMAND_TOOLS
                and _runtime_mode == "light"
                and (
                    _binding_set_targets_system_repo(self._ctx, resolved_binding)
                    or acting_self_worktree
                )
            )
            else None
        )
        workspace_refs_before = (
            _git_ref_snapshot(active_repo_dir_for(self._ctx))
            if name in _PROCESS_COMMAND_TOOLS and workspace_mode and acting_self_worktree
            else None
        )
        worktree_before = (
            self._worktree_status_snapshot() if entry.mutates_worktree else None
        )
        early_error, result = self._invoke_builtin_handler(
            name, entry, args, resolved_binding, python_resolution, worktree_before,
        )
        if early_error is not None:
            return early_error
        if name in _PROCESS_COMMAND_TOOLS:
            result = self._run_shell_post_checks(
                result,
                owner_snapshot=owner_snapshot,
                state_drive_root=state_drive_root,
                light_repo_before=light_repo_before,
                workspace_refs_before=workspace_refs_before,
                tool_name=name,
            )

        return _compose_execute_result(result, _route_note, safety_msg)

    def execute_result(self, name: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch once and adapt only producers that still return legacy text."""
        result = self._execute_legacy_text(name, args)
        if isinstance(result, ToolResult):
            return result
        return LegacyTextResultAdapter.from_text(name, result)

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Compatibility ABI: return the exact model-facing text projection."""
        return self.execute_result(name, args).text

    def _worktree_status_snapshot(self) -> str:
        try:
            from ouroboros.utils import run_cmd

            return run_cmd(["git", "status", "--porcelain"], cwd=self._ctx.repo_dir, timeout=20)
        except Exception:
            return "<status-unavailable>"

    def _invalidate_advisory_if_worktree_changed(self, tool_name: str, before: str) -> None:
        after = self._worktree_status_snapshot()
        if after == before:
            return
        try:
            from ouroboros.review_state import invalidate_advisory_after_mutation

            invalidate_advisory_after_mutation(
                pathlib.Path(self._ctx.drive_root),
                mutation_root=pathlib.Path(self._ctx.repo_dir),
                source_tool=tool_name,
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "Central advisory invalidation failed for %s", tool_name, exc_info=True
            )

    def override_handler(self, name: str, handler) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            projected = replace(entry, handler=handler)
            self._handler_overrides[name] = handler
            self._entries[name] = projected
            if name in self._scoped_entries:
                self._scoped_entries[name] = projected

    @property
    def CODE_TOOLS(self) -> frozenset:
        return frozenset(e.name for e in self._entries.values() if e.is_code_tool)
