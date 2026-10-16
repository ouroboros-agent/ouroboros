import test from 'node:test';
import assert from 'node:assert/strict';
import { apiClient } from '../modules/api_client.js';
import { SETTINGS_INPUT_FIELDS, SECRET_KEYS, renderSettingsPage } from '../modules/settings_ui.js';
import { CallSession, callAvailability, mediaFailure, mountCall, mountRoomCall, reasonWords, statePresentation } from '../modules/call.js';
import { decodeFrame, encodeFrame, newerEpoch, pcm16, PlaybackBuffer, playedMilliseconds,
    MAX_FRAME_PAYLOAD, MAX_BUFFER_SECONDS } from '../modules/call_worklet.js';

test('PCM uses the frozen 16-byte little-endian header, signed mono samples and wrapping counters', () => {
    const wire = encodeFrame({ kind: 2, epoch: 0x1234, seq: 0xabcdef12, rate: 48000,
        stream: 0x5678, pcm: new Int16Array([-32768, -1, 0, 32767]) });
    assert.deepEqual([...new Uint8Array(wire)], [79, 67, 1, 2, 52, 18, 18, 239, 205, 171,
        128, 187, 0, 0, 120, 86, 0, 128, 255, 255, 0, 0, 255, 127]);
    assert.deepEqual(decodeFrame(wire), { kind: 2, epoch: 0x1234, seq: 0xabcdef12, rate: 48000,
        stream: 0x5678, pcm: new Int16Array([-32768, -1, 0, 32767]) });
    const wrapped = decodeFrame(encodeFrame({ epoch: 65536, seq: 2 ** 32, rate: 44100, pcm: new Int16Array() }));
    assert.equal(wrapped.epoch, 0); assert.equal(wrapped.seq, 0); assert.equal(wrapped.rate, 44100);
});

test('call settings use normal draft fields, preserve arbitrary routes and ship no duration ceiling', () => {
    const html = renderSettingsPage();
    const fields = SETTINGS_INPUT_FIELDS.filter(([, key]) => key.includes('CALL_'));
    assert.equal(fields.length, 8);
    for (const [id, key, fallback] of fields) {
        assert.equal(html.split(`id="${id}"`).length - 1, 1, key);
        if (key.endsWith('MAX_MINUTES')) {
            assert.equal(fallback, undefined);
            assert.match(html, new RegExp(`id="${id}"[^>]*min="1"[^>]*step="1"`));
        }
        if (key.includes('_MODEL_') || key.includes('_VOICE_'))
            assert.match(html, new RegExp(`<input id="${id}"[^>]*type="text"`));
    }
    assert.ok(SECRET_KEYS.some(([key]) => key === 'GOOGLE_API_KEY'));
    assert.equal(html.split('data-secret-setting="GOOGLE_API_KEY"').length - 1, 2);
    assert.match(html, /data-secret-setting="GOOGLE_API_KEY"[^>]*type="password"/);
});

test('framing rejects bad magic, version, kind, rate, lengths and oversized audio', () => {
    for (const [index, value] of [[0, 0], [1, 0], [2, 2], [3, 0]]) {
        const wire = encodeFrame({ rate: 1, pcm: new Int16Array([1]) });
        new DataView(wire).setUint8(index, value);
        assert.throws(() => decodeFrame(wire));
    }
    assert.throws(() => decodeFrame(new ArrayBuffer(15)));
    assert.throws(() => decodeFrame(new ArrayBuffer(17)));
    assert.throws(() => encodeFrame({ rate: 0, pcm: new Int16Array() }));
    assert.throws(() => encodeFrame({ kind: 7, rate: 1, pcm: new Int16Array() }));
    assert.throws(() => encodeFrame({ rate: 1, pcm: new Int16Array(MAX_FRAME_PAYLOAD / 2 + 1) }));
    assert.equal(decodeFrame(encodeFrame({ rate: 1, pcm: new Int16Array(MAX_FRAME_PAYLOAD / 2) })).pcm.byteLength,
        MAX_FRAME_PAYLOAD);
    const zero = encodeFrame({ rate: 1, pcm: new Int16Array() });
    new DataView(zero).setUint32(10, 0, true);
    assert.throws(() => decodeFrame(zero), /sample rate/);
});

