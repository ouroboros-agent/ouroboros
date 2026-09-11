"""Wire assembly for completed Chat Completions and native Messages SSE replies.

Consumption belongs inside the physical send closure. Only protocol-complete
assemblies leave it as responses; partial bytes stay in private observability
custody and never become an assistant message or a successful settlement.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any


class IncompleteProviderStream(RuntimeError):
    code = "model_outcome_unknown"
    stream_incomplete = True


class ProviderStreamError(IncompleteProviderStream):
    """An explicit SSE error, distinct from an EOF or a socket failure."""

    def __init__(self, body: dict):
        self.body = copy.deepcopy(body)
        self.status_code = 200
        error = body.get("error") or {}
        self.type = str(error.get("type") or error.get("code") or "provider_stream_error")
        self.provider_message = str(error.get("message") or "")
        # Keep producer facts in body/private evidence. Text-only pre-routing
        # classifiers must not turn an HTTP-200 SSE failure into a free rejection.
        super().__init__("Provider reported an SSE error after stream dispatch")


class AssembledResponse:
    """The two existing response readers share this detached, non-iterator value."""

    def __init__(self, body: dict):
        self.body = body

    def model_dump(self):
        return copy.deepcopy(self.body)

    def json(self):
        return self.model_dump()


def _index(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IncompleteProviderStream("Stream item lacks a non-negative integer index")
    return value


def _snapshot(target: dict, update: dict) -> None:
    """Usage counters and metadata are cumulative snapshots, never token deltas."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _snapshot(target[key], value)
        elif value is not None:
            target[key] = copy.deepcopy(value)


def _delta(target: dict, update: dict) -> None:
    for key, value in update.items():
        if key == "tool_calls" and value is not None and not isinstance(value, list):
            raise IncompleteProviderStream("Tool call delta is not a list")
        if value is None:
            target.setdefault(key, None)
        elif isinstance(value, dict):
            if target.get(key) is None:
                target[key] = {}
            current = target.setdefault(key, {})
            if not isinstance(current, dict):
                raise IncompleteProviderStream(f"Conflicting stream field: {key}")
            _delta(current, value)
        elif isinstance(value, list):
            if target.get(key) is None:
                target[key] = []
            current = target.setdefault(key, [])
            if not isinstance(current, list):
                raise IncompleteProviderStream(f"Conflicting stream list: {key}")
            for item in value:
                if isinstance(item, dict) and "index" in item:
                    index = _index(item["index"])
                    match = next((row for row in current if isinstance(row, dict)
                                  and row.get("index") == index), None)
                    if match is None:
                        match = {"index": index}
                        current.append(match)
                    _delta(match, item)
                elif key == "tool_calls":
                    raise IncompleteProviderStream("Tool call delta lacks its index")
                else:
                    current.append(copy.deepcopy(item))
        elif isinstance(value, str) and key not in {"type", "role", "format", "id"}:
            current = target.get(key)
            if current is not None and not isinstance(current, str):
                raise IncompleteProviderStream(f"Conflicting stream text: {key}")
            target[key] = (current or "") + value
        elif key in {"type", "role", "format", "id"} and target.get(key) not in (None, value):
            raise IncompleteProviderStream(f"Conflicting stream identity: {key}")
        else:
            target[key] = copy.deepcopy(value)


