"""An author's chosen view of exact review history, without changing review authority.

The history producer owns retrieval and current facts. Pure projection accepts full producer history; runtime hooks use the existing
readers and task-result writer, without a second history store. Specs, current
author stance, decision rows, gaps, substantive attachments and unknown fields
stay in the mandatory part. Only exact-bound dispute fields become selectable
bodies. No attachment-to-spec transfer is inferred here.

Root wiring supplies these body bindings as source_refs of the typed transcript
units, then retains the EXACT resulting actor capsule only after the complete
candidate was applied. One task_source pointer belongs outside current_attempt
in the existing task result. New packets and cold author assembly use the same reader;
already dispatched packets are never rewritten. Missing selection custody keeps
full bodies and adds a gap, never a replacement verdict or another paid call.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

_DEFAULT_SELECTION = object()

VIEW_KIND = "review_history_view"
BODY_KIND = "review_history_body"
REVIEW_HISTORY_MESSAGE_KEY = "_review_history_source"
REVIEW_CONTEXT_INDEX_KEY = "_review_context_index"
SELECTED_VIEW_FIELD = "selected_review_history_view"
_VIEW_VERSION = 1
_HEX = frozenset("0123456789abcdef")


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _immutable_ref(value: Any) -> dict | None:
    """Identity only: display readers/absolute aliases do not change the source."""
    if not isinstance(value, Mapping) or not _hash(value.get("sha256")):
        return None
    if not isinstance(value.get("path"), str) or not value["path"]:
        return None
    size = value.get("size", value.get("bytes"))
    if type(size) is not int or size < 0:
        return None
    return {"kind": value.get("kind", "legacy_wave"), "root": value.get("root", ""),
            "path": value["path"], "size": size, "sha256": value["sha256"]}


def body_binding(source: Mapping[str, Any], field: str, value: Any) -> dict | None:
    """Bind a logical field to its immutable wave/supplement AND its exact value.

    ``source`` is C's {source_ref, field, read, file} address or a bare source ref.
    The returned object can ride the existing unit/capsule ``source_refs`` list.
    Unbound legacy inline material remains full; a cycle id is never a source.
    """
    ref = _immutable_ref(source.get("source_ref", source)) if isinstance(source, Mapping) else None
    if ref is None or not isinstance(field, str) or not field:
        return None
    return {"kind": BODY_KIND, "source_ref": ref, "field": field, "content_sha256": _sha(value)}


def _binding(value: Any) -> dict:
    if not isinstance(value, Mapping) or value.get("kind") != BODY_KIND:
        raise ValueError("Invalid review body binding")
    ref = _immutable_ref(value.get("source_ref"))
    if ref is None or not isinstance(value.get("field"), str) or not value["field"] or not _hash(value.get("content_sha256")):
        raise ValueError("Incomplete review body source identity")
    return {"kind": BODY_KIND, "source_ref": ref, "field": value["field"],
            "content_sha256": value["content_sha256"]}


def _at(value: Any, path: Sequence[Any]) -> Any:
    for part in path:
        value = value[part]
    return value


def split_review_history(history: dict, *, operative_subject: Any = None) -> dict:
    """Pure, lossless split into mandatory facts and explicitly selectable fields.

    Specs/plan prose remain mandatory unless the caller supplies the full current
    operative subject separately; only then may historical versions be selected.
    ``path`` locates today's projection; ``binding`` survives row reordering and
    matches only the same immutable source+field+value, not a mutable fingerprint.
    Replacing each body at its path reconstructs the original history exactly.
    """
    mandatory = copy.deepcopy(history)
    bodies: list[dict] = []

    def take(path: list, address: Any, field: str) -> None:
        parent = _at(mandatory, path[:-1])
        if path[-1] not in parent:
            return
        value = parent[path[-1]]
        bound = body_binding(address, field, value)
        if bound is not None:
            bodies.append({"path": path, "binding": bound, "value": value})
            del parent[path[-1]]

    for i, wave in enumerate(mandatory.get("rounds", [])):
        if not isinstance(wave, dict):
            continue
        path = ["rounds", i]
        source = wave.get("source", {})
        fields = ("findings", "dispositions", "spec", "plan_prose") if operative_subject is not None else ("findings", "dispositions")
        for field in fields:
            address = wave.get("spec_address", source) if field == "spec" else source
            take([*path, field], address, address.get("field", field) or field)
        for j, row in enumerate(wave.get("reviewer_outputs", [])):
            if isinstance(row, dict):
                take([*path, "reviewer_outputs", j, "text"], row.get("request_source", source),
                     f"reviewer_outputs[{j}].text")
        # Commit/change sources are individual retained UTF-8 records, not fields
        # inside a mutable ledger JSON. Match their own immutable source identity.
        for j, seat in enumerate(wave.get("reviewers", [])):
            response = seat.get("response") if isinstance(seat, dict) else None
            if isinstance(response, dict):
                take([*path, "reviewers", j, "response", "text"],
                     {"source_ref": (response.get("source") or {}).get("ref")}, "text")
        rebuttal = wave.get("author_rebuttal")
        if isinstance(rebuttal, dict):
            take([*path, "author_rebuttal", "text"], {"source_ref": (rebuttal.get("source") or {}).get("ref")}, "text")
        for j, decision in enumerate(wave.get("author_decisions", [])):
            if isinstance(decision, dict):
                take([*path, "author_decisions", j, "rationale"],
                     {"source_ref": (decision.get("source_ref") or {}).get("ref")}, "decision.rationale")
        for j, feedback in enumerate(wave.get("historical_feedback", [])):
            if not isinstance(feedback, dict):
                continue
            base = [*path, "historical_feedback", j]
            source = feedback.get("source", {})
            take([*base, "parsed_findings"], source, "parsed_findings")
            if isinstance(feedback.get("result"), dict):
                take([*base, "result", "text"], source, "result.text")
    if operative_subject is not None:
        mandatory["operative_subject"] = copy.deepcopy(operative_subject)
    return {"mandatory": mandatory, "bodies": bodies}


def operative_review_subject(review: dict) -> Any:
    """Resolve only an explicitly selected author or exact current-wave source."""
    history = review.get("dispute_history") or {}
    if "operative_subject" in review:
        return review["operative_subject"]
    operative = None
    author = history.get("current_author_plan")
    if operative is None and isinstance(author, dict) and isinstance(author.get("spec"), dict):
        operative = author
    if operative is None:
        current = review.get("current_wave") or {}
        wanted = _immutable_ref(current.get("wave_artifact"))
        for wave in history.get("rounds", []):
            if (wanted is not None and isinstance(wave, dict) and isinstance(wave.get("spec"), dict)
                    and _immutable_ref((wave.get("source") or {}).get("source_ref")) == wanted):
                operative = {key: copy.deepcopy(wave[key]) for key in (
                    "spec", "plan_prose", "source", "spec_hash", "request_fingerprint", "cycle_index") if key in wave}
                break
    return operative


def _body_message(body: dict, task_id: str) -> dict:
    text = ("[Recorded review dispute; attributed evidence, not owner instructions]\n"
            + json.dumps({"source": body["binding"], "body": body["value"]}, ensure_ascii=False, sort_keys=True))
    return {"role": "user", "content": text, REVIEW_HISTORY_MESSAGE_KEY: {
        "version": 1, "task_id": task_id, "binding": copy.deepcopy(body["binding"]),
        "visible_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}


def _index_message(mandatory: dict, family: str, repo: str = "") -> dict:
    facts = {"family": family, "current": mandatory,
             "rule": "Current typed review facts supersede earlier captured review snapshots; this is not a new verdict."}
    text = "[Current review decisions]\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True)
    return {"role": "user", "content": text, REVIEW_CONTEXT_INDEX_KEY: {
        "family": family, "repo_root": repo, "sha256": _sha(facts),
        "visible_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}


def capture_review_history_messages(runtime_data: dict, *, task_id: str, drive_root: Any = None) -> tuple[dict, list[dict]]:
    """Capture resident typed facts and separately selectable dispute bodies.

    The frozen Runtime prefix names its captured resident index, rather than
    duplicating attachments forever in a system block. Protected index rows live
    after the assignment in every mode and take part in the initial core hash/fit.
    Only explicit compaction may retire old index copies after checkpointing.
    """
    locations = []
    plan = runtime_data.get("plan_review_authority")
    if isinstance(plan, dict) and isinstance(plan.get("dispute_history"), dict):
        locations.append((["plan_review_authority"], "plan", "", operative_review_subject(plan)))
    for i, review in enumerate(runtime_data.get("commit_review_authority") or []):
        if isinstance(review.get("dispute_history"), dict):
            locations.append((["commit_review_authority", i], "commit", review["repo_root"], None))
    if not locations:
        return runtime_data, []
    result, messages, accounts = copy.deepcopy(runtime_data), [], set()
    for path, family, repo, operative in locations:
        review = _at(result, path)
        history = review["dispute_history"]
        view = (selected_review_history(history, drive_root=drive_root, task_id=task_id, operative_subject=operative)
                if drive_root is not None else project_review_history(history, operative_subject=operative))
        index = _index_message(view["mandatory"], family, repo)
        review["dispute_history"] = {"representation": "resident_review_index", **index[REVIEW_CONTEXT_INDEX_KEY],
            "rule": "Full operative facts, decision rows, gaps and untransferred attachments follow the assignment in a protected resident row."}
        messages.append(index)
        if view["actor_account"]:
            capsule = view["actor_account"]["capsule"]
            if _sha(capsule) not in accounts:
                messages.append(copy.deepcopy(capsule))
                accounts.add(_sha(capsule))
        messages.extend(_body_message(body, task_id) for body in view["bodies"])
    return result, messages


def _actor_capsule(capsule: Any) -> dict:
    """Check attribution/integrity without depending on an unmerged compactor."""
    if not isinstance(capsule, dict) or capsule.get("role") != "assistant":
        raise ValueError("Selection is not an applied actor capsule")
    blocks = capsule.get("content")
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
        raise ValueError("Actor capsule content is not one exact text block")
    block = blocks[0]
    meta = block.get("_context_capsule")
    text = block.get("text")
    if (block.get("type") != "text" or not isinstance(text, str) or not text.strip()
            or not isinstance(meta, dict) or meta.get("authorship") != "actor"
            or meta.get("visible_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
        raise ValueError("Actor capsule attribution or visible hash mismatch")
    return meta


def retain_review_history_view(drive_root: Any, task_id: str, *, capsule: dict,
                               covered: Sequence[dict], applied_receipt: Mapping[str, Any], transfers: Sequence[dict] = ()) -> dict:
    """Retain the exact applied capsule; return ONE pointer for existing plan state.

    Caller owns publication order: complete fit/apply first, then this write and
    locked selection-pointer update. Coverage is host-bound removed-unit metadata,
    not a model declaration or a match against prose. No state/decision is changed.
    """
    from ouroboros.artifacts import store_actor_source_bytes

    if applied_receipt.get("status") != "applied":
        raise ValueError("Only an applied authored view can select review history")
    meta = _actor_capsule(capsule)
    bindings = [_binding(ref) for ref in covered]
    if not bindings and not transfers:
        raise ValueError("Selected view covers no exact review body")
    inherited = {_sha(_binding(ref)) for ref in meta.get("source_refs", [])
                 if isinstance(ref, Mapping) and ref.get("kind") == BODY_KIND}
    if any(_sha(ref) not in inherited for ref in bindings):
        raise ValueError("Covered source is absent from the applied capsule lineage")
    record = {"kind": VIEW_KIND, "version": _VIEW_VERSION, "task_id": task_id,
              "capsule": copy.deepcopy(capsule), "covered": bindings, "transfers": copy.deepcopy(list(transfers)),
              "applied_view_revision": applied_receipt.get("view_revision")}
    raw = _bytes(record)
    ref = store_actor_source_bytes(drive_root, task_id, category="context_checkpoints",
                                  source_id="review-history-view", data=raw, extension="json")
    return {"kind": VIEW_KIND, "version": _VIEW_VERSION, "task_id": task_id, "source_ref": ref}


def load_review_history_view(selection: Mapping[str, Any], source_reader: Callable[[dict], bytes]) -> dict:
    """Read one selected task source. The supplied reader binds its owner/root."""
    if (not isinstance(selection, Mapping) or selection.get("kind") != VIEW_KIND
            or selection.get("version") != _VIEW_VERSION or not selection.get("task_id")):
        raise ValueError("Invalid selected review history pointer")
    ref = selection.get("source_ref")
    identity = _immutable_ref(ref)
    if identity is None:
        raise ValueError("Selected review history has no immutable source")
    raw = source_reader(copy.deepcopy(ref))
    if not isinstance(raw, bytes) or len(raw) != identity["size"] or hashlib.sha256(raw).hexdigest() != identity["sha256"]:
        raise ValueError("Selected review history source checksum mismatch")
    record = json.loads(raw.decode("utf-8"))
    if (not isinstance(record, dict) or record.get("kind") != VIEW_KIND
            or record.get("version") != _VIEW_VERSION or record.get("task_id") != selection["task_id"]):
        raise ValueError("Selected review history source owner mismatch")
    meta = _actor_capsule(record.get("capsule"))
    covered = [_binding(ref) for ref in record["covered"]]
    inherited = {_sha(_binding(ref)) for ref in meta.get("source_refs", [])
                 if isinstance(ref, Mapping) and ref.get("kind") == BODY_KIND}
    if (not covered and not record.get("transfers")) or any(_sha(ref) not in inherited for ref in covered):
        raise ValueError("Selected capsule does not carry its covered sources")
    return record


def project_review_history(history: dict, *, operative_subject: Any = None,
                           selection: Mapping[str, Any] | None = None,
                           source_reader: Callable[[dict], bytes] | None = None) -> dict:
    """Pure shared author/NEW-packet view; no selection means identical history.

    Only matching fields are replaced by an explicit address to the selected
    author's account. Current facts and attachments remain full. A new/superseding
    wave or late supplement has another exact binding and remains full. A missing
    account restores full delivery with a gap; it never changes a review verdict.
    """
    split = split_review_history(history, operative_subject=operative_subject)
    shown = copy.deepcopy(history)
    answer = {**split, "history": shown, "actor_account": None, "selection_status": "absent",
              "covered_bindings": [], "unmatched_bindings": [], "selection_gaps": []}
    if selection is None:
        return answer
    try:
        if source_reader is None:
            raise ValueError("Selected account has no source reader")
        record = load_review_history_view(selection, source_reader)
        wanted = {_sha(_binding(ref)): _binding(ref) for ref in record["covered"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        gap = {"code": "REVIEW_HISTORY_VIEW_SOURCE_UNAVAILABLE", "reason": type(exc).__name__ + ": " + str(exc),
               "selection": copy.deepcopy(selection)}
        answer["selection_status"] = "source_unavailable"
        answer["selection_gaps"] = [gap]
        answer["mandatory"]["status"] = shown["status"] = "source_unavailable"
        answer["mandatory"].setdefault("gaps", []).append(copy.deepcopy(gap))
        shown.setdefault("gaps", []).append(copy.deepcopy(gap))
        return answer
    transferred = apply_attachment_transfers(shown, record.get("transfers") or [], operative_subject)
    mandatory_transfers = apply_attachment_transfers(answer["mandatory"], record.get("transfers") or [], operative_subject)
    answer["attachment_transfers"] = mandatory_transfers
    remaining, used = [], set()
    for body in split["bodies"]:
        ident = _sha(body["binding"])
        if ident not in wanted:
            remaining.append(body)
            continue
        used.add(ident)
        _at(shown, body["path"][:-1])[body["path"][-1]] = {
            "representation": "actor_authored_view", "original": copy.deepcopy(body["binding"]),
            "selected_view": copy.deepcopy(selection["source_ref"])}
        answer["covered_bindings"].append(copy.deepcopy(body["binding"]))
    answer["bodies"] = remaining
    answer["unmatched_bindings"] = [ref for ident, ref in wanted.items() if ident not in used]
    answer["selection_status"] = "applied" if used or transferred else "not_applicable"
    if used or transferred:
        account = {"capsule": copy.deepcopy(record["capsule"]),
                   "source_ref": copy.deepcopy(selection["source_ref"]),
                   "rule": "Author's account of named earlier sources, not a new reviewer verdict."}
        answer["actor_account"] = account
        shown["authored_view"] = copy.deepcopy(account)
    return answer


def attachment_bindings(history: dict) -> list[dict]:
    """Attachments remain full unless an explicit transfer names this identity."""
    found = []
    for i, wave in enumerate(history.get("rounds") or []):
        if not isinstance(wave, dict):
            continue
        attached = (wave.get("evidence") or {}).get("attached") or []
        for j, item in enumerate(attached):
            source = wave.get("evidence_source") or wave.get("source") or {}
            field = f"{source.get('field') or 'evidence_manifest_full'}.attached[{j}]"
            binding = body_binding(source, field, item)
            if binding:
                found.append({"path": ["rounds", i, "evidence", "attached", j], "binding": binding})
    return found


def validate_attachment_transfers(history: dict, operative_subject: Any, transfers: Sequence[dict]) -> list[dict]:
    """Check source/current-spec identity and named decisions, never meaning."""
    if not transfers:
        return []
    if not isinstance(transfers, (list, tuple)) or any(not isinstance(row, dict) for row in transfers):
        raise ValueError("Attachment transfers must be objects naming exact sources and spec decisions")
    spec = (operative_subject or {}).get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Attachment transfer needs the full current operative spec")
    ids = {row.get("id") or f"decision_{i}" for i, row in enumerate(spec.get("decisions") or [], 1) if isinstance(row, dict)}
    available = {_sha(row["binding"]) for row in attachment_bindings(history)}
    result = []
    for row in transfers:
        binding = _binding(row.get("source"))
        decisions = row.get("decision_ids")
        if (row.get("operative_spec_sha256") != _sha(spec) or _sha(binding) not in available
                or not isinstance(decisions, list) or not decisions or any(d not in ids for d in decisions)):
            raise ValueError("Attachment transfer source/spec/decision binding is stale or unknown")
        result.append({"source": binding, "operative_spec_sha256": _sha(spec), "decision_ids": list(decisions)})
    return result


def apply_attachment_transfers(history: dict, transfers: list, operative_subject: Any) -> list:
    """A changed source or operative spec restores full text without a new gate."""
    try:
        accepted = validate_attachment_transfers(history, operative_subject, transfers)
    except (ValueError, TypeError, KeyError):
        return []
    chosen = {_sha(row["source"]): row for row in accepted}
    applied = []
    for item in attachment_bindings(history):
        transfer = chosen.get(_sha(item["binding"]))
        if transfer:
            parent = _at(history, item["path"][:-1])
            parent[item["path"][-1]] = {"representation": "author_transferred_to_operative_spec", **copy.deepcopy(transfer),
                "rule": "Author declared the decision transfer; source identity is checked, semantic completeness is not certified."}
            applied.append(copy.deepcopy(transfer))
    return applied


def selected_review_history(history: dict, *, drive_root: Any, task_id: str, operative_subject: Any = None) -> dict:
    """One selected task-result pointer for both plan and commit/change history."""
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.task_results import load_task_result

    try:
        saved = load_task_result(drive_root, task_id, strict=True) or {}
        selection = saved.get(SELECTED_VIEW_FIELD)
    except (OSError, ValueError, TypeError) as exc:
        selection = {"unavailable": type(exc).__name__}
    return project_review_history(history, operative_subject=operative_subject, selection=selection,
        source_reader=lambda ref: read_actor_source_bytes(drive_root, task_id, ref))


def publish_review_history_view(ctx: Any, capsule: dict, receipt: dict, *, transfers: Sequence[dict] = (), pointer: dict | None = None, expected_selection: Any = _DEFAULT_SELECTION) -> dict | None:
    """Publish only the exact applied actor capsule; retain unrelated lifecycle."""
    from ouroboros.task_results import write_task_result

    root = getattr(ctx, "budget_drive_root", None) or ctx.drive_root
    task = str(ctx.task_id)
    meta = _actor_capsule(capsule)
    covered = [ref for ref in meta.get("source_refs", []) if isinstance(ref, dict) and ref.get("kind") == BODY_KIND]
    if not covered and not transfers:
        return None
    pointer = pointer or retain_review_history_view(root, task, capsule=capsule, covered=covered,
                                                   applied_receipt=receipt, transfers=transfers)
    def select(existing: dict, _incoming: dict) -> dict:
        if expected_selection is not _DEFAULT_SELECTION and existing.get(SELECTED_VIEW_FIELD) != expected_selection:
            raise ValueError("Selected review view changed during materialization")
        return {"status": existing.get("status") or "running", SELECTED_VIEW_FIELD: pointer}
    result = write_task_result(root, task, "running", strict_existing_dict=True, _field_projector=select)
    if result.get(SELECTED_VIEW_FIELD) != pointer:
        raise ValueError("Selected review view was not durably published")
    return pointer


def queue_review_history_context(ctx: Any, *, family: str, repo_root: Any = None) -> None:
    """Typed producer notification, consumed once at the completed tool boundary."""
    pending = dict(getattr(ctx, "_pending_review_context", {}) or {})
    repo = str(repo_root or getattr(ctx, "repo_dir", "") or "")
    pending["plan" if family == "plan" else "commit:" + repo] = "" if family == "plan" else repo
    ctx._pending_review_context = pending


def current_plan_history(ctx: Any, *, state: dict | None = None) -> tuple[dict, Any]:
    from ouroboros.task_results import load_plan_review_state, current_plan_review_wave
    from ouroboros.tools.plan_review_artifacts import (plan_review_dispute_history, current_author_plan,
                                                      authority_wave, PlanReviewSourceUnavailable)

    root = getattr(ctx, "budget_drive_root", None) or ctx.drive_root
    state = load_plan_review_state(root, str(ctx.task_id)) if state is None else state
    history = plan_review_dispute_history(root, str(ctx.task_id), state)
    try:
        # Resolve before history dedup; an alias to a critic is not the author's
        # current full spec. An unavailable selected author does not select a critic.
        author = current_author_plan(root, str(ctx.task_id), state)
        if author is not None:
            operative = {key: copy.deepcopy(author[key]) for key in (
                "spec", "plan_prose", "fingerprint", "review_fingerprint", "author_disposition") if key in author}
            operative["source_ref"] = ((state.get("current_attempt") or {}).get("author_subject") or {}).get("source_ref")
        else:
            wave = authority_wave(root, str(ctx.task_id), current_plan_review_wave(state))
            operative = ({key: copy.deepcopy(wave[key]) for key in (
                "spec", "plan_prose", "spec_hash", "request_fingerprint", "cycle_index", "spec_source_ref", "wave_artifact") if key in wave}
                if wave is not None else None)
    except PlanReviewSourceUnavailable:
        operative = None  # the shared history already carries the exact source gap
    return history, operative


def _project_for_context(ctx, history, operative, selection):
    root = getattr(ctx, "budget_drive_root", None) or ctx.drive_root
    if selection is _DEFAULT_SELECTION:
        return selected_review_history(history, drive_root=root, task_id=str(ctx.task_id), operative_subject=operative)
    from ouroboros.artifacts import read_actor_source_bytes
    return project_review_history(history, operative_subject=operative, selection=selection,
        source_reader=lambda ref: read_actor_source_bytes(root, str(ctx.task_id), ref))


def review_context_updates(ctx: Any, *, families: Mapping[str, str] | None = None,
                           messages: Sequence[dict] = (), selection: Any = _DEFAULT_SELECTION) -> tuple[list, dict]:
    """Typed producer updates, measured with the whole batch before delivery.

    Exact bodies already in canonical messages are not appended twice. Current
    index snapshots remain append-only until the actor's checkpointed compaction.
    """
    from ouroboros.review_history import review_dispute_history

    root, task = getattr(ctx, "budget_drive_root", None) or ctx.drive_root, str(ctx.task_id)
    pending = dict(families if families is not None else getattr(ctx, "_pending_review_context", {}) or {})
    known = {_sha(row[REVIEW_HISTORY_MESSAGE_KEY].get("binding")) for row in messages
             if isinstance(row.get(REVIEW_HISTORY_MESSAGE_KEY), dict)}
    indexes = {(_sha(row[REVIEW_CONTEXT_INDEX_KEY])) for row in messages if isinstance(row.get(REVIEW_CONTEXT_INDEX_KEY), dict)}
    updates, identities = [], {}
    for key, repo in pending.items():
        family = "plan" if key == "plan" else "commit"
        if family == "plan":
            history, operative = current_plan_history(ctx)
        else:
            history, operative = review_dispute_history(drive_root=root, repo_root=repo, task_id=task), None
        view = _project_for_context(ctx, history, operative, selection)
        index = _index_message(view["mandatory"], family, repo)
        if _sha(index[REVIEW_CONTEXT_INDEX_KEY]) not in indexes:
            updates.append(index)
        identities[key] = index[REVIEW_CONTEXT_INDEX_KEY]["sha256"]
        for body in view["bodies"]:
            ident = _sha(body["binding"])
            if ident not in known:
                updates.append(_body_message(body, task))
                known.add(ident)
    return updates, identities


def attachment_transfer_options(ctx: Any) -> dict:
    history, operative = current_plan_history(ctx)
    spec = (operative or {}).get("spec")
    if not isinstance(spec, dict):
        return {}
    return {"operative_spec_sha256": _sha(spec),
            "decision_ids": [row.get("id") or f"decision_{i}" for i, row in enumerate(spec.get("decisions") or [], 1) if isinstance(row, dict)],
            "sources": [row["binding"] for row in attachment_bindings(history)],
            "rule": "Attachments stay full until you explicitly declare their decisions and reasons transferred to these current spec decisions using review_transfers with your working_note. This checks identity, not semantic completeness."}


def prepare_review_view(ctx: Any, candidate: list, receipt: dict, transfers: Sequence[dict]) -> tuple[dict | None, dict | None, list, Any]:
    """Retain a candidate capsule, without publishing the task's selected pointer."""
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.task_results import load_task_result

    capsule = next((row for row in candidate if row.get("role") == "assistant" and isinstance(row.get("content"), list)
        and len(row["content"]) == 1 and isinstance(row["content"][0], dict)
        and (row["content"][0].get("_context_capsule") or {}).get("authorship") == "actor"
        and (row["content"][0].get("_context_capsule") or {}).get("unit_id") == "view:" + str(receipt.get("selection_fingerprint") or "")), None)
    if capsule is None:
        return None, None, [], None
    root, task = getattr(ctx, "budget_drive_root", None) or ctx.drive_root, str(ctx.task_id)
    covered = [ref for ref in _actor_capsule(capsule).get("source_refs", []) if isinstance(ref, dict) and ref.get("kind") == BODY_KIND]
    inherited = []
    saved = (load_task_result(root, task, strict=True) or {}).get(SELECTED_VIEW_FIELD)
    if saved:
        try:
            inherited = load_review_history_view(saved, lambda ref: read_actor_source_bytes(root, task, ref)).get("transfers") or []
        except (OSError, ValueError, KeyError, TypeError):
            pass  # full bodies + visible selection gap remain the reader's fallback
    accepted = []
    if transfers or inherited:
        history, operative = current_plan_history(ctx)
        accepted = validate_attachment_transfers(history, operative, transfers)
        for row in inherited:
            try:
                accepted.extend(validate_attachment_transfers(history, operative, [row]))
            except (ValueError, TypeError, KeyError):
                pass  # changed current spec restores attachments
        accepted = list({_sha(row): row for row in accepted}.values())
    if not covered and not accepted:
        return None, capsule, [], saved
    pointer = retain_review_history_view(root, task, capsule=capsule, covered=covered,
                                         applied_receipt=receipt, transfers=accepted)
    return pointer, capsule, accepted, saved


