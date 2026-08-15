"""Typed internal tool results with a byte-compatible legacy text adapter."""

from __future__ import annotations

import json
import re
import signal
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

ToolStatus = Literal["ok", "error", "blocked", "timeout", "unavailable"]
_TOOL_STATUSES = frozenset({"ok", "error", "blocked", "timeout", "unavailable"})
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FIRST_MARKER_RE = re.compile(r"^⚠️ ([A-Z][A-Z0-9_]*)")
_SAFETY_SEPARATOR = "\n\n---\n"
_MCP_RESULT_ENVELOPE_PREFIX = "External MCP tool result from "
_MAX_META_ITEMS = 32
_MAX_META_BYTES = 8192
_HOST_META_KEYS = frozenset(
    {
        "route_note",
        "safety_warning",
        "ambiguous_safety_wrapper",
        "owner_state_restored",
        "light_repo_changed",
        "workspace_git_refs_changed",
    }
)
# Producer metadata keeps its exact limits. Composition may add only these
# closed, boolean host annotations, within a separately bounded byte reserve.
_MAX_HOST_META_BYTES = 256
_PROCESS_TOOL_RESULT_ATTR = "_active_process_tool_result"


@dataclass(frozen=True)
class ToolCodeSpec:
    """Stable meaning attached to one internal tool-result code."""

    status: ToolStatus
    outcome_bucket: str
    ui_severity: Literal["info", "warning", "error"]
    recovery: str

    def __post_init__(self) -> None:
        if self.status not in _TOOL_STATUSES:
            raise ValueError(f"invalid tool status: {self.status!r}")
        if not self.outcome_bucket:
            raise ValueError("outcome_bucket must be non-empty")
        if self.ui_severity not in {"info", "warning", "error"}:
            raise ValueError(f"invalid UI severity: {self.ui_severity!r}")
        if not isinstance(self.recovery, str) or not self.recovery:
            raise TypeError("recovery must be non-empty descriptive text")


def _code_spec(
    status: ToolStatus,
    outcome_bucket: str,
    ui_severity: Literal["info", "warning", "error"],
    recovery: str,
) -> ToolCodeSpec:
    return ToolCodeSpec(status, outcome_bucket, ui_severity, recovery)