class ChatAccumulator:
    def __init__(self, expected_choices: int = 1):
        self.body: dict = {"object": "chat.completion"}
        self.choices: dict[int, dict] = {}
        self.expected_choices = expected_choices
        self.done = False

    def accept(self, event: str, data: str) -> None:
        if self.done:
            raise IncompleteProviderStream("Data after stream terminal frame")
        if data == "[DONE]":
            self.done = True
            return
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise IncompleteProviderStream("Stream chunk is not an object")
        if isinstance(chunk.get("error"), dict):
            _snapshot(self.body, {key: value for key, value in chunk.items()
                                  if key not in {"choices", "usage", "object"}})
            raise ProviderStreamError(chunk)
        for key, value in chunk.items():
            if key in {"choices", "object", "obfuscation"} or value is None:
                continue
            if key in {"id", "model"} and self.body.get(key) not in (None, value):
                raise IncompleteProviderStream(f"Conflicting completion {key}")
            _snapshot(self.body, {key: value})
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise IncompleteProviderStream("Stream chunk lacks choices")
        for update in choices:
            if not isinstance(update, dict):
                raise IncompleteProviderStream("Stream choice is not an object")
            index = _index(update.get("index"))
            choice = self.choices.setdefault(index, {"index": index, "message": {"role": "assistant", "content": None}})
            delta = update.get("delta")
            if not isinstance(delta, dict):
                raise IncompleteProviderStream("Stream choice lacks delta")
            # OpenRouter's final usage frame repeats finish_reason and an empty
            # delta. It updates accounting without creating a second answer.
            substantive = any(value not in (None, "", [], {}) for key, value in delta.items() if key != "role")
            if choice.get("finish_reason") and substantive:
                raise IncompleteProviderStream("Content after choice completion")
            _delta(choice["message"], delta)
            if update.get("logprobs") is not None:
                _delta(choice.setdefault("logprobs", {}), update["logprobs"])
            finish = update.get("finish_reason")
            if finish is not None and (not isinstance(finish, str) or not finish):
                raise IncompleteProviderStream("Invalid choice terminal reason")
            if finish == "error":
                raise ProviderStreamError({"error": {"type": "stream_finish_error"}})
            if finish is not None and choice.get("finish_reason") not in (None, finish):
                raise IncompleteProviderStream("Conflicting choice terminal")
            _snapshot(choice, {key: value for key, value in update.items()
                               if key not in {"delta", "logprobs", "index"}})

    def result(self) -> dict:
        if not self.done or set(self.choices) != set(range(self.expected_choices)):
            raise IncompleteProviderStream("Stream ended without complete terminal framing")
        body = self.partial()
        for choice in self.choices.values():
            calls = choice["message"].get("tool_calls") or []
            if {call["index"] for call in calls} != set(range(len(calls))):
                raise IncompleteProviderStream("Stream ended with missing tool call indices")
        for choice in body["choices"]:
            if not choice.get("finish_reason"):
                raise IncompleteProviderStream("Stream ended before every choice finished")
            calls = choice["message"].get("tool_calls") or []
            legacy = choice["message"].get("function_call")
            if (calls or legacy) and choice["finish_reason"] in {"length", "content_filter"}:
                raise IncompleteProviderStream("Stream exhausted output while producing tool calls")
            for call in calls:
                kind = call.get("type")
                payload = call.get(kind) if kind in {"function", "custom"} else None
                if (not isinstance(call.get("id"), str) or not call["id"]
                        or not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not payload["name"]
                        or not isinstance(payload.get("arguments" if kind == "function" else "input"), str)):
                    raise IncompleteProviderStream("Incomplete streamed tool call")
            if legacy is not None and (not isinstance(legacy, dict) or not legacy.get("name")
                                       or not isinstance(legacy.get("arguments"), str)):
                raise IncompleteProviderStream("Incomplete streamed legacy function call")
        return body

    def partial(self) -> dict:
        body = copy.deepcopy(self.body)
        body["choices"] = [copy.deepcopy(self.choices[key]) for key in sorted(self.choices)]
        for choice in body["choices"]:
            calls = choice["message"].get("tool_calls")
            if isinstance(calls, list) and calls:
                calls.sort(key=lambda call: call["index"])
                for call in calls:
                    call.pop("index", None)
        return body


