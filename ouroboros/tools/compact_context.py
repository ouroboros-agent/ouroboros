"""LLM-requested tool-history compaction trigger."""

from __future__ import annotations

import logging
import copy
import json
from pathlib import Path
from typing import List

from ouroboros.tools.registry import ToolEntry

log = logging.getLogger(__name__)
_CANONICAL_OBSERVATION = object()


def record_context_view(ctx, messages, tool_schemas, *, physical_capture=_CANONICAL_OBSERVATION) -> None:
    """Capture a usable turn's canonical source; prospective pricing never calls this.

    Nothing is added to the prompt or cache identity. Inspect pins this one
    observation for a later authored request, so its own tool pair and newer
    owner messages cannot make that request stale by construction.
    Physical vision/provider projections remain in the existing request artifacts.
    """
    from ouroboros.context_compaction import context_reclaim_transcript_sha256

    observed = copy.deepcopy(messages)
    ctx._last_context_observation = {
        "revision": context_reclaim_transcript_sha256(observed),
        "messages": observed, "tool_schemas": copy.deepcopy(tool_schemas),
    }
    if physical_capture is _CANONICAL_OBSERVATION:
        return  # Existing non-Main actors retain their own observation boundary.
    observation = ctx._last_context_observation
    observation.update(exposed_units=[], physical_source_status="unavailable")
    if physical_capture is None or not physical_capture.candidate_manifest_ref:
        return
    from ouroboros.context_compaction import exposed_context_units, exposed_context_units_from_atoms
    from ouroboros.observability import read_blob_ref, read_call_manifest_ref

    for root in (getattr(ctx, "budget_drive_root", None), getattr(ctx, "drive_root", None)):
        if root is None:
            continue
        try:
            manifest = read_call_manifest_ref(Path(root), physical_capture.candidate_manifest_ref, task_id=ctx.task_id)
            # The send path records the content identities of the exact prepared bytes before
            # observability redaction (``model_send_seal.persist_physical_candidate``): a unit
            # whose result carries a redacted secret or native content is still recognized.
            # Older manifests fall back to the redacted custody projection they stored. The
            # physical source status names the same attempt manifest either way (the
            # historical-input reader copies its redacted projection); ``exposure_basis``
            # tells which identities decided the exposure.
            atoms = manifest.get("exposure_atoms")
            if isinstance(atoms, dict) and atoms:
                exposed = exposed_context_units_from_atoms(observed, atoms)
                basis = str(manifest.get("exposure_atoms_basis") or "prepared_atoms")
            else:
                payload = read_blob_ref(Path(root), manifest["full_payload_ref"])
                physical_messages = payload.get("messages")
                if not isinstance(physical_messages, list):
                    continue
                exposed, basis = exposed_context_units(observed, physical_messages), "redacted_projection"
            observation.update(
                exposed_units=list(exposed), exposure_basis=basis,
                physical_source_status="observed_projection", physical_attempt_id=physical_capture.attempt_id,
                physical_source_ref=physical_capture.candidate_manifest_ref,
            )
            return
        except (OSError, ValueError, KeyError, TypeError):
            log.debug("Physical context source unavailable", exc_info=True)


def owner_protected_texts(ctx) -> tuple:
    """Verbatim owner/principal words from the task's typed owner corpus (``_owner_directives``).

    Typed provenance is the only authorship signal: ``role=user`` alone never protects a
    row. Text and ``input_text`` blocks of a multipart directive count; nothing else does.
    """
    from ouroboros.context_compaction import _plain_text, _unique_strings

    directives = getattr(ctx, "_owner_directives", None)
    texts = []
    for row in directives if isinstance(directives, list) else []:
        content = row.get("content") if isinstance(row, dict) else None
        text = _plain_text(content) if not isinstance(content, list) else "".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, dict) and str(block.get("type") or "") in {"text", "input_text"})
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return _unique_strings(texts)


