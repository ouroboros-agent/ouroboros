"""The languages a live call's speech recognition expects, as the voice recorded them.

Gemini Live's recognizer takes a list of languages only when a session opens
(``inputAudioTranscription.languageCodes``, ``realtime_gemini``); without one it guesses each
utterance and can hear noisy Russian as Korean. The list is not a setting: the voice records it
itself with the call's own ``set_call_languages`` tool, from what it knows of the people it talks
to (the room's correspondence, its memory), and corrects it when someone speaks another language.
The owner's calls share one list in every room; a Presence binding keeps its own, since its
correspondent is someone else. A call reads the record when it starts, and a list recorded
during a call reopens its session at the next completed turn (``live_call_state``). A hint never
decides the reply's language: the voice answers in the language the conversation is in.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional, Sequence, Tuple

from ouroboros.ui_language import normalize_language_tag
from ouroboros.utils import read_json_dict, update_json_locked, utc_now_iso

SET_CALL_LANGUAGES_TOOL = "set_call_languages"
SET_CALL_LANGUAGES_SCHEMA = {"type": "function", "function": {
    "name": SET_CALL_LANGUAGES_TOOL,
    "description": ("Record the languages speech recognition should expect in calls with these people, the most "
                    "spoken first, as BCP-47 tags such as ru-RU or en-US; an empty list lets recognition guess "
                    "each utterance again. Judge them from the correspondence and your memory, and add a language "
                    "you hear someone use. Recognition takes the list at the next pause and later calls start "
                    "with it. It never decides the language you answer in."),
    "parameters": {"type": "object", "properties": {"languages": {"type": "array", "items": {"type": "string"}}},
                   "required": ["languages"], "additionalProperties": False}}}


def _record_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "call_languages.json"


def _tags(raw: Any) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The well-formed tags of ``raw`` in order and once each, and the items that are not tags."""
    kept, refused = [], []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        tag = normalize_language_tag(item) if isinstance(item, str) else None
        if not tag:
            refused.append(str(item))
        elif tag not in kept:
            kept.append(tag)
    return tuple(kept), tuple(refused)


def recorded_call_languages(drive_root: Any, binding_id: str = "") -> Tuple[str, ...]:
    """The list recorded for the owner's calls, or for one Presence binding's; ``()`` when none."""
    if not drive_root:
        return ()
    data = read_json_dict(_record_path(drive_root)) or {}
    bindings = data.get("bindings") if isinstance(data.get("bindings"), dict) else {}
    entry = bindings.get(binding_id) if binding_id else data.get("owner")
    return _tags(entry.get("languages"))[0] if isinstance(entry, dict) else ()


def record_call_languages(drive_root: Any, languages: Any, *, binding_id: str = "",
                          task_id: str = "") -> Tuple[str, ...]:
    """Record the list for the owner's calls (or the binding's) and return it as recorded; a list
    that is not one, or an item that is not a language tag, raises ``ValueError`` naming it."""
    if not isinstance(languages, (list, tuple)):
        raise ValueError("languages must be a list of language tags such as ru-RU")
    kept, refused = _tags(languages)
    if refused:
        raise ValueError(f"not a language tag: {', '.join(refused)} (use tags such as ru-RU or en-US)")
    entry = {"languages": list(kept), "recorded_at": utc_now_iso(), "task_id": str(task_id or "")}

    def mutate(current: Dict[str, Any]) -> Dict[str, Any]:
        if binding_id:
            bindings = current.get("bindings") if isinstance(current.get("bindings"), dict) else {}
            current["bindings"] = {**bindings, binding_id: entry}
        else:
            current["owner"] = entry
        return current

    update_json_locked(_record_path(drive_root), mutate)
    return kept


def languages_recorded_text(languages: Sequence[str]) -> str:
    """The tool's answer and the call transcript's line for a recorded list."""
    if not languages:
        return "Recorded: no languages. Speech recognition guesses each utterance again from the next pause."
    return (f"Recorded: speech recognition expects {', '.join(languages)} from the next pause, "
            "and later calls start with it.")


def languages_opening_sentence(languages: Optional[Sequence[str]]) -> str:
    """What a call's opening says about its recognition languages (``call_opening_text``)."""
    if languages:
        return (f"Speech recognition expects {', '.join(languages)}, as you recorded for these calls; when someone "
                f"speaks a language that is not listed, record the new list with {SET_CALL_LANGUAGES_TOOL}.")
    return ("Speech recognition has no languages recorded for these calls yet and guesses each utterance: assume "
            "the owner's language from this room's correspondence and your memory and record it right away with "
            f"{SET_CALL_LANGUAGES_TOOL}, the owner's first; ask when unsure.")
