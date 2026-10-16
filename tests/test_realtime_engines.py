"""Live provider wire replay and the credential/transport consumer boundaries."""

import asyncio
import base64
from dataclasses import replace
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.gateways import realtime_gemini as gemini, realtime_openai as openai
from ouroboros.gateways.realtime_engine import (
    AudioIn, AudioOut, Caption, Close, Closed, ContextItem, EngineError, EngineRoute,
    GoAway, Interrupt, PlayedAudio, Ready, ResponseDone, ResponseStarted,
    SessionConfig, SpeechStarted, ToolCall, ToolCallCancelled,
    ToolResult, ToolsApplied, TranscriptionUsage, UpdateTools, USAGE_KEYS,
)
from ouroboros.gateways.realtime_transport import LiveEngineRefusal, transport_error

WIRE = Path(__file__).parent / "fixtures" / "realtime_wire"
TOOL = {"type": "function", "function": {"name": "lookup", "description": "Return the fixture value.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
NEW_TOOL = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
PCM = b"\x00\x00\x01\x00"


def run_async(test):
    @functools.wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


async def eventually(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(), 2)


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.received = 0
        self.closed = False

    async def recv(self):
        value = await self.incoming.get()
        self.received += 1
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)

    async def send(self, value):
        assert not self.closed
        self.sent.append(json.loads(value))

    async def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.sockets = []
        self.options = []

    async def __call__(self, url, **kwargs):
        self.options.append((url, kwargs))
        socket = Socket()
        self.sockets.append(socket)
        return socket


async def feed(engine, socket, message):
    count = socket.received + 1
    socket.incoming.put_nowait(message)
    await eventually(lambda: socket.received == count)
    async with engine._command_lock:
        pass
    await asyncio.sleep(0)


async def opened(provider, *, config=None, collect=True):
    connection = Connection()
    cls = openai.OpenAIRealtimeEngine if provider == "openai" else gemini.GeminiLiveEngine
    engine = cls(api_key="test-key-never-log", connector=connection)
    config = config or SessionConfig(EngineRoute(provider, "unseen-model", voice="test-voice"), "Whole instructions", (TOOL,))
    events = []

    async def consume():
        async for event in engine.events():
            events.append(event)

    collector = asyncio.create_task(consume()) if collect else None
    await engine.open(config)
    ready = {"type": "session.updated", "session": openai.session_update(config)["session"]} if provider == "openai" else {"setupComplete": {}}
    await feed(engine, connection.sockets[0], ready)
    return engine, connection, events, collector


async def finished(engine, collector):
    await engine.close("test_finished")
    if collector:
        await asyncio.wait_for(collector, 2)
    assert not engine._tasks


def selected(events, kind):
    return [event for event in events if isinstance(event, kind)]


@pytest.mark.parametrize("valid", [True, False])
@run_async
async def test_recorded_instruction_refusal_retains_only_capacity_evidence(valid):
    engine, connection, events, collector = await opened("openai")
    # Recorded from the real integrated probe, before any audio was sent.
    error = {"type": "invalid_request_error", "code": "invalid_value", "param": "session.instructions",
             "message": "Instructions cannot be longer than 65536 tokens, you have provided 186324 tokens."}
    if not valid:
        error["param"] = "session.tools"
    await feed(engine, connection.sockets[0], {"type": "error", "error": error})
    await finished(engine, collector)
    event = selected(events, EngineError)[0]
    if valid:
        assert event.kind == "context_length" and not event.retryable
        assert json.loads(event.message) == {"limit_tokens": 65536, "input_tokens": 186324, "field": "session.instructions"}
    else:
        assert event.kind == "provider"



@run_async
async def test_a_reworded_instruction_refusal_is_still_a_context_length_refusal():
    engine, connection, events, collector = await opened("openai")
    error = {"type": "invalid_request_error", "code": "invalid_value", "param": "session.instructions",
             "message": "The instructions are too long for this session."}  # no numbers to read
    await feed(engine, connection.sockets[0], {"type": "error", "error": error})
    await finished(engine, collector)
    event = selected(events, EngineError)[0]
    assert event.kind == "context_length" and json.loads(event.message) == {"field": "session.instructions"}


@run_async
async def test_one_unreadable_input_yields_no_words_and_the_session_goes_on():
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    await feed(engine, socket, {"type": "conversation.item.input_audio_transcription.failed", "item_id": "noise",
                                "content_index": 0, "error": {"type": "transcription_error",
                                                              "code": "audio_unintelligible", "message": "unclear"}})
    assert selected(events, Caption)[-1] == Caption("owner", "", True, "noise")
    assert not selected(events, EngineError) and not socket.closed
    await finished(engine, collector)

@run_async
async def test_native_vad_cancel_racing_host_interrupt_keeps_session_open():
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    await feed(engine, socket, {"type": "error", "error": {"type": "invalid_request_error",
        "code": "response_cancel_not_active", "message": "Cancellation failed: no active response found", "param": None}})
    assert not socket.closed and not selected(events, EngineError)
    await engine.send(ContextItem("Continue the call", "owner_mailbox"))
    assert socket.sent[-1]["item"]["content"][0]["text"] == "[owner_mailbox] Continue the call"
    await finished(engine, collector)


@pytest.mark.parametrize("name", ["text_tool_exchange.json", "audio_tool_exchange.json", "adapter_audio_exchange.json"])
@run_async
async def test_recorded_openai_exchange(name):
    fixture = json.loads((WIRE / "openai" / name).read_text())
    assert fixture["header"]["provenance"] == "recorded_live"
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    expected_audio = bytearray()
    for row in fixture["events"]:
        message = row["message"]
        if row["direction"] == "server":
            await feed(engine, socket, message)
            if message["type"] == "response.output_audio.delta":
                expected_audio.extend(base64.b64decode(message["delta"]))
        elif message["type"] == "conversation.item.create":
            item = message["item"]
            if item["type"] == "function_call_output":
                await engine.send(ToolResult(item["call_id"], item["output"]))
                assert message in socket.sent
            else:
                await engine.send(ContextItem(item["content"][0]["text"], "fixture"))
        elif message["type"] == "input_audio_buffer.append":
            await engine.send(AudioIn(base64.b64decode(message["audio"]), 24000))
            assert socket.sent[-1] == message
    await finished(engine, collector)
    done = selected(events, ResponseDone)
    raw_done = [row["message"]["response"] for row in fixture["events"] if row["message"].get("type") == "response.done"]
    assert len(done) == len(raw_done) == 2
    assert [e.response_id for e in done] == [r["id"] for r in raw_done]
    assert [e.raw_usage for e in done] == [r["usage"] for r in raw_done]
    assert all(set(e.usage) == set(USAGE_KEYS) for e in done)
    if name == "text_tool_exchange.json":
        assert done[1].usage["cached_input_tokens"] > 0
    else:
        assert expected_audio and done[1].usage["audio_output_tokens"] > 0
    calls = selected(events, ToolCall)
    assert len(calls) == 1  # arguments.done and response.done carry the same call.
    assert json.loads(calls[0].arguments) == {"query": "42" if name == "adapter_audio_exchange.json" else "fixture"}
    assert calls[0].name == "lookup"
    assert len(selected(events, ResponseStarted)) == 2
    assert b"".join(e.pcm for e in selected(events, AudioOut)) == bytes(expected_audio)
    assert any(e.final for e in selected(events, Caption))
    if name == "adapter_audio_exchange.json":
        transcript = selected(events, TranscriptionUsage)
        assert len(transcript) == 1 and transcript[0].model == "gpt-4o-transcribe"
        assert transcript[0].usage["audio_input_tokens"] == 11
        assert transcript[0].usage["text_output_tokens"] == 6
    assert not selected(events, EngineError)


@run_async
async def test_documented_gemini_exchange_both_directions():
    fixture = json.loads((WIRE / "gemini" / "documented_exchange.json").read_text())
    assert fixture["header"]["provenance"] == "synthetic_from_docs"
    engine, connection, events, collector = await opened("google")
    socket = connection.sockets[0]
    for row in fixture["events"][1:]:
        message = row["message"]
        if row["direction"] == "server":
            await feed(engine, socket, message)
        else:
            if "realtimeInput" in message:
                await engine.send(AudioIn(PCM, 16000))
            elif "toolResponse" in message:
                await engine.send(ToolResult("synthetic-call-1", "42"))
            else:
                await engine.send(ContextItem("A typed message.", "room"))
            assert socket.sent[-1] == message
    await finished(engine, collector)
    assert [e.pcm for e in selected(events, AudioOut)] == [PCM, b"\x02\x00\x03\x00"]
    assert len(selected(events, ResponseStarted)) == len(selected(events, ResponseDone)) == 1
    done = selected(events, ResponseDone)[0]
    assert done.item_id.startswith("local:gemini:") and done.response_id == ""
    assert done.usage == dict(zip(USAGE_KEYS, (10, 6, 3, 5, 2, 6)))
    assert done.raw_usage["cacheTokensDetails"][1]["tokenCount"] == 2
    assert selected(events, ToolCallCancelled) == [ToolCallCancelled(("synthetic-call-2",))]
    assert selected(events, GoAway) == [GoAway(12.5)]
    assert [e.text for e in selected(events, Caption) if e.final] == ["Look up fixture.", "I will check."]
    assert not selected(events, EngineError)


@pytest.mark.parametrize("provider,rate", [("openai", 24000), ("google", 16000)])
@run_async
async def test_session_configuration_audio_limits_and_history(provider, rate):
    config = SessionConfig(EngineRoute(provider, "custom-live", voice="chosen-voice"), "Full core", (TOOL,),
                           ({"role": "assistant", "content": "Earlier reply"}, {"role": "tool", "content": "Retained result"}), True, "")
    engine, connection, events, collector = await opened(provider, config=config)
    socket = connection.sockets[0]
    setup = socket.sent[0]
    if provider == "openai":
        session = setup["session"]
        assert session["type"] == "realtime" and session["instructions"] == "Full core"
        assert session["truncation"] == {"type": "retention_ratio", "retention_ratio": .8}
        assert session["audio"]["input"]["turn_detection"] == {"type": "semantic_vad", "create_response": True, "interrupt_response": True}
        assert session["audio"]["output"]["voice"] == "chosen-voice"
        assert all(row["item"]["type"] == "message" for row in socket.sent[1:])
    else:
        setup = setup["setup"]
        assert setup["systemInstruction"]["parts"] == [{"text": "Full core"}]
        assert setup["tools"][0]["functionDeclarations"][0]["behavior"] == "NON_BLOCKING"
        assert setup["tools"][1] == {"googleSearch": {}}
        assert setup["sessionResumption"] == {}
        assert setup["contextWindowCompression"] == {"slidingWindow": {}}
        assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
        assert socket.sent[1]["clientContent"]["turnComplete"] is False
    assert selected(events, Ready)[0].effective["input_rate"] == rate
    await engine.send(AudioIn(PCM, rate))
    before = len(socket.sent)
    for command in (AudioIn(PCM, 48000), AudioIn(b"x", rate), AudioIn(bytes(65538), rate)):
        with pytest.raises(LiveEngineRefusal):
            await engine.send(command)
    await engine.send(PlayedAudio("played-item", 100))
    assert len(socket.sent) == before
    assert connection.options[0][1]["logger"].disabled
    await finished(engine, collector)


@run_async
async def test_openai_tools_apply_only_on_matching_ack_and_results_coalesce():
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    await engine.send(UpdateTools((TOOL, NEW_TOOL)))
    assert not selected(events, ToolsApplied)
    await feed(engine, socket, {"type": "session.updated", "session": {"tools": []}})
    assert not selected(events, ToolsApplied)
    await feed(engine, socket, {"type": "session.updated", "session": socket.sent[-1]["session"]})
    assert selected(events, ToolsApplied) == [ToolsApplied(("lookup", "read_file"))]
    await feed(engine, socket, {"type": "response.created", "response": {"id": "response-1"}})
    await engine.send(ToolResult("call-1", "first"))
    await engine.send(ToolResult("call-2", "second"))
    await asyncio.sleep(0)
    assert not any(row["type"] == "response.create" for row in socket.sent)
    await feed(engine, socket, {"type": "response.done", "response": {"id": "response-1", "status": "completed", "usage": {}}})
    await eventually(lambda: any(row["type"] == "response.create" for row in socket.sent))
    await engine.send(ToolResult("call-3", "third"))
    await asyncio.sleep(0)
    assert sum(row["type"] == "response.create" for row in socket.sent) == 1
    await feed(engine, socket, {"type": "response.created", "response": {"id": "response-2"}})
    await feed(engine, socket, {"type": "response.done", "response": {"id": "response-2", "status": "completed", "usage": {}}})
    await eventually(lambda: sum(row["type"] == "response.create" for row in socket.sent) == 2)
    await finished(engine, collector)


@run_async
async def test_openai_interrupt_uses_item_not_response_id_and_preserves_usage():
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    await feed(engine, socket, {"type": "response.created", "response": {"id": "response"}})
    await feed(engine, socket, {"type": "response.output_audio.delta", "item_id": "audio-item", "content_index": 2,
                                "delta": base64.b64encode(PCM).decode()})
    await engine.send(Interrupt("audio-item", 123))
    assert socket.sent[-2:] == [{"type": "response.cancel"}, {"type": "conversation.item.truncate", "item_id": "audio-item", "content_index": 2, "audio_end_ms": 123}]
    with pytest.raises(LiveEngineRefusal):
        await engine.send(Interrupt("audio-item", -1))
    await feed(engine, socket, {"type": "input_audio_buffer.speech_started", "item_id": "owner-item", "audio_start_ms": 1250})
    await feed(engine, socket, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "owner-item",
                                "transcript": "Hello", "usage": {"type": "tokens", "input_tokens": 4, "input_token_details": {"audio_tokens": 4}, "output_tokens": 2}})
    assert selected(events, SpeechStarted) == [SpeechStarted("owner-item", 1250)]  # its place in the input audio
    assert selected(events, TranscriptionUsage)[0].usage["audio_input_tokens"] == 4
    assert selected(events, TranscriptionUsage)[0].usage["text_output_tokens"] == 2
    assert selected(events, Caption)[-1] == Caption("owner", "Hello", True, "owner-item")
    await finished(engine, collector)


