/** Settings → Models → Live calls: Gemini Live's model and voice as editable suggestions, the
 * call's own instructions, its turn-taking (how long a silence ends the owner's turn, whether
 * speaking over Ouroboros interrupts it), search and duration limits. Changes apply to the next
 * call: the host captures them when a call is admitted.
 */
import { modelChooserHtml, bindModelChoosers } from './model_chooser.js';

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
                        <div class="form-field ui-field">
                            <label for="s-call-instructions">Instructions for calls</label>
                            <textarea id="s-call-instructions" class="ui-control" rows="3" placeholder="How Ouroboros should talk in calls: tone, pace, what to keep short" aria-describedby="s-call-instructions-help"></textarea>
                            <div class="settings-inline-note ui-field-help" id="s-call-instructions-help">Every call reads these first, inside its memory; empty adds nothing.</div>
                        </div>
                    </div>`;
}

/** The model and voice suggestions; returns their disposer. */
export function bindCallSettings(root) {
    return bindModelChoosers(root?.querySelector?.('#s-call-model-google')?.closest('.form-section') || null);
}
