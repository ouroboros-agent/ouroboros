import test from 'node:test';
import assert from 'node:assert/strict';
import { computeHydratedDirectActivities } from '../modules/chat_activity.js';
import { summarizeRoom } from '../modules/project_activity.js';
import { censusTaskPhase, desiredLiveCardPhase, setHistoricalUnavailable, setLiveCardPhase, setLiveCardTypingVisible, syncParkedPhase } from '../modules/task_phase_chip.js';
import { emptyWorkState, roomStatusView, workRowView } from '../modules/work_status.js';
import { readFileSync } from 'node:fs';
import { projection, room, work } from './work_fixture.js';

const hold = { label: 'Waiting for Project verification', reason: 'project_routing_fence_lookup_failed', detail: 'Authority is unreadable.' };
const held = { activity_id: 'same-id', chat_id: 7, kind: 'managed_task', phase: 'queued', project_admission_hold: hold };
// The server classifies a Project or task-scope hold as a wait whose reason is the hold's own label.
const heldWork = (row, label = hold.label) => work(row, { activity: 'waiting',
    because: [{ code: 'project_admission', text: label }] });
const inRoom = (activity, extra = {}) => ({ ...emptyWorkState(), room: room(activity, extra) });

test('a known non-Project wait keeps its scope label, on its row: the header says Waiting', () => {
    const scopeHold = { ...hold, label: 'Waiting for task scope verification' };
    const row = { ...held, chat_id: 1, project_admission_hold: scopeHold };
    assert.equal(roomStatusView(inRoom('waiting')).text, 'Waiting');
    assert.deepEqual([workRowView(heldWork(row, scopeHold.label)).word, workRowView(heldWork(row, scopeHold.label)).secondary],
        ['Waiting', scopeHold.label]);
});

test('a held task is stationary in Chat and the sidebar, its label beside the word', () => {
    assert.deepEqual(roomStatusView(inRoom('waiting')),
        { kind: 'online', text: 'Waiting', tone: 'held', showDots: false, dots: 'one', amber: '', mark: 'dot' });
    const sidebar = summarizeRoom({ ...room('waiting'), works: ['same-id'] }, [heldWork(held)]);
    assert.deepEqual(sidebar, { state: 'dot', motion: false, amber: false, word: 'Waiting', label: `Waiting · ${hold.label}` });
});

for (const [activity, extra, text, kind, showDots, tone] of [
    ['working', { transition: 'pausing' }, 'Pausing…', 'thinking', true, 'held'],
    ['paused', {}, 'Paused', 'online', false, 'held'],
    ['unknown', {}, 'Activity unconfirmed', 'online', false, 'unknown'],
    ['waiting', {}, 'Waiting', 'online', false, 'held'],
]) {
    test(`a held task's room says ${text}; a card's hold never sets the header`, () => {
        const view = roomStatusView(inRoom(activity, extra));
        assert.deepEqual([view.kind, view.text, view.tone, view.showDots], [kind, text, tone, showDots]);
        // Same-id recovery: the server's room works again.
        assert.equal(roomStatusView(inRoom('working')).text, 'Working');
    });
}

test('same-ID recovery clears the hold on its row; independent work and a pause remain truthful', () => {
    const activities = computeHydratedDirectActivities(new Map(), [held], 7);
    const recovered = computeHydratedDirectActivities(activities, [{ ...held, phase: 'working', project_admission_hold: undefined }], 7);
    assert.equal(recovered.get('same-id').project_admission_hold, undefined);
    assert.equal(roomStatusView(inRoom('working')).text, 'Working');
    const sibling = { ...held, activity_id: 'sibling', phase: 'working', project_admission_hold: undefined };
    const mixed = summarizeRoom({ ...room('working'), works: ['sibling', 'same-id'] }, [work(sibling), heldWork(held)]);
    assert.deepEqual([mixed.state, mixed.motion], ['working', true]);
    // One room word: a budget pause, the owner's Pause and a Restart hold all read Paused; the row says which.
    assert.equal(roomStatusView(inRoom('paused')).text, 'Paused');
});