@run_async
async def test_gemini_tool_activation_waits_for_result_then_fresh_checkpoint():
    engine, connection, events, collector = await opened("google")
    socket = connection.sockets[0]
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "old", "resumable": True}})
    await feed(engine, socket, {"toolCall": {"functionCalls": [{"id": "get-tools", "name": "lookup", "args": {}}]}})
    await engine.send(UpdateTools((TOOL, NEW_TOOL)))
    assert len(connection.sockets) == 1 and not selected(events, ToolsApplied)
    await engine.send(ToolResult("get-tools", "Activation pending", scheduling="silent"))
    assert socket.sent[-1]["toolResponse"]["functionResponses"][0]["scheduling"] == "SILENT"
    await feed(engine, socket, {"serverContent": {"turnComplete": True}})
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "", "resumable": False}})
    assert len(connection.sockets) == 1
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "fresh", "resumable": True}})
    assert socket.closed and len(connection.sockets) == 2
    resumed = connection.sockets[1]
    assert resumed.sent[0]["setup"]["sessionResumption"] == {"handle": "fresh"}
    assert len(resumed.sent[0]["setup"]["tools"][0]["functionDeclarations"]) == 2
    audio = asyncio.create_task(engine.send(AudioIn(PCM, 16000)))
    await asyncio.sleep(0)
    assert not audio.done() and not selected(events, ToolsApplied)
    await feed(engine, resumed, {"setupComplete": {}})
    await audio
    assert selected(events, ToolsApplied) == [ToolsApplied(("lookup", "read_file"))]
    assert len(selected(events, Ready)) == 1
    await finished(engine, collector)


