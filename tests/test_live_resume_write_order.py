"""Independent R30 check: handle ordering inside a multi-write ToolOutputs."""
import asyncio
import json
import threading

import pytest

from ouroboros import call_protocol as cp
from ouroboros.contracts.live_conversation import Audio
from tests._live_stack import stack
from tests.test_live_conversation import host_world as host_world


@pytest.mark.parametrize('answer', [False, True])
@pytest.mark.parametrize('thinking', [False, True])
@pytest.mark.parametrize('handle_timing', ['before-result', 'queued-result', 'after-result'])
def test_handle_during_requested_pixels_cannot_cover_later_result(
        host_world, monkeypatch, record_property, thinking, handle_timing, answer):
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        async with stack(host_world, monkeypatch, 'gemini', thinking=thinking) as live:
            host, call, old = live.host, live.call, live.wire
            old.put({'sessionResumptionUpdate': {'resumable': True, 'newHandle': 'before-look'}})
            await live.wait(lambda: host.state.get('resume_handle') == 'before-look')
            retain = host.retain_frame

            async def delayed(data, mime, *, source=''):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
                return await retain(data, mime, source=source)

            monkeypatch.setattr(host, 'retain_frame', delayed)
            native = call.session
            write = native.ws.send
            order, queued = [], []
            revisions = {}

            async def timed_write(raw):
                # Image and function response are separate native writes. Hold the
                # image write's completion until an actual incoming handle event
                # has been processed. This models the writer yielding under load.
                payload = json.loads(raw)
                if 'video' in payload.get('realtimeInput', {}):
                    revisions['before_pixels'] = call.resume_revision
                await write(raw)
                is_image = 'video' in payload.get('realtimeInput', {})
                is_result = 'toolResponse' in payload
                if is_image:
                    order.append('pixels-written')
                    if handle_timing == 'queued-result':
                        # Audio takes the shared writer lock before the function
                        # response, then yields while its handle arrives.
                        queued.append(asyncio.create_task(native.send(Audio(b'\0\0' * 320, 16000))))
                        await asyncio.sleep(0)
                if is_result:
                    order.append('function-response-written')
                if ((is_image and handle_timing == 'before-result')
                        or (is_result and handle_timing == 'after-result')
                        or ('audio' in payload.get('realtimeInput', {}) and queued
                            and 'handle-received' not in order)):
                    handle = handle_timing
                    old.put({'sessionResumptionUpdate': {'resumable': True, 'newHandle': handle}})
                    while host.state.get('resume_handle') != handle:
                        await asyncio.sleep(.01)
                    revisions['new_handle'] = call.resume_revision
                    order.append('handle-received')

            monkeypatch.setattr(native.ws, 'send', timed_write)
            call.attention.frame(cp.PeerVideo('screen', cp.PEER_OWNER, 'image/jpeg', b'exact requested frame'))
            complete = old.complete
            if not answer:
                old.complete = lambda: None  # tool turn stays open until its result
                old.answer = complete  # complete that tool turn, without an answer
            old.tool('ordered-look', 'attend', {'see': 'asked', 'look': True, 'revisit_after_s': None})
            old.complete = complete
            try:
                await live.wait(entered.is_set)
                old.put({'goAway': {'timeLeft': '30s'}})
                await live.wait(lambda: call.renewing and call.resume)
                release.set()
                await live.wait(lambda: 'ordered-look' in old.outputs)
                await live.wait(lambda: len(live.provider.connections) == 2 and live.wire.sent)
                setup = live.wire.sent[0]['setup']['sessionResumption']
                observed = dict(thinking=thinking, handle_timing=handle_timing, order=order,
                                successor=setup, revisions=revisions,
                                old_output='ordered-look' in old.outputs,
                                successor_output='ordered-look' in live.wire.outputs,
                                effects=sum(r.get('call_id') == 'ordered-look' for r in host.trace['tool_calls']))
                record_property('write_order', json.dumps(observed))
                if handle_timing != 'after-result':
                    assert order == ['pixels-written', 'handle-received', 'function-response-written']
                    assert setup == {}, 'handle predating function response must use a fresh fit'
                else:
                    assert order == ['pixels-written', 'function-response-written', 'handle-received']
                    assert setup == {'handle': 'after-result'}
                    assert not any('The requested tool batch returned' in json.dumps(r)
                                   for r in live.wire.sent if 'clientContent' in r)
                await live.wait(lambda: not call.pending_offers)
                await live.quiet()
                assert observed['effects'] == 1 and not live.wire.outputs
                pointer = any('The requested tool batch returned' in json.dumps(r)
                              for r in live.wire.sent if 'clientContent' in r)
                assert pointer is (handle_timing != 'after-result')
                assert len(live.wire.spoken) == int(handle_timing != 'after-result')
                assert sum('toolResponse' in row for row in old.sent) == 1
            finally:
                release.set()
                await live.wait(lambda: all(task.done() for task in queued))
                for task in queued:
                    task.result()
        assert live.result[1]['execution_status'] == 'ok', live.result

    asyncio.run(scenario())


@pytest.mark.parametrize('handle_after', [False, True])
def test_legacy_session_keeps_send_boundary_order(handle_after):
    from ouroboros.contracts.live_conversation import ToolOutputs
    from skills.live_conversation.driver import _Call
    from tests.test_live_conversation_driver import Host, Session

    async def scenario():
        host = Host()
        call = _Call(host)
        call.session = Session()  # no additive native-write fact
        call.connected.set()
        host.state['resume_handle'] = 'same-value'
        call.resume_revision = 1
        await call.send(ToolOutputs(({'role': 'tool', 'tool_call_id': 'c', 'content': 'Done'},), ()))
        if handle_after:
            call.resume_revision += 1
        call.request_renewal(resuming=True)
        await call.peers()
        assert call.resume is handle_after
        assert bool(call.pending_offers) is not handle_after
    asyncio.run(scenario())