TOOL_CODE_SPECS: Mapping[str, ToolCodeSpec] = MappingProxyType(
    {
        "OK": _code_spec("ok", "ok", "info", "none"),
        "SAFETY_WARNING": _code_spec(
            "ok",
            "ok",
            "warning",
            "review the safety warning before continuing",
        ),
        "SHELL_REGEX_AUTO_CORRECTED": _code_spec(
            "ok",
            "ok_autocorrected",
            "warning",
            "inspect the corrected command when relevant",
        ),
        "SHELL_NO_MATCH": _code_spec(
            "ok",
            "ok",
            "info",
            "none",
        ),
        "OWNER_STATE_RESTORED": _code_spec(
            "ok",
            "ok",
            "warning",
            "inspect the attempted owner-state mutation",
        ),
        "REVIEW_BLOCKED": _code_spec(
            "ok",
            "review_blocked",
            "warning",
            "address or rebut the review findings",
        ),
        "GIT_ERROR": _code_spec(
            "ok",
            "git_error",
            "warning",
            "inspect the version-control refusal",
        ),
        "LEGACY_WARNING": _code_spec(
            "ok",
            "ok",
            "warning",
            "inspect the warning before continuing",
        ),
        "LEGACY_UNTYPED": _code_spec(
            "ok",
            "untyped",
            "warning",
            "migrate the dynamic producer before consuming typed status",
        ),
        "ACCESS_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "use an authority permitted by the task contract",
        ),
        "CORE_PROTECTION_BLOCKED": _code_spec(
            "blocked",
            "protected_blocked",
            "warning",
            "use the reviewed protected-write path",
        ),
        "ROOT_REQUIRED": _code_spec(
            "blocked",
            "root_required",
            "warning",
            "retry against the required resource root",
        ),
        "RESOURCE_BLOCKED": _code_spec(
            "blocked",
            "resource_blocked",
            "warning",
            "use a resource allowed by the task contract",
        ),
        "WORKSPACE_BLOCKED": _code_spec(
            "blocked",
            "workspace_blocked",
            "warning",
            "repair or select a valid workspace binding",
        ),
        "SHELL_CWD_BLOCKED": _code_spec(
            "blocked",
            "cwd_blocked",
            "warning",
            "select a working directory within an allowed root",
        ),
        "SUDO_INTERACTIVE_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "use non-interactive elevation or report the environment limitation",
        ),
        "SUBAGENT_SECRET_READ_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "use a gated read surface without accessing owner secrets",
        ),
        "ELEVATION_BLOCKED": _code_spec(
            "blocked",
            "elevation_blocked",
            "warning",
            "request an owner-controlled setting change",
        ),
        "CONTEXT_MODE_SELF_LOWERING_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "ask the owner to select the cognitive context mode",
        ),
        "SCOPE_REVIEW_FLOOR_SELF_LOWERING_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "use read-only inspection or ask the owner to change the setting",
        ),
        "SAFETY_MODE_SELF_LOWERING_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "ask the owner to select the safety mode",
        ),
        "OWNER_SKILL_ATTESTATION_SELF_CALL_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "ask the owner to attest the eligible skill",
        ),
        "SKILL_STATE_WRITE_BLOCKED": _code_spec(
            "blocked",
            "skill_state_blocked",
            "warning",
            "use the reviewed skill lifecycle or owner controls",
        ),
        "GIT_VIA_SHELL_BLOCKED": _code_spec(
            "blocked",
            "git_via_shell_blocked",
            "warning",
            "use the reviewed repository mutation path for the Ouroboros runtime",
        ),
        "LIGHT_MODE_BLOCKED": _code_spec(
            "blocked",
            "light_mode_blocked",
            "warning",
            "use a permitted target or owner-selected mode",
        ),
        "LIGHT_MODE_REPO_WRITE_BLOCKED": _code_spec(
            "blocked",
            "light_mode_blocked",
            "warning",
            "use advanced or pro mode for repository writes",
        ),
        "WORKSPACE_GIT_REF_CHANGED": _code_spec(
            "blocked",
            "workspace_blocked",
            "warning",
            "leave external workspace changes as files or patch artifacts",
        ),
        "HEAL_MODE_BLOCKED": _code_spec(
            "blocked",
            "heal_mode_blocked",
            "warning",
            "stay within the admitted repair surface",
        ),
        "ARTIFACT_OUTPUT_UNDECLARED": _code_spec(
            "blocked",
            "artifact_output_undeclared",
            "warning",
            "declare the produced artifact",
        ),
        "SAFETY_VIOLATION": _code_spec(
            "blocked",
            "safety_violation",
            "error",
            "choose a safe alternative",
        ),
        "LEGACY_BLOCKED": _code_spec(
            "blocked",
            "blocked",
            "warning",
            "follow the refusal text",
        ),
        "CAPABILITY_UNAVAILABLE": _code_spec(
            "unavailable",
            "unavailable",
            "warning",
            "enable or configure the capability",
        ),
        "MCP_UNAVAILABLE": _code_spec(
            "unavailable",
            "unavailable",
            "warning",
            "enable or repair the MCP provider",
        ),
        "EXTENSION_UNAVAILABLE": _code_spec(
            "unavailable",
            "unavailable",
            "warning",
            "enable or repair the extension",
        ),
        "LEGACY_UNAVAILABLE": _code_spec(
            "unavailable",
            "unavailable",
            "warning",
            "inspect availability and retry when restored",
        ),
        "TOOL_TIMEOUT": _code_spec(
            "timeout",
            "timeout",
            "error",
            "retry with a suitable bounded timeout",
        ),
        "MCP_TIMEOUT": _code_spec(
            "timeout",
            "timeout",
            "error",
            "inspect MCP health before retrying",
        ),
        "EXTENSION_TIMEOUT": _code_spec(
            "timeout",
            "timeout",
            "error",
            "inspect extension health before retrying",
        ),
        "TOOL_ARG_ERROR": _code_spec(
            "error",
            "argument_error",
            "error",
            "correct the tool arguments",
        ),
        "UNKNOWN_TOOL": _code_spec(
            "error",
            "unknown_tool",
            "error",
            "select a registered visible tool",
        ),
        "TOOL_ERROR": _code_spec(
            "error",
            "error",
            "error",
            "inspect the tool error and correct the call",
        ),
        "TOOL_INTERNAL_ERROR": _code_spec(
            "error",
            "error",
            "error",
            "inspect the internal tool contract",
        ),
        "EXECUTOR_ERROR": _code_spec(
            "error",
            "executor_error",
            "error",
            "inspect executor health and custody",
        ),
        "SHELL_ERROR": _code_spec(
            "error",
            "shell_error",
            "error",
            "correct the command or environment",
        ),
        "SHELL_EXIT_ERROR": _code_spec(
            "error",
            "non_zero_exit",
            "error",
            "inspect the command result before retrying",
        ),
        "RUN_SCRIPT_ERROR": _code_spec(
            "error",
            "run_script_error",
            "error",
            "correct the script or environment",
        ),
        "ARTIFACT_OUTPUT_ERROR": _code_spec(
            "error",
            "artifact_output_error",
            "error",
            "repair artifact registration",
        ),
        "MCP_ERROR": _code_spec(
            "error",
            "mcp_error",
            "error",
            "inspect the MCP response and provider health",
        ),
        "EXTENSION_ERROR": _code_spec(
            "error",
            "extension_error",
            "error",
            "inspect the extension response and health",
        ),
        "SAFETY_ERROR": _code_spec(
            "error",
            "safety_error",
            "error",
            "restore the safety provider before retrying",
        ),
        "TOOL_REPORTED_FAILURE": _code_spec(
            "error",
            "tool_reported_failure",
            "error",
            "inspect the structured tool failure",
        ),
        "LEGACY_TOOL_ERROR": _code_spec(
            "error",
            "error",
            "error",
            "follow the legacy failure text",
        ),
    }
)


