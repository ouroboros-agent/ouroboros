/** Settings → Models → Live calls: Gemini Live's model and voice as editable suggestions, the
 * call's own instructions, its turn-taking (how long a silence ends the owner's turn, whether
 * speaking over Ouroboros interrupts it), search and duration limits. Changes apply to the next
 * call: the host captures them when a call is admitted. Below them, this device's microphone,
 * speaker and camera with the microphone's cleaning: client-local (`call_devices.js`), outside the
 * server draft, and applied to a running call at once.
 */
import { modelChooserHtml, bindModelChoosers } from './model_chooser.js';
import { DEFAULT_DEVICE_PREFS, listDevices, loadDevicePrefs, openMicrophone, routeSpeaker, saveDevicePrefs, speakerChoice }
    from './call_devices.js';

// Gemini Live's documented models on AI Studio keys; any other model id can be typed.
const CALL_MODELS = [
    { value: 'gemini-3.8-live', label: 'gemini-3.8-live — Google’s live voice model' },
];

// Google's prebuilt voices with the one-word style its speech docs give each
// (ai.google.dev/gemini-api/docs/speech-generation, read 2026-10-17); empty = Google's default voice.
const CALL_VOICES = [
    ['Zephyr', 'Bright'], ['Puck', 'Upbeat'], ['Charon', 'Informative'], ['Kore', 'Firm'], ['Fenrir', 'Excitable'],
    ['Leda', 'Youthful'], ['Orus', 'Firm'], ['Aoede', 'Breezy'], ['Callirrhoe', 'Easy-going'], ['Autonoe', 'Bright'],
    ['Enceladus', 'Breathy'], ['Iapetus', 'Clear'], ['Umbriel', 'Easy-going'], ['Algieba', 'Smooth'],
    ['Despina', 'Smooth'], ['Erinome', 'Clear'], ['Algenib', 'Gravelly'], ['Rasalgethi', 'Informative'],
    ['Laomedeia', 'Upbeat'], ['Achernar', 'Soft'], ['Alnilam', 'Firm'], ['Schedar', 'Even'], ['Gacrux', 'Mature'],
    ['Pulcherrima', 'Forward'], ['Achird', 'Friendly'], ['Zubenelgenubi', 'Casual'], ['Vindemiatrix', 'Gentle'],
    ['Sadachbia', 'Lively'], ['Sadaltager', 'Knowledgeable'], ['Sulafat', 'Warm'],
].map(([value, style]) => ({ value, label: `${value} — ${style}` }));

/** [input id, setting, fallback] rows the settings page loads and saves like its other inputs. */
export const CALL_SETTINGS_FIELDS = [
    ['s-call-model-google', 'OUROBOROS_CALL_MODEL_GOOGLE'], ['s-call-voice-google', 'OUROBOROS_CALL_VOICE_GOOGLE'],
    ['s-call-instructions', 'OUROBOROS_CALL_INSTRUCTIONS'],
    ['s-call-pause', 'OUROBOROS_CALL_PAUSE', 'normal'], ['s-call-interruptions', 'OUROBOROS_CALL_INTERRUPTIONS', 'true'],
    ['s-call-native-search', 'OUROBOROS_CALL_NATIVE_SEARCH', 'true'],
    ['s-call-max-minutes', 'OUROBOROS_CALL_MAX_MINUTES'], ['s-presence-call-max-minutes', 'OUROBOROS_PRESENCE_CALL_MAX_MINUTES'],
];