class AnthropicAccumulator:
    def __init__(self):
        self.body: dict = {}
        self.blocks: dict[int, dict] = {}
        self.open_blocks: set[int] = set()
        self.inputs: dict[int, str] = {}
        self.done = False

    def accept(self, event: str, data: str) -> None:
        if self.done:
            raise IncompleteProviderStream("Data after native stream terminal")
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise IncompleteProviderStream("Native stream chunk is not an object")
        kind = chunk.get("type")
        if event and event != kind:
            raise IncompleteProviderStream("Native SSE event differs from payload type")
        if kind == "error":
            raise ProviderStreamError(chunk)
        if kind == "ping":
            return
        if kind == "message_start":
            if self.body or not isinstance(chunk.get("message"), dict):
                raise IncompleteProviderStream("Invalid native message start")
            self.body = copy.deepcopy(chunk["message"])
            if self.body.get("content"):
                raise IncompleteProviderStream("Native message start contains unexpected blocks")
        elif kind == "content_block_start":
            index = _index(chunk.get("index"))
            if (not self.body or self.body.get("stop_reason") or index in self.blocks
                    or not isinstance(chunk.get("content_block"), dict)):
                raise IncompleteProviderStream("Invalid native block start")
            self.blocks[index] = copy.deepcopy(chunk["content_block"])
            self.open_blocks.add(index)
        elif kind in {"content_block_delta", "content_block_stop"}:
            index = _index(chunk.get("index"))
            if index not in self.open_blocks:
                raise IncompleteProviderStream("Native block delta/stop without an open block")
            block = self.blocks[index]
            if kind == "content_block_stop":
                self.open_blocks.remove(index)
                if index in self.inputs:
                    block["input"] = json.loads(self.inputs[index])
                    if not isinstance(block["input"], dict):
                        raise IncompleteProviderStream("Native tool input is not an object")
                return
            delta = chunk.get("delta")
            if not isinstance(delta, dict):
                raise IncompleteProviderStream("Native block lacks delta")
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                if block.get("type") not in {"tool_use", "server_tool_use"}:
                    raise IncompleteProviderStream("Native input delta belongs to a non-tool block")
                fragment = delta.get("partial_json")
                if not isinstance(fragment, str):
                    raise IncompleteProviderStream("Native input delta is not text")
                self.inputs[index] = self.inputs.get(index, "") + fragment
            elif delta_type == "citations_delta":
                block.setdefault("citations", []).append(copy.deepcopy(delta["citation"]))
            else:
                _delta(block, {key: value for key, value in delta.items() if key != "type"})
        elif kind == "message_delta":
            if not self.body or self.open_blocks:
                raise IncompleteProviderStream("Native message delta before blocks finished")
            _snapshot(self.body, chunk.get("delta") or {})
            _snapshot(self.body.setdefault("usage", {}), chunk.get("usage") or {})
        elif kind == "message_stop":
            self.done = True
        # Future non-content events are retained in the exact wire evidence.

    def result(self) -> dict:
        if (not self.done or not self.body or self.open_blocks or not self.body.get("stop_reason")
                or set(self.blocks) != set(range(len(self.blocks)))):
            raise IncompleteProviderStream("Native stream ended without complete terminal framing")
        if self.body.get("stop_reason") == "max_tokens" and any(
                block.get("type") == "tool_use" for block in self.blocks.values()):
            raise IncompleteProviderStream("Native output exhausted while producing tool calls")
        for block in self.blocks.values():
            kind = block.get("type")
            if kind in {"tool_use", "server_tool_use"} and (
                    not isinstance(block.get("id"), str) or not block["id"]
                    or not isinstance(block.get("name"), str) or not block["name"]
                    or not isinstance(block.get("input"), dict)):
                raise IncompleteProviderStream("Incomplete native tool block")
            if kind == "thinking" and (not isinstance(block.get("thinking"), str)
                                       or not isinstance(block.get("signature"), str) or not block["signature"]):
                raise IncompleteProviderStream("Native thinking block lacks its complete signature")
        return self.partial()

    def partial(self) -> dict:
        return {**copy.deepcopy(self.body), "content": [copy.deepcopy(self.blocks[key]) for key in sorted(self.blocks)]}


class _SSEFrames:
    """Incremental UTF-8 SSE framing; only CR/LF are line delimiters."""

    def __init__(self):
        self.buffer = b""
        self.data: list[str] = []
        self.event = ""
        self.first_line = True

    def feed(self, chunk: bytes, *, final: bool = False):
        self.buffer += chunk
        while match := re.search(b"\r\n|\r|\n", self.buffer):
            if not final and match.group() == b"\r" and match.end() == len(self.buffer):
                break  # CRLF may straddle network chunks.
            line = self.buffer[:match.start()].decode("utf-8")
            self.buffer = self.buffer[match.end():]
            if self.first_line:
                line = line.removeprefix("\ufeff")
                self.first_line = False
            if not line:
                data, event = self.data, self.event
                self.data, self.event = [], ""
                if data:
                    yield event, "\n".join(data)
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    self.data.append(value)
                elif field == "event":
                    self.event = value


