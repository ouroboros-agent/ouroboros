import test from 'node:test';
import assert from 'node:assert/strict';
import { callPresentation, createTalkFacts } from '../modules/call_capsule.js';
import { callMemberOf, callSpan, createCallCards } from '../modules/call_card.js';
import { createCallLight } from '../modules/call_light.js';
import { callClock, markedCall } from '../modules/call_mark.js';
import { CALL_VISUAL, waveShape } from '../modules/call_aura.js';

const live = (extra = {}) => ({ state: 'live', code: '', detail: '', action: '', words: '', phase: '',
    muted: false, activity: { working: false, label: '' }, ...extra });
const ended = (code, extra = {}) => live({ state: 'closed', code, ...extra });

test('every phase the call reports has its words; nothing claims a connection that is not happening', () => {
    assert.deepEqual(callPresentation({ state: 'idle' }), { mode: 'idle' });
    assert.deepEqual(callPresentation(live({ state: 'ended', code: 'owner_end' })), { mode: 'idle' });  // the owner's End
    const words = (view, facts) => { const p = callPresentation(view, facts); return [p.title, p.sub, p.light]; };
    assert.deepEqual(words(live({ state: 'permission' })), ['Waiting for the microphone', 'Allow it if your browser asks', 'trace']);
    assert.deepEqual(words(live({ state: 'admission' })), ['Starting the call…', '', 'trace']);
    assert.deepEqual(words(live({ state: 'preparing', phase: 'memory' })), ['Preparing memory…', 'Reading this room and my notes', 'trace']);
    assert.deepEqual(words(live({ state: 'preparing' })), ['Preparing the call…', '', 'trace']);
    // One engine: an old server's bare "connecting" and a new one's both read the same.
    assert.deepEqual(words(live({ state: 'connecting' })), ['Connecting to Google…', '', 'trace']);
    assert.deepEqual(words(live({ state: 'connecting', engine: 'google' })), ['Connecting to Google…', '', 'trace']);
    assert.deepEqual(words(live({ state: 'reconnecting', code: 'session_cap' })), ['Reconnecting…', 'Starting a fresh voice session', 'trace']);
    assert.deepEqual(words(live({ state: 'reconnecting', code: 'transport_lost:1006' })), ['Reconnecting…', 'The voice connection dropped', 'trace']);
    assert.deepEqual(words(live({ state: 'closing' })), ['Ending the call…', '', 'trace']);
    assert.deepEqual(words(live()), ['Listening', 'Speak whenever you’re ready', 'listen']);
    assert.deepEqual(words(live(), { ownerSpeaking: true }), ['Listening', 'You’re speaking', 'listen']);
    assert.deepEqual(words(live(), { thinking: true }), ['Thinking', '', 'think']);
    assert.deepEqual(words(live({ activity: { working: true, label: 'Searching the web' } })), ['Working', 'Searching the web', 'think']);
    assert.deepEqual(words(live({ activity: { working: true, label: '' } })), ['Working', 'Using a tool', 'think']);
    assert.deepEqual(words(live(), { heard: true, thinking: true }), ['Ouroboros', 'Speaking', 'listen']);
    assert.deepEqual(words(live({ muted: true })), ['Muted', 'Ouroboros can’t hear you', 'muted']);
    // Whose light may show: the owner's unless Ouroboros is heard (then echo never paints blue).
    assert.equal(callPresentation(live()).voice, 'owner');
    assert.equal(callPresentation(live(), { heard: true }).voice, 'ouroboros');
    assert.equal(callPresentation(live({ muted: true }), { heard: true }).voice, 'ouroboros');
    for (const state of ['permission', 'admission', 'preparing', 'connecting', 'live', 'reconnecting', 'closing'])
        assert.equal(callPresentation(live({ state })).mode, 'live', state);
});

