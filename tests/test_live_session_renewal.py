"""A long call's session renewals: the call so far returns as text, the newest stretch stays audio.

Pure parts (``live_session_view``, ``live_call_state``) run alone; the driver parts use the
harness of ``tests/test_live_turn.py`` (fake hub, engine, LLM and ledger).
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from ouroboros import live_call_state
from ouroboros.call_protocol import STATE_RECONNECTING, PeerAudio
from ouroboros.gateways.realtime_engine import (
    SPEAKER_OUROBOROS, SPEAKER_OWNER, AudioIn, AudioOut, Caption, ResponseDone, ResponseStarted, ResumeCheckpoint,
)
from ouroboros.live_call_route import CALL_AUDIO_SPAN_TOKENS
from ouroboros.live_session_view import (
    SHORT_ROW_CHARS, _view_tokens, audio_reserve, history_rows, history_view, mark_spoken,
)
from tests import test_live_turn as base
from tests.test_live_turn import _wait_for

harness = base.harness  # the same fixture, registered in this module

SPAN_REACHED = {"text_input_tokens": 4_000, "audio_input_tokens": CALL_AUDIO_SPAN_TOKENS}


def _fact(room: int) -> SimpleNamespace:
    return SimpleNamespace(window_tokens=8_192 + room, reply_allowance_tokens=8_192)


def _utterances(count: int, words: int = 90):
    return [{"role": "user" if index % 2 else "assistant", "content": f"utterance {index} " + "word " * words}
            for index in range(count)]


# ---- what a fresh session replays -------------------------------------------------------------

def test_a_fresh_session_leaves_one_audio_span_for_its_own_speech_and_at_most_half_the_room():
    assert audio_reserve(_fact(100_000), "v", []) == CALL_AUDIO_SPAN_TOKENS
    small = _fact(10_000)
    assert audio_reserve(small, "v", []) == (10_000 - _view_tokens("v", [])) // 2  # a small room keeps half and half
    assert audio_reserve(None, "v", []) == 0


def test_a_long_call_replays_whole_where_half_the_room_would_have_cut_it():
    from ouroboros.live_session_view import _replayed_tokens

    messages = [{"role": "system", "content": "v"}, *_utterances(130)]  # about 17k tokens of speech
    room = 30_000
    assert room // 2 < sum(_replayed_tokens(row) for row in messages[1:]) < room - CALL_AUDIO_SPAN_TOKENS
    view = history_view(_fact(room), messages, [])
    assert len(view) == 130 and not str(view[0]["content"]).startswith("[Earlier in this call:")
    assert view[0]["content"].startswith("utterance 0 ")  # the call's first minute is still there
    longer = [{"role": "system", "content": "v"}, *_utterances(260)]  # past the room less one audio span
    kept = history_view(_fact(room), longer, [])
    assert kept[0]["content"].startswith("[Earlier in this call:") and kept[-1]["content"].startswith("utterance 259 ")


def test_tool_weight_is_shortened_before_any_speech_is_dropped():
    messages = [{"role": "system", "content": "v"},
                {"role": "user", "content": "find the contract"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "result " * 20_000},  # about 35k tokens
                *_utterances(20)]
    view = history_view(_fact(30_000), messages, [])
    assert not str(view[0]["content"]).startswith("[Earlier in this call:")  # nothing said was dropped
    tool = next(item for item in view if item["role"] == "tool")
    assert len(tool["content"]) < SHORT_ROW_CHARS + 200 and "shortened in this session" in tool["content"]
    assert [item["content"] for item in view if str(item["content"]).startswith("utterance")] == [
        row["content"] for row in _utterances(20)]  # every utterance whole
    whole = history_view(_fact(120_000), messages, [])
    assert next(item for item in whole if item["role"] == "tool")["content"] == "result " * 20_000  # room: kept whole


def test_an_answer_the_owner_cut_off_replays_as_cut_off():
    messages = [{"role": "system", "content": "v"},
                {"role": "assistant", "content": "Here are all twelve steps", "_item_id": "a1"},
                {"role": "assistant", "content": "And the summary", "_item_id": "a2"}]
    mark_spoken(messages, "a1", "interrupted")
    mark_spoken(messages, "a2", "played")
    rows = history_rows(messages)
    assert rows[0]["content"].startswith("[The owner interrupted this; only its start was heard] Here are")
    assert rows[1]["content"] == "And the summary"  # an answer heard to its end replays as said


# ---- when a renewal is due ------------------------------------------------------------------

def _renewal(**call_fields):
    lane = SimpleNamespace(running=False, pending=[], claimed={})
    fields = dict(engine=object(), closing=False, reconnecting=False, response_open=False, spoken={},
                  tools_lane=lane, messages=[{"role": "system", "content": "v"}])
    fields.update(call_fields)
    call = SimpleNamespace(**fields)
    renewal = live_call_state.SessionRenewal(call)
    renewal.reset((50_000, 12_000))
    return call, renewal


def test_a_renewal_is_due_for_the_audio_span_the_window_or_the_providers_goodbye():
    call, renewal = _renewal()
    call.messages.append({"role": "user", "content": "hello"})
    renewal.observe({"text_input_tokens": 30_000, "audio_input_tokens": 2_000})
    assert renewal.due() == ""
    renewal.observe({"text_input_tokens": 30_000, "audio_input_tokens": 12_000})
    assert renewal.due() == "audio_span"
    call, renewal = _renewal()
    call.messages.append({"role": "user", "content": "hello"})
    renewal.observe({"text_input_tokens": 40_000, "audio_input_tokens": 1_000})
    renewal.observe({"text_input_tokens": 44_000, "audio_input_tokens": 3_000})  # 6k grew in one turn
    assert renewal.due() == "window"  # the next turn would reach the provider's sliding window
    renewal.go_away(30)
    assert renewal.due() == "go_away"


def test_a_session_with_nothing_new_since_it_opened_is_not_renewed():
    call, renewal = _renewal()
    renewal.observe({"text_input_tokens": 49_000, "audio_input_tokens": 12_000})
    assert renewal.due() == ""  # it would replay the same text
    renewal.go_away(30)
    assert renewal.due() == "go_away"  # the provider's goodbye still ends this session


def test_a_renewal_waits_for_a_completed_turn():
    call, renewal = _renewal()
    assert renewal.at_turn_boundary()
    for field, busy in (("response_open", True), ("spoken", {"a1": object()}), ("reconnecting", True),
                        ("engine", None), ("closing", True)):
        call, renewal = _renewal(**{field: busy})
        assert not renewal.at_turn_boundary(), field
    call, renewal = _renewal()
    renewal.owner_caption("so the plan", final=False)
    assert not renewal.at_turn_boundary()  # the owner is mid-sentence
    renewal.owner_caption("so the plan is fine", final=True)
    assert renewal.at_turn_boundary()
    call.tools_lane.claimed = {"c1": 1}
    assert not renewal.at_turn_boundary()  # a tool call is still unanswered


def test_the_gap_backlog_keeps_the_first_words_within_its_bound():
    call, renewal = _renewal()
    for index in range(4):
        renewal.hold(f"frame-{index}", live_call_state.GAP_AUDIO_MS / 3)
    assert renewal.take_backlog() == ["frame-0", "frame-1", "frame-2"] and renewal.take_backlog() == []


# ---- the driver -------------------------------------------------------------------------------

def _answer(engine, item_id: str, usage=None) -> None:
    engine.push(ResponseStarted(item_id=item_id, response_id=f"r-{item_id}"))
    engine.push(ResponseDone(item_id=item_id, status="completed", usage=dict(usage or SPAN_REACHED)))


def test_a_session_is_renewed_at_a_completed_turn_once_its_audio_reaches_the_span(harness):
    h = harness()
    first = h.start()
    first.push(Caption(speaker=SPEAKER_OWNER, text="The number is 73", final=True, item_id="u1"))
    _answer(first, "a1")
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.llm.engines[1].config is not None)
    second = h.engine()
    assert second.config.resume_handle == "" and any("73" in str(row["content"]) for row in second.config.history)
    assert first.closed == ["handover"] and not any(state == STATE_RECONNECTING for state, _ in h.hub.states)
    h.stop()
    text, usage, _trace = h.finish()
    assert usage["live_call"]["reconnects"] == 1 and usage["live_call"]["ended"] == "owner_stop"


def test_no_renewal_while_ouroboros_is_still_heard_and_one_once_it_was(harness):
    h = harness()
    first = h.start()
    first.push(Caption(speaker=SPEAKER_OWNER, text="Tell me a story", final=True, item_id="u1"))
    first.push(ResponseStarted(item_id="a1", response_id="r1"))
    first.push(AudioOut(pcm=b"\x00\x00" * 2_400, rate=24_000, item_id="a1"))  # 100 ms of answer
    first.push(Caption(speaker=SPEAKER_OUROBOROS, text="Once upon a time", final=True, item_id="a1"))
    first.push(ResponseDone(item_id="a1", status="completed", usage=dict(SPAN_REACHED)))
    assert _wait_for(lambda: ("a1", b"\x00\x00" * 2_400) in h.hub.played)
    time.sleep(0.6)
    assert len(h.llm.engines) == 1  # the owner is still hearing the answer
    h.hub.control("played", item_id="a1", played_ms=100)
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.llm.engines[1].config is not None)
    h.stop()
    h.finish()


def test_no_renewal_while_the_owner_is_speaking(harness):
    h = harness()
    first = h.start()
    first.push(Caption(speaker=SPEAKER_OWNER, text="Hello", final=True, item_id="u1"))
    first.push(ResponseStarted(item_id="a1", response_id="r-a1"))
    first.push(Caption(speaker=SPEAKER_OWNER, text="and one more", final=False, item_id="u2"))  # speaks again
    first.push(ResponseDone(item_id="a1", status="completed", usage=dict(SPAN_REACHED)))
    time.sleep(0.6)
    assert len(h.llm.engines) == 1  # mid-sentence: no renewal
    first.push(Caption(speaker=SPEAKER_OWNER, text="and one more thing", final=True, item_id="u2"))
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.llm.engines[1].config is not None)
    h.stop()
    h.finish()


def test_the_owners_words_during_a_renewal_reach_the_next_session_in_order(harness):
    h = harness()
    gate = threading.Event()

    class Slow(base.FakeEngine):
        async def open(self, config):
            await asyncio.to_thread(gate.wait, 5)
            await super().open(config)

    def open_live(route):
        h.llm.routes.append(route)
        h.llm.engines.append(base.FakeEngine() if not h.llm.engines else Slow())
        return h.llm.engines[-1]

    h.llm.open_live = open_live
    first = h.start()
    first.push(Caption(speaker=SPEAKER_OWNER, text="Hello", final=True, item_id="u1"))
    _answer(first, "a1")
    assert _wait_for(lambda: len(h.llm.engines) == 2)  # the renewal is connecting
    frames = [bytes([index, 0]) * 320 for index in range(1, 4)]
    for pcm in frames:
        h.hub.feed(PeerAudio(peer_id="owner-1", role="owner", pcm=pcm))
    time.sleep(0.3)
    gate.set()
    second = h.llm.engines[1]
    assert _wait_for(lambda: len(second.sent_of(AudioIn)) == 3)
    assert [command.pcm for command in second.sent_of(AudioIn)] == frames
    h.stop()
    h.finish()


def test_a_fresh_session_forgets_its_predecessors_resume_handle(harness):
    h = harness()
    first = h.start()
    first.push(ResumeCheckpoint(handle="h-first", resumable=True))
    first.push(Caption(speaker=SPEAKER_OWNER, text="Hello", final=True, item_id="u1"))
    _answer(first, "a1")
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.llm.engines[1].config is not None)
    h.llm.engines[1].end_stream()  # lost before the fresh session sent a checkpoint of its own
    assert _wait_for(lambda: len(h.llm.engines) == 3 and h.llm.engines[2].config is not None)
    third = h.llm.engines[2]
    assert third.config.resume_handle == "" and third.config.history  # never the first session's handle
    h.stop()
    h.finish()


def test_an_unplanned_loss_still_resumes_with_the_handle(harness):
    h = harness()
    first = h.start()
    first.push(ResumeCheckpoint(handle="h-1", resumable=True))
    first.push(Caption(speaker=SPEAKER_OWNER, text="Hello", final=True, item_id="u1"))
    first.end_stream()
    assert _wait_for(lambda: len(h.llm.engines) == 2 and h.llm.engines[1].config is not None)
    assert h.llm.engines[1].config.resume_handle == "h-1"
    assert any(state == STATE_RECONNECTING for state, _ in h.hub.states)  # a loss is said, a renewal is not
    h.stop()
    h.finish()