@run_async
async def test_gemini_context_waits_for_idle_interrupt_is_local_and_cancellation_refuses_results():
    engine, connection, events, collector = await opened("google")
    socket = connection.sockets[0]
    content = {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": "AAABAA=="}}]}}}
    await feed(engine, socket, content)
    before = len(socket.sent)
    await engine.send(ContextItem("Text from the room", "owner typed"))
    await engine.send(Interrupt(selected(events, AudioOut)[0].item_id, 10))
    assert len(socket.sent) == before
    await feed(engine, socket, content)
    assert len(selected(events, AudioOut)) == 1
    await feed(engine, socket, {"serverContent": {"interrupted": True}})
    await feed(engine, socket, {"serverContent": {"turnComplete": True}, "usageMetadata": {"responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 7}]}})
    assert selected(events, ResponseDone)[0].status == "interrupted"
    assert selected(events, ResponseDone)[0].usage["audio_output_tokens"] == 7
    assert socket.sent[-1]["clientContent"]["turnComplete"] is False
    await feed(engine, socket, {"toolCall": {"functionCalls": [{"id": "cancel-me", "name": "lookup"}]}})
    await feed(engine, socket, {"toolCallCancellation": {"ids": ["cancel-me"]}})
    with pytest.raises(LiveEngineRefusal, match="cancelled"):
        await engine.send(ToolResult("cancel-me", "late result"))
    await finished(engine, collector)


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_transport_loss_is_retryable_and_close_is_final(provider):
    engine, connection, events, collector = await opened(provider)
    await feed(engine, connection.sockets[0], ConnectionError("secret URL and audio must not be echoed"))
    await asyncio.wait_for(collector, 2)
    assert selected(events, EngineError)[0].kind == "transport"
    assert selected(events, EngineError)[0].retryable
    assert "secret" not in selected(events, EngineError)[0].message
    assert isinstance(events[-1], Closed)
    with pytest.raises(LiveEngineRefusal):
        await engine.send(AudioIn(PCM, 24000))
    await finished(engine, collector)


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_full_event_queue_fails_explicitly_and_stop_never_waits_for_consumer(provider):
    engine, connection, _, _ = await opened(provider, collect=False)
    socket = connection.sockets[0]
    message = {"type": "input_audio_buffer.speech_started"} if provider == "openai" else {"sessionResumptionUpdate": {"resumable": False}}
    for _ in range(engine._events.maxsize):
        socket.incoming.put_nowait(message)
    await eventually(lambda: engine._closed_reason is not None)
    await asyncio.wait_for(engine.close("stop"), 1)
    events = [event async for event in engine.events()]
    assert events[-2].kind == "backpressure"
    assert isinstance(events[-1], Closed)
    assert socket.closed


@pytest.mark.parametrize("status,retryable,kind", [(401, False, "authentication"), (403, False, "authentication"),
    (400, False, "invalid_request"), (429, True, "rate_limit"), (503, True, "transport")])
def test_handshake_error_classification(status, retryable, kind):
    error = RuntimeError("credential-bearing URL")
    error.response = SimpleNamespace(status_code=status)
    event = transport_error(error)
    assert (event.kind, event.retryable) == (kind, retryable)
    assert "credential" not in event.message


def test_usage_normalization_never_maps_undifferentiated_totals_to_text():
    assert openai.normalize_usage({"input_tokens": 300, "output_tokens": 20}) == dict.fromkeys(USAGE_KEYS, 0)
    assert gemini.normalize_usage({"promptTokenCount": 300, "responseTokenCount": 20}) == dict.fromkeys(USAGE_KEYS, 0)
    raw = {"input_token_details": {"text_tokens": 10, "audio_tokens": 20, "image_tokens": 3, "cached_tokens": 7,
           "cached_tokens_details": {"text_tokens": 3, "audio_tokens": 4}}, "output_token_details": {"text_tokens": 4, "audio_tokens": 6}}
    assert openai.normalize_usage(raw) == dict(zip(USAGE_KEYS, (10, 20, 3, 7, 4, 6)))


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_llm_entry_point_resolves_captured_credentials_and_optional_open(provider, monkeypatch):
    from ouroboros import llm_live
    from ouroboros.gateways import realtime_transport
    from ouroboros.llm import LLMClient

    connection = Connection()
    monkeypatch.setattr(realtime_transport, "connect", connection)
    monkeypatch.setattr(llm_live, "runtime_setting", lambda key, default="": "google-key" if key == "GOOGLE_API_KEY" else default)
    client = LLMClient()
    targets = []
    def resolve(model):
        targets.append(model)
        return {"api_key": "openai-key"}
    monkeypatch.setattr(client, "resolve_remote_target", resolve)
    route = EngineRoute(provider, "custom-model", voice="chosen-voice")
    engine = client.open_live(route)
    assert not connection.sockets
    assert engine.route.input_rate == (16000 if provider == "google" else 24000)
    await engine.close("unused")
    engine = await client.open_live(route, SessionConfig(route, "Instructions"))
    assert len(connection.sockets) == 1
    if provider == "openai":
        assert targets == ["openai::custom-model", "openai::custom-model"]
        assert connection.options[0][1]["additional_headers"]["Authorization"] == "Bearer openai-key"
    else:
        url, options = connection.options[0]
        # The key is a request header, as in Google's own Live clients, never part of the URL.
        assert options["additional_headers"] == {"x-goog-api-key": "google-key"} and not targets
        assert "google-key" not in url and "?" not in url
    await engine.close("stop")
    monkeypatch.setattr(llm_live, "runtime_setting", lambda *args: "")
    monkeypatch.setattr(client, "resolve_remote_target", lambda *args: {"api_key": ""})
    with pytest.raises(LiveEngineRefusal) as missing:
        client.open_live(route)
    assert missing.value.code == "missing_credential"


@run_async
async def test_gemini_old_checkpoint_never_discards_new_server_or_client_content():
    engine, connection, events, collector = await opened("google")
    socket = connection.sockets[0]
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "old", "resumable": True}})
    await feed(engine, socket, {"serverContent": {"modelTurn": {"parts": [{"text": "New response"}]}}})
    await feed(engine, socket, {"serverContent": {"turnComplete": True}})
    await engine.send(UpdateTools((NEW_TOOL,)))
    assert len(connection.sockets) == 1
    await engine.send(ContextItem("Fresh room context", "room"))
    assert not engine._checkpoint
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "current", "resumable": True}})
    assert connection.sockets[1].sent[0]["setup"]["sessionResumption"] == {"handle": "current"}
    await finished(engine, collector)