export function callSettingsHtml() {
    const model = modelChooserHtml('id="s-call-model-google" aria-label="Gemini Live model"', '', 's-call-model-options',
        CALL_MODELS, { placeholder: 'gemini-3.8-live (default)' });
    const voice = modelChooserHtml('id="s-call-voice-google" aria-label="Voice"', '', 's-call-voice-options',
        CALL_VOICES, { placeholder: 'Google’s default voice' });
    return `
                    <div class="form-section">
                        <h3>Live calls</h3>
                        <div class="settings-section-copy">Talk with Ouroboros by voice in any room: press Talk in an empty message field. Calls run on Google’s Gemini Live with the Google AI key in Accounts → Google. Changes apply to the next call.</div>
                        <div class="form-grid two">
                            <div class="form-field ui-field">
                                <label for="s-call-model-google">Gemini Live model</label>
                                ${model}
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-call-voice-google">Voice</label>
                                ${voice}
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-call-pause">Pause before answering</label>
                                <select id="s-call-pause" class="ui-control" aria-describedby="s-call-pause-help"><option value="short">Short</option><option value="normal">Normal</option><option value="long">Long</option></select>
                                <div class="settings-inline-note ui-field-help" id="s-call-pause-help">How long a silence ends your turn. Long lets you think aloud without being answered.</div>
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-call-interruptions">Speaking interrupts Ouroboros</label>
                                <select id="s-call-interruptions" class="ui-control"><option value="true">On</option><option value="false">Off</option></select>
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-call-native-search">Google native search</label>
                                <select id="s-call-native-search" class="ui-control"><option value="false">Off</option><option value="true">On</option></select>
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-call-max-minutes">Call limit (minutes)</label>
                                <input id="s-call-max-minutes" class="ui-control" type="number" min="1" step="1" placeholder="No limit" aria-describedby="s-call-duration-help">
                            </div>
                            <div class="form-field ui-field">
                                <label for="s-presence-call-max-minutes">Presence call limit (minutes)</label>
                                <input id="s-presence-call-max-minutes" class="ui-control" type="number" min="1" step="1" placeholder="No limit" aria-describedby="s-call-duration-help">
                            </div>
                        </div>
                        <div id="s-call-duration-help" class="settings-inline-note ui-field-help">Leave a duration empty for no time limit. The shared spending limit still applies.</div>
                        ${devicesHtml()}
                        <div class="form-field ui-field">
                            <label for="s-call-instructions">Instructions for calls</label>
                            <textarea id="s-call-instructions" class="ui-control" rows="3" placeholder="How Ouroboros should talk in calls: tone, pace, what to keep short" aria-describedby="s-call-instructions-help"></textarea>
                            <div class="settings-inline-note ui-field-help" id="s-call-instructions-help">Every call reads these first, inside its memory; empty adds nothing.</div>
                        </div>
                    </div>`;
}

const DEVICE_KINDS = [['mic', 'audioinput', 'Microphone'], ['speaker', 'audiooutput', 'Speaker'], ['camera', 'videoinput', 'Camera']];
const PROCESSING = [['echoCancellation', 'Echo cancellation'], ['noiseSuppression', 'Noise suppression'],
    ['autoGainControl', 'Automatic microphone volume']];
const TEST_MS = 15000;  // a microphone test releases the device by itself

function devicesHtml() {
    const select = ([key, , label]) => `
                                <div class="form-field ui-field" data-call-device-field="${key}">
                                    <label for="s-call-device-${key}">${label}</label>
                                    <select id="s-call-device-${key}" class="ui-control" data-call-device="${key}"></select>
                                    <div class="settings-inline-note ui-field-help" data-call-device-note="${key}" hidden></div>
                                </div>`;
    const toggle = ([key, label]) => `
                            <label class="local-toggle ui-field ui-field-inline">
                                <input type="checkbox" class="ui-checkbox" data-call-processing="${key}"> ${label}
                            </label>`;
    return `
                        <div class="settings-effort-card call-devices" data-call-devices>
                            <div class="settings-section-copy"><strong>On this device:</strong> which microphone, speaker and
                            camera a call uses. Devices belong to this browser or app, so these choices are stored here and never
                            sent to the server; a change reaches a running call at once, and a device that is gone falls back to the
                            system's own.</div>
                            <div class="form-grid two">${DEVICE_KINDS.map(select).join('')}
                            </div>${PROCESSING.map(toggle).join('')}
                            <div class="call-device-meter" data-call-mic-meter hidden aria-hidden="true"><span></span></div>
                            <div class="settings-toolbar">
                                <button type="button" class="btn btn-default btn-sm" data-call-mic-test>Test microphone</button>
                                <button type="button" class="btn btn-default btn-sm" data-call-speaker-test>Play a test sound</button>
                                <button type="button" class="btn btn-default btn-sm" data-call-devices-allow hidden>Show device names</button>
                            </div>
                            <div class="settings-inline-note" data-call-devices-status role="status" aria-live="polite"></div>
                        </div>`;
}