def _compact_context(ctx, keep_last_n: int | None = None, *, inspect: bool = False,
                     expected_view_revision: str = "", working_note: str | None = None,
                     keep_unit_ids: List[str] | None = None, restore_unit_refs: List[dict] | None = None,
                     schema_names: List[str] | None = None, review_transfers: List[dict] | None = None, **kwargs) -> str:
    """Inspect or queue a replacement through the existing context materializer.

    The explicit authored view (inspect / working_note / restore) addresses the dialogue
    scope: complete tool units, capsules, the actor's own completed replies and host prose
    rows, all under the same positional, hash-bound unit ids. Rows carrying the owner's
    typed words are listed but never eligible. The count-only request (no note) keeps the
    automatic tool-unit reader and the helper.
    """
    from ouroboros.context_compaction import context_units, owner_protected_unit_ids, unit_kind
    from ouroboros.tools.tool_result import ToolResult, _publish_tool_result

    if inspect:
        observed = getattr(ctx, "_last_context_observation", None)
        if not isinstance(observed, dict):
            return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_REPORTED_FAILURE",
                text="Context view unavailable: this actor has no recorded model-send observation yet."))
        ctx._inspected_context_view = observed
        units = context_units(observed["messages"], scope="dialogue")
        protected = owner_protected_unit_ids(observed["messages"], units, owner_protected_texts(ctx))
        checkpoint = observed.get("checkpoint_ref")
        if not checkpoint and getattr(ctx, "task_id", "") and getattr(ctx, "drive_root", None):
            from ouroboros.artifacts import store_actor_source_bytes

            try:
                roots = dict.fromkeys((Path(getattr(ctx, "budget_drive_root", None) or ctx.drive_root).resolve(),
                                       Path(ctx.drive_root).resolve()))
                raw = json.dumps({"messages": observed["messages"],
                    "observed_view_revision": observed["revision"], "selection_fingerprint": observed["revision"],
                    "selected_unit_ids": [unit.unit_id for unit in units]}, ensure_ascii=False).encode("utf-8")
                for root in roots:
                    checkpoint = store_actor_source_bytes(root, ctx.task_id, category="context_checkpoints",
                        source_id="view", data=raw, extension="json")
                observed["checkpoint_ref"] = checkpoint
            except (OSError, ValueError):
                checkpoint = None
                log.debug("Inspected context source could not be retained", exc_info=True)
        # Every restore ref remains self-contained. The reader needs the stable
        # content-addressed path/size/hash, not a repeated read_file invocation.
        # Keep the full reader hint on the pinned observation, outside this O(N) reply.
        restore_checkpoint = ({key: checkpoint[key] for key in ("kind", "root", "path", "size", "sha256")}
                              if checkpoint else None)
        from ouroboros.review_history_view import attachment_transfer_options
        transfers = attachment_transfer_options(ctx) if getattr(ctx, "task_id", "") else {}
        return json.dumps({
            **({"review_attachment_transfer": transfers} if transfers else {}),
            "view_revision": observed["revision"],
            "units": [{"unit_id": unit.unit_id, "raw_sha256": unit.raw_sha256, "kind": unit_kind(observed["messages"], unit),
                       "eligible": unit.unit_id not in protected,
                       "estimated_tokens": unit.context_size_tokens, "source_refs": unit.source_refs,
                       "restore_ref": {"checkpoint_ref": restore_checkpoint, "unit_id": unit.unit_id,
                                       "raw_sha256": unit.raw_sha256} if checkpoint else None,
                       "physically_exposed": ({"unit_id": unit.unit_id, "raw_sha256": unit.raw_sha256}
                                              in observed["exposed_units"]) if "exposed_units" in observed else None}
                      for unit in units],
            "schema_names": [s["function"]["name"] for s in observed["tool_schemas"]],
            "rule": ("This revision names the observed messages; schemas are listed separately. Units are complete tool "
                     "units, earlier records, your own completed replies (kind assistant) and host prose rows (kind host); "
                     "a unit with eligible=false carries the owner's words and stays whole. Select complete unit IDs to "
                     "keep, write one working_note, and preserve original sources. The system view, the assignment and "
                     "newer owner/tool messages remain untouched."),
        }, ensure_ascii=False, separators=(",", ":"))
    if working_note is not None:
        # This invocation is a response to the actor's recorded physical send.
        # The host already owns that causal binding; echoing its hash is only
        # needed when the actor explicitly selects an earlier inspected view.
        observed = (getattr(ctx, "_inspected_context_view", None) if expected_view_revision
                    else getattr(ctx, "_last_context_observation", None))
        if expected_view_revision and (not isinstance(observed, dict) or observed.get("revision") != expected_view_revision):
            last = getattr(ctx, "_last_context_observation", None)
            if isinstance(last, dict) and last.get("revision") == expected_view_revision:
                observed = last
        if not isinstance(observed, dict) or expected_view_revision and observed.get("revision") != expected_view_revision:
            return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR",
                text="Context view mismatch: call compact_context(inspect=true) and use its view_revision. Current context is unchanged."))
        if (not isinstance(working_note, str)
                or any(values is not None and (not isinstance(values, list)
                       or not all(isinstance(v, str) and v for v in values))
                       for values in (keep_unit_ids, schema_names))
                or restore_unit_refs is not None and (not isinstance(restore_unit_refs, list)
                    or not all(isinstance(ref, dict) for ref in restore_unit_refs))):
            return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR",
                text="Invalid context view request: use a prose working_note, arrays of exact unit/schema names and checkpoint reference objects."))
        if review_transfers:
            from ouroboros.review_history_view import current_plan_history, validate_attachment_transfers
            try:
                history, operative = current_plan_history(ctx)
                review_transfers = validate_attachment_transfers(history, operative, review_transfers)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR",
                    text=f"Review attachment transfer was not selected: {exc}. Inspect current sources/spec, then declare exact transfers with your working_note."))
        if keep_unit_ids is None and keep_last_n is not None:
            # keep_last_n counts completed tool units; everything from the N-th most recent
            # tool unit onward (prose rows between and after them included) stays raw.
            count = max(2, min(keep_last_n, 20))
            units = context_units(observed["messages"], scope="dialogue")
            tool_units = [unit for unit in units if unit_kind(observed["messages"], unit) == "tool"]
            anchor = tool_units[-count].start if len(tool_units) >= count else 0
            keep_unit_ids = [unit.unit_id for unit in units if unit.start >= anchor]
        ctx._pending_compaction = {
            "observed": observed, "working_note": working_note, "review_transfers": list(review_transfers or []),
            "expected_view_revision": observed["revision"],
            "keep_unit_ids": None if keep_unit_ids is None else tuple(keep_unit_ids),
            "restore_unit_refs": tuple(restore_unit_refs or ()),
            "schema_names": None if schema_names is None else tuple(schema_names),
        }
        return "Working view requested. The next complete tool boundary preserves exact sources and checks the full candidate before applying it; the resulting receipt reports actual changes."

    if review_transfers:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR",
            text="review_transfers requires your working_note; ordinary helper compaction cannot declare decision transfer."))
    keep_last_n = max(2, min(6 if keep_last_n is None else keep_last_n, 20))

    ctx._pending_compaction = keep_last_n

    return (
        f"✅ Context reclaim scheduled: keeping the last {keep_last_n} completed tool units raw. "
        "After a useful selection is known, Ouroboros checkpoints the exact actor-visible "
        "transcript and replaces only fully covered older atomic units with active-context "
        "summaries carrying checkpoint/CAS provenance. The raw evidence remains retrievable "
        "from those references. This takes effect on the next round."
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="compact_context",
            schema={
                "name": "compact_context",
                "description": (
                    "Request complete-input context reclaim. "
                    "Supply your working_note to author the replacement, or omit it for helper summarization of old "
                    "completed tool units. An authored view addresses complete tool units, earlier records, your own "
                    "completed replies and host prose rows (inspect lists them with kind and eligibility); the owner's "
                    "words, the assignment and the system view always stay whole. "
                    "Select exact keep_unit_ids or explicitly keep_last_n recent tool units raw; "
                    "an authored note without either keeps all units. A selected older assistant tool call and all of its "
                    "contiguous matching results stay atomic; Ouroboros checkpoints their exact "
                    "actor-visible bytes before replacing them with summaries whose metadata points "
                    "to the checkpoint/CAS evidence. Active context becomes summarized; raw evidence "
                    "remains retrievable through the recorded provenance (restore_unit_refs; an emergency "
                    "record lists its checkpoint once and its member units separately)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "inspect": {"type": "boolean", "description": "Return and pin the last observed view revision, complete unit IDs, source references and current schema names without changing context."},
                        "expected_view_revision": {"type": "string", "description": "Optional view_revision from inspect, checked exactly. Omitted binds the actual model-send view that produced this call; inspect is not required to replace all completed units."},
                        "working_note": {"type": "string", "description": "One coherent account of current understanding, corrections and unresolved work. Supplying it selects your authored view; omission keeps legacy helper compaction."},
                        "keep_unit_ids": {"type": "array", "items": {"type": "string"}, "description": "Exact inspected complete units to retain raw; takes precedence over keep_last_n, empty keeps none eligible. If both selectors are omitted, an authored note keeps all. Owner words, the assignment, the system view and the newer tail are preserved whatever the selection."},
                        "restore_unit_refs": {"type": "array", "items": {"type": "object", "properties": {
                            "checkpoint_ref": {"type": "object"}, "unit_id": {"type": "string"}, "raw_sha256": {"type": "string"}},
                            "required": ["checkpoint_ref", "unit_id", "raw_sha256"]},
                            "description": "Read exact checkpoint-local units back as labelled sources, never live tool protocol replay."},
                        "schema_names": {"type": "array", "items": {"type": "string"}, "description": "Nano only: desired canonical schemas. This selects residency, never execution permissions. Low/Max retain their full permitted envelope."},
                        "review_transfers": {"type": "array", "items": {"type": "object", "properties": {
                            "source": {"type": "object"}, "operative_spec_sha256": {"type": "string"},
                            "decision_ids": {"type": "array", "items": {"type": "string"}}},
                            "required": ["source", "operative_spec_sha256", "decision_ids"]},
                            "description": "With working_note only: explicitly declare that an attachment's decisions and reasons have been transferred into the named current plan spec decisions. Inspect supplies exact source/spec identities. No semantic completeness is inferred; absent or stale transfer keeps the attachment full."},
                        "keep_last_n": {
                            "type": "integer",
                            "description": "Number of recent completed atomic tool units to keep raw (range 2-20). With working_note, used only when explicitly supplied and keep_unit_ids is omitted. Without working_note, defaults to 6 for helper summarization.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_compact_context,
            timeout_sec=15,
        ),
    ]
