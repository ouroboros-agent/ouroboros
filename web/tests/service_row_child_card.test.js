// #1868 R6: a service-finalization checkpoint (`task_checkpoint` carrying the host's own
// `card_row_id`) registers ONCE on the card it belongs to — a helper's card included — whatever
// the order of the helper's terminal row and the service event, live and on reload. Its key is
// the host's own row id (`cardrow|service:…`): it never revives a finished card, never repaints
// its phase, title or meta, and never adds a second row on redelivery. Everything below drives
// the real Chat module.
import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { ElementStub, installDom, restoreDom, walkCard } from './chat_dom_fixture.js';

const ROOT = 'root-turn';
const KID = 'kid-turn';
const LINE = 'Background service stopped: paper (abc1234567) · at task end · log saved';
const LATE_WORDS = 'late words after the child ended';
const T = (n) => `2026-11-20T10:00:0${n}Z`;
const SERVICE = { role: 'system', system_type: 'task_checkpoint', checkpoint_kind: 'services_stopped',
    text: LINE, content: LINE, detail: LINE, card_row_id: 'service:launch-abc1234567' };
const CHILD = { delegation_role: 'subagent', subagent_task_id: KID, parent_task_id: ROOT, subagent_role: 'scout' };
const childFrame = (patch = {}) => ({ role: 'assistant', is_progress: true, task_id: KID, ts: T(1), ...CHILD, ...patch });
const serviceRow = (patch = {}) => ({ ...SERVICE, task_id: KID, ts: T(3), ...CHILD, ...patch });
const rootFrame = (patch = {}) => ({ task_id: ROOT, role: 'assistant', is_progress: true, content: 'Root working', ts: T(0), ...patch });

// The flat fixture's querySelector does not descend; the card's chip, title and meta do.
const originalQuery = ElementStub.prototype.querySelector;
after(() => { ElementStub.prototype.querySelector = originalQuery; });
ElementStub.prototype.querySelector = function (selector) {
    const direct = originalQuery.call(this, selector);
    if (direct) return direct;
    for (const child of this.children) { const found = child.querySelector(selector); if (found) return found; }
    return null;
};

function fixture(history = []) {
    let historyReads = 0;
    const env = installDom(async (url) => {
        const isHistory = String(url).startsWith('/api/chat/history');
        if (isHistory) historyReads++;
        return { ok: true, json: async () => (isHistory
            ? { messages: history, window: { complete: true } } : { active_direct_turns: [] }) };
    });
    const handlers = new Map();
    let generation = 0;
    const instance = createChatInstance({
        ws: { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); }, isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
        updateUnreadBadge() {}, chatId: 1, idPrefix: 'chat', mountEl: env.mount,
        stateSnapshots: { begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
            gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {} },
    });
    const messages = document.byId.get('chat-messages');
    const card = (id) => walkCard(messages, id);
    // A card's own rows: a nested child card's lines belong to the child, never to its parent.
    const owned = (node, root) => (node?.children || []).flatMap((child) => (child !== root && child.classList?.contains('chat-live-card')
        ? [] : [...(child.classList?.contains('chat-live-line') ? [child] : []), ...owned(child, root)]));
    return {
        instance, messages, card,
        historyReads: () => historyReads,
        rows: (id) => owned(card(id), card(id)),
        said: (id, text = LINE) => owned(card(id), card(id)).filter((row) => row.innerHTML.includes(text)),
        phase: (id) => card(id)?.querySelector('[data-live-phase]')?.textContent || '',
        title: (id) => card(id)?.querySelector('[data-live-title]')?.textContent || '',
        meta: (id) => card(id)?.querySelector('[data-live-meta]')?.innerHTML || '',
        chat: (row) => handlers.get('chat')({ chat_id: 1, ...row }),
        fire: (type, row) => handlers.get(type)({ chat_id: 1, ...row }),
        log: (row) => handlers.get('log')({ chat_id: 1, data: row }),
        close() { instance.destroy(); restoreDom(env.prior); },
    };
}

const runningChild = () => childFrame({ content: 'Reading the spec.', narration: true });
const terminalChild = () => childFrame({ ts: T(2), subagent_event: 'completed', status: 'completed', result: 'Child result' });

