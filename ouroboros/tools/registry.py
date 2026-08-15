"""Tool registry SSOT: load tool modules, expose schemas, execute safely."""

from __future__ import annotations

import copy
import inspect
import logging
import pathlib
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

import ouroboros.tools.registry_guard_process as registry_guard_process
import ouroboros.tools.registry_guards as registry_guards
from ouroboros.runtime_mode_policy import (
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
from ouroboros.tools.shell_guards import process_shell_guard_args
from ouroboros.tool_access import (
    binding_targets_system_repo,  # noqa: F401 -- public compatibility re-export
    canonical_repo_relative_path,
    light_cognitive_or_root_redirect,
    normalize_root_relative,  # noqa: F401 -- public compatibility re-export
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
    _binding_items,  # noqa: F401 -- public compatibility re-export
    _binding_set_is_light_restricted,
    _binding_set_targets_system_repo,
    _binding_state_drive_root,
    _build_builtin_target_binding,
    _coerce_real_path,  # noqa: F401 -- public compatibility re-export
    _normalize_dispatch_path_args,
    _target_binding_operation,
    active_repo_dir_for,  # noqa: F401 -- public compatibility re-export
    system_repo_dir_for,  # noqa: F401 -- public compatibility re-export
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
    _authorized_managed_update_resolver,  # noqa: F401 -- public compatibility re-export
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
from ouroboros.contracts.task_constraint import TaskConstraint, VALID_WRITE_SURFACES, normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import (
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    constraint_bucket_skill,  # noqa: F401 -- public compatibility re-export
    cross_skill_redirect_error,
    decide_payload_short_form,
    is_skill_payload_control_filename,  # noqa: F401 -- public compatibility re-export
    synthesize_payload_constraint,
)

log = logging.getLogger(__name__)
_FROZEN_TOOL_MANIFEST_PATH: pathlib.Path | None = None


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


_HEAL_PROTECTED_PAYLOAD_FILENAMES = SKILL_PAYLOAD_CONTROL_FILENAMES


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
            registry_guards._light_mode_payload_mutation_allowed(
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
            block_result = registry_guard_process._run_shell_safety_check(
                self,
                process_shell_guard_args(name, args, ctx=self._ctx, runtime_mode=_runtime_mode),
                _runtime_mode,
                resolved_binding,
            )
            if block_result is not None:
                return block_result

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
            registry_guard_process._snapshot_owner_files(self, state_drive_root)
            if name in _PROCESS_COMMAND_TOOLS else {}
        )
        light_repo_before = (
            registry_guard_process._light_repo_snapshot(system_repo_dir_for(self._ctx))
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
            registry_guard_process._git_ref_snapshot(active_repo_dir_for(self._ctx))
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
            result = registry_guard_process._run_shell_post_checks(
                self,
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