test('float capture saturates correctly and played time never rounds up unheard samples', () => {
    assert.deepEqual([-2, -1, -0.5, 0, 0.5, 1, 2, NaN].map(pcm16), [-32768, -32768, -16384, 0, 16384, 32767, 32767, 0]);
    assert.equal(playedMilliseconds(47, 48000), 0);
    assert.equal(playedMilliseconds(480, 48000), 10);
    assert.equal(playedMilliseconds(441, 44100), 10);
    assert.equal(newerEpoch(0, 65535), true);
    assert.equal(newerEpoch(65535, 0), false);
    assert.equal(newerEpoch(7, 7), false);
});

test('jitter buffer counts rendered samples per item and flush preserves only heard progress', () => {
    const reports = [];
    const buffer = new PlaybackBuffer(1000, data => reports.push(data));
    const frame = (item_id, count, epoch = 0) => ({ item_id, epoch, rate: 1000, pcm: new Int16Array(count).fill(16384) });
    buffer.push(frame('one', 20));
    const output = new Float32Array(10);
    buffer.render(output);
    assert.deepEqual([...output], Array(10).fill(0));
    assert.deepEqual(reports, []);
    buffer.push(frame('one', 30));
    buffer.render(output);
    assert.deepEqual([...output], Array(10).fill(0.5));
    buffer.flush(1);
    assert.deepEqual(reports, [{ type: 'played', item_id: 'one', played_ms: 10 }]);
    buffer.render(output);
    assert.deepEqual([...output], Array(10).fill(0));
    assert.equal(buffer.push(frame('old', 100, 0)), false);
    buffer.push(frame('two', 40, 1));
    buffer.push(frame('three', 40, 1));
    buffer.render(new Float32Array(80));
    assert.deepEqual(reports.slice(1), [{ type: 'played', item_id: 'two', played_ms: 40 },
        { type: 'played', item_id: 'three', played_ms: 40 }]);
    buffer.flush(0);
    assert.equal(buffer.epoch, 1);
});

test('short final audio drains after bounded jitter wait, and physical overflow/rate mismatch are explicit', () => {
    const reports = [];
    const buffer = new PlaybackBuffer(1000, report => reports.push(report));
    const frame = { item_id: 'short', epoch: 0, rate: 1000, pcm: new Int16Array(5).fill(32767) };
    buffer.push(frame);
    for (let i = 0; i < 4; i++) buffer.render(new Float32Array(10));
    assert.deepEqual(reports, [{ type: 'played', item_id: 'short', played_ms: 5 }]);
    assert.throws(() => buffer.push({ ...frame, rate: 16000 }), /rate/);
    buffer.push({ ...frame, pcm: new Int16Array(1000 * MAX_BUFFER_SECONDS) });
    assert.throws(() => buffer.push(frame), /buffer is full/);
});

function fixture({ open } = {}) {
    const tracks = [{ enabled: true, stopped: false, stop() { this.stopped = true; } }];
    const media = { getTracks: () => tracks, getAudioTracks: () => tracks };
    const contexts = [], nodes = [], sockets = [], requests = [], views = [], captions = [];
    class AudioContext {
        constructor() { this.sampleRate = 48000; this.destination = {}; contexts.push(this); }
        get audioWorklet() { return { addModule: async () => {} }; }
        resume() { return Promise.resolve(); }
        close() { this.closed = true; return Promise.resolve(); }
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    }
    class AudioWorkletNode {
        constructor() {
            this.messages = [];
            this.port = { postMessage: data => this.messages.push(data), close() {} };
            nodes.push(this);
        }
        connect() {}
        disconnect() { this.disconnected = true; }
    }
    const env = { isSecureContext: true, navigator: { mediaDevices: { getUserMedia: async () => media } },
        AudioContext, AudioWorkletNode, WebSocket: class {}, crypto: { randomUUID: () => 'call-request' }, setTimeout, clearTimeout };
    const socket = path => {
        const sock = { path, readyState: 0, bufferedAmount: 0, sent: [],
            send(data) { this.sent.push(typeof data === 'string' ? JSON.parse(data) : decodeFrame(data)); },
            close() { this.readyState = 3; this.closed = true; this.onclose?.(); },
            connect() { this.readyState = 1; this.onopen(); } };
        sockets.push(sock); return sock;
    };
    const session = new CallSession({ chatId: 71, env, socket, open: async body => {
        requests.push(body); return open ? open(body) : { call_id: 'c', ws_path: '/ws/call/c' };
    }, changed: view => views.push(view), caption: msg => captions.push(msg) });
    return { session, env, tracks, contexts, nodes, sockets, requests, views, captions };
}