test('each ending says what happened and offers its one next step', () => {
    const show = (view) => { const p = callPresentation(view); return [p.title, p.actions.map(a => a.label), p.tone]; };
    assert.deepEqual(show(ended('owner_bye')), ['Call ended', [], '']);
    assert.deepEqual(show(ended('owner_stop')), ['Call ended', [], '']);
    assert.deepEqual(show(ended('max_minutes')), ['The call reached its time limit', [], '']);
    assert.deepEqual(show(ended('peers_left')), ['Everyone left the call', [], '']);
    assert.deepEqual(show(ended('budget_exceeded')), ['The spending limit is reached', ['Open Settings'], 'error']);
    for (const code of ['missing_credential', 'missing_credential_google'])
        assert.deepEqual(show(ended(code)), ['Calls need a Google AI key — add it in Settings', ['Open Settings'], 'error'], code);
    assert.deepEqual(show(ended('authentication')), ['Google refused the key', ['Open Settings'], 'error']);
    assert.deepEqual(show(ended('rate_limit', { detail: 'Slow down, please.' })), ['Google says: “Slow down, please.”', ['Try again'], 'error']);
    assert.deepEqual(show(ended('rate_limit')), ['Google asked to slow down', ['Try again'], 'error']);
    // Google's own sentence is quoted as Google's (a quota close is one of them); without one, plain words.
    assert.deepEqual(show(ended('provider_closed', { detail: 'You exceeded your current quota.' })),
        ['Google closed the call: “You exceeded your current quota.”', ['Try again'], 'error']);
    assert.deepEqual(show(ended('provider_closed')), ['Google closed the call', ['Try again'], 'error']);
    assert.deepEqual(show(ended('engine_unavailable')), ['Can’t reach Google’s voice service', ['Try again'], 'error']);
    assert.deepEqual(show(ended('reconnect_failed')), ['Can’t reach Google’s voice service', ['Try again'], 'error']);
    assert.deepEqual(show(ended('preparation_failed')), ['The call could not start; the room has the details', ['Try again'], 'error']);
    assert.deepEqual(show(ended('call_not_started')), ['The call could not start; the room has the details', ['Try again'], 'error']);
    for (const code of ['invalid_request', 'protocol', 'provider_error']) {
        const again = code === 'invalid_request' ? [] : ['Try again'];  // a refused setup would only repeat
        assert.deepEqual(show(ended(code, { detail: 'Bad config.' })), ['Google refused the call: “Bad config.”', again, 'error'], code);
        assert.deepEqual(show(ended(code)), ['Google refused the call', again, 'error'], code);
    }
    // The host's own refusals are the host's words, never framed as Google's.
    assert.deepEqual(show(ended('engine_refused', { detail: 'A live route requires the provider’s model ID.' })),
        ['The call could not start: A live route requires the provider’s model ID.', ['Open Settings'], 'error']);
    assert.deepEqual(show(ended('backpressure')), ['The call fell behind the voice stream', ['Try again'], 'error']);
    // The server's own action wins over the page's table; an action this page does not know offers nothing.
    assert.deepEqual(show(ended('engine_unavailable', { action: 'settings' })), ['Can’t reach Google’s voice service', ['Open Settings'], 'error']);
    assert.deepEqual(show(ended('provider_closed', { action: 'other_engine' })), ['Google closed the call', [], 'error']);
    // Settings opens where the step is: the key in Accounts → Google, the model in Models → Live calls.
    const target = (code) => callPresentation(ended(code)).actions.map(a => [a.tab, a.focus]);
    assert.deepEqual(target('missing_credential'), [['providers', 's-google']]);
    assert.deepEqual(target('authentication'), [['providers', 's-google']]);
    assert.deepEqual(target('engine_refused'), [['models', 's-call-model-google']]);
    assert.deepEqual(target('budget_exceeded'), [['behavior', '']]);
    // Nothing about another engine is left in the words.
    for (const code of Object.keys({ missing_credential: 1, authentication: 1, rate_limit: 1, engine_unavailable: 1, invalid_request: 1 }))
        assert.doesNotMatch(callPresentation(ended(code, { detail: 'x' })).title, /OpenAI/);
});

test('a reasonless or unknown ending still shows the capsule: the call ended, and what the room has', () => {
    for (const code of ['', 'finished']) {
        const p = callPresentation(ended(code));
        assert.deepEqual([p.mode, p.title, p.sub, p.small, p.actions], ['sheet', 'The call ended', 'The room has its transcript', '', []]);
    }
    const unknown = callPresentation(ended('a_code_from_the_future'));
    assert.deepEqual([unknown.mode, unknown.title, unknown.small], ['sheet', 'The call ended', 'a_code_from_the_future']);
    // Only an unknown code reaches the owner raw, in small print; known codes are words.
    for (const code of ['owner_bye', 'max_minutes', 'peers_left', 'budget_exceeded', 'missing_credential', 'authentication',
        'rate_limit', 'provider_closed', 'engine_unavailable', 'reconnect_failed',
        'preparation_failed', 'call_not_started', 'invalid_request', 'protocol', 'backpressure', 'provider_error',
        'engine_refused', 'socket_closed', 'socket_silent', 'microphone_busy', 'permission_denied', 'old_app']) {
        const p = callPresentation(ended(code));
        assert.doesNotMatch(p.title, /_/, code);
        assert.equal(p.small, '', code);
    }
});