def refresh_compacted_review_context(ctx: Any, candidate: list, *, selection: Any = _DEFAULT_SELECTION) -> list:
    """At the author's checkpointed rewrite only, replace old resident indexes.

    The raw checkpoint includes the observed old indexes. Newer tail indexes
    represent already durable producer state, and the current full state is
    appended here before final whole-candidate measurement and publication.
    """
    families = dict(getattr(ctx, "_pending_review_context", {}) or {})
    for row in candidate:
        meta = row.get(REVIEW_CONTEXT_INDEX_KEY)
        if isinstance(meta, dict):
            repo = str(meta.get("repo_root") or "")
            families["plan" if meta.get("family") == "plan" else "commit:" + repo] = repo
    if not families:
        return candidate
    base = [row for row in candidate if REVIEW_CONTEXT_INDEX_KEY not in row]
    updates, _ = review_context_updates(ctx, families=families, messages=base, selection=selection)
    return [*base, *updates]


def retain_transfer_only_checkpoint(ctx: Any, messages: list, receipt: dict) -> dict:
    """An explicit transfer can update residency while keeping the same note.

    The ordinary materializer called this a no-op. Checkpoint the exact observed
    view before changing the protected index; preserve the actual actor capsule.
    """
    from ouroboros.artifacts import store_actor_source_bytes
    capsule = next((row for row in messages if row.get("role") == "assistant" and isinstance(row.get("content"), list)
        and len(row["content"]) == 1 and isinstance(row["content"][0], dict)
        and (row["content"][0].get("_context_capsule") or {}).get("authorship") == "actor"), None)
    if capsule is None:
        raise ValueError("Attachment transfer has no applied actor account")
    meta = _actor_capsule(capsule)
    root = getattr(ctx, "budget_drive_root", None) or ctx.drive_root
    ref = store_actor_source_bytes(root, str(ctx.task_id), category="context_checkpoints",
        source_id="review-transfer", data=_bytes({"messages": messages, "kind": "review_transfer_checkpoint"}), extension="json")
    return {**receipt, "status": "applied", "checkpoint_ref": ref,
            "selection_fingerprint": str(meta.get("unit_id") or "").removeprefix("view:")}