test('feature detection gives separate visible disabled reasons and excludes Telegram', () => {
    const { env } = fixture();
    assert.equal(callAvailability(env).supported, true);
    for (const [override, reason] of [[{ isSecureContext: false }, /HTTPS/], [{ navigator: {} }, /microphone/],
        [{ AudioWorkletNode: null }, /live audio/], [{ WebSocket: null }, /connections/]]) {
        const result = callAvailability({ ...env, ...override });
        assert.equal(result.supported, false); assert.match(result.reason, reason);
    }
    for (const extra of [{ Telegram: { WebApp: {} } }, { document: { documentElement: { dataset: { ouroborosHost: 'telegram' } } } },
        { parent: { Telegram: { WebApp: {} } } }]) assert.equal(callAvailability({ ...env, ...extra }).hidden, true);
    assert.match(mediaFailure({ name: 'NotAllowedError' }), /denied/);
    assert.match(mediaFailure({ name: 'NotFoundError' }), /No microphone/);
    assert.match(mediaFailure({ name: 'NotReadableError' }), /could not be opened/);
    assert.match(mediaFailure({ name: 'SecurityError' }), /blocked/);
});

test('room admission, hello, states, actual-rate mic frames, mute and teardown share one lifecycle', async () => {
    const f = fixture();
    try {
        await f.session.start({ engine: 'google', model: 'owner-model', voice: 'owner-voice' });
        assert.deepEqual(f.requests, [{ chat_id: 71, client_message_id: 'call-request', engine: 'google', model: 'owner-model', voice: 'owner-voice' }]);
        const socket = f.sockets[0]; socket.connect();
        assert.deepEqual(socket.sent[0], { type: 'hello', protocol: 1, role: 'owner', mic: true, speaker: true, rate: 48000, label: 'Owner' });
        assert.equal(f.session.state, 'connecting');
        f.session.receive(JSON.stringify({ type: 'state', state: 'live' }));
        assert.equal(f.views.at(-1).label, 'Listening');
        const audio = () => ({ type: 'capture', generation: f.session.captureGeneration, rate: 48000, pcm: new Int16Array([100]) });
        f.session.onAudio(audio());
        assert.equal(socket.sent.at(-1).rate, 48000); assert.equal(socket.sent.at(-1).kind, 1);
        const old = audio();
        f.session.setMuted(true);
        assert.equal(f.tracks[0].enabled, false);
        const sent = socket.sent.length; f.session.onAudio(audio());
        assert.equal(socket.sent.length, sent);
        f.session.setMuted(false); f.session.onAudio(old);
        assert.equal(socket.sent.at(-1).type, 'mute');
        f.session.receive(JSON.stringify({ type: 'state', state: 'reconnecting', reason: 'session_cap' }));
        assert.deepEqual([f.views.at(-1).reason, f.views.at(-1).code], ['Starting a fresh voice session.', 'session_cap']);
        f.session.receive(JSON.stringify({ type: 'state', state: 'live' }));
        assert.deepEqual([f.views.at(-1).reason, f.views.at(-1).code], ['', '']);
        f.session.receive(JSON.stringify({ type: 'state', state: 'closed', reason: 'max_minutes' }));
        assert.equal(f.session.state, 'closed');
        assert.deepEqual([f.views.at(-1).reason, f.views.at(-1).code], ['The call reached its time limit in Settings.', 'max_minutes']);
        assert.equal(f.tracks[0].stopped, true); assert.equal(f.contexts[0].closed, true);
        assert.equal(socket.closed, true); assert.equal(f.nodes[0].disconnected, true);
    } finally { f.session.stop(); }
});