// The live order that loses the fact: the helper's terminal row lands first.
test('a service row landing after the helper\'s terminal row registers once on its card', () => {
    const f = fixture();
    try {
        f.chat(rootFrame());
        f.chat(runningChild());
        f.chat(terminalChild());
        const card = f.card(KID);
        assert.ok(card, 'the child frames mint the child card');
        assert.equal(card.dataset.finished, '1', 'the terminal frame finishes the child card');
        const [phase, title, meta] = [f.phase(KID), f.title(KID), f.meta(KID)];
        f.log(serviceRow());
        assert.equal(f.said(KID).length, 1, 'the service fact is one row of the child card');
        f.log(serviceRow());
        assert.equal(f.said(KID).length, 1, 'redelivery adds nothing');
        assert.equal(f.card(KID).dataset.finished, '1', 'the card stays finished');
        assert.deepEqual([f.phase(KID), f.title(KID), f.meta(KID)], [phase, title, meta],
            'the row states no phase, title or meta of its own');
        assert.equal(f.said(ROOT).length, 0, 'the fact stays on the card its task names');
    } finally { f.close(); }
});

// The other order must land the same ONE row.
test('the service row arriving before the terminal frame is the same one row', () => {
    const f = fixture();
    try {
        f.chat(rootFrame());
        f.chat(runningChild());
        f.log(serviceRow());
        assert.equal(f.said(KID).length, 1, 'a running card takes the row');
        f.chat(terminalChild());
        assert.equal(f.said(KID).length, 1, 'the terminal frame neither duplicates nor drops it');
        assert.equal(f.card(KID).dataset.finished, '1');
    } finally { f.close(); }
});

// A reopened room reads the same ONE row, whichever order the stored rows carry.
for (const order of ['terminal-first', 'service-first']) {
    test(`a reloaded helper card reads one service row (${order})`, async () => {
        const stored = {
            'terminal-first': [runningChild(), terminalChild(), serviceRow()],
            'service-first': [runningChild(), serviceRow(), terminalChild()],
        }[order].map((row, index) => ({ ...row, chat_id: 1, is_progress: true,
            history_id: `progress:${index + 1}`, history_position: { source: 'progress', offset: index + 1 } }));
        const f = fixture([{ ...rootFrame(), chat_id: 1, history_id: 'progress:0', history_position: { source: 'progress', offset: 0 } }, ...stored]);
        try {
            await f.instance.refreshHistory({ revision: 1 });
            assert.equal(f.said(KID).length, 1, 'one row after the reload');
            assert.equal(f.card(KID)?.dataset.finished, '1', 'the stored terminal settles the card');
            f.fire('open', { previouslyConnected: true });
            await new Promise(setImmediate);
            await new Promise(setImmediate);
            assert.ok(f.historyReads() >= 2, 'the reconnect refetched history');
            assert.equal(f.said(KID).length, 1, 'the refetch keeps one row');
        } finally { f.close(); }
    });
}

// A late row on a finished ROOT card (the R5 card) registers by the same rule, live.
test('a late service row registers once on a finished root card too', () => {
    const f = fixture();
    try {
        f.chat(rootFrame());
        f.log({ task_id: ROOT, ts: T(1), type: 'task_done', status: 'completed' });
        assert.equal(f.card(ROOT).dataset.finished, '1');
        f.log({ ...SERVICE, task_id: ROOT, ts: T(3) });
        assert.equal(f.said(ROOT).length, 1, 'the host-keyed row still registers on the closed card');
        assert.equal(f.phase(ROOT), 'Done', 'and the card keeps its own phase');
        f.log({ ...SERVICE, task_id: ROOT, ts: T(3) });
        assert.equal(f.said(ROOT).length, 1, 'redelivery adds nothing');
    } finally { f.close(); }
});

// The preserved rule: an ordinary late frame still never revives a terminal child.
test('an ordinary late checkpoint or progress frame does not revive the finished helper card', () => {
    const f = fixture();
    try {
        f.chat(rootFrame());
        f.chat(runningChild());
        f.chat(terminalChild());
        const before = f.rows(KID).length;
        f.log({ task_id: KID, ts: T(4), ...CHILD, type: 'task_checkpoint', checkpoint_kind: 'context_view', status: 'no_op' });
        f.log({ task_id: KID, ts: T(5), ...CHILD, type: 'send_message', is_progress: true, content: LATE_WORDS });
        assert.equal(f.rows(KID).length, before, 'no row is added');
        assert.equal(f.said(KID, 'Context inspected').length, 0);
        assert.equal(f.said(KID, LATE_WORDS).length, 0);
        assert.equal(f.card(KID).dataset.finished, '1');
    } finally { f.close(); }
});