/** Fill the device block from this client's devices and choice; returns its disposer. */
function bindDevices(block, env = globalThis) {
    if (!block) return () => {};
    const $ = selector => block.querySelector(selector);
    const media = env.navigator?.mediaDevices;
    const status = $('[data-call-devices-status]');
    let test = null;  // { stream, context, frame, timer } while the microphone test runs
    let gone = false;

    async function paint() {
        const prefs = loadDevicePrefs(env);
        for (const [key] of PROCESSING) $(`[data-call-processing="${key}"]`).checked = prefs[key] ?? DEFAULT_DEVICE_PREFS[key];
        if (!media?.enumerateDevices) {
            for (const node of block.querySelectorAll('select, input, button')) node.disabled = true;
            status.textContent = 'This browser or app cannot use a microphone here, so a call cannot start from it.';
            return;
        }
        let devices;
        try { devices = await listDevices(env); } catch { devices = { audioinput: [], audiooutput: [], videoinput: [], named: false }; }
        if (gone) return;
        const outputs = speakerChoice(env);
        for (const [key, kind, label] of DEVICE_KINDS) {
            const select = $(`[data-call-device="${key}"]`);
            const note = $(`[data-call-device-note="${key}"]`);
            const rows = devices[kind];
            const options = [['', 'System default'], ...rows.map((row, index) => [row.id, row.label || `${label} ${index + 1}`])];
            // A remembered device that is not here now stays visible, so the choice is not lost silently.
            if (prefs[key] && !rows.some(row => row.id === prefs[key])) options.push([prefs[key], `${label} not connected`]);
            select.replaceChildren(...options.map(([value, text]) => {
                const option = env.document.createElement('option');
                option.value = value; option.textContent = text;
                return option;
            }));
            select.value = prefs[key];
            const unsupported = key === 'speaker' && !outputs;
            select.disabled = unsupported || (key === 'camera' && !rows.length && !prefs.camera);
            note.hidden = !(unsupported || (key === 'camera' && !rows.length));
            note.textContent = unsupported ? 'This browser plays calls through the system speaker; choose it in your system’s sound settings.'
                : 'No camera found on this device.';
        }
        $('[data-call-speaker-test]').hidden = !env.AudioContext;
        $('[data-call-devices-allow]').hidden = devices.named || !(devices.audioinput.length || devices.videoinput.length);
    }

    function stopTest(words = '') {
        if (!test) return;
        env.clearTimeout(test.timer);
        env.cancelAnimationFrame?.(test.frame);
        for (const track of test.stream.getTracks()) track.stop();
        test.context.close().catch(() => {});
        test = null;
        $('[data-call-mic-meter]').hidden = true;
        $('[data-call-mic-test]').textContent = 'Test microphone';
        if (words) status.textContent = words;
    }

    async function testMicrophone() {
        if (test) { stopTest(); return; }
        const prefs = loadDevicePrefs(env);
        let stream;
        try {
            stream = (await openMicrophone(env, prefs)).stream;
        } catch (error) {
            status.textContent = error?.name === 'NotAllowedError' ? 'The microphone was not allowed.' : 'The microphone could not open.';
            return;
        }
        if (gone) { stream.getTracks().forEach(track => track.stop()); return; }
        const context = new env.AudioContext();
        const analyser = context.createAnalyser();
        analyser.fftSize = 512;
        context.createMediaStreamSource(stream).connect(analyser);
        const samples = new Float32Array(analyser.fftSize);
        const meter = $('[data-call-mic-meter]');
        meter.hidden = false;
        $('[data-call-mic-test]').textContent = 'Stop the test';
        status.textContent = 'Speak: the bar follows what this microphone hears.';
        test = { stream, context, frame: 0, timer: env.setTimeout(() => stopTest('Test finished.'), TEST_MS) };
        const draw = () => {
            if (!test) return;
            analyser.getFloatTimeDomainData(samples);
            let sum = 0;
            for (const sample of samples) sum += sample * sample;
            const level = Math.min(1, Math.sqrt(sum / samples.length) * 6);
            meter.style.setProperty('--level', level.toFixed(3));
            test.frame = env.requestAnimationFrame(draw);
        };
        draw();
        void paint();  // the permission just given names the devices
    }

    async function playTestSound() {
        const prefs = loadDevicePrefs(env);
        const context = new env.AudioContext();
        const route = await routeSpeaker(context, prefs);
        const now = context.currentTime;
        for (const [index, frequency] of [660, 880].entries()) {
            const tone = context.createOscillator();
            const gain = context.createGain();
            tone.frequency.value = frequency;
            gain.gain.setValueAtTime(0, now + index * 0.18);
            gain.gain.linearRampToValueAtTime(0.18, now + index * 0.18 + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.001, now + index * 0.18 + 0.32);
            tone.connect(gain).connect(context.destination);
            tone.start(now + index * 0.18);
            tone.stop(now + index * 0.18 + 0.34);
        }
        env.setTimeout(() => context.close().catch(() => {}), 900);
        status.textContent = route === 'fell_back' ? 'The chosen speaker is not connected; the sound played through the system speaker.'
            : 'If you heard two soft notes, the speaker works.';
    }

    async function allowNames() {
        try {
            const stream = await media.getUserMedia({ audio: true });
            stream.getTracks().forEach(track => track.stop());
        } catch { status.textContent = 'Device names need microphone permission.'; }
        void paint();
    }

    const onChange = (event) => {
        const device = event.target.closest?.('[data-call-device]');
        const processing = event.target.closest?.('[data-call-processing]');
        if (device) saveDevicePrefs({ [device.dataset.callDevice]: device.value }, env);
        else if (processing) saveDevicePrefs({ [processing.dataset.callProcessing]: processing.checked }, env);
        else return;
        if (test && (device?.dataset.callDevice === 'mic' || processing)) { stopTest(); void testMicrophone(); }
    };
    const onClick = (event) => {
        if (event.target.closest('[data-call-mic-test]')) void testMicrophone();
        else if (event.target.closest('[data-call-speaker-test]')) void playTestSound();
        else if (event.target.closest('[data-call-devices-allow]')) void allowNames();
    };
    // Plugging a headset in or out changes the lists; a page that left the document stops listening.
    const onDeviceChange = () => { if (!block.isConnected) { dispose(); return; } void paint(); };
    block.addEventListener('change', onChange);
    block.addEventListener('click', onClick);
    media?.addEventListener?.('devicechange', onDeviceChange);
    void paint();
    function dispose() {
        if (gone) return;
        gone = true;
        stopTest();
        block.removeEventListener('change', onChange);
        block.removeEventListener('click', onClick);
        media?.removeEventListener?.('devicechange', onDeviceChange);
    }
    return dispose;
}

/** The model and voice suggestions and this device's block; returns their disposer. */
export function bindCallSettings(root, env = globalThis) {
    const disposeChoosers = bindModelChoosers(root?.querySelector?.('#s-call-model-google')?.closest('.form-section') || null);
    const disposeDevices = bindDevices(root?.querySelector?.('[data-call-devices]') || null, env);
    return () => { disposeChoosers?.(); disposeDevices(); };
}