test('item binding, final captions, playback acknowledgements and stale epochs never claim queued audio', async () => {
    const f = fixture();
    try {
        await f.session.start(); f.sockets[0].connect();
        const caption = (item_id, extra = {}) => f.session.receive(JSON.stringify({ type: 'caption', speaker: 'ouroboros', item_id, text: '<b>literal</b>', final: false, ...extra }));
        const audio = epoch => encodeFrame({ kind: 2, epoch, rate: 48000, pcm: new Int16Array([100]) });
        assert.throws(() => f.session.receive(audio(0)), /without its speech item/);
        caption('one'); f.session.receive(audio(0));
        assert.equal(f.nodes[0].messages.at(-1).item_id, 'one');
        assert.equal(f.sockets[0].sent.filter(m => m.type === 'played').length, 0);
        caption('two'); caption('one', { final: true }); f.session.receive(audio(0));
        assert.equal(f.nodes[0].messages.at(-1).item_id, 'two');
        f.session.onAudio({ type: 'played', item_id: 'one', played_ms: 17 });
        f.session.onAudio({ type: 'played', item_id: 'one', played_ms: 10 });
        assert.deepEqual(f.sockets[0].sent.at(-1), { type: 'played', item_id: 'one', played_ms: 17 });
        f.session.receive(JSON.stringify({ type: 'flush', epoch: 1 }));
        const count = f.nodes[0].messages.length;
        f.session.receive(audio(0));
        assert.equal(f.nodes[0].messages.length, count);
        caption('three', { epoch: 1 });
        f.session.receive(JSON.stringify({ type: 'flush', epoch: 0 }));
        f.session.receive(audio(1));
        assert.equal(f.nodes[0].messages.at(-1).item_id, 'three');
        assert.equal(f.captions[0].text, '<b>literal</b>');
    } finally { f.session.stop(); }
});

test('permission and transport failures release all resources; microphone ownership can be reacquired', async () => {
    const f = fixture(), other = fixture();
    try {
        await f.session.start();
        await other.session.start(); assert.match(other.session.reason, /Another room/);
        f.sockets[0].onerror(); assert.equal(f.session.state, 'failed');
        await other.session.start(); other.sockets[0].connect();
        other.session.receive(JSON.stringify({ type: 'state', state: 'live' }));
        other.sockets[0].bufferedAmount = 48000 * 2 + 1;
        other.session.onAudio({ type: 'capture', generation: other.session.captureGeneration, rate: 48000, pcm: new Int16Array([1]) });
        assert.match(other.session.reason, /too slow/); assert.equal(other.tracks[0].stopped, true);
        const denied = fixture();
        denied.env.navigator.mediaDevices.getUserMedia = async () => { throw { name: 'NotAllowedError' }; };
        await denied.session.start();
        assert.equal(denied.session.state, 'failed'); assert.equal(denied.contexts[0].closed, true);
    } finally { f.session.stop(); other.session.stop(); }
});

test('leaving while permission or admission is pending cleans up late success', async () => {
    let resolveMedia, resolveOpen;
    const f = fixture();
    f.env.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { resolveMedia = resolve; });
    const pending = f.session.start(); f.session.stop();
    resolveMedia({ getTracks: () => f.tracks }); await pending;
    assert.equal(f.tracks[0].stopped, true); assert.equal(f.requests.length, 0);
    const g = fixture({ open: () => new Promise(resolve => { resolveOpen = resolve; }) });
    const admission = g.session.start();
    while (!resolveOpen) await new Promise(resolve => setImmediate(resolve));
    g.session.stop(); resolveOpen({ call_id: 'late', ws_path: '/ws/call/late' }); await admission;
    g.sockets[0].connect();
    assert.equal(g.sockets[0].sent[0].mic, false);
    assert.deepEqual(g.sockets[0].sent[1], { type: 'bye', reason: 'left' });
    assert.equal(g.sockets[0].closed, true);
});