test('the census phase (a Main portal\'s chip source) keeps a budget pause beside the Project wait, as the sidebar does', () => {
    const paused = { ...held, phase: 'budget_paused' };
    assert.equal(censusTaskPhase(paused, null).text, `Paused · ${hold.label}`);
    assert.equal(censusTaskPhase(paused, null).motion, false);
    assert.equal(censusTaskPhase({ ...held, phase: 'budget_pausing' }, null).text, `Pausing… · ${hold.label}`);
    assert.equal(censusTaskPhase({ ...held, phase: 'budget_pausing' }, null).motion, false);
    assert.equal(censusTaskPhase(held, null).text, hold.label);
    assert.equal(censusTaskPhase(paused, { status: 'cancelled' }).text, 'Stopped');
});

test('a held card with retained progress waits statically; Stop, terminal and same-ID recovery outrank it', () => {
    const card = { finished: false, isSubagent: false, root: { dataset: {} },
        phaseEl: { hidden: false, dataset: {}, attrs: {}, textContent: '', className: '',
            getAttribute(key) { return this.attrs[key]; }, setAttribute(key, value) { this.attrs[key] = value; } },
        phaseSecondaryEl: { hidden: true, textContent: '', isConnected: true },
        inlineTypingEl: { style: { display: '' }, isConnected: true } };
    setLiveCardPhase(card, 'working', 'Working', 'chat-live-phase working');  // replayed progress
    assert.equal(card.inlineTypingEl.style.display, '');
    assert.equal(setHistoricalUnavailable(card, false, hold.label), true);  // census restore
    assert.equal(card.phaseEl.textContent, hold.label);
    assert.equal(card.phaseEl.className, 'chat-live-phase warn');  // static amber, no pulse class
    assert.equal(card.phaseEl.dataset.phase, 'working');  // still unfinished, never a terminal phase
    assert.equal(card.inlineTypingEl.style.display, 'none');
    setLiveCardTypingVisible(card, true);  // a later typing writer cannot animate the wait
    assert.equal(card.inlineTypingEl.style.display, 'none');
    assert.equal(setHistoricalUnavailable(card, false), false);  // no census fact: the hold stays
    assert.equal(desiredLiveCardPhase({ ...card, cancelPendingPolicy: 'immediate' }).text, 'Cancelling…');
    assert.equal(desiredLiveCardPhase({ ...card, finished: true }, 'error').phase, 'error');
    assert.equal(setHistoricalUnavailable(card, false, ''), true);  // same-ID recovery
    assert.equal(card.phaseEl.textContent, 'Working');
    assert.equal(card.inlineTypingEl.style.display, '');
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(chat, /restoreCardActivity\(liveCardRecords\.get\(k\), v\.project_admission_hold\)/);
    assert.match(chat, /function restoreCardActivity\(record, held = \{\}\) \{\r?\n\s+if \(!setHistoricalUnavailable\(record, false, held\)\)/);
});


for (const [parkedPhase, phase, label] of [
    ['budget_paused', 'paused', 'Paused'], ['unknown', 'unknown', 'Activity unconfirmed'],
]) {
    for (const releaseFirst of ['Project', 'parked']) {
        test(`Project hold and ${parkedPhase} recover independently (${releaseFirst} first)`, () => {
            const attrs = new Map();
            const card = { finished: false, root: { dataset: {} },
                phaseEl: { hidden: false, dataset: {}, textContent: '', className: '',
                    getAttribute: key => attrs.get(key), setAttribute: (key, value) => attrs.set(key, value) },
                inlineTypingEl: { style: { display: '' }, isConnected: true } };
            setLiveCardPhase(card, 'working', 'Working', 'chat-live-phase working');
            assert.equal(card.inlineTypingEl.style.display, '');
            syncParkedPhase(card, parkedPhase);
            setHistoricalUnavailable(card, false, hold);
            assert.equal(card.parkedPhase, parkedPhase);
            assert.equal(card.projectHold, hold.label);
            assert.equal(card.projectHoldDetail, hold.detail);
            assert.equal(card.phaseEl.dataset.phase, phase);
            assert.ok(card.phaseEl.textContent.includes(label), 'the parked fact remains visible');
            setLiveCardTypingVisible(card, true);
            assert.equal(card.inlineTypingEl.style.display, 'none');

            if (releaseFirst === 'Project') {
                setHistoricalUnavailable(card, false, {});
                assert.equal(card.projectHold, '');
                assert.equal(card.projectHoldDetail, '');
                assert.equal(card.parkedPhase, parkedPhase, 'Project recovery cannot release another hold');
                assert.equal(card.phaseEl.dataset.phase, phase);
                assert.equal(card.phaseEl.textContent, label);
                setLiveCardTypingVisible(card, true);
                assert.equal(card.inlineTypingEl.style.display, 'none', 'the remaining parked phase stays still');
                syncParkedPhase(card, 'working');
            } else {
                syncParkedPhase(card, 'working');
                assert.equal(card.parkedPhase, '');
                assert.equal(card.projectHold, hold.label, 'a positive task phase cannot clear the Project hold');
                assert.equal(card.phaseEl.textContent, hold.label);
                assert.equal(card.phaseEl.className, 'chat-live-phase warn');
                setLiveCardTypingVisible(card, true);
                assert.equal(card.inlineTypingEl.style.display, 'none', 'the remaining Project hold stays still');
                setHistoricalUnavailable(card, false, {});
            }
            assert.equal(card.parkedPhase, '');
            assert.equal(card.projectHold, '');
            assert.equal(card.phaseEl.dataset.phase, 'working');
            assert.equal(card.phaseEl.textContent, 'Working');
            assert.equal(card.phaseEl.className, 'chat-live-phase working');
            assert.equal(card.inlineTypingEl.style.display, '', 'clearing both causes restores real activity');
        });
    }
}

test('nested wait keeps the host cause as text and clears it only on a recovery fact', () => {
    const card = { isSubagent: true };
    setHistoricalUnavailable(card, false, hold);
    assert.equal(card.projectHoldDetail, hold.detail);
    assert.equal(card.projectHold, hold.label);
    setHistoricalUnavailable(card, false);
    assert.equal(card.projectHoldDetail, hold.detail);
    const unconfirmed = { label: 'Waiting for previous run verification', reason: 'project_dispatch_unconfirmed',
        detail: 'The previous run cannot be confirmed; automatic recovery is not authorized.' };
    setHistoricalUnavailable(card, false, unconfirmed);
    assert.equal(desiredLiveCardPhase(card).text, unconfirmed.label);
    assert.equal(card.projectHoldDetail, unconfirmed.detail);
    setHistoricalUnavailable(card, false, {});
    assert.equal(card.projectHoldDetail, '');
    assert.equal(desiredLiveCardPhase(card).text, 'Working');
});

test('held children never vote on the room: the server room speaks whatever the cards hold', () => {
    const child = { groupId: 'child', isSubagent: true, root: { isConnected: true }, projectHold: hold.label };
    const state = (activity) => ({ ...inRoom(activity), works: [] });
    assert.equal(roomStatusView(state('waiting')).text, 'Waiting');
    assert.equal(roomStatusView(state('working')).text, 'Working', 'a held child under working work');
    assert.equal(child.projectHold, hold.label, 'its hold stays its own card\'s fact');
});

test('actual child queue hydration refreshes room status once and reconciles departure without polling reviews', async () => {
    const { createChatInstance } = await import('../modules/chat.js');
    const { installDom, restoreDom, walkCard } = await import('./chat_dom_fixture.js');
    const settle = () => new Promise(resolve => setImmediate(resolve));
    let queue = { pending: [], running: [{ id: 'child', task: { project_admission_hold: {} } }] };
    let connected = true;
    let detailStatus = 'running';
    const reads = [];
    const { prior, mount } = installDom(async url => {
        url = String(url);
        if (url === '/api/tasks?queue_only=1') return { ok: true, json: async () => ({ queue }) };
        if (url.startsWith('/api/tasks/')) {
            reads.push(url);
            return { ok: true, json: async () => ({ task_id: url.split('/').at(-1), status: detailStatus }) };
        }
        return { ok: true, json: async () => url.startsWith('/api/chat/history')
            ? { messages: [] } : { active_direct_turns: [] } };
    });
    const handlers = new Map();
    const ws = { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); },
        isConnected: () => connected, send() {} };
    let instance;
    try {
        instance = createChatInstance({ ws, state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
            updateUnreadBadge() {}, stateSnapshots: { gate: async () => ({}), isCurrent: () => true, apply() {} },
            chatId: 7, idPrefix: 'chat', mountEl: mount, asPanel: true });
        await instance.refreshHistory({ revision: 1 });
        const frame = (id, fields = {}) => handlers.get('chat')({ chat_id: 7, role: 'assistant', is_progress: true,
            task_id: id, content: 'Inspecting sources', ts: '2026-09-30T10:00:00Z', ...fields });
        frame('same-id');
        const childFrame = id => frame(id, { delegation_role: 'subagent', subagent_event: 'scheduled',
            subagent_task_id: id, parent_task_id: 'same-id', root_task_id: 'same-id', subagent_role: 'reader' });
        childFrame('child');
        // The server's room for the held root: a wait (its reason, the hold's label, is on the row).
        const snapshot = () => instance.hydrateStateSnapshot({ active_chat_activities: [held],
            active_chat_activities_complete: true, supervisor_ready: true,
            ...projection(7, [heldWork(held)], room('waiting')) });
        const status = () => globalThis.document.byId.get('chat-status').textContent;
        snapshot(); await settle();
        assert.equal(status(), 'Waiting', 'the room says what the server says');
        queue.pending = queue.running.splice(0);
        queue.pending[0].task.project_admission_hold = hold;
        snapshot();
        assert.equal(status(), 'Waiting', 'the snapshot finishes before its queue read');
        await settle();
        assert.equal(status(), 'Waiting', 'the child\'s queue read paints its card, never the room\'s header');
        const messages = globalThis.document.byId.get('chat-messages');
        const child = walkCard(messages, 'child');
        assert.equal(child.querySelector('[data-live-phase]').textContent, hold.label);
        connected = false; handlers.get('close')();
        snapshot(); await settle();
        assert.equal(child.querySelector('[data-live-phase]').textContent, 'Activity unconfirmed');
        assert.equal(child.querySelector('[data-live-phase]').dataset.motion, '0');
        connected = true;
        snapshot(); await settle();
        assert.equal(child.querySelector('[data-live-phase]').textContent, hold.label,
            'a fresh child queue read restores its own hold after reconnect');
        assert.equal(child.querySelector('[data-live-phase]').dataset.motion, '0');
        queue.pending[0].task.project_admission_hold = {};
        snapshot(); await settle();
        assert.equal(child.querySelector('[data-live-phase]').textContent, 'Queued');
        assert.equal(child.querySelector('[data-live-phase]').dataset.motion, '0');
        queue.pending[0].task.project_admission_hold = hold;
        snapshot(); await settle();
        frame('working-sibling');
        assert.equal(status(), 'Waiting', 'a card the projection does not list never sets the header');
        handlers.get('log')({ chat_id: 7, data: { type: 'task_done', task_id: 'working-sibling', status: 'cancelled' } });

        childFrame('review-child');
        queue.running.push({ id: 'review-child', task: { project_admission_hold: {} } });
        handlers.get('chat')({ chat_id: 7, role: 'system', system_type: 'skill_review', task_id: 'review-child',
            review_group: { surface: 'skill', id: 'task:review-child:alpha', presentation_owner_task_id: 'review-child',
                skill: 'alpha', status: 'clean', attempts: [{ job_id: 'job-child', skill: 'alpha', status: 'clean' }] } });
        for (let pass = 0; pass < 3; pass++) { snapshot(); await settle(); }
        assert.equal(reads.filter(url => url === '/api/tasks/review-child').length, 0,
            'an ordinary child review never joins root-census absence reconciliation');

        queue.pending = [];
        snapshot(); await settle();
        assert.equal(reads.filter(url => url === '/api/tasks/child').length, 1, 'held queue departure reads detail once');
        for (let pass = 0; pass < 3; pass++) { snapshot(); await settle(); }
        assert.equal(reads.filter(url => url === '/api/tasks/child').length, 1,
            'a current nonterminal answer retires missing membership without inventing hold recovery');
        assert.equal(child.querySelector('[data-live-phase]').textContent, hold.label);
        queue.pending = [{ id: 'child', task: { project_admission_hold: hold } }];
        snapshot(); await settle();
        queue.pending = [];
        detailStatus = 'cancelled';
        snapshot(); await settle();
        assert.equal(reads.filter(url => url === '/api/tasks/child').length, 2, 'a new queue departure is a new observation');
        assert.equal(child.dataset.finished, '1', 'terminal detail heals a missing terminal frame');
        for (let pass = 0; pass < 3; pass++) { snapshot(); await settle(); }
        assert.equal(reads.filter(url => url === '/api/tasks/child').length, 2);
    } finally { instance?.destroy(); restoreDom(prior); }
});