test('what this page finds itself, and surfaces that cannot hold a call, get words and the step that helps', () => {
    const failed = (code, extra) => callPresentation(live({ state: 'failed', code, ...extra }));
    const denied = failed('permission_denied');
    assert.deepEqual([denied.mode, denied.tone, denied.light], ['sheet', 'notice', 'still']);  // words, not an alarm
    assert.deepEqual([failed('socket_silent').tone, failed('socket_silent').light], ['error', 'error']);
    assert.match(denied.title, /^Allow the microphone for Ouroboros in System Settings → Privacy & Security → Microphone \(or in the browser’s site settings\), then press Talk again\.$/);
    const old = failed('old_app');
    assert.match(old.title, /^This version of the Ouroboros app can’t use the microphone\. Talk works in your browser, or after you install the new app\.$/);
    assert.deepEqual(old.actions.map(a => [a.kind, a.label]), [['open_browser', 'Open in browser'], ['get_app', 'Get the new app']]);
    assert.deepEqual(failed('microphone_busy').actions.map(a => [a.kind, a.label]), [['reveal', 'Go to the call']]);
    assert.deepEqual([failed('socket_closed', { detail: 'Policy violation' }).title, failed('socket_closed').title],
        ['The call connection closed: “Policy violation”', 'The call ended unexpectedly']);
    assert.equal(failed('socket_silent').title, 'The call service did not answer');
    assert.match(failed('insecure').title, /HTTPS/);
    // An admission refusal keeps the server's words; the page adds the next step for its code.
    const refusal = failed('call_model_missing', { words: 'No live model is set for this engine in Settings (Models, Live calls).' });
    assert.deepEqual([refusal.title, refusal.actions.map(a => [a.kind, a.tab, a.focus])],
        ['No live model is set for this engine in Settings (Models, Live calls).', [['settings', 'models', 's-call-model-google']]]);
    const noKey = failed('missing_credential_google', { words: 'Calls need a Google AI key — add it in Settings' });
    assert.deepEqual([noKey.title, noKey.actions.map(a => [a.kind, a.focus])],
        ['Calls need a Google AI key — add it in Settings', [['settings', 's-google']]]);
    assert.deepEqual(failed('repo_writer_gate_closed', { words: 'Ouroboros is applying an update; try the call again in a moment.' })
        .actions.map(a => a.kind), ['retry']);
});

test('thinking runs from the owner\'s final words to the first heard answer, on either engine\'s order', () => {
    // Gemini: the owner's words stream, end, then the answer is named and heard.
    const gemini = createTalkFacts();
    gemini.caption({ speaker: 'owner', text: 'Hello', final: false });
    assert.deepEqual(gemini.facts, { ownerSpeaking: true, thinking: false, answered: false });
    gemini.caption({ speaker: 'owner', text: 'Hello there', final: true });
    assert.deepEqual(gemini.facts, { ownerSpeaking: false, thinking: true, answered: false });
    gemini.caption({ speaker: 'ouroboros', text: '', final: false });  // the answer is named before its audio
    assert.equal(gemini.facts.thinking, true);
    gemini.heard(true);
    assert.deepEqual(gemini.facts, { ownerSpeaking: false, thinking: false, answered: true });
    // The owner's final words can land after the answer began (Gemini's, observed live): no thinking after it.
    const late = createTalkFacts();
    late.voice();
    late.caption({ speaker: 'ouroboros', text: '', final: false });
    late.caption({ speaker: 'ouroboros', text: 'Forty-two.', final: true });
    late.caption({ speaker: 'owner', text: 'What is six times seven?', final: true });
    assert.equal(late.facts.thinking, false);
    // The next question starts a new answer.
    late.voice();
    late.caption({ speaker: 'owner', text: 'And eight times nine?', final: true });
    assert.equal(late.facts.thinking, true);
    // Words heard over the answer, or no words at all, are no question.
    const heardNow = createTalkFacts();
    heardNow.caption({ speaker: 'owner', text: 'mm', final: true }, true);
    assert.equal(heardNow.facts.thinking, false);
    const empty = createTalkFacts();
    empty.caption({ speaker: 'owner', text: '  ', final: true });
    assert.equal(empty.facts.thinking, false);
    // An answer that was never heard (text only) still ends thinking when it is complete.
    const silent = createTalkFacts();
    silent.caption({ speaker: 'owner', text: 'Hi', final: true });
    silent.caption({ speaker: 'ouroboros', text: 'Hi.', final: true });
    assert.equal(silent.facts.thinking, false);
});