test('gateway client uses the call contract and refuses cross-origin or arbitrary sockets', async () => {
    const old = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
        assert.equal(url, '/api/call/open'); assert.equal(init.method, 'POST');
        assert.deepEqual(JSON.parse(init.body), { chat_id: 9, client_message_id: 'one' });
        return { ok: true, json: async () => ({ call_id: 'one', ws_path: '/ws/call/one' }) };
    };
    try { assert.equal((await apiClient.openCall({ chat_id: 9, client_message_id: 'one' })).call_id, 'one'); }
    finally { globalThis.fetch = old; }
    const options = { location: { href: 'https://app.test/', origin: 'https://app.test' }, WebSocket: class { constructor(url) { this.url = url; } } };
    assert.equal(apiClient.callSocket('/ws/call/id', options).url, 'wss://app.test/ws/call/id');
    for (const path of ['https://other.test/ws/call/id', '/ws', '/ws/call/id?token=secret', '//other.test/ws/call/id'])
        assert.throws(() => apiClient.callSocket(path, options), /invalid call socket/);
    assert.equal(statePresentation('reconnecting').active, true);
    assert.equal(statePresentation('failed', 'why').reason, 'why');
    assert.equal(statePresentation('live', '', true).label, 'Muted');
});

test('the registered AudioWorklet captures real quanta at device rate and never echoes the microphone', async () => {
    const saved = Object.fromEntries(['AudioWorkletProcessor', 'registerProcessor', 'sampleRate'].map(key => [key, globalThis[key]]));
    let Processor;
    globalThis.sampleRate = 48000;
    globalThis.AudioWorkletProcessor = class {
        constructor() { this.sent = []; this.port = { postMessage: msg => this.sent.push(msg) }; }
    };
    globalThis.registerProcessor = (name, cls) => { assert.equal(name, 'ouroboros-call-audio'); Processor = cls; };
    try {
        await import('../modules/call_worklet.js?processor-test');
        const processor = new Processor();
        processor.port.onmessage({ data: { type: 'capture', enabled: true, generation: 7 } });
        const output = new Float32Array(128);
        for (let i = 0; i < 8; i++) processor.process([[new Float32Array(128).fill(0.5)]], [[output]]);
        assert.deepEqual([...output], Array(128).fill(0));
        const captured = processor.sent[0];
        assert.equal(captured.pcm.length, 960); assert.equal(captured.rate, 48000);
        assert.equal(captured.generation, 7); assert.equal(captured.pcm[0], 16384);
        processor.port.onmessage({ data: { type: 'capture', enabled: false, generation: 8 } });
        for (let i = 0; i < 8; i++) processor.process([[new Float32Array(128).fill(1)]], [[output]]);
        assert.equal(processor.sent.length, 1);
        processor.port.onmessage({ data: { type: 'audio', epoch: 1, rate: 48000,
            item_id: 'spoken', pcm: new Int16Array(1920).fill(16384) } });
        processor.process([[]], [[output]]); assert.equal(output[0], 0.5);
        processor.port.onmessage({ data: { type: 'flush', epoch: 2 } });
        assert.deepEqual(processor.sent.at(-1), { type: 'played', item_id: 'spoken', played_ms: 2 });
        processor.process([[]], [[output]]); assert.equal(output[0], 0);
    } finally {
        for (const [key, value] of Object.entries(saved)) {
            if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
        }
    }
});

test('a room that only leaves the screen keeps its call; destroying the room hangs up', () => {
    const calls = [];
    const room = { closeTransient: () => calls.push('room-leave'), destroy: () => calls.push('room-destroy') };
    mountRoomCall({ room, mount: () => ({ leave: () => calls.push('call-leave'), destroy: () => calls.push('call-destroy') }) });
    room.closeTransient();
    assert.deepEqual(calls, ['room-leave']);
    room.destroy();
    assert.deepEqual(calls, ['room-leave', 'call-destroy', 'room-destroy']);
});