@dataclass(frozen=True)
class ToolResult:
    """Internal result; ``text`` remains the complete model-facing projection."""

    status: ToolStatus
    code: str
    text: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _CODE_RE.fullmatch(self.code):
            raise ValueError(f"invalid tool result code: {self.code!r}")
        spec = TOOL_CODE_SPECS.get(self.code)
        if spec is None:
            raise ValueError(f"unknown tool result code: {self.code!r}")
        if self.status != spec.status:
            raise ValueError(f"status {self.status!r} does not match {self.code} ({spec.status!r})")
        if not isinstance(self.text, str):
            raise TypeError("tool result text must be a string")
        raw_meta = dict(self.meta or {})
        if any(not isinstance(key, str) for key in raw_meta):
            raise ValueError("tool result meta keys must be strings")
        producer_meta = {
            key: value for key, value in raw_meta.items() if key not in _HOST_META_KEYS
        }
        if len(producer_meta) > _MAX_META_ITEMS:
            raise ValueError("tool result meta must have at most 32 non-host keys")
        try:
            encoded = json.dumps(
                raw_meta,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            producer_encoded = json.dumps(
                producer_meta,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (RecursionError, TypeError, ValueError) as exc:
            raise TypeError("tool result meta must contain JSON-safe values") from exc
        producer_bytes = len(producer_encoded.encode("utf-8"))
        if producer_bytes > _MAX_META_BYTES:
            raise ValueError("tool result meta exceeds 8192 encoded bytes")
        # Keep every payload valid under the old aggregate cap, then permit
        # only the fixed host reserve beyond it as producer bytes approach it.
        total_limit = max(_MAX_META_BYTES, producer_bytes + _MAX_HOST_META_BYTES)
        if len(encoded.encode("utf-8")) > total_limit:
            raise ValueError("tool result host metadata exceeds its reserved overhead")
        object.__setattr__(self, "meta", MappingProxyType(json.loads(encoded)))


def _replace_tool_result(
    result: ToolResult,
    *,
    text: str | None = None,
    code: str | None = None,
    meta_updates: Mapping[str, Any] | None = None,
) -> ToolResult:
    """Replace immutable result fields without re-adapting its trusted facts."""

    selected_code = code or result.code
    meta = dict(result.meta)
    meta.update(dict(meta_updates or {}))
    return ToolResult(
        status=TOOL_CODE_SPECS[selected_code].status,
        code=selected_code,
        text=result.text if text is None else text,
        meta=meta,
    )


def _publish_process_tool_result(ctx: Any, result: ToolResult) -> str:
    """Publish one registry-scoped process result while keeping the string ABI."""

    if hasattr(ctx, _PROCESS_TOOL_RESULT_ATTR):
        setattr(ctx, _PROCESS_TOOL_RESULT_ATTR, result)
    return result.text


def _publish_process_result(
    ctx: Any,
    code: str,
    text: str,
    *,
    exit_code: int | None = None,
    signal_name: str = "",
    artifact_registered: bool = False,
    shell_regex_auto_corrected: bool = False,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """Publish trusted process facts through the transient string-bound sidecar."""

    facts = dict(meta or {})
    if exit_code is not None:
        facts["exit_code"] = int(exit_code)
        if int(exit_code) < 0 and not signal_name:
            signal_number = abs(int(exit_code))
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"SIG{signal_number}"
    if signal_name:
        facts["signal"] = signal_name
    if artifact_registered:
        facts["artifact_registered"] = True
    if shell_regex_auto_corrected:
        facts["shell_regex_auto_corrected"] = True
    return _publish_process_tool_result(
        ctx,
        ToolResult(
            status=TOOL_CODE_SPECS[code].status,
            code=code,
            text=text,
            meta=facts,
        ),
    )


def _wrap_run_script_process_result(
    ctx: Any,
    result: str,
    audit_note: str,
    script_path: Any,
) -> str:
    """Republish an inner shell result after the exact run-script text wrapper."""

    if str(result).lstrip().startswith("⚠️"):
        tail = f"\n{audit_note}" if audit_note else ""
        wrapped = f"{result}{tail}\n# script_path={script_path}"
    elif audit_note:
        wrapped = f"{audit_note}\n# script_path={script_path}"
    else:
        wrapped = f"# script_path={script_path}\n{result}"
    base = getattr(ctx, _PROCESS_TOOL_RESULT_ATTR, None)
    if isinstance(base, ToolResult) and base.text == result:
        code = "ARTIFACT_OUTPUT_UNDECLARED" if audit_note and base.status == "ok" else base.code
        return _publish_process_tool_result(
            ctx,
            _replace_tool_result(base, text=wrapped, code=code),
        )
    return wrapped


_EXACT_IDENTIFIER_CODES = MappingProxyType(
    {
        "ACCESS_DENIED": "ACCESS_BLOCKED",
        "TOOL_ACCESS_BLOCKED": "ACCESS_BLOCKED",
        "ACTING_NO_WORKSPACE_BLOCKED": "ACCESS_BLOCKED",
        "ACTING_SUBAGENT_BLOCKED": "ACCESS_BLOCKED",
        "ACTING_SUBAGENT_TOOL_NOT_GRANTED": "ACCESS_BLOCKED",
        "LOCAL_READONLY_SUBAGENT_BLOCKED": "ACCESS_BLOCKED",
        "MUTATIVE_SUBAGENTS_DISABLED": "ACCESS_BLOCKED",
        "CORE_PROTECTION_BLOCKED": "CORE_PROTECTION_BLOCKED",
        "ROOT_REQUIRED_USER_FILES": "ROOT_REQUIRED",
        "ROOT_REQUIRED_ACTIVE_WORKSPACE": "ROOT_REQUIRED",
        "RESOURCE_CONSTRAINT_BLOCKED": "RESOURCE_BLOCKED",
        "RESOURCE_POLICY_BLOCKED": "RESOURCE_BLOCKED",
        "WORKSPACE_MODE_BLOCKED": "WORKSPACE_BLOCKED",
        "WORKSPACE_GIT_BLOCKED": "WORKSPACE_BLOCKED",
        "WORKSPACE_SHELL_BLOCKED": "WORKSPACE_BLOCKED",
        "WORKSPACE_EXECUTOR_STATE_WRITE_BLOCKED": "WORKSPACE_BLOCKED",
        "LIGHT_MODE_BLOCKED": "LIGHT_MODE_BLOCKED",
        "LIGHT_MODE_REPO_WRITE_BLOCKED": "LIGHT_MODE_REPO_WRITE_BLOCKED",
        "WORKSPACE_GIT_REF_CHANGED": "WORKSPACE_GIT_REF_CHANGED",
        "OWNER_STATE_RESTORED": "OWNER_STATE_RESTORED",
        "HEAL_MODE_BLOCKED": "HEAL_MODE_BLOCKED",
        "ARTIFACT_OUTPUT_UNDECLARED": "ARTIFACT_OUTPUT_UNDECLARED",
        "ARTIFACT_OUTPUT_ERROR": "ARTIFACT_OUTPUT_ERROR",
        "SAFETY_VIOLATION": "SAFETY_VIOLATION",
        "CAPABILITY_UNAVAILABLE": "CAPABILITY_UNAVAILABLE",
        "MCP_DISABLED": "MCP_UNAVAILABLE",
        "MCP_TOOL_NOT_FOUND": "MCP_UNAVAILABLE",
        "MCP_TOOL_DISALLOWED": "ACCESS_BLOCKED",
        "MCP_TOOL_TIMEOUT": "MCP_TIMEOUT",
        "MCP_TOOL_ERROR": "MCP_ERROR",
        "TOOL_TIMEOUT": "TOOL_TIMEOUT",
        "TOOL_ARG_ERROR": "TOOL_ARG_ERROR",
        "INVALID_ARG": "TOOL_ARG_ERROR",
        "TOOL_ERROR": "TOOL_ERROR",
        "TOOL_INTERNAL_ERROR": "TOOL_INTERNAL_ERROR",
        "EXECUTOR_UNAVAILABLE": "LEGACY_UNAVAILABLE",
        "SHELL_EXIT_ERROR": "SHELL_EXIT_ERROR",
        "SHELL_NO_MATCH": "SHELL_NO_MATCH",
        "SHELL_ERROR": "SHELL_ERROR",
        "SHELL_ARG_ERROR": "SHELL_ERROR",
        "SHELL_CMD_ERROR": "SHELL_ERROR",
        "SHELL_CWD_BLOCKED": "SHELL_CWD_BLOCKED",
        "SUDO_INTERACTIVE_BLOCKED": "SUDO_INTERACTIVE_BLOCKED",
        "SUBAGENT_SECRET_READ_BLOCKED": "SUBAGENT_SECRET_READ_BLOCKED",
        "ELEVATION_BLOCKED": "ELEVATION_BLOCKED",
        "CONTEXT_MODE_SELF_LOWERING_BLOCKED": "CONTEXT_MODE_SELF_LOWERING_BLOCKED",
        "SCOPE_REVIEW_FLOOR_SELF_LOWERING_BLOCKED": "SCOPE_REVIEW_FLOOR_SELF_LOWERING_BLOCKED",
        "SAFETY_MODE_SELF_LOWERING_BLOCKED": "SAFETY_MODE_SELF_LOWERING_BLOCKED",
        "OWNER_SKILL_ATTESTATION_SELF_CALL_BLOCKED": "OWNER_SKILL_ATTESTATION_SELF_CALL_BLOCKED",
        "SKILL_STATE_WRITE_BLOCKED": "SKILL_STATE_WRITE_BLOCKED",
        "GIT_VIA_SHELL_BLOCKED": "GIT_VIA_SHELL_BLOCKED",
        "RUN_SCRIPT_BLOCKED": "LEGACY_BLOCKED",
        "REVIEW_BLOCKED": "REVIEW_BLOCKED",
        "GIT_ERROR": "GIT_ERROR",
    }
)


def _compose_execute_result(result: str, route_note: str, safety_msg: str) -> str:
    """Assemble the final tool result.

    The auto-route note TRAILS the result: failure classification
    (loop_tool_execution) inspects the FIRST line, so a leading note would mask
    an underlying tool error on the auto-routed read path (review round 3). The
    safety warning keeps its historical leading position — its ``---`` separator
    is an established transcript convention the metadata scan already handles."""
    if route_note:
        result = f"{result}\n\n{route_note}"
    if safety_msg:
        return f"{safety_msg}\n\n---\n{result}"
    return result


def _structured_failure(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("{") or '"ok"' not in stripped:
        return False
    try:
        payload = json.loads(stripped)
    except Exception:  # The compatibility adapter must never break a legacy result.
        return False
    return isinstance(payload, dict) and payload.get("ok") is False


def _classification(code: str, meta: Mapping[str, Any] | None = None) -> tuple[ToolStatus, str, dict[str, Any]]:
    spec = TOOL_CODE_SPECS[code]
    return spec.status, code, dict(meta or {})


def _classify_legacy_text(
    text: str,
    *,
    wrapper_depth: int = 0,
) -> tuple[ToolStatus, str, dict[str, Any]]:
    if wrapper_depth >= 4:
        return _classification(
            "LEGACY_TOOL_ERROR",
            {"wrapper_depth_exceeded": True},
        )
    if text.startswith("⚠️ SAFETY_WARNING"):
        if text.count(_SAFETY_SEPARATOR) > 1:
            return _classification(
                "SAFETY_ERROR",
                {"ambiguous_safety_wrapper": True},
            )
        _warning, separator, inner = text.partition(_SAFETY_SEPARATOR)
        if separator:
            status, code, meta = _classify_legacy_text(
                inner,
                wrapper_depth=wrapper_depth + 1,
            )
            if code != "OK":
                return status, code, {**meta, "safety_warning": True}
        return _classification("SAFETY_WARNING")

    if text.startswith("⚠️ SHELL_REGEX_AUTO_CORRECTED"):
        _warning, separator, inner = text.partition("\n")
        if separator:
            status, code, meta = _classify_legacy_text(
                inner,
                wrapper_depth=wrapper_depth + 1,
            )
            if status != "ok":
                return status, code, {**meta, "shell_regex_auto_corrected": True}
        return _classification("SHELL_REGEX_AUTO_CORRECTED")

    if _structured_failure(text):
        return _classification("TOOL_REPORTED_FAILURE")
    if text.startswith("⚠️ CRITICAL SAFETY_VIOLATION"):
        return _classification("SAFETY_VIOLATION")
    if text.startswith("⚠️ Unknown tool:"):
        return _classification("UNKNOWN_TOOL")

    first_line = text.splitlines()[0] if text else ""
    marker = _FIRST_MARKER_RE.match(first_line)
    if marker is None:
        return _classification("OK")
    identifier = marker.group(1)
    exact = _EXACT_IDENTIFIER_CODES.get(identifier)
    if exact is not None:
        return _classification(exact)
    if identifier.startswith("MCP_"):
        if "TIMEOUT" in identifier:
            return _classification("MCP_TIMEOUT")
        if "UNAVAILABLE" in identifier or "NOT_FOUND" in identifier or "DISABLED" in identifier:
            return _classification("MCP_UNAVAILABLE")
        return _classification("MCP_ERROR")
    if identifier.startswith("EXTENSION_"):
        if "TIMEOUT" in identifier:
            return _classification("EXTENSION_TIMEOUT")
        if "UNAVAILABLE" in identifier or "NOT_FOUND" in identifier or "DISABLED" in identifier:
            return _classification("EXTENSION_UNAVAILABLE")
        return _classification("EXTENSION_ERROR")
    if identifier.startswith("SHELL_"):
        return _classification("SHELL_ERROR")
    if identifier.startswith("RUN_SCRIPT_"):
        return _classification("RUN_SCRIPT_ERROR")
    if "_TIMEOUT" in identifier:
        return _classification("TOOL_TIMEOUT")
    if "_UNAVAILABLE" in identifier:
        return _classification("LEGACY_UNAVAILABLE")
    if any(part in identifier for part in ("_BLOCKED", "_FORBIDDEN", "_DISALLOWED")):
        return _classification("LEGACY_BLOCKED")
    if any(part in identifier for part in ("_ERROR", "_FAILED", "_VIOLATION", "_CORRUPT")):
        return _classification("LEGACY_TOOL_ERROR")
    return _classification("LEGACY_WARNING")


class LegacyTextResultAdapter:
    """One compatibility adapter from host-owned legacy text to ``ToolResult``."""

    @classmethod
    def from_text(cls, tool_name: str, text: str) -> ToolResult:
        if not isinstance(text, str):
            text = str(text)
        normalized_name = str(tool_name or "").strip()
        dynamic_body_untyped = normalized_name.startswith("ext_") or (
            normalized_name.startswith("mcp_")
            and text.startswith(_MCP_RESULT_ENVELOPE_PREFIX)
        )
        if dynamic_body_untyped:
            return ToolResult(
                status="ok",
                code="LEGACY_UNTYPED",
                text=text,
                meta={"dynamic_provider": True},
            )
        status, code, meta = _classify_legacy_text(text)
        return ToolResult(status=status, code=code, text=text, meta=meta)


def _compose_execute_result_result(
    tool_name: str,
    base: str | ToolResult,
    route_note: str,
    safety_msg: str,
) -> ToolResult:
    """Compose host-owned annotations without re-adapting a typed base result."""

    base_result = (
        base
        if isinstance(base, ToolResult)
        else LegacyTextResultAdapter.from_text(tool_name, base)
    )
    text = _compose_execute_result(base_result.text, route_note, safety_msg)
    if safety_msg and text.count(_SAFETY_SEPARATOR) > 1:
        meta = dict(base_result.meta)
        meta["ambiguous_safety_wrapper"] = True
        if route_note:
            meta["route_note"] = True
        return ToolResult(
            status="error",
            code="SAFETY_ERROR",
            text=text,
            meta=meta,
        )

    meta = dict(base_result.meta)
    if route_note:
        meta["route_note"] = True
    if not safety_msg:
        return ToolResult(
            status=base_result.status,
            code=base_result.code,
            text=text,
            meta=meta,
        )
    if base_result.code == "OK":
        return ToolResult(
            status="ok",
            code="SAFETY_WARNING",
            text=text,
            meta=meta,
        )
    meta["safety_warning"] = True
    return ToolResult(
        status=base_result.status,
        code=base_result.code,
        text=text,
        meta=meta,
    )