@run_async
async def test_gemini_resume_skips_history_and_search_and_voice_are_opt_in():
    config = SessionConfig(EngineRoute("google", "models/custom"), "instructions", history=({"role": "tool", "content": "old effect"},), resume_handle="resume-me")
    engine, connection, events, collector = await opened("google", config=config)
    setup = connection.sockets[0].sent[0]["setup"]
    assert setup["model"] == "models/custom"
    assert setup["sessionResumption"] == {"handle": "resume-me"}
    assert "speechConfig" not in setup["generationConfig"] and not setup["tools"]
    assert len(connection.sockets[0].sent) == 1
    await finished(engine, collector)
    assert "voice" not in openai.session_update(replace(config, route=EngineRoute("openai", "custom")))["session"]["audio"]["output"]


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_tool_images_preserve_bytes_and_invalid_images_send_nothing(provider):
    engine, connection, events, collector = await opened(provider)
    socket = connection.sockets[0]
    if provider == "google":
        await feed(engine, socket, {"toolCall": {"functionCalls": [{"id": "image-call", "name": "lookup"}]}})
    image = b"\x89PNG\r\n\x1a\nfixture"
    before = len(socket.sent)
    with pytest.raises(LiveEngineRefusal) as refused:
        await engine.send(ToolResult("image-call", "image", images=(b"invalid",)))
    assert refused.value.code == "image_format" and len(socket.sent) == before
    await engine.send(ToolResult("image-call", "image", images=(image,)))
    if provider == "google":
        encoded = socket.sent[-1]["toolResponse"]["functionResponses"][0]["parts"][0]["inlineData"]["data"]
    else:
        encoded = socket.sent[-1]["item"]["content"][1]["image_url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == image
    await finished(engine, collector)


@run_async
async def test_oversized_result_images_refuse_before_partial_tool_output(monkeypatch):
    from ouroboros.gateways import realtime_transport

    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    before = len(socket.sent)
    monkeypatch.setattr(realtime_transport, "MAX_WIRE_BYTES", 300)
    with pytest.raises(LiveEngineRefusal) as refused:
        await engine.send(ToolResult("image-call", "result", images=(b"\x89PNG\r\n\x1a\n" + bytes(300),)))
    assert refused.value.code == "wire_size" and len(socket.sent) == before
    await engine.send(ToolResult("image-call", "small result"))
    assert socket.sent[-1]["item"]["output"] == "small result"
    await finished(engine, collector)


@run_async
async def test_openai_completed_calls_only_and_repeated_done_settles_once():
    engine, connection, events, collector = await opened("openai")
    socket = connection.sockets[0]
    await feed(engine, socket, {"type": "response.function_call_arguments.delta", "call_id": "partial", "delta": "{"})
    assert not selected(events, ToolCall)
    response = {"type": "response.done", "response": {"id": "response", "status": "cancelled", "output": [
        {"type": "function_call", "status": "incomplete", "id": "item1", "call_id": "partial", "name": "lookup", "arguments": "{"},
        {"type": "function_call", "status": "completed", "id": "item2", "call_id": "complete", "name": "lookup", "arguments": "{}"}]}}
    await feed(engine, socket, response)
    await feed(engine, socket, response)
    assert [e.call_id for e in selected(events, ToolCall)] == ["complete"]
    assert len(selected(events, ResponseDone)) == 1
    # Malformed arguments reach the host as the model produced them: the ordinary tool
    # path answers with its typed argument error and the session stays open.
    await feed(engine, socket, {"type": "response.function_call_arguments.done", "call_id": "bad", "name": "lookup", "arguments": "{"})
    assert [(e.call_id, e.arguments) for e in selected(events, ToolCall)] == [("complete", "{}"), ("bad", "{")]
    assert not selected(events, EngineError)
    await finished(engine, collector)


@pytest.mark.parametrize("wire_expiry", [False, True])
@run_async
async def test_openai_provider_session_limit_requests_host_rollover(wire_expiry, monkeypatch):
    if not wire_expiry:
        monkeypatch.setattr(openai, "SESSION_LIMIT_SECONDS", 0)
    engine, connection, events, collector = await opened("openai")
    if wire_expiry:
        await feed(engine, connection.sockets[0], {"type": "error", "error": {"code": "session_expired"}})
    await asyncio.wait_for(collector, 2)
    assert events[-1] == Closed("session_limit")
    assert not selected(events, EngineError)


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_rejected_setup_wakes_waiting_sends_and_never_echoes_provider_body(provider):
    connection = Connection()
    cls = openai.OpenAIRealtimeEngine if provider == "openai" else gemini.GeminiLiveEngine
    engine = cls(api_key="fixture-key", connector=connection)
    await engine.open(SessionConfig(EngineRoute(provider, "model"), "instructions"))
    pending = asyncio.create_task(engine.send(ContextItem("context", "room")))
    error = {"type": "error", "error": {"code": "invalid_api_key", "message": "fixture-key"}} if provider == "openai" else {"error": {"code": 403, "message": "fixture-key"}}
    await feed(engine, connection.sockets[0], error)
    with pytest.raises(LiveEngineRefusal):
        await asyncio.wait_for(pending, 1)
    events = [event async for event in engine.events()]
    assert events[-2].kind == "authentication" and not events[-2].retryable
    assert "fixture-key" not in repr(events)
    assert isinstance(events[-1], Closed)


@pytest.mark.parametrize("provider", ["openai", "google"])
@run_async
async def test_stop_cleans_up_before_ready_or_during_pending_activation(provider):
    connection = Connection()
    cls = openai.OpenAIRealtimeEngine if provider == "openai" else gemini.GeminiLiveEngine
    engine = cls(api_key="fixture-key", connector=connection)
    await engine.open(SessionConfig(EngineRoute(provider, "model"), "instructions"))
    pending = asyncio.create_task(engine.send(AudioIn(PCM, 16000 if provider == "google" else 24000)))
    await engine.send(Close("stop"))
    with pytest.raises(LiveEngineRefusal):
        await pending
    assert [event async for event in engine.events()] == [Closed("stop")]
    assert connection.sockets[0].closed
    assert not engine._tasks


@run_async
async def test_gemini_cancelled_tools_allow_next_checkpoint_activation_and_usage_resets():
    engine, connection, events, collector = await opened("google")
    socket = connection.sockets[0]
    for index in range(2):
        await feed(engine, socket, {"serverContent": {"modelTurn": {"parts": [{"text": str(index)}]}},
                                    "usageMetadata": {"responseTokensDetails": [{"modality": "TEXT", "tokenCount": index + 1}]}})
        await feed(engine, socket, {"serverContent": {"turnComplete": True}})
    assert [e.usage["text_output_tokens"] for e in selected(events, ResponseDone)] == [1, 2]
    assert len({e.item_id for e in selected(events, ResponseDone)}) == 2
    await feed(engine, socket, {"toolCall": {"functionCalls": [{"id": "pending", "name": "lookup"}]}})
    await engine.send(UpdateTools((NEW_TOOL,)))
    await feed(engine, socket, {"toolCallCancellation": {"ids": ["pending"]}})
    await feed(engine, socket, {"serverContent": {"turnComplete": True}})
    await feed(engine, socket, {"sessionResumptionUpdate": {"newHandle": "after-cancel", "resumable": True}})
    assert len(connection.sockets) == 2
    await finished(engine, collector)


@pytest.mark.parametrize("configured", [False, True])
def test_google_credential_uses_its_own_settings(configured, monkeypatch):
    from ouroboros import provider_models as providers

    values = {"OPENROUTER_API_KEY": "router-test-key", "GOOGLE_API_KEY": "google-test-key" if configured else ""}
    monkeypatch.setattr(providers, "runtime_setting", lambda key, default="": values.get(key, default))
    assert providers.provider_has_credentials("google") is configured
    assert providers.provider_has_credentials_in_settings("google", values) is configured
    assert providers.PROVIDER_CREDENTIAL_GROUPS["google"] == ("GOOGLE_API_KEY",)


def test_google_secret_has_every_core_boundary():
    from ouroboros.colab_bootstrap import _SECRET_KEYS
    from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
    from ouroboros.provider_models import MODEL_PROVIDER_CREDENTIAL_KEYS
    from ouroboros.safety import _PROVIDER_KEY_ENV, _REMOTE_PROVIDER_KEYS
    from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS, looks_masked_settings_secret
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS

    for keys in (_SECRET_KEYS, FORBIDDEN_SKILL_SETTINGS, MODEL_PROVIDER_CREDENTIAL_KEYS,
                 _REMOTE_PROVIDER_KEYS, MASKED_SECRET_SETTING_KEYS, SETTINGS_DEFAULTS):
        assert "GOOGLE_API_KEY" in keys
    assert _PROVIDER_KEY_ENV["google"] == "GOOGLE_API_KEY"
    assert looks_masked_settings_secret("GOOGLE_API_KEY", "***set***")
    assert not looks_masked_settings_secret("GOOGLE_API_KEY", "actual-test-value")


def test_colab_collects_google_without_treating_live_as_text_access(monkeypatch):
    from ouroboros import colab_bootstrap as colab

    requested = []

    def secret(name, *, required=True):
        requested.append((name, required))
        return "google-test-key" if name == "GOOGLE_API_KEY" else ""

    monkeypatch.setattr(colab, "get_colab_secret", secret)
    assert colab.collect_colab_secrets()["GOOGLE_API_KEY"] == "google-test-key"
    assert ("GOOGLE_API_KEY", False) in requested
    assert ("OPENROUTER_API_KEY", True) in requested
