/** PCM transport and sample-counted playback share one implementation with the worklet.
 * Conversion to the engine's rate belongs to the server; this side uses the device rate.
 */
export const HEADER_SIZE = 16;
export const MAX_FRAME_PAYLOAD = 64 * 1024;
export const CAPTURE_MS = 20;
export const JITTER_MS = 40;
// Physical memory bound: a provider sends a whole answer many times faster than it plays,
// so a long answer queues here; frames are held only as they arrive, nothing is preallocated.
export const MAX_BUFFER_SECONDS = 600;

export function encodeFrame({ kind = 1, epoch = 0, seq = 0, rate, stream = 0, pcm }) {
    if (!(pcm instanceof Int16Array) || pcm.byteLength > MAX_FRAME_PAYLOAD)
        throw new Error('Audio payload must be PCM16 and at most 64 KiB.');
    if (![1, 2].includes(kind) || !Number.isInteger(rate) || rate <= 0 || rate > 0xffffffff)
        throw new Error('Invalid audio kind or sample rate.');
    const buffer = new ArrayBuffer(HEADER_SIZE + pcm.byteLength);
    const view = new DataView(buffer);
    view.setUint8(0, 79); view.setUint8(1, 67); view.setUint8(2, 1); view.setUint8(3, kind);
    view.setUint16(4, epoch, true); view.setUint32(6, seq, true);
    view.setUint32(10, rate, true); view.setUint16(14, stream, true);
    pcm.forEach((value, i) => view.setInt16(HEADER_SIZE + i * 2, value, true));
    return buffer;
}

export function decodeFrame(buffer) {
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < HEADER_SIZE)
        throw new Error('Audio frame is shorter than its header.');
    const view = new DataView(buffer);
    const bytes = buffer.byteLength - HEADER_SIZE;
    if (view.getUint8(0) !== 79 || view.getUint8(1) !== 67 || view.getUint8(2) !== 1
        || ![1, 2].includes(view.getUint8(3)) || bytes % 2 || bytes > MAX_FRAME_PAYLOAD)
        throw new Error('Malformed call audio frame.');
    const rate = view.getUint32(10, true);
    if (!rate) throw new Error('Audio frame has no sample rate.');
    const pcm = new Int16Array(bytes / 2);
    pcm.forEach((_, i) => { pcm[i] = view.getInt16(HEADER_SIZE + i * 2, true); });
    return { kind: view.getUint8(3), epoch: view.getUint16(4, true), seq: view.getUint32(6, true),
        rate, stream: view.getUint16(14, true), pcm };
}

/** Epochs wrap at u16; an old flush must never resurrect interrupted audio. */
export function newerEpoch(next, current) {
    const distance = (next - current + 65536) % 65536;
    return distance > 0 && distance < 32768;
}

export const playedMilliseconds = (samples, rate) => Math.floor(samples * 1000 / rate);

export function pcm16(value) {
    const sample = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
    return Math.round(sample * (sample < 0 ? 32768 : 32767));
}

/** Whole frames wait briefly for network jitter; only rendered samples earn acknowledgements. */
export class PlaybackBuffer {
    constructor(rate, report = () => {}) {
        this.rate = rate;
        this.report = report;
        this.epoch = 0;
        this.queue = [];
        this.queued = 0;
        this.waited = 0;
        this.playing = false;
        this.played = new Map();
        this.dirty = new Set();
        this.sinceReport = 0;
    }

    flush(epoch) {
        if (epoch !== this.epoch && !newerEpoch(epoch, this.epoch)) return;
        this.acknowledge();
        this.epoch = epoch;
        this.queue = [];
        this.queued = this.waited = 0;
        this.playing = false;
    }

    push(frame) {
        if (newerEpoch(frame.epoch, this.epoch)) this.flush(frame.epoch);
        if (frame.epoch !== this.epoch) return false;
        if (frame.rate !== this.rate) throw new Error('Playback rate differs from the negotiated device rate.');
        if (this.queued + frame.pcm.length > this.rate * MAX_BUFFER_SECONDS)
            throw new Error('Playback buffer is full. Leave the call and reconnect.');
        if (frame.pcm.length) {
            this.queue.push({ ...frame, offset: 0 });
            this.queued += frame.pcm.length;
        }
        return true;
    }

    acknowledge() {
        for (const id of this.dirty) this.report({ type: 'played', item_id: id,
            played_ms: playedMilliseconds(this.played.get(id), this.rate) });
        this.dirty.clear();
        this.sinceReport = 0;
    }

    render(output) {
        output.fill(0);
        if (!this.queue.length) { this.playing = false; this.waited = 0; return; }
        if (!this.playing) {
            this.waited += output.length;
            if (Math.max(this.queued, this.waited) < this.rate * JITTER_MS / 1000) return;
            this.playing = true;
        }
        let written = 0;
        while (written < output.length && this.queue.length) {
            const frame = this.queue[0];
            const count = Math.min(output.length - written, frame.pcm.length - frame.offset);
            for (let i = 0; i < count; i++) output[written + i] = frame.pcm[frame.offset + i] / 32768;
            frame.offset += count;
            written += count;
            this.queued -= count;
            if (frame.item_id) {
                this.played.set(frame.item_id, (this.played.get(frame.item_id) || 0) + count);
                this.dirty.add(frame.item_id);
            }
            if (frame.offset === frame.pcm.length) this.queue.shift();
        }
        this.sinceReport += written;
        if (!this.queue.length || this.sinceReport >= this.rate / 10) this.acknowledge();
    }
}

// globalThis also keeps this leaf importable by node tests and the main thread.
if (typeof globalThis.registerProcessor === 'function') {
    class CallAudioProcessor extends globalThis.AudioWorkletProcessor {
        constructor() {
            super();
            this.rate = globalThis.sampleRate;
            this.capture = new Int16Array(Math.round(this.rate * CAPTURE_MS / 1000));
            this.offset = 0;
            this.enabled = false;
            this.generation = 0;
            this.playback = new PlaybackBuffer(this.rate, message => this.port.postMessage(message));
            this.port.onmessage = ({ data }) => {
                try {
                    if (data.type === 'audio') this.playback.push(data);
                    if (data.type === 'flush') this.playback.flush(data.epoch);
                    if (data.type === 'capture') {
                        this.enabled = data.enabled;
                        this.generation = data.generation;
                        this.offset = 0;
                    }
                } catch (error) { this.port.postMessage({ type: 'error', reason: error.message }); }
            };
        }

        process(inputs, outputs) {
            const channels = inputs[0];
            if (this.enabled && channels?.length) {
                for (let i = 0; i < channels[0].length; i++) {
                    let value = 0;
                    for (const channel of channels) value += channel[i] / channels.length;
                    this.capture[this.offset++] = pcm16(value);
                    if (this.offset === this.capture.length) {
                        this.port.postMessage({ type: 'capture', pcm: this.capture, rate: this.rate,
                            generation: this.generation }, [this.capture.buffer]);
                        this.capture = new Int16Array(Math.round(this.rate * CAPTURE_MS / 1000));
                        this.offset = 0;
                    }
                }
            }
            if (outputs[0]?.[0]) this.playback.render(outputs[0][0]);
            return true;
        }
    }
    globalThis.registerProcessor('ouroboros-call-audio', CallAudioProcessor);
}