test('server reason codes read as short owner words; no code reaches the owner raw', () => {
    assert.equal(reasonWords('closed', 'engine_error:invalid_request'), 'The voice service refused the call setup.');
    assert.equal(reasonWords('closed', 'engine_error:authentication'), 'The voice service refused the API key; check it in Settings.');
    assert.equal(reasonWords('closed', 'engine_error:something_new'), 'The voice service reported an error.');
    assert.equal(reasonWords('reconnecting', 'transport_lost:session_limit'), 'The voice connection dropped.');
    assert.equal(reasonWords('closing', 'budget_exceeded'), 'The spending limit is reached; raise it in Settings to call.');
    assert.equal(reasonWords('closed', 'owner_stop'), 'You stopped the call.');
    assert.equal(reasonWords('closed', 'finished'), '');
    assert.equal(reasonWords('closed', ''), '');
    assert.equal(reasonWords('closed', 'a_reason_this_page_does_not_know'), 'The call ended.');
    assert.equal(reasonWords('reconnecting', 'a_reason_this_page_does_not_know'), 'Restoring the voice connection.');
    assert.equal(reasonWords('closed', 'toString'), 'The call ended.');
    for (const code of ['owner_stop', 'owner_bye', 'peers_left', 'max_minutes', 'budget_exceeded', 'engine_unavailable',
        'reconnect_failed', 'session_cap', 'go_away', 'context_length', 'transport_lost:x', 'engine_error:provider'])
        assert.doesNotMatch(reasonWords('closed', code), /_|engine_error/, code);
});

test('the call strip shows owner words for a server reason and keeps the code in the title', async () => {
    const f = fixture();
    const elements = [];
    const element = (name) => {
        const listeners = {};
        const node = { name, className: '', dataset: {}, hidden: false, textContent: '', title: '', value: '', disabled: false, listeners,
            children: new Map(), setAttribute() {}, addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
            removeEventListener() {}, after() {}, prepend() {}, append() {}, remove() {}, focus() {},
            closest: selector => element(selector),
            querySelector(selector) {
                if (!this.children.has(selector)) this.children.set(selector, element(selector));
                return this.children.get(selector);
            } };
        elements.push(node);
        return node;
    };
    const doc = { head: element('head'), querySelector: () => null, createElement: tag => element(tag),
        addEventListener() {}, removeEventListener() {} };
    const page = element('page');
    Object.assign(page, { ownerDocument: doc, id: 'room-71' });
    const env = { ...f.env, addEventListener() {}, removeEventListener() {} };
    const sockets = [];
    const installed = {};
    const install = (name, value) => {
        installed[name] = Object.getOwnPropertyDescriptor(globalThis, name);
        Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
    };
    install('fetch', async () => ({ ok: true, json: async () => ({ call_id: 'c', ws_path: '/ws/call/c' }) }));
    install('location', { href: 'https://app.test/', origin: 'https://app.test' });
    install('WebSocket', class { constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
        send(data) { this.sent.push(data); } close() { this.readyState = 3; } });
    const call = mountCall({ page, chatId: 71, env });
    try {
        const button = elements.find(node => node.className.includes('call-button'));
        button.listeners.click[0]();
        while (!sockets.length) await new Promise(resolve => setImmediate(resolve));
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'state', state: 'closed', reason: 'engine_error:invalid_request' }) });
        const reason = elements.find(node => node.name === '.call-reason');
        assert.equal(reason.textContent, 'The voice service refused the call setup.');
        assert.equal(reason.title, 'engine_error:invalid_request');
        assert.equal(reason.hidden, false);
    } finally {
        call.destroy();
        for (const [name, descriptor] of Object.entries(installed)) {
            if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name];
        }
    }
});

test('a long answer arriving faster than it plays is held, not a failed call', () => {
    const buffer = new PlaybackBuffer(1000, () => {});
    const second = { item_id: 'long', epoch: 0, rate: 1000, pcm: new Int16Array(1000) };
    for (let i = 0; i < 30; i++) assert.equal(buffer.push(second), true);  // thirty seconds in one burst
    assert.equal(buffer.queued, 30000);
});
