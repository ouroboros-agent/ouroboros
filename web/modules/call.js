/** A room call owns its microphone, socket and playback until its room is destroyed.
 * One room hook carries the actual chat id and decorates destroy; hiding the room keeps the call.
 * Captions bind output stream ids to item ids before PCM; bytes queued are never heard bytes.
 */
import { apiClient } from './api_client.js';
import { decodeFrame, encodeFrame, newerEpoch } from './call_worklet.js';

const CONNECT_TIMEOUT_MS = 15000; // Bound a dead handshake, never the conversation.
let microphoneOwner = null;

export function callAvailability(env = globalThis) {
    let telegram = Boolean(env.Telegram?.WebApp)
        || env.document?.documentElement?.dataset?.ouroborosHost === 'telegram';
    try {
        telegram ||= Boolean(env.parent?.Telegram?.WebApp)
            || env.parent?.document?.documentElement?.dataset?.ouroborosHost === 'telegram';
    } catch { /* Cross-origin parents expose no host facts. */ }
    if (telegram) return { supported: false, hidden: true, reason: 'Calls are unavailable in the Telegram Mini App.' };
    if (!env.isSecureContext) return { supported: false, reason: 'Calls need HTTPS or localhost to use the microphone.' };
    if (typeof env.navigator?.mediaDevices?.getUserMedia !== 'function')
        return { supported: false, reason: 'This browser cannot access a microphone.' };
    if (!env.AudioContext || !env.AudioWorkletNode || !('audioWorklet' in env.AudioContext.prototype))
        return { supported: false, reason: 'This browser does not support live audio. Open it in a current browser.' };
    if (!env.WebSocket) return { supported: false, reason: 'This browser does not support call connections.' };
    return { supported: true, reason: '' };
}

// The call service's reasons are codes; the owner reads these words and the code stays in the title.
const REASON_WORDS = {
    finished: '',
    owner_stop: 'You stopped the call.',
    owner_bye: 'The call was left from another screen.',
    peers_left: 'Everyone left the call.',
    max_minutes: 'The call reached its time limit in Settings.',
    budget_exceeded: 'The spending limit is reached; raise it in Settings to call.',
    engine_unavailable: 'The voice service could not be reached; the log has the details.',
    reconnect_failed: 'The voice connection was lost and could not be restored.',
    admission_failed: 'The call could not start; the log has the details.',
    session_cap: 'Starting a fresh voice session.',
    go_away: 'The voice service is moving the session.',
    context_length: 'Fitting the conversation to the voice model.',
    transport_lost: 'The voice connection dropped.',
};
const ENGINE_ERROR_WORDS = {
    authentication: 'The voice service refused the API key; check it in Settings.',
    invalid_request: 'The voice service refused the call setup.',
    rate_limit: 'The voice service asked to slow down.',
    context_length: 'The conversation does not fit the voice model.',
    transport: 'The voice connection dropped.',
    protocol: 'The voice service sent a message the call could not read.',
    backpressure: 'The voice connection fell behind.',
    provider: 'The voice service failed.',
};

/** Owner words for a server reason code (`kind` or `kind:detail`); an unknown code still reads as words. */
export function reasonWords(state, code = '') {
    const value = String(code || '');
    if (!value) return '';
    const colon = value.indexOf(':');
    const kind = colon < 0 ? value : value.slice(0, colon);
    if (kind === 'engine_error') return ENGINE_ERROR_WORDS[value.slice(colon + 1)] || 'The voice service reported an error.';
    if (Object.hasOwn(REASON_WORDS, kind)) return REASON_WORDS[kind];
    return state === 'reconnecting' ? 'Restoring the voice connection.' : 'The call ended.';
}

export function statePresentation(state, reason = '', muted = false, code = '') {
    const labels = { idle: 'Call', connecting: 'Connecting…', live: muted ? 'Muted' : 'Listening',
        reconnecting: 'Reconnecting…', closing: 'Leaving…', closed: 'Call ended', failed: 'Call failed' };
    return { label: labels[state] || 'Call state unavailable', reason, code,
        active: ['connecting', 'live', 'reconnecting', 'closing'].includes(state),
        tone: state === 'failed' ? 'danger' : state === 'live' ? 'ok'
            : state === 'reconnecting' ? 'warn' : 'muted' };
}