class FakeNode {
    constructor(tag, doc) {
        Object.assign(this, { tagName: tag.toUpperCase(), ownerDocument: doc, children: [], parentNode: null,
            dataset: {}, className: '', ownText: '', attributes: {}, listeners: {}, offsetTop: 0, offsetHeight: 0 });
    }
    get classList() {
        const names = () => this.className.split(/\s+/).filter(Boolean);
        return {
            contains: name => names().includes(name),
            add: name => { if (!names().includes(name)) this.className = [...names(), name].join(' '); },
            remove: name => { this.className = names().filter(other => other !== name).join(' '); },
        };
    }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.children = []; this.ownText = String(value); }
    get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === this.ownerDocument.feed; }
    get lastChild() { return this.children.at(-1) || null; }
    get previousElementSibling() { const kids = this.parentNode?.children || []; return kids[kids.indexOf(this) - 1] || null; }
    append(...nodes) { for (const node of nodes) { node.remove(); node.parentNode = this; this.children.push(node); } }
    replaceChildren(...nodes) { for (const child of [...this.children]) child.remove(); this.append(...nodes); }
    insertBefore(node, before) { node.remove(); node.parentNode = this; this.children.splice(this.children.indexOf(before), 0, node); }
    remove() { if (!this.parentNode) return; this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; }
    querySelector(selector) {
        const name = selector.replace(/^\./, '');
        for (const child of this.children) {
            if (child.classList.contains(name)) return child;
            const deeper = child.querySelector(selector);
            if (deeper) return deeper;
        }
        return null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    click() { this.listeners.click?.(); }
}

function feedFixture({ live = () => 'c1' } = {}) {
    const doc = { createElement: tag => new FakeNode(tag, doc) };
    const feed = new FakeNode('div', doc);
    doc.feed = feed;
    Object.assign(feed, { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 });
    const listeners = {};
    const ws = { on(type, fn) { (listeners[type] ||= new Set()).add(fn); return () => listeners[type].delete(fn); },
        emit(type, data) { for (const fn of listeners[type] || []) fn(data); }, listeners };
    let liveCall = live;
    const cards = createCallCards({ feed, ws, doc, live: () => liveCall() });
    const card = () => feed.children.find(node => node.className === 'call-card');
    const lines = () => card()?.querySelector('.call-card-lines').children.map(node => [node.className, node.textContent]) || [];
    // A room row as the chat mounts it: a bubble with its identity stamped.
    const row = (role, text, data = {}, ts = 0) => {
        const node = new FakeNode('div', doc);
        node.className = `chat-bubble ${role}`;
        const sender = new FakeNode('div', doc);
        sender.className = 'sender';
        sender.textContent = role === 'user' ? 'voice' : 'Ouroboros';
        const message = new FakeNode('div', doc);
        message.className = 'message';
        message.textContent = text;
        node.append(sender, message);
        Object.assign(node.dataset, data, ts ? { ts: String(ts) } : {});
        feed.append(node);
        return node;
    };
    return { doc, feed, ws, cards, card, lines, row, setLive: fn => { liveCall = fn; } };
}

