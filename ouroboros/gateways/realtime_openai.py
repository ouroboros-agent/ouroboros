"""OpenAI Realtime wire projection, reached through ``LLMClient.open_live``.

Wire evidence: tests/fixtures/realtime_wire/openai. The public client/server
event references at https://developers.openai.com/api/reference/resources/realtime
define PCM rates, singular usage details, cancellation and complete function calls.
Only the host builds rollover history; this adapter never replays tool effects.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import re
from urllib.parse import urlencode

from ouroboros.gateways.realtime_engine import (
    AudioIn, AudioOut, Caption, ContextItem, EngineError, Interrupt, Ready,
    ResponseDone, ResponseStarted, SpeechStarted, ToolCall, ToolResult,
    ToolsApplied, TranscriptionUsage, UpdateTools, USAGE_KEYS, register_engine,
)
from ouroboros.gateways.realtime_transport import (
    LiveEngineRefusal, RealtimeTransport, check_audio, function_schemas, image_part,
    transport_error, wire_text,
)

TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
SESSION_LIMIT_SECONDS = 60 * 60  # OpenAI's physical connection lifetime.


def normalize_usage(raw):
    """Modality totals include cached input; raw details retain its modality split."""
    usage = dict.fromkeys(USAGE_KEYS, 0)
    inputs = raw.get("input_token_details") or {}
    outputs = raw.get("output_token_details") or {}
    for modality in ("text", "audio", "image"):
        usage[f"{modality}_input_tokens"] = inputs.get(f"{modality}_tokens", 0)
    for modality in ("text", "audio"):
        usage[f"{modality}_output_tokens"] = outputs.get(f"{modality}_tokens", 0)
    usage["cached_input_tokens"] = inputs.get("cached_tokens", 0)
    # Separate transcription usage reports text output only, without an output
    # details object. Realtime response totals never stand in for modalities.
    if raw.get("type") == "tokens" and not outputs:
        usage["text_output_tokens"] = raw.get("output_tokens", 0)
    return usage


def session_update(config):
    output = {"format": {"type": "audio/pcm", "rate": 24000}}
    if config.route.voice:
        output["voice"] = config.route.voice
    return {"type": "session.update", "session": {
        "type": "realtime", "instructions": config.instructions,
        "tools": [{"type": "function", **tool} for tool in function_schemas(config.tools)],
        "output_modalities": ["audio"],
        "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "transcription": {"model": TRANSCRIPTION_MODEL},
            "turn_detection": {"type": "semantic_vad", "create_response": True,
                               "interrupt_response": True},
        }, "output": output},
        "truncation": {"type": "retention_ratio", "retention_ratio": 0.8},
    }}


def history_item(row):
    """Project host dialogue views as messages, never executable function calls."""
    import json

    role = row.get("role", "user")
    content = row.get("content", "")
    if role not in ("user", "assistant"):
        content = f"[{role}] {content}" if isinstance(content, str) else json.dumps(row)
        role = "user"
    kind = "text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        content = [{"type": kind, "text": content}]
    else:
        parts = []
        for part in content:
            if part.get("type") in ("text", "input_text", "output_text"):
                parts.append({"type": kind, "text": part["text"]})
            elif part.get("type") == "image_url" and role == "user":
                image = part["image_url"]
                parts.append({"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image})
            else:
                raise LiveEngineRefusal("history_content", "Live history requires text or user image content.")
        content = parts
    if row.get("tool_calls"):
        content = [*content, {"type": kind, "text": "[Earlier tool calls] " + json.dumps(row["tool_calls"])}]
    return {"type": "conversation.item.create", "item": {"type": "message", "role": role, "content": content}}


class OpenAIRealtimeEngine(RealtimeTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._active_response = ""
        self._response_requested = False
        self._need_response = False
        self._flush_task = None
        self._calls_seen = set()
        self._responses_done = set()
        self._tool_updates = deque()
        self._audio_indices = {}

    async def open(self, config):
        if config.route.engine != "openai":
            raise LiveEngineRefusal("route", "OpenAI requires an openai live route.")
        payload = session_update(config)
        history = [history_item(row) for row in config.history]
        self._begin(config)
        try:
            await self._connect("wss://api.openai.com/v1/realtime?" + urlencode({"model": config.route.model}),
                                {"Authorization": "Bearer " + self._api_key})
            await self._write(payload)
            for item in history:
                await self._write(item)
            self._spawn(self._read())
            self._spawn(self._session_limit())
        except asyncio.CancelledError:
            await self.close("cancelled")
            raise
        except LiveEngineRefusal:
            await self.close("configuration")
            raise
        except Exception as exc:
            await self._fail(transport_error(exc))

    async def _session_limit(self):
        await asyncio.sleep(SESSION_LIMIT_SECONDS)
        await self.close("session_limit")

    def _schedule_response(self):
        if self._need_response and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = self._spawn(self._flush_response())

    async def _flush_response(self):
        # One loop tick batches a serialized tool-result batch without a timer
        # deciding when the mind should speak. In-flight creates count as active.
        await asyncio.sleep(0)
        try:
            async with self._command_lock:
                if self._need_response and not self._active_response and not self._response_requested:
                    self._response_requested = True
                    self._need_response = False
                    await self._write({"type": "response.create"})
        except LiveEngineRefusal:
            return
        except Exception as exc:
            await self._fail(transport_error(exc))

    async def _send_command(self, command):
        if isinstance(command, AudioIn):
            check_audio(command, 24000)
            await self._write({"type": "input_audio_buffer.append", "audio": base64.b64encode(command.pcm).decode("ascii")})
        elif isinstance(command, ContextItem):
            await self._write(history_item({"role": "user", "content": f"[{command.source}] {command.text}"}))
        elif isinstance(command, ToolResult):
            images = [image_part(data) for data in command.images]
            messages = [{"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": command.call_id, "output": command.output}}]
            if images:
                messages.append({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": f"[Tool images: {command.call_id}]"},
                        *[{"type": "input_image", "image_url": f"data:{mime};base64,{data}"} for mime, data in images],
                    ]}})
            for message in messages:
                wire_text(message)
            for message in messages:
                await self._write(message)
            self._need_response = True
            self._schedule_response()
        elif isinstance(command, Interrupt):
            if command.played_ms < 0:
                raise LiveEngineRefusal("played_ms", "Played audio duration must be nonnegative.")
            if self._active_response or self._response_requested:
                await self._write({"type": "response.cancel"})
            if command.item_id:
                await self._write({"type": "conversation.item.truncate", "item_id": command.item_id,
                                   "content_index": self._audio_indices.get(command.item_id, 0),
                                   "audio_end_ms": command.played_ms})
        elif isinstance(command, UpdateTools):
            tools = [{"type": "function", **tool} for tool in function_schemas(command.tools)]
            await self._write({"type": "session.update", "session": {"type": "realtime", "tools": tools}})
            self._tool_updates.append(tools)
        else:
            raise LiveEngineRefusal("command", "Unsupported live engine command.")

    def _tool_call(self, item):
        # Arguments travel as the model produced them: a malformed object is the
        # ordinary tool path's typed argument error back to the model, not a protocol
        # failure that ends the session.
        call_id = item["call_id"]
        if call_id not in self._calls_seen:
            self._calls_seen.add(call_id)
            self._emit(ToolCall(call_id, item["name"], str(item.get("arguments") or "")))

    async def _handle_message(self, message):
        kind = message.get("type")
        if kind == "session.updated":
            session = message.get("session", {})
            if not self._ready.is_set():
                self._ready.set()
                self._emit(Ready({**session, "input_rate": 24000, "output_rate": 24000}))
            elif self._tool_updates:
                tools = self._tool_updates[0]
                if list(function_schemas(session.get("tools", ()))) == list(function_schemas(tools)):
                    self._tool_updates.popleft()
                    self._emit(ToolsApplied(tuple(tool["name"] for tool in tools)))
        elif kind == "response.created":
            response = message["response"]
            self._active_response = response["id"]
            self._response_requested = False
            self._emit(ResponseStarted("", response["id"]))
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            self._audio_indices[message["item_id"]] = message.get("content_index", 0)
            self._emit(AudioOut(base64.b64decode(message["delta"], validate=True), 24000, message["item_id"]))
        elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta",
                      "response.output_text.delta", "response.text.delta"):
            self._emit(Caption("ouroboros", message["delta"], False, message["item_id"]))
        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done",
                      "response.output_text.done", "response.text.done"):
            self._emit(Caption("ouroboros", message.get("transcript", message.get("text", "")), True, message["item_id"]))
        elif kind == "conversation.item.input_audio_transcription.delta":
            self._emit(Caption("owner", message["delta"], False, message["item_id"]))
        elif kind == "conversation.item.input_audio_transcription.completed":
            self._emit(Caption("owner", message["transcript"], True, message["item_id"]))
            if "usage" in message:
                raw = message["usage"]
                self._emit(TranscriptionUsage(TRANSCRIPTION_MODEL, normalize_usage(raw), raw))
        elif kind == "input_audio_buffer.speech_started":
            start = message.get("audio_start_ms")
            self._emit(SpeechStarted(str(message.get("item_id") or ""),
                                     start if isinstance(start, int) and not isinstance(start, bool) else -1))
        elif kind == "response.function_call_arguments.done":
            self._tool_call(message)
        elif kind == "response.done":
            response = message["response"]
            if response["id"] in self._responses_done:
                return
            self._responses_done.add(response["id"])
            output = response.get("output") or []
            for item in output:
                if item.get("type") == "function_call" and item.get("status", "completed") == "completed":
                    self._tool_call(item)
            raw = response.get("usage") or {}
            item_id = next((item["id"] for item in output if item.get("type") == "message"), "")
            self._emit(ResponseDone(item_id, response.get("status", "unknown"), normalize_usage(raw), raw, response["id"]))
            if response["id"] == self._active_response:
                self._active_response = ""
                self._response_requested = False
            self._schedule_response()
        elif kind == "conversation.item.input_audio_transcription.failed":
            # One input the transcription model could not read: the session goes on, and
            # that item simply yields no words (an empty final caption retires it).
            if message.get("item_id"):
                self._emit(Caption("owner", "", True, str(message["item_id"])))
        elif kind == "error":
            error = message.get("error") or {}
            code = error.get("code") or error.get("type")
            if code in ("session_expired", "session_timeout", "session_duration_exceeded"):
                await self.close("session_limit")
            elif code == "conversation_already_has_active_response":
                self._response_requested = False
                self._need_response = True
            elif code == "response_cancel_not_active":
                # Native VAD may cancel just before the host's playback flush.
                # Cancellation is idempotent; response.done still owns usage.
                return
            else:
                # The instruction field has its own physical limit, separate
                # from the model window. The typed code and field decide; the
                # measured numbers ride along when the message carries them, so
                # the host can use its ordinary actual-refusal projection ladder.
                if code == "invalid_value" and error.get("param") == "session.instructions":
                    size = re.fullmatch(r"Instructions cannot be longer than (\d+) tokens, you have provided (\d+) tokens\.",
                                        str(error.get("message") or ""))
                    facts = {"field": "session.instructions"}
                    if size:
                        facts.update(limit_tokens=int(size[1]), input_tokens=int(size[2]))
                    await self._fail(EngineError("context_length", json.dumps(facts)))
                    return
                retryable = code in ("server_error", "rate_limit_exceeded")
                category = {"rate_limit_exceeded": "rate_limit", "invalid_api_key": "authentication",
                            "invalid_request_error": "invalid_request"}.get(code, "provider")
                await self._fail(EngineError(category, "OpenAI rejected a live event.", retryable))


register_engine("openai", OpenAIRealtimeEngine)
