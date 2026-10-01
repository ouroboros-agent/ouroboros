"""The mind's revisable remembering guidance, on the ordinary knowledge shelf.

The note is not another policy store: the existing knowledge writer, revisions,
history and index own it. Memory helpers read its complete current text at the
start of their operation, so a correction can change subsequent remembering.
"""

from __future__ import annotations

import json
from pathlib import Path

REMEMBERING_TOPIC = "remembering"


def remembering_guidance(data_root: Path) -> str:
    """Return complete authored guidance and its source, or an explicit read gap."""
    from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address

    try:
        note = read_knowledge_note(resolve_knowledge_address(data_root, REMEMBERING_TOPIC, "global"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, UnicodeError):
        return "\n## Remembering guidance\nThe current global remembering note is unavailable; do not infer its contents.\n"
    return ("\n## Remembering guidance maintained through the shared knowledge tools\n"
            "Apply this revisable guidance to the supplied evidence. Preserve the distinction between "
            "the acting mind's words and your helper interpretation; this note is not new owner authority.\n"
            + json.dumps(note.source_ref(), ensure_ascii=False) + "\n" + note.text + "\n")