test('a call is one card of the room: its rows fold into it, its captions stream at its end', () => {
    const { feed, ws, cards, card, lines, row, setLive } = feedFixture();
    const typed = row('user', 'Typed before the call', { clientMessageId: 'web-1', taskId: 't0' });
    const typing = new FakeNode('div', feed.ownerDocument);
    typing.className = 'chat-bubble assistant typing-bubble';
    feed.append(typing);
    // The owner speaks before any row of the call exists: the card opens at the room's end, live and open.
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: 'o1', text: 'Hello', final: false });
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: 'o1', text: ', Ouroboros', final: false });
    assert.equal(feed.children.at(-1), typing);  // the typing indicator stays last
    assert.deepEqual([card().dataset.callCard, card().dataset.callOpen], ['live', '1']);
    assert.deepEqual(lines(), [['call-line owner call-caption', 'YouHello, Ouroboros']]);
    // The chat paints the owner's row (it listens first): the caption leaves, the row folds in as a line.
    const owner = row('user', 'Hello, Ouroboros!', { clientMessageId: 'call:c1:o1', taskId: 'call-task' }, 1000);
    ws.emit('chat', { type: 'chat', role: 'user', content: 'Hello, Ouroboros!', client_message_id: 'call:c1:o1' });
    cards.scan();
    assert.ok(owner.classList.contains('call-folded') && !typed.classList.contains('call-folded'));
    assert.deepEqual(lines(), [['call-line owner', 'YouHello, Ouroboros!']]);
    assert.equal(card().querySelector('.call-card-facts').textContent, 'Live · 1 line');
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: 'o1', text: 'Hello, Ouroboros!', final: true });
    assert.equal(cards.count(), 0);  // a late caption of a settled item stays out
    // Ouroboros's spoken narration of the same task folds in; a link it sent stays a room message.
    cards.update({ call_id: 'c1', speaker: 'ouroboros', item_id: 'local:turn:3', text: 'Forty', final: false });
    assert.equal(lines().at(-1)[1], 'OuroborosForty');
    const narration = row('assistant', '💬 Forty-two.', { systemType: 'model_narration', taskId: 'call-task' }, 61000);
    ws.emit('chat', { type: 'chat', role: 'assistant', spoken: { call_id: 'c1', item_id: 'local:turn:3' } });
    const link = row('assistant', 'Here is the link: https://example.com', { taskId: 'call-task' }, 62000);
    const work = new FakeNode('div', feed.ownerDocument);
    work.className = 'chat-live-card';
    work.dataset.taskId = 'call-task';
    feed.append(work);
    const elsewhere = row('assistant', '💬 A text turn narrates.', { systemType: 'model_narration', taskId: 'other' });
    cards.scan();
    assert.ok(narration.classList.contains('call-folded') && work.classList.contains('call-folded'));
    assert.ok(!link.classList.contains('call-folded') && !elsewhere.classList.contains('call-folded'));
    assert.deepEqual(lines(), [['call-line owner', 'YouHello, Ouroboros!'], ['call-line ouroboros', 'OuroborosForty-two.']]);
    assert.equal(feed.children.indexOf(card()), feed.children.indexOf(owner) - 1);  // the card stands where the call began
    // The call ends: its closing notice reads under the words, and the card folds to one line.
    row('system', 'Live call c1 ended (owner_bye).', { taskId: 'call-task' }, 63000);
    setLive(() => '');
    cards.finish();
    cards.scan();
    assert.deepEqual(lines().at(-1), ['call-line notice', 'Live call c1 ended (owner_bye).']);
    assert.deepEqual([card().dataset.callCard, card().dataset.callOpen], ['ended', '0']);
    assert.equal(card().querySelector('.call-card-facts').textContent, '1 min 2 s · 2 lines');  // to the closing notice
    card().querySelector('.call-card-head').click();
    assert.equal(card().dataset.callOpen, '1');
    cards.destroy();
    assert.ok(!card() && !owner.classList.contains('call-folded'));
    assert.equal(ws.listeners.chat.size + ws.listeners.open.size, 0);
});

test('a call already in the room\'s history is one closed card, and an older page of it joins the same card', () => {
    const { feed, card, lines, row, doc } = feedFixture({ live: () => '' });
    row('user', 'Earlier typed words', { clientMessageId: 'web-2', taskId: 't1' });
    const first = row('user', 'One', { clientMessageId: 'call:c7:a', taskId: 'call-7' }, 5000);
    row('assistant', '💬 Two', { systemType: 'model_narration', taskId: 'call-7' }, 50000);
    const cards = createCallCards({ feed, doc, live: () => '' });
    assert.deepEqual([card().dataset.callCard, card().dataset.callOpen], ['ended', '0']);
    assert.equal(feed.children.indexOf(card()), feed.children.indexOf(first) - 1);
    // Paging up mounts an older row of the same call above: the card moves to stand before it.
    const older = new FakeNode('div', doc);
    older.className = 'chat-bubble user';
    const message = new FakeNode('div', doc);
    message.className = 'message';
    message.textContent = 'Zero';
    older.append(message);
    Object.assign(older.dataset, { clientMessageId: 'call:c7:z', taskId: 'call-7', ts: '1000' });
    feed.insertBefore(older, feed.children[0]);
    cards.scan();
    assert.equal(feed.children.indexOf(card()), 0);
    assert.deepEqual(lines().map(([, text]) => text), ['YouZero', 'YouOne', 'OuroborosTwo']);
    assert.equal(card().querySelector('.call-card-facts').textContent, '49 s · 3 lines');
    cards.destroy();
});

