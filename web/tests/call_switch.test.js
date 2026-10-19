// Calls are a beta the owner turns on: one Settings switch, off by default whatever keys exist,
// saved only when he flips it, so a later default still reaches every install that never chose.
import assert from 'node:assert/strict';
import test from 'node:test';

import { callsChange } from '../modules/settings.js';
import { renderSettingsPage } from '../modules/settings_ui.js';

test('the switch travels only when flipped; an explicit off is a choice too', () => {
    for (const loaded of [undefined, false, 'false', 'False', '']) assert.deepEqual(callsChange(false, loaded), {}, String(loaded));
    for (const loaded of [true, 'true', 'True']) assert.deepEqual(callsChange(true, loaded), {}, String(loaded));
    assert.deepEqual(callsChange(true, false), { OUROBOROS_CALLS_ENABLED: 'true' });
    assert.deepEqual(callsChange(true, undefined), { OUROBOROS_CALLS_ENABLED: 'true' }, 'a core that never sent it');
    assert.deepEqual(callsChange(false, 'true'), { OUROBOROS_CALLS_ENABLED: 'false' });
});

test('Live calls opens with the switch, and the copy says where calls do not work', () => {
    const html = renderSettingsPage();
    assert.equal(html.split('id="s-calls-enabled"').length - 1, 1);
    const live = html.slice(html.indexOf('<h3>Live calls</h3>'));
    assert.ok(live.indexOf('id="s-calls-enabled"') < live.indexOf('id="s-call-model-google"'));
    const help = html.match(/id="s-calls-enabled-help">([^<]*)</)[1];
    assert.match(help, /^Off by default, even when the key is set/);
    assert.match(help, /no call opens, from Talk or from a meeting skill/);
    assert.match(help, /Gemini Live in Russia or Belarus/);
    assert.match(html, /Google does not offer the Gemini API in Russia or Belarus, so calls do not work from there/);
});