export function mediaFailure(error) {
    const reasons = { NotAllowedError: 'Microphone access was denied. Allow it in your browser or system settings.',
        NotFoundError: 'No microphone was found. Connect one and try again.',
        NotReadableError: 'The microphone could not be opened. Check whether another app is using it.',
        SecurityError: 'The browser blocked microphone access.' };
    return reasons[error?.name] || error?.message || 'The call could not be opened.';
}

export class CallSession {
    constructor({ chatId, env = globalThis, open = apiClient.openCall,
        socket = path => apiClient.callSocket(path), changed = () => {}, caption = () => {} }) {
        this.chatId = chatId;
        this.env = env;
        this.open = open;
        this.makeSocket = socket;
        this.changed = changed;
        this.caption = caption;
        this.state = 'idle';
        this.reason = '';
        this.reasonCode = '';
        this.generation = 0;
        this.captureGeneration = 0;
        this.items = new Map();
        this.played = new Map();
        this.seq = 0;
        this.epoch = 0;
        this.muted = false;
    }

    setState(state, reason = '', code = '') {
        this.state = state;
        this.reason = reason;
        this.reasonCode = code;
        this.changed(statePresentation(state, reason, this.muted, code));
    }

    send(message) {
        if (this.socket?.readyState === 1) this.socket.send(JSON.stringify(message));
    }