test('what a room row is to a call, and how a call\'s span reads', () => {
    const calls = new Map([['call-task', 'c1']]);
    const node = (className, data) => ({ classList: { contains: name => className.split(' ').includes(name) }, dataset: data });
    assert.deepEqual(callMemberOf(node('chat-bubble user', { clientMessageId: 'call:c1:x', taskId: 'call-task' }), new Map()),
        { task: 'call-task', kind: 'owner', call: 'c1' });
    assert.equal(callMemberOf(node('chat-bubble assistant', { systemType: 'model_narration', taskId: 'call-task' }), new Map()), null);
    assert.deepEqual(callMemberOf(node('chat-bubble assistant', { systemType: 'model_narration', taskId: 'call-task' }), calls),
        { task: 'call-task', kind: 'ouroboros' });
    assert.equal(callMemberOf(node('chat-bubble user', { clientMessageId: 'web-1', taskId: 'call-task' }), calls), null);
    assert.equal(callMemberOf(node('chat-bubble assistant', { taskId: 'call-task' }), calls), null);  // a link it sent
    assert.deepEqual([callSpan(45_000), callSpan(60_000), callSpan(210_400)], ['45 s', '1 min', '3 min 30 s']);
});

test('a caption follows the newest message only for a reader who is there', () => {
    const { feed, cards } = feedFixture();
    feed.scrollTop = 600;  // at the bottom (1000 - 600 - 400 = 0)
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: '1', text: 'Hi', final: false });
    feed.scrollHeight = 1100;
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: '1', text: ' there', final: false });
    assert.equal(feed.scrollTop, 1100);
    feed.scrollTop = 100;  // reading older messages
    feed.scrollHeight = 1200;
    cards.update({ call_id: 'c1', speaker: 'owner', item_id: '1', text: ' again', final: false });
    assert.equal(feed.scrollTop, 100);
});

function lightFixture({ reduce = false, shown = true } = {}) {
    let time = 0;
    const frames = [];
    const props = {};
    const heard = [];
    let voices = 0;
    let visible = shown;
    const env = {
        performance: { now: () => time },
        requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
        cancelAnimationFrame: () => {},
        matchMedia: () => ({ matches: reduce }),
        document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    };
    const light = createCallLight({ env, visible: () => visible, heard: now => heard.push(now), voice: () => { voices += 1; } });
    light.attach({ style: { setProperty: (name, value) => { props[name] = Number(value); } } });
    light.setActive(true);
    const step = (ms = 16) => { time += ms; const run = frames.splice(0); for (const fn of run) fn(time); return run.length; };
    return { light, step, frames, props, heard, voices: () => voices, show: (value) => { visible = value; }, advance: ms => { time += ms; } };
}

test('the light answers the microphone at once and playback only when it leaves the speaker', () => {
    const f = lightFixture();
    f.light.level({ mic: 0.8, out: 0.7, playing: true, latency: 0.1 });
    assert.equal(f.voices(), 1);  // the owner's voice began
    f.step(16);
    assert.ok(f.props['--call-mic'] > 0.2, f.props['--call-mic']);  // the first frame already moves
    assert.equal(f.props['--call-out'], 0);
    assert.deepEqual(f.heard, []);
    f.step(50);
    assert.equal(f.props['--call-out'], 0);  // 66 ms: still inside the 100 ms output latency
    f.step(50);
    assert.deepEqual(f.heard, [true]);  // heard once it is due
    f.step(16);
    assert.ok(f.props['--call-out'] > 0.2);
    // Silence: the envelope releases slowly, then the loop stops instead of spinning.
    f.light.level({ mic: 0, out: 0, playing: false, latency: 0 });
    let frames = 0;
    for (let i = 0; i < 200 && f.step(16); i++) frames += 1;
    assert.ok(frames > 10 && frames < 200, frames);
    assert.deepEqual([f.props['--call-mic'], f.props['--call-out']], [0, 0]);
    assert.equal(f.frames.length, 0);
    assert.deepEqual(f.heard, [true, false]);
    // Echo while Ouroboros is heard is not the owner's voice.
    const echo = lightFixture();
    echo.light.level({ mic: 0, out: 0.7, playing: true, latency: 0 });
    echo.step(16);
    echo.light.level({ mic: 0.5, out: 0.7, playing: true, latency: 0 });
    assert.equal(echo.voices(), 0);
});