class _StreamAssembly:
    def __init__(self, response: Any, native: bool, expected_choices: int):
        self.accumulator = AnthropicAccumulator() if native else ChatAccumulator(expected_choices)
        self.frames = _SSEFrames()
        self.raw: list[bytes] = []
        headers = getattr(response, "headers", {})
        self.generation_id = headers.get("x-generation-id", "")

    def feed(self, chunk: bytes) -> bool:
        self.raw.append(chunk)
        # The socket's per-phase bound was narrowed at dispatch. A late physical
        # terminal still settles its original attempt; no cadence or wall-clock
        # watchdog abandons an in-flight stream to return its wrapper sooner.
        for event, data in self.frames.feed(chunk):
            self.accumulator.accept(event, data)
        return self.accumulator.done

    def retain(self, *, complete: bool, error: BaseException | None = None) -> dict:
        from ouroboros import config
        from ouroboros.observability import persist_call, write_blob
        from ouroboros.usage_accounting import current_usage_scope, last_physical_attempt_capture

        capture = last_physical_attempt_capture()
        scope = current_usage_scope()
        attempt_id = str(getattr(capture, "attempt_id", "") or "")
        facts = {"attempt_id": attempt_id, "complete": complete,
                 "generation_id": self.generation_id or self.accumulator.body.get("id", "")}
        evidence = {"wire_base64": base64.b64encode(b"".join(self.raw)).decode("ascii"),
                    "partial_assembly": self.accumulator.partial(), **facts}
        try:
            if not attempt_id:
                raise RuntimeError("stream has no physical attempt identity")
            root = scope.drive_root if scope is not None else config.DATA_DIR
            # Raw frames can contain private native signatures. Only the blob
            # reference and structural facts enter the ordinary public projection.
            raw_ref = write_blob(root, evidence, kind="json")
            retained = persist_call(root, task_id=str(getattr(scope, "task_id", "") or "llm"),
                                    call_id=f"physical_{attempt_id}_stream", call_type="physical_stream",
                                    payload={**facts, "private_wire_ref": raw_ref}, manifest=facts)
            facts["manifest_ref"] = retained["manifest_ref"]
        except Exception as retention_error:
            facts["retention_error"] = type(retention_error).__name__
            if error is not None:
                error.stream_evidence = evidence
        if error is not None:
            error.stream_receipt = facts
            error.stream_incomplete = True
        return facts

    def result(self) -> dict:
        for event, data in self.frames.feed(b"", final=True):
            self.accumulator.accept(event, data)
        return self.accumulator.result()


def consume_stream(stream: Any, *, native: bool = False, expected_choices: int = 1) -> AssembledResponse:
    response = stream if native else getattr(stream, "response", stream)
    assembly = _StreamAssembly(response, native, expected_choices)
    try:
        chunks = response.iter_content(chunk_size=8192) if native else response.iter_bytes()
        for chunk in chunks:
            if assembly.feed(chunk):
                break
        body = assembly.result()
        response.close()
    except BaseException as exc:
        assembly.retain(complete=False, error=exc)
        try:
            response.close()
        except BaseException:
            pass  # Cleanup cannot replace the original stream/cancellation cause.
        raise
    body["_stream_receipt"] = assembly.retain(complete=True)
    return AssembledResponse(body)


async def consume_stream_async(stream: Any, *, expected_choices: int = 1) -> AssembledResponse:
    response = getattr(stream, "response", stream)
    assembly = _StreamAssembly(response, False, expected_choices)
    try:
        async for chunk in response.aiter_bytes():
            if assembly.feed(chunk):
                break
        body = assembly.result()
        await response.aclose()
    except BaseException as exc:
        assembly.retain(complete=False, error=exc)
        try:
            await response.aclose()
        except BaseException:
            pass
        raise
    body["_stream_receipt"] = assembly.retain(complete=True)
    return AssembledResponse(body)
