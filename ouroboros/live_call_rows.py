"""A live call's writes to the room and the store, in order, beside its audio loop.

The call's event loop moves audio frames, captions, the mailbox and Stop, while one store commit
can take seconds on a busy install (a task record's commit was measured at 2.7 s): so the owner's
words, what was heard, what was said (its narration), a Presence utterance and the call's
checkpoint run on ONE writer thread, in the order the loop hands them over, inside the turn's
context (budget scope, settings). The loop keeps the call's own state (its transcript, counts and
spoken items) and hands the writer only what each write needs; the only state the writer keeps is
its own count of the utterances the conversation record refused. A content acknowledgement waits
for the checkpoint queued after the content, so a write that failed leaves it pending
(``live_turn._deliver_forever``).
"""

from __future__ import annotations

import contextvars
import logging
import pathlib
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict

from ouroboros.live_awareness import SOURCE_CALL

log = logging.getLogger(__name__)


class CallRows:
    """The writer of one call: ``call`` is the driver whose spec, task and progress emitter the rows use."""

    def __init__(self, call: Any) -> None:
        self.call = call
        self.unrecorded = 0  # Presence utterances the conversation record did not take (the writer's own count)
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"call-rows-{call.task_id}")

    def _submit(self, write: Callable[..., Any], *args: Any) -> Future:
        return self._writer.submit(contextvars.copy_context().run, write, *args)

    def shutdown(self) -> None:
        self._writer.shutdown(wait=True)  # every row and the last checkpoint land before the terminal

    def heard(self, text: str, item_id: str) -> Future:
        """Words heard in the call (meeting audio feeds the session): a system row of the room, never the owner's."""
        spec, task_id = self.call.spec, self.call.task_id

        def write() -> None:
            try:
                from supervisor.message_bus import send_with_budget

                send_with_budget(spec.chat_id, f"Heard in the call: {text}", role="system", system_type="call_heard",
                                 narration=False, progress_meta={"source": SOURCE_CALL, "call_id": spec.call_id,
                                                                 "item_id": item_id, "call_task_id": task_id})
            except Exception:
                log.warning("live call %s could not write a heard row", spec.call_id, exc_info=True)
        return self._submit(write)

    def owner(self, text: str, item_id: str, number: int) -> Future:
        """The owner's words as an owner row of the room, without an owner turn behind it."""
        call, spec = self.call, self.call.spec

        def write() -> None:
            try:
                from supervisor.message_bus import get_bridge, log_chat

                client_message_id = f"call:{spec.call_id}:{item_id or number}"
                row = log_chat("in", spec.chat_id, 1, text, source=SOURCE_CALL, sender_label="voice",
                               client_message_id=client_message_id, task_id=call.task_id, drive_root=call.drive_root)
                if row:
                    get_bridge().broadcast({"type": "chat", "role": "user", "content": text, "ts": row.get("ts"),
                                            "source": SOURCE_CALL, "chat_id": spec.chat_id, "task_id": call.task_id,
                                            "client_message_id": client_message_id})
            except Exception:
                log.warning("live call %s could not write an owner row", spec.call_id, exc_info=True)
        return self._submit(write)

    def presence(self, direction: str, text: str, speaker_label: str = "", item_id: str = "") -> Future:
        """One utterance of a Presence call into its conversation's dialogue record."""
        call = self.call

        def write() -> None:
            try:
                from ouroboros.presence_call import record_call_utterance

                if record_call_utterance(call.drive_root, call.task, direction=direction, text=text,
                                         speaker_label=speaker_label, utterance_id=item_id):
                    return
            except Exception:
                log.warning("live call %s could not record a Presence utterance", call.spec.call_id, exc_info=True)
            self.unrecorded += 1
        return self._submit(write)

    def spoken(self, text: str, meta: Dict[str, Any]) -> Future:
        """What Ouroboros said, as the turn's narration (its spoken status in the meta)."""
        emit = self.call.emit_progress

        def write() -> None:
            try:
                emit(text, narration=True, _progress_meta_override=meta)
            except TypeError:
                emit(text, narration=True)
        return self._submit(write)

    def checkpoint(self, block: Dict[str, Any]) -> Future:
        """The call's durable state on its task row; resolves to whether it was written."""
        drive_root, task_id, call_id = self.call.drive_root, self.call.task_id, self.call.spec.call_id

        def write() -> bool:
            from ouroboros.task_result_schema import require_writable_task_result_schema, stamp_task_result_schema
            from ouroboros.task_store import patch_document

            def _mutate(current: Dict[str, Any]) -> Dict[str, Any]:
                require_writable_task_result_schema(current)
                return stamp_task_result_schema({**current, "live_call": block})

            try:
                patch_document(pathlib.Path(drive_root), task_id, _mutate)
                return True
            except Exception:
                log.warning("live call %s checkpoint unwritten; mailbox content stays unacknowledged", call_id,
                            exc_info=True)
                return False
        return self._submit(write)