test('reduced motion shows whose voice in calm steps; an off-screen room runs no frames', () => {
    const calm = lightFixture({ reduce: true });
    calm.light.level({ mic: 0.9, out: 0, playing: false, latency: 0 });
    calm.step(16);
    assert.equal(calm.props['--call-mic'], 0.6);
    calm.light.level({ mic: 0.3, out: 0, playing: false, latency: 0 });
    calm.step(16);
    assert.equal(calm.props['--call-mic'], 0.6);  // no swell with loudness
    calm.light.level({ mic: 0, out: 0, playing: false, latency: 0 });
    calm.step(16);
    assert.equal(calm.props['--call-mic'], 0);
    assert.equal(calm.step(16), 0);
    const hidden = lightFixture({ shown: false });
    hidden.light.level({ mic: 0.9, out: 0.9, playing: true, latency: 0 });
    assert.equal(hidden.frames.length, 0);
    assert.deepEqual(hidden.heard, [true]);  // the words and the mark still know who is heard
    hidden.show(true);
    hidden.light.wake();
    assert.equal(hidden.frames.length, 1);
    // The call ends: stillness, nothing heard.
    hidden.light.setActive(false);
    assert.deepEqual([hidden.props['--call-mic'], hidden.props['--call-out']], [0, 0]);
    assert.deepEqual(hidden.heard, [true, false]);
});

test('the light moves by itself only in moving phases, and an ended call draws its still state once', () => {
    const drawn = [];
    let time = 0;
    const frames = [];
    const env = { performance: { now: () => time }, requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
        cancelAnimationFrame() {}, matchMedia: () => ({ matches: false }),
        document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} } };
    const light = createCallLight({ env, draw: state => drawn.push(state) });
    light.attach({ style: { setProperty() {} } });
    const step = () => { time += 16; const run = frames.splice(0); for (const fn of run) fn(time); return run.length; };
    light.setActive(true);
    light.setMode('listen', 'owner');
    for (let i = 0; i < 5; i++) assert.equal(step(), 1);  // a gentle wave while listening, with no sound at all
    assert.deepEqual([drawn.at(-1).mode, drawn.at(-1).voice], ['listen', 'owner']);
    light.setMode('muted', 'owner');
    assert.equal(step(), 1);
    assert.equal(step(), 0);  // muted and silent: still, the loop stops
    light.setActive(false);
    const before = drawn.length;
    light.setMode('error', '');
    assert.equal(drawn.length, before + 1);  // drawn at once, without a frame loop
    assert.deepEqual([drawn.at(-1).mode, drawn.at(-1).mic, drawn.at(-1).out], ['error', 0, 0]);
    assert.equal(frames.length, 0);
});

test('the mark stands for the live call only while its room is off screen, with the call\'s own time', () => {
    const entry = (active, onScreen) => ({ session: { active }, onScreen: () => onScreen });
    assert.equal(markedCall(new Set()), null);
    assert.equal(markedCall(new Set([entry(false, false)])), null);
    assert.equal(markedCall(new Set([entry(true, true)])), null);
    const away = entry(true, false);
    assert.equal(markedCall(new Set([entry(false, true), away])), away);
    assert.equal(callClock(0, 5000), '');
    assert.equal(callClock(1000, 1000), '00:00');
    assert.equal(callClock(1000, 1000 + 192_000), '03:12');
    assert.equal(callClock(0 + 1, 1 + 3_725_000), '1:02:05');
});

