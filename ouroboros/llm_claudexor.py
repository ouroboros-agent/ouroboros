"""Owned-engine model transport over one physical-attempt ledger entry.

Claudexor owns translation, account selection and one generation per operation.
This client owns its prepared request and returned result, using the existing
private observability CAS before ACK. Re-reading a lost HTTP reply rejoins the
same operation; it never buys another inference. A live typed operation is not
an idle socket: only continuous loss of the control connection spends the
transport timeout. Task deadlines and cancellation retain their outer owners.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import logging
import threading
import time
from typing import Any

from ouroboros import config
from ouroboros._usage_response import provider_cost_value
from ouroboros.anthropic_native_custody import scrub_native_custody
from ouroboros.claudexor_daemon import ensure_owned_gateway, read_owned_gateway
from ouroboros.deadline_utils import llm_transport_timeout_sec
from ouroboros.gateways.claudexor import ClaudexorUnavailable, _READ_TIMEOUT_SEC
from ouroboros.llm_attempt import _attempt_request, _candidate_before_dispatch
from ouroboros.model_slots import MODEL_ACCOUNTS_KEY, model_role_option
from ouroboros.model_wait import ModelWaitInterrupted, current_model_wait, prepared_call_scope
from ouroboros.observability import persist_call
from ouroboros.transport_custody import ProviderNotDispatched
from ouroboros.usage_accounting import (
    PhysicalAttemptPreparationFailed, current_physical_attempt_context, current_usage_scope,
    execute_physical_attempt, execute_physical_attempt_async,
    last_physical_attempt_capture,
)
from ouroboros.utils import append_jsonl, sanitize_tool_result_for_log, utc_now_iso

log = logging.getLogger(__name__)


def model_catalog(source: str, credential_profile_id: str | None = None, *,
                  requested_model: str | None = None) -> dict:
    """Metadata-only transport; the capability evidence owner interprets the envelope."""
    gateway = read_owned_gateway()
    try:
        hint = {"requested_model": requested_model} if requested_model is not None else {}
        return gateway.list_source_models(source, credential_profile_id, **hint)
    finally:
        gateway.close()


def model_sources() -> dict:
    """Expose declared model sources and their credential owners, without a routing table."""
    gateway = read_owned_gateway()
    try:
        return gateway.list_model_sources()
    finally:
        gateway.close()


class ClaudexorModelError(RuntimeError):
    """A model-operation fact with its exact role, route and recovery identity."""

    def __init__(self, problem: dict, *, model_role: str = "", operation_id: str = "",
                 route: dict | None = None, unknown: bool = False):
        self.problem = copy.deepcopy(problem)
        self.code = "model_outcome_unknown" if unknown else str(problem.get("code") or "model_operation_failed")
        super().__init__(f"{self.code}: {problem.get('message') or 'Model operation did not complete'}")
        self.body = {"code": self.code} if unknown else self.problem
        context = problem.get("context") or {}
        self.status_code = 0 if unknown else int(context.get("httpStatus") or 0)
        self.reset_at = str(context.get("resetsAt") or "")
        self.retryable = False if unknown else problem.get("retryable") is True
        self.model_role = model_role
        self.operation_id = operation_id
        self.route = copy.deepcopy(route or {})

    @property
    def display_message(self) -> str:
        """Show typed provider details without changing exception classification text."""
        context = self.problem.get("context") or {}
        details = [] if self.code == "model_outcome_unknown" else [
            f"{label}={value.strip()}"
            for key, label in (("vendorCode", "provider_code"), ("parameter", "parameter"))
            if isinstance(value := context.get(key), str) and value.strip()
        ]
        # Details lead so the existing terminal preview can name the refusal.
        return sanitize_tool_result_for_log("; ".join([", ".join(details), str(self)]) if details else str(self))


class ClaudexorModelNotDispatched(ClaudexorModelError, ProviderNotDispatched):
    """Only a terminal engine receipt proving dispatch.state=not_started mints this."""


def propagate_model_error(error: Exception) -> None:
    """Preserve control/resource waits and unknown custody across helper fallbacks.

    A confirmed ordinary provider refusal still belongs to the helper's existing
    retry or disclosed-unavailable path, just as it does for direct API calls.
    """
    from ouroboros.model_wait import ModelWaitInterrupted, model_wait_reason

    if isinstance(error, ModelWaitInterrupted):
        raise error
    if isinstance(error, ClaudexorModelError):
        capture = getattr(error, "physical_attempt_capture", None)
        if (error.code in {"model_outcome_unknown", "model_operation_interrupted"}
                or model_wait_reason(error)
                or getattr(capture, "state", None) in {"dispatched", "unresolved"}):
            raise error


def _usage(result: dict) -> tuple[dict, float | None, bool]:
    """Normalize explicit model usage; never run the generic body-error/free branch."""
    counters = result.get("usage") or {}
    cost_evidence = result.get("cost") or {}
    cash = provider_cost_value(cost_evidence.get("cashUsd"))
    knowledge = cost_evidence.get("knowledge")
    cost = cash if knowledge in {"exact", "estimated"} else None
    if knowledge == "estimated" and cost is None:
        cost = provider_cost_value(cost_evidence.get("estimatedUsd"))
    usage = {
        "prompt_tokens": counters.get("input_tokens"),
        "completion_tokens": counters.get("output_tokens"),
        "cached_tokens": counters.get("cached_input_tokens"),
        "cache_write_tokens": counters.get("cache_write_tokens"),
        "reasoning_tokens": counters.get("reasoning_tokens"),
    }
    usage["total_tokens"] = (
        int(usage["prompt_tokens"] or 0) + int(usage["completion_tokens"] or 0)
        if all(usage[key] is not None for key in ("prompt_tokens", "completion_tokens")) else None
    )
    return usage, cost, cost is not None and knowledge == "exact"


def _request(target: dict, messages: list, tools: list | None, parameters: dict) -> dict:
    from ouroboros.llm_messages import _MessageShapingMixin

    for name in ("response_format", "allow_server_web_search", "bypass_response_cache"):
        if parameters.get(name) or (name == "response_format" and parameters.get(name) is not None):
            raise ClaudexorModelError({"code": "unsupported_parameter", "message": f"Claudexor model transport does not support {name}.",
                                       "context": {"parameter": name}}, model_role=parameters.get("model_role", ""))
    # Only known host and foreign-provider metadata leave the send copy. Native
    # Claudexor payloads and tool schemas are opaque here and are never walked.
    prepared = scrub_native_custody(_MessageShapingMixin._normalize_system_message_placement(messages))
    for message in prepared:
        for name in ("_context_capsule", "reasoning", "reasoning_details", "reasoning_content", "response_id", "stop_reason"):
            message.pop(name, None)
        # A direct provider's refusal is assistant content, not routing metadata.
        # Preserve both text parts verbatim when a response carries both fields;
        # keep the original history untouched and translate only the send copy.
        refusal = message.pop("refusal", None)
        if refusal:
            if not isinstance(refusal, str):
                raise ClaudexorModelError({"code": "invalid_request", "message": "Assistant refusal must be text."})
            content = message.get("content")
            if not content:
                message["content"] = refusal
            else:
                parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
                message["content"] = [*parts, {"type": "text", "text": refusal}]
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict):
                for name in ("_caption", "_source_path", "_context_capsule", "cache_control"):
                    block.pop(name, None)
    role = parameters.get("model_role", "")
    override = parameters.get("model_account_override")
    if override is not None and not isinstance(override, str):
        raise ValueError("model_account_override must be a profile name, empty Auto, or None")
    pin = override.strip() if override is not None else model_role_option(MODEL_ACCOUNTS_KEY, role)
    account = {"mode": "pin", "profileId": pin} if pin else {"mode": "auto"}
    if not pin:
        # Carry the conversation's last account as a preference, not admission.
        # The engine is still the only actor choosing an eligible account.
        for message in reversed(prepared):
            native = message.get("nativeContinuation") or {}
            route = native.get("route") or {}
            if route.get("source") == target["source"] and route.get("model") == target["resolved_model"]:
                if route.get("credentialProfileId"):
                    account["preferredProfileId"] = route["credentialProfileId"]
                break
    options = {wire: parameters[key] for key, wire in (
        ("reasoning_effort", "reasoningEffort"), ("temperature", "temperature"),
        ("cache_affinity", "cacheKey"),
    ) if parameters.get(key) is not None and parameters.get(key) != ""}
    return {"source": target["source"], "model": target["resolved_model"], "account": account,
            "messages": prepared, "tools": copy.deepcopy(tools or []),
            "toolChoice": copy.deepcopy(parameters.get("tool_choice", "auto")), "options": options}


class _ModelInvocation:
    """Custody of one temporary gateway and one caller-identified operation."""

    def __init__(self, target: dict, payload: dict, parameters: dict):
        self.target, self.payload = target, payload
        self.role = str(parameters.get("model_role") or "")
        self.output_reserve = int(parameters.get("max_tokens") or 0)
        self.timeout = llm_transport_timeout_sec(parameters.get("timeout"))
        self.gateway = None
        self.operation_id = ""
        self.invocation_id = ""
        self.request_ref: dict = {}
        self.response_ref: dict = {}
        self.retained: dict = {}
        self.retention_error = ""
        self.root = config.DATA_DIR
        self.task_id = ""
        self.capture = None
        self.detail: dict = {}
        self.poll_control = parameters.get("model_poll_control")
        self.operation_observer = parameters.get("model_operation_observer")
        self.request_manifest_ref: dict = {}
        self.interrupt_reason = ""
        self.create_attempted = False
        self.defer_close = False
        self.io_active = False
        self.io_lock = threading.Lock()

    def check_control(self):
        """The caller supplies deadline/cancel policy; this seam transports it."""
        reason = self.interrupt_reason or (self.poll_control() if self.poll_control else None)
        if not reason:
            return
        cancellation = "not_requested"
        if self.operation_id:
            try:
                self.gateway.cancel_model_operation(self.operation_id, reason_code="host_cancelled")
                cancellation = "requested"  # a POST never proves terminality
            except Exception:
                cancellation = "unconfirmed"
        problem = {"code": "model_operation_interrupted", "message": "The caller interrupted model-result waiting.",
                   "context": {"control_reason": reason, "cancellation": cancellation}}
        cls = ClaudexorModelError if self.create_attempted else ClaudexorModelNotDispatched
        error = cls(problem, model_role=self.role, operation_id=self.operation_id,
                    route=(self.detail.get("dispatch") or {}).get("route"))
        error.control_reason = reason
        if self.capture is not None:
            error.physical_attempt_capture = self.capture
        raise error from None

    def prepare(self, reservation):
        self.invocation_id = reservation.attempt_id
        self.root = reservation.drive_root
        scope = current_usage_scope()
        self.task_id = scope.task_id if scope else ""
        self.check_control()
        try:
            self.gateway = ensure_owned_gateway()
            self.request_ref = self.gateway.upload_model_request(self.payload, idempotency_key=self.invocation_id)
            self.request_manifest_ref = persist_call(self.root, task_id=self.task_id, call_id=f"{self.invocation_id}_model_request",
                         call_type="llm_claudexor_request", payload=self.payload, keep_raw=True,
                         manifest={"invocation_id": self.invocation_id, "request_ref": self.request_ref,
                                   "model_role": self.role})["manifest_ref"]
        except ClaudexorUnavailable as error:
            raise ClaudexorModelError({"code": error.code, "message": str(error)}, model_role=self.role) from None

    def observe_operation(self, *, accepted: bool = False) -> None:
        """Publish custody to an optional process boundary, not provider content."""
        if accepted:
            try:
                # Only this operational request manifest is updated. The
                # ledger's immutable physical-candidate manifest is untouched.
                self.request_manifest_ref = persist_call(
                    self.root, task_id=self.task_id, call_id=f"{self.invocation_id}_model_request",
                    call_type="llm_claudexor_request", payload=self.payload, keep_raw=True,
                    manifest={"invocation_id": self.invocation_id, "request_ref": self.request_ref,
                              "model_role": self.role, "operation_id": self.operation_id})["manifest_ref"]
            except Exception as error:
                self.request_manifest_ref = {}
                log.warning("Model custody checkpoint unavailable: %s", type(error).__name__)
        if self.operation_observer is not None:
            try:
                self.operation_observer({"operation_id": self.operation_id, "invocation_id": self.invocation_id,
                                         "request_ref": copy.deepcopy(self.request_ref),
                                         "request_manifest_ref": copy.deepcopy(self.request_manifest_ref)})
            except Exception as error:
                log.warning("Model custody observer unavailable: %s", type(error).__name__)

    def error(self, problem: dict | None, detail: dict | None = None, *, unknown: bool = False):
        detail = detail or {}
        route = (detail.get("dispatch") or {}).get("route") or {}
        cls = ClaudexorModelError if unknown else ClaudexorModelNotDispatched
        return cls(problem or {"code": "model_operation_failed", "message": "The engine returned no model result."},
                   model_role=self.role, operation_id=self.operation_id, route=route, unknown=unknown)

    def receive(self) -> dict:
        outage_started = None
        detail: dict = {}
        while True:
            self.check_control()
            try:
                if not self.operation_id:
                    self.create_attempted = True
                    self.observe_operation()
                    detail = self.gateway.create_model_operation(self.request_ref, idempotency_key=self.invocation_id)
                    self.operation_id = detail["id"]
                    self.observe_operation(accepted=True)
                else:
                    detail = self.gateway.get_model_operation(self.operation_id, timeout_sec=min(self.timeout, _READ_TIMEOUT_SEC))
                self.detail = detail
                if detail.get("state") not in {"queued", "running"}:
                    self.detail = detail
                    response = detail.get("response") or {}
                    if response.get("state") != "ready":
                        raise self.error(detail.get("problem"), detail,
                                         unknown=(detail.get("dispatch") or {}).get("state") != "not_started")
                    self.response_ref = response["ref"]
                    raw = self.gateway.get_model_result(self.operation_id, expected_ref=self.response_ref,
                                                        timeout_sec=min(self.timeout, _READ_TIMEOUT_SEC), raw_bytes=True)
                    self.retain(raw)
                    result = json.loads(raw.decode("utf-8"))
                    if (detail.get("dispatch") or {}).get("state") == "not_started":
                        error = self.error(result.get("problem"), detail)
                        error.route = copy.deepcopy(result.get("route") or error.route)
                        raise error
                    if result.get("outcome") == "unknown" or (detail.get("dispatch") or {}).get("state") == "unknown":
                        raise self.error(result.get("problem"), detail, unknown=True)
                    if (detail.get("dispatch") or {}).get("state") != "response_received" or result.get("outcome") not in {"completed", "incomplete", "failed"}:
                        raise self.error({"code": "malformed_response", "message": "The engine did not prove a terminal provider response."}, detail, unknown=True)
                    return result
                outage_started = None
            except ClaudexorUnavailable as error:
                if not self.operation_id and error.code == "model_request_invalid":
                    # This exact create refusal is minted by the engine only
                    # after its idempotency lookup proves no accepted command,
                    # and before enqueue. A GET or an arbitrary 4xx cannot mint
                    # non-dispatch authority for an earlier accepted request.
                    raise self.error({"code": error.code, "message": str(error),
                                      "context": {"httpStatus": error.status_code}}) from None
                # A failed read after creation is never a provider connect failure.
                # Drop its causal HTTP chain at this boundary: even ConnectError
                # means only the local control read failed, not inference un-sent.
                if error.code != "daemon_unreachable" and not 500 <= error.status_code < 600:
                    raise self.error({"code": error.code, "message": str(error)}, detail, unknown=True) from None
                if outage_started is None:
                    outage_started = time.monotonic()
                if time.monotonic() - outage_started >= self.timeout:
                    raise self.error({"code": "model_control_unreachable", "message": "Control connection lost; the same model operation may still finish."}, detail, unknown=True) from None
            time.sleep(min(config.CLAUDEXOR_MODEL_POLL_INTERVAL_SEC, self.timeout))

    def retain(self, raw: bytes) -> None:
        try:
            self.retained = persist_call(
                self.root, task_id=self.task_id, call_id=f"{self.invocation_id}_model_response",
                # A UTF-8 string in the existing JSON CAS is reversible to the
                # verified wire bytes, including whitespace and numeric spelling.
                call_type="llm_claudexor_response", payload={"result_json_utf8": raw.decode("utf-8")}, keep_raw=True,
                manifest={"operation_id": self.operation_id, "invocation_id": self.invocation_id,
                          "response_ref": self.response_ref, "model_role": self.role},
            )
        except Exception as error:
            # The caller keeps the useful result and the engine keeps its bytes.
            # Lack of local durable custody withholds ACK, never the paid answer.
            self.retention_error = type(error).__name__

    def acknowledge(self) -> dict:
        custody = {"state": "pending", "operation_id": self.operation_id, "response_ref": self.response_ref,
                   "retained_manifest_ref": self.retained.get("manifest_ref")}
        if not self.retained:
            custody["reason"] = f"result_retention_failed:{self.retention_error}"
            return custody
        try:
            receipt = self.gateway.acknowledge_model_result(self.operation_id, self.response_ref["sha256"])
            custody["state"] = (receipt.get("response") or {}).get("state", "pending")
        except Exception as error:
            custody["reason"] = error.code if isinstance(error, ClaudexorUnavailable) else type(error).__name__
        return custody

    def finish(self, result: dict) -> tuple[dict, dict]:
        usage, cost, final = _usage(result)
        route = result.get("route") or {}
        usage.update(provider="claudexor", resolved_model=self.target["usage_model"], cost=cost, cost_final=final,
                     cost_estimated=cost is not None and not final,
                     claudexor={"operation_id": self.operation_id, "model_role": self.role,
                                "route": copy.deepcopy(route), "cost_evidence": copy.deepcopy(result.get("cost")),
                                "outcome": result.get("outcome"), "problem": copy.deepcopy(result.get("problem")),
                                "applied_options": copy.deepcopy(result.get("appliedOptions")),
                                "output_reserve_tokens": self.output_reserve, "output_cap_applied": False,
                                "result_custody": {"state": "pending", "operation_id": self.operation_id,
                                                   "response_ref": self.response_ref,
                                                   "retained_manifest_ref": self.retained.get("manifest_ref")}})
        try:
            self.check_control()
            usage["claudexor"]["result_custody"] = self.acknowledge()
            self.check_control()
        except ClaudexorModelError as error:
            # Control changes authority to continue, never ownership of the
            # already received result or its settled usage.
            error.usage = usage
            error.model_result = copy.deepcopy(result)
            error.route = copy.deepcopy(route)
            raise
        if result.get("outcome") == "failed" or self.detail.get("state") == "cancelled":
            problem = ({"code": "model_operation_cancelled", "message": "The engine cancelled this model operation."}
                       if self.detail.get("state") == "cancelled" else result.get("problem") or {})
            error = ClaudexorModelError(problem, model_role=self.role,
                                       operation_id=self.operation_id, route=route)
            error.physical_attempt_capture = self.capture
            error.usage = usage
            raise error
        message = result.get("message")
        if not isinstance(message, dict):
            error = self.error({"code": "malformed_response", "message": "The provider returned no model message."}, unknown=True)
            error.physical_attempt_capture = self.capture
            error.usage = usage
            raise error
        if result.get("outcome") == "incomplete":
            usage["response_finish_reason"] = (
                "length" if ((result.get("problem") or {}).get("context") or {}).get("reason") == "max_output_tokens"
                else "incomplete"
            )
        return copy.deepcopy(message), usage

    async def offload(self, function, *args):
        """A cancelled caller leaves the current I/O thread owning its gateway."""
        with self.io_lock:
            self.io_active = True

        def run():
            try:
                return function(*args)
            finally:
                with self.io_lock:
                    self.io_active = False
                    close = self.defer_close
                if close:
                    self.close()

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with self.io_lock:
                self.interrupt_reason = "caller_cancelled"
                self.defer_close = True
                close = not self.io_active
            if close:
                self.close()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise

    def close(self):
        with self.io_lock:
            gateway, self.gateway = self.gateway, None
        if gateway is not None:
            gateway.close()


def _reset_native(payload: dict, error: ClaudexorModelNotDispatched, invocation: _ModelInvocation) -> dict | None:
    capture = getattr(error, "physical_attempt_capture", None)
    if error.code != "invalid_continuation" or getattr(capture, "state", None) != "released":
        return None
    route = error.route
    changed = []
    prepared = copy.deepcopy(payload)
    for message in prepared["messages"]:
        native = message.get("nativeContinuation")
        if not isinstance(native, dict):
            continue
        old = native.get("route") or {}
        if (route.get("source") == old.get("source") == payload["source"]
                and route.get("model") in (None, payload["model"])
                and any(route.get(key) and old.get(key) and route[key] != old[key]
                        for key in ("credentialProfileId", "accountFingerprint"))):
            changed.append({"old_route": old, "new_route": route})
            message.pop("nativeContinuation")
    if not changed:
        return None
    append_jsonl(invocation.root / "logs" / "events.jsonl", {
        "ts": utc_now_iso(), "type": "native_continuation_reset", "task_id": invocation.task_id,
        "model_role": invocation.role, "operation_id": invocation.operation_id, "routes": changed,
    })
    return prepared


def _accounted_request(invocation: _ModelInvocation):
    request = replace(_attempt_request(invocation.target, invocation.payload),
                      force_unknown_reservation=True, max_completion_tokens=invocation.output_reserve)
    existing = _candidate_before_dispatch(invocation.payload, request)

    def before(reservation):
        manifest = existing(reservation)
        invocation.prepare(reservation)
        return manifest

    return request, before


def _native_retry_preparation(target: dict, payload: dict, parameters: dict,
                              error: ClaudexorModelNotDispatched):
    """Rebind the already-authorized un-sent repair before preparing its next attempt.

    The engine's new account receipt replaces provisional discovery. Passing
    the sanitized send copy is essential: rebuilding from the old transcript
    would reintroduce the incompatible native envelope just removed above.
    """
    values = {**parameters, "messages": payload["messages"], "tools": payload["tools"],
              "model": target["usage_model"], "use_local": False}
    waiter = current_model_wait()
    if waiter is None:
        if current_physical_attempt_context() is not None:
            raise ModelWaitInterrupted("model_wait_reprepare_required", role=parameters.get("model_role", ""), cause=error)
        return values  # Bare helpers have no captured Main fit to replace.
    values["_model_observed_route"] = {**error.route, "source": target["source"], "model": target["resolved_model"]}
    return waiter.reprepare(parameters.get("model_role", ""), values)


def chat_claudexor(target: dict, messages: list, tools: list | None, **parameters: Any) -> tuple[dict, dict]:
    """One generation, plus at most one proven-un-sent continuation preparation."""
    payload = _request(target, messages, tools, parameters)
    retry_preparation = None
    for preparation in range(2):
        invocation = _ModelInvocation(target, payload, parameters)
        try:
            with prepared_call_scope(retry_preparation or {}) as prepared:
                if prepared:
                    invocation.payload = payload = _request(target, prepared["messages"], prepared.get("tools"), prepared)
                request, before = _accounted_request(invocation)
                result = execute_physical_attempt(request, invocation.receive, extractor=_usage, before_dispatch=before)
                invocation.capture = last_physical_attempt_capture()
                return invocation.finish(result)
        except ClaudexorModelNotDispatched as error:
            if invocation.response_ref:
                invocation.acknowledge()
            updated = _reset_native(payload, error, invocation) if preparation == 0 else None
            if updated is None:
                raise
            payload = updated
            retry_preparation = _native_retry_preparation(target, payload, parameters, error)
        except PhysicalAttemptPreparationFailed as error:
            cause = error.__cause__
            if isinstance(cause, ClaudexorModelError):
                cause.physical_attempt_capture = error.physical_attempt_capture
                raise cause from None
            raise
        finally:
            invocation.close()
    raise AssertionError("Unreachable model preparation loop")


async def chat_claudexor_async(target: dict, messages: list, tools: list | None, **parameters: Any) -> tuple[dict, dict]:
    """Keep accounting/capture in the async caller; offload only synchronous I/O."""
    payload = _request(target, messages, tools, parameters)
    retry_preparation = None
    for preparation in range(2):
        invocation = _ModelInvocation(target, payload, parameters)
        try:
            with prepared_call_scope(retry_preparation or {}) as prepared:
                if prepared:
                    invocation.payload = payload = _request(target, prepared["messages"], prepared.get("tools"), prepared)
                request, before = _accounted_request(invocation)

                async def prepare(reservation):
                    return await invocation.offload(before, reservation)

                async def receive():
                    return await invocation.offload(invocation.receive)

                result = await execute_physical_attempt_async(
                    request, receive, extractor=_usage, before_dispatch=prepare)
                invocation.capture = last_physical_attempt_capture()
                return await invocation.offload(invocation.finish, result)
        except ClaudexorModelNotDispatched as error:
            if invocation.response_ref:
                await invocation.offload(invocation.acknowledge)
            updated = _reset_native(payload, error, invocation) if preparation == 0 else None
            if updated is None:
                raise
            payload = updated
            retry_preparation = _native_retry_preparation(target, payload, parameters, error)
        except PhysicalAttemptPreparationFailed as error:
            cause = error.__cause__
            if isinstance(cause, (ClaudexorModelError, asyncio.CancelledError)):
                cause.physical_attempt_capture = error.physical_attempt_capture
                raise cause from None
            raise
        finally:
            if not invocation.defer_close:
                invocation.close()
    raise AssertionError("Unreachable model preparation loop")