    async start(selection = {}) {
        if (statePresentation(this.state).active) return;
        const capability = callAvailability(this.env);
        if (!capability.supported) { this.setState('failed', capability.reason); return; }
        if (microphoneOwner && microphoneOwner !== this) {
            this.setState('failed', 'Another room is using the microphone. Leave that call first.'); return;
        }
        microphoneOwner = this;
        const generation = ++this.generation;
        const current = () => this.generation === generation;
        this.items.clear(); this.played.clear();
        this.seq = this.epoch = 0;
        this.muted = false;
        this.setState('connecting');
        try {
            const context = new this.env.AudioContext({ latencyHint: 'interactive' });
            this.context = context;
            // Resume during the gesture, before permission or admission can consume it on iOS.
            const resume = context.resume();
            resume.catch(() => {});
            const media = await this.env.navigator.mediaDevices.getUserMedia({ audio: {
                channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
            if (!current()) { media.getTracks().forEach(track => track.stop()); return; }
            this.media = media;
            for (const track of media.getAudioTracks()) track.onended = () => this.fail('The microphone was disconnected.');
            await resume;
            if (!current()) return;
            await context.audioWorklet.addModule(new URL('./call_worklet.js', import.meta.url));
            if (!current()) return;
            const node = new this.env.AudioWorkletNode(context, 'ouroboros-call-audio', {
                numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1 });
            this.node = node;
            node.onprocessorerror = () => this.fail('Live audio stopped working. Leave the call and try again.');
            node.port.onmessage = ({ data }) => { if (current()) this.onAudio(data); };
            this.source = context.createMediaStreamSource(media);
            this.source.connect(node);
            node.connect(context.destination);
            const result = await this.open({ chat_id: this.chatId,
                client_message_id: this.env.crypto.randomUUID(), ...selection });
            if (!result?.call_id || typeof result.ws_path !== 'string') throw new Error('The server did not return a call address.');
            if (!current()) { this.releaseAdmission(result.ws_path, context.sampleRate); return; }
            const socket = this.makeSocket(result.ws_path);
            this.socket = socket;
            socket.binaryType = 'arraybuffer';
            this.timer = this.env.setTimeout(() => this.fail('The call did not connect in time.'), CONNECT_TIMEOUT_MS);
            socket.onopen = () => {
                if (!current()) return;
                this.send({ type: 'hello', protocol: 1, role: 'owner', mic: true, speaker: true,
                    rate: context.sampleRate, label: 'Owner' });
                this.capture();
            };
            socket.onmessage = ({ data }) => {
                if (!current()) return;
                try { this.receive(data); } catch (error) { this.fail(mediaFailure(error)); }
            };
            socket.onerror = () => { if (current()) this.fail('The call connection failed. Check your connection and try again.'); };
            socket.onclose = () => { if (current()) this.fail('The call connection closed. Start a new call to reconnect.'); };
        } catch (error) { if (current()) this.fail(mediaFailure(error), String(error?.body?.code || '')); }
    }

    /** A leave during admission still closes the admitted turn when its address arrives. */
    releaseAdmission(path, rate) {
        const socket = this.makeSocket(path);
        const timer = this.env.setTimeout(() => socket.close(), CONNECT_TIMEOUT_MS);
        socket.onopen = () => {
            socket.send(JSON.stringify({ type: 'hello', protocol: 1, role: 'owner', mic: false, speaker: false, rate, label: 'Owner' }));
            socket.send(JSON.stringify({ type: 'bye', reason: 'left' }));
            socket.close();
        };
        socket.onclose = socket.onerror = () => this.env.clearTimeout(timer);
    }

    capture() {
        const enabled = !this.muted && ['live', 'reconnecting'].includes(this.state);
        for (const track of this.media?.getAudioTracks() || []) track.enabled = enabled;
        this.node?.port.postMessage({ type: 'capture', enabled, generation: ++this.captureGeneration });
    }

    setMuted(muted) {
        this.muted = muted;
        this.capture();
        this.send({ type: 'mute', muted });
        this.changed(statePresentation(this.state, this.reason, muted, this.reasonCode));
    }

    onAudio(data) {
        if (data.type === 'error') { this.fail(data.reason); return; }
        if (data.type === 'played') {
            const last = this.played.get(data.item_id) || 0;
            if (data.item_id && Number.isInteger(data.played_ms) && data.played_ms > last) {
                this.played.set(data.item_id, data.played_ms);
                this.send({ type: 'played', item_id: data.item_id, played_ms: data.played_ms });
            }
        }
        if (data.type !== 'capture' || this.muted || data.generation !== this.captureGeneration
            || !['live', 'reconnecting'].includes(this.state) || this.socket?.readyState !== 1) return;
        // One second of unsent microphone PCM is a broken uplink, not useful live speech.
        if (this.socket.bufferedAmount > data.rate * 2) { this.fail('The connection is too slow to send live audio.'); return; }
        this.socket.send(encodeFrame({ epoch: this.epoch, seq: this.seq++, rate: data.rate, pcm: data.pcm }));
    }

    receive(data) {
        if (data instanceof ArrayBuffer) {
            if (this.state === 'closing') return;
            const frame = decodeFrame(data);
            if (frame.kind !== 2) throw new Error('The server sent microphone audio as playback.');
            if (newerEpoch(frame.epoch, this.epoch)) this.flush(frame.epoch);
            if (frame.epoch !== this.epoch) return;
            const item_id = this.items.get(frame.stream);
            if (!item_id) throw new Error('Playback arrived without its speech item. Reconnect after the server is updated.');
            this.node.port.postMessage({ type: 'audio', ...frame, item_id }, [frame.pcm.buffer]);
            return;
        }
        if (typeof data !== 'string' || new TextEncoder().encode(data).length > 16 * 1024)
            throw new Error('The server sent an invalid call message.');
        const msg = JSON.parse(data);
        if (msg.type === 'state') {
            if (!['connecting', 'live', 'reconnecting', 'closing', 'closed'].includes(msg.state))
                throw new Error('The server sent an unknown call state.');
            this.env.clearTimeout(this.timer);
            const code = String(msg.reason || '');
            if (msg.state === 'closed') { this.stop('closed', reasonWords(msg.state, code), code); return; }
            this.setState(msg.state, reasonWords(msg.state, code), code);
            this.capture();
            if (msg.state === 'closing') this.node?.port.postMessage({ type: 'flush', epoch: (this.epoch + 1) & 65535 });
        } else if (msg.type === 'flush') {
            if (!Number.isInteger(msg.epoch) || msg.epoch < 0 || msg.epoch > 65535) throw new Error('Invalid playback epoch.');
            this.flush(msg.epoch);
        } else if (msg.type === 'caption') {
            if (!['owner', 'ouroboros'].includes(msg.speaker) || typeof msg.text !== 'string'
                || typeof msg.item_id !== 'string') throw new Error('Invalid live caption.');
            if (msg.epoch !== undefined && msg.epoch !== this.epoch) return;
            // A final caption may arrive after the next item's audio has begun; it cannot rebind that stream.
            if (msg.speaker === 'ouroboros' && msg.item_id && !msg.final) {
                const stream = msg.stream ?? 0;
                if (!Number.isInteger(stream) || stream < 0 || stream > 65535) throw new Error('Invalid speech stream.');
                this.items.set(stream, msg.item_id);
            }
            if (msg.text) this.caption(msg);
        } else throw new Error('The server sent an unknown call message.');
    }

    flush(epoch) {
        if (!newerEpoch(epoch, this.epoch)) return;
        this.epoch = epoch;
        this.items.clear();
        this.node?.port.postMessage({ type: 'flush', epoch });
    }

    fail(reason, code = '') { this.stop('failed', reason, code); }

    stop(state = 'closed', reason = '', code = '') {
        this.generation++;
        this.env.clearTimeout(this.timer);
        for (const track of this.media?.getTracks() || []) { track.onended = null; track.enabled = false; track.stop(); }
        this.media = null;
        this.source?.disconnect(); this.source = null;
        if (this.node) {
            this.node.port.onmessage = null;
            this.node.port.close(); this.node.disconnect(); this.node = null;
        }
        if (this.context) { this.context.close().catch(() => {}); this.context = null; }
        if (this.socket) {
            this.send({ type: 'bye', reason: state === 'failed' ? 'client_error' : 'left' });
            this.socket.onopen = this.socket.onmessage = this.socket.onerror = this.socket.onclose = null;
            this.socket.close(); this.socket = null;
        }
        this.items.clear(); this.played.clear();
        if (microphoneOwner === this) microphoneOwner = null;
        this.setState(state, reason, code);
    }
}

const PHONE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.9v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 3.1 5.2 2 2 0 0 1 5.1 3h3a2 2 0 0 1 2 1.7l.5 2.8a2 2 0 0 1-.6 1.8L8.7 10.6a16 16 0 0 0 4.7 4.7l1.3-1.3a2 2 0 0 1 1.8-.6l2.8.5A2 2 0 0 1 22 16.9Z"/></svg>';

export function mountCall({ page, chatId, env = globalThis }) {
    const doc = page.ownerDocument;
    const attach = page.querySelector('.chat-attach-btn');
    const composer = attach?.closest('.chat-input-wrap');
    const available = callAvailability(env);
    if (!composer || available.hidden) return { destroy() {}, leave() {} };
    if (!doc.querySelector('link[data-call-style]')) {
        const sheet = doc.createElement('link');
        sheet.rel = 'stylesheet'; sheet.href = new URL('../call.css', import.meta.url).href;
        sheet.dataset.callStyle = ''; doc.head.append(sheet);
    }
    const button = doc.createElement('button');
    button.type = 'button'; button.className = 'chat-attach-btn call-button';
    button.innerHTML = PHONE; button.title = 'Call'; button.setAttribute('aria-label', 'Call');
    button.disabled = !available.supported;
    attach.after(button);
    const strip = doc.createElement('section');
    strip.className = 'call-strip'; strip.setAttribute('aria-label', 'Live call');
    strip.innerHTML = `<div class="call-actions"><span class="ui-status call-state" role="status" aria-live="polite"></span>
        <button type="button" class="btn btn-default call-mute" aria-pressed="false" hidden>Mute</button>
        <button type="button" class="btn btn-danger call-leave" hidden>Leave</button></div>
        <div class="call-reason"></div><div class="call-captions" role="log" aria-live="polite" data-i18n-authored></div>
        <details class="call-options"><summary>Call options</summary><div class="call-fields">
        <label class="ui-field">Engine<select class="ui-control call-engine"><option value="">Use Settings</option><option value="openai">OpenAI</option><option value="google">Google</option></select></label>
        <label class="ui-field">Model<input class="ui-control call-model" placeholder="Use Settings"></label>
        <label class="ui-field">Voice<input class="ui-control call-voice" placeholder="Use Settings"></label></div></details>`;
    composer.prepend(strip);
    const status = strip.querySelector('.call-state');
    const reason = strip.querySelector('.call-reason');
    const captions = strip.querySelector('.call-captions');
    const mute = strip.querySelector('.call-mute');
    const leave = strip.querySelector('.call-leave');
    const options = strip.querySelector('.call-options');
    const engine = strip.querySelector('.call-engine');
    const model = strip.querySelector('.call-model');
    const voice = strip.querySelector('.call-voice');
    reason.id = `call-reason-${page.id}`;
    button.setAttribute('aria-describedby', reason.id);
    let destroyed = false;
    const session = new CallSession({ chatId, env, changed(view) {
        if (destroyed) return;
        strip.hidden = view.label === 'Call ended' && !view.reason;
        status.textContent = view.label; status.dataset.tone = view.tone;
        reason.textContent = view.reason; reason.hidden = !view.reason; reason.title = view.code || '';
        button.disabled = !available.supported || view.active;
        chooser.disabled = !available.supported || view.active;
        mute.hidden = leave.hidden = !view.active;
        mute.textContent = session.muted ? 'Unmute' : 'Mute';
        mute.setAttribute('aria-pressed', String(session.muted));
        engine.disabled = model.disabled = voice.disabled = view.active;
        options.hidden = view.active;
    }, caption(msg) {
        // The strip is a current utterance view; complete finalized speech lives in room history.
        captions.textContent = `${msg.speaker === 'owner' ? 'You' : 'Ouroboros'}: ${msg.text}`;
    } });
    strip.hidden = available.supported;
    reason.textContent = available.reason;
    if (!available.supported) button.title = available.reason;
    const open = () => {
        if (!available.supported) return;
        const selection = {};
        for (const [key, input] of [['engine', engine], ['model', model], ['voice', voice]])
            if (input.value.trim()) selection[key] = input.value.trim();
        captions.textContent = '';
        session.start(selection);
    };
    button.addEventListener('click', open);
    mute.addEventListener('click', () => session.setMuted(!session.muted));
    const end = () => { if (available.supported) session.stop(); };
    leave.addEventListener('click', end);
    const panic = event => { if (event.target.closest?.('[data-chat-command="panic"], [data-chat-command="restart"]')) end(); };
    doc.addEventListener('click', panic, true);
    env.addEventListener('pagehide', end);
    const showOptions = event => { event.preventDefault(); strip.hidden = false; options.open = true; engine.focus(); };
    button.addEventListener('contextmenu', showOptions);
    const chooser = doc.createElement('button');
    chooser.type = 'button'; chooser.className = 'chat-attach-btn call-choose';
    chooser.setAttribute('aria-label', 'Call options'); chooser.title = 'Call options';
    chooser.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
    chooser.disabled = !available.supported;
    button.after(chooser);
    chooser.addEventListener('click', showOptions);
    button.addEventListener('keydown', event => { if (event.key === 'ArrowDown') showOptions(event); });
    const drafts = new Map();
    let selectedEngine = '';
    engine.addEventListener('change', () => {
        drafts.set(selectedEngine, { model: model.value, voice: voice.value });
        selectedEngine = engine.value;
        model.value = drafts.get(selectedEngine)?.model || '';
        voice.value = drafts.get(selectedEngine)?.voice || '';
    });
    return {
        leave: end,
        destroy() {
            if (destroyed) return;
            destroyed = true; end();
            doc.removeEventListener('click', panic, true); env.removeEventListener('pagehide', end);
            button.remove(); chooser.remove(); strip.remove();
        },
    };
}

/** Only a destroyed room hangs up; leave() also runs when the room just leaves the screen
 * (Settings, a Project, a notification), and the call goes on like a phone call. */
export function mountRoomCall({ room, mount = mountCall, ...options }) {
    const call = mount(options);
    const { destroy } = room;
    room.destroy = () => { call.destroy(); destroy(); };
    return call;
}