test('the visual is one swappable contract: a slot, its own row layers, the glow behind, a small sign', () => {
    const made = [];
    const node = (tag) => { const element = { tag, className: '', attrs: {}, innerHTML: '', removed: false, props: {},
        style: { setProperty(name, value) { element.props[name] = value; } },
        setAttribute(name, value) { this.attrs[name] = value; }, remove() { this.removed = true; } }; made.push(element); return element; };
    const strokes = [];
    const doc = { createElement: tag => {
        const element = node(tag);
        if (tag === 'canvas') element.getContext = () => ({ setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {},
            lineTo() {}, stroke() { strokes.push(this.strokeStyle); } });
        return element;
    } };
    const slot = { children: [], replaceChildren(...nodes) { this.children = nodes; } };
    const row = { offsetHeight: 50, appended: [], append(child) { this.appended.push(child); } };
    const wrap = { appended: [], append(child) { this.appended.push(child); } };
    const env = { devicePixelRatio: 3, getComputedStyle: () => ({ getPropertyValue: name => `colour(${name})` }),
        addEventListener() {}, removeEventListener() {} };
    const handle = CALL_VISUAL.mount({ slot, row, wrap, doc, env });
    const [canvas] = slot.children;
    assert.deepEqual([canvas.className, canvas.attrs['aria-hidden'], canvas.width, canvas.height], ['call-wave', 'true', 128, 60]);
    const [rim] = row.appended;
    assert.equal(rim.className, 'call-rim');
    for (const layer of ['trace', 'listen', 'owner', 'ouroboros']) assert.match(rim.innerHTML, new RegExp(`class="call-rim-${layer}"`), layer);
    const [aura] = wrap.appended;  // behind the row, so the row's frosted glass is lit from behind
    assert.equal(aura.className, 'call-aura');
    assert.match(aura.innerHTML, /call-aura-owner.*call-aura-ouroboros/);
    assert.equal(aura.props['--call-row-height'], '50px');
    handle.draw({ mic: 0.8, out: 0, dt: 0.1, time: 1, mode: 'listen', voice: 'owner', owner: true });
    assert.ok(parseFloat(rim.props['--call-spin']) > 0);
    assert.equal(rim.props['--call-rim-level'], '0.800');
    assert.deepEqual([...new Set(strokes)], ['colour(--call-wave-owner)']);
    const spin = rim.props['--call-spin'];
    handle.draw({ mic: 0.8, out: 0, dt: 0.1, time: 1.1, mode: 'muted', voice: 'owner' });
    assert.equal(rim.props['--call-spin'], spin);  // muted is still
    handle.draw({ mic: 0, out: 0.9, dt: 0.1, time: 1.2, mode: 'listen', voice: 'owner', reduced: true });
    assert.equal(rim.props['--call-spin'], spin);  // reduced motion holds the rim
    assert.equal(rim.props['--call-rim-level'], '0.000');
    const mark = CALL_VISUAL.mark(doc);
    assert.equal(mark.className, 'call-entry-mark');
    assert.equal((mark.innerHTML.match(/<i><\/i>/g) || []).length, 5);  // five bars of a voice, no lens
    handle.destroy();
    assert.equal(slot.children.length, 0);
    assert.deepEqual([rim.removed, aura.removed], [true, true]);
    assert.throws(() => { CALL_VISUAL.name = 'lens'; }, TypeError);
});

test('the wave: whose colour from the state, its height from the voice, a pulse while preparing or thinking', () => {
    const shape = (state) => { const { colour, amp, pulse } = waveShape(state); return [colour, Number(amp.toFixed(2)), pulse]; };
    assert.deepEqual(shape({ mode: 'listen', time: 0 }), ['idle', 0.07, false]);  // a gentle low wave while listening
    assert.deepEqual(shape({ mode: 'listen', mic: 0.7, owner: true }), ['owner', 0.7, false]);
    assert.deepEqual(shape({ mode: 'listen', mic: 0.7, out: 0.6, voice: 'ouroboros' }), ['ouroboros', 0.6, false]);  // no echo
    assert.deepEqual(shape({ mode: 'trace' }), ['idle', 0, true]);
    assert.deepEqual(shape({ mode: 'think' }), ['ouroboros', 0, true]);
    assert.deepEqual(shape({ mode: 'error', mic: 0.9 }), ['error', 0, false]);  // still, in amber
    assert.deepEqual(shape({ mode: 'muted', mic: 0.9, owner: true }), ['idle', 0, false]);
    assert.deepEqual(shape({ mode: 'muted', out: 0.5, voice: 'ouroboros' }), ['ouroboros', 0.5, false]);
    assert.deepEqual(shape({ mode: 'listen', mic: 0.7, owner: true, reduced: true }), ['owner', 0, false]);
    assert.deepEqual(shape({ mode: 'think', reduced: true }), ['ouroboros', 0, false]);
});
