import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { ElementStub, installDom, restoreDom, walkCard } from './chat_dom_fixture.js';
// Preserve the production test fixture, adding actual descendant lookup for
// lazily inserted controls, which the original flat fixture does not model.
const originalQuery = ElementStub.prototype.querySelector;
ElementStub.prototype.querySelector = function(selector) {
    const direct = originalQuery.call(this, selector);
    if (direct) return direct;
    for (const child of this.children) { const found = child.querySelector(selector); if (found) return found; }
    return null;
};
function makeInstance(rows, details = {}) {
    const env = installDom(async url => String(url).startsWith('/api/tasks/')
        ? (details[String(url).split('/').at(-1)] ? { ok: true, json: async () => details[String(url).split('/').at(-1)] }
            : { ok: false, status: 404, json: async () => ({error: 'missing'}) })
        : { ok: true, json: async () => String(url).startsWith('/api/chat/history')
            ? {messages: rows} : {active_direct_turns: []} });
    const handlers = new Map();
    const instance = createChatInstance({
        ws: {on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); }, isConnected: () => true, send() {}},
        state: {activePage: 'chat', projectChatIds: new Set(), unreadCount: 0}, updateUnreadBadge() {},
        stateSnapshots: {begin: () => ({generation: 1, requestedAt: Date.now()}), isCurrent: () => true, apply() {}},
        chatId: 1, idPrefix: 'chat', mountEl: env.mount,
    });
    return {...env, handlers, instance, card: id => walkCard(globalThis.document.byId.get('chat-messages'), id)};
}
test(' current activity restores previously attested Stop after cold unconfirmed replay', async () => {
    const fx = makeInstance([{task_id: 'live', is_progress: true, text: 'Working on the report',
        cancelable: true, ts: '2026-09-09T09:00:00Z'}]);
    try {
        await fx.instance.refreshHistory({revision: 1});
        const card = fx.card('live');
        assert.equal(card.querySelector('[data-live-phase]').hidden, true);
        assert.equal(card.querySelector('[data-cancel-run]'), null);
        fx.instance.hydrateStateSnapshot({active_chat_activities: [{activity_id: 'live', chat_id: 1, kind: 'managed_task', phase: 'working'}],
            active_chat_activities_complete: true, supervisor_ready: true}, Infinity, 2);
        assert.equal(card.querySelector('[data-live-phase]').hidden, false);
        assert.ok(card.querySelector('[data-cancel-run]'), 'positive current activity restores existing control authority');
        fx.handlers.get('chat')({chat_id: 1, task_id: 'live', role: 'assistant', is_progress: true,
            cancelable: true, content: 'Continued real work'});
        assert.ok(card.querySelector('[data-cancel-run]'), 'fresh positive evidence must restore Stop');
    } finally {fx.instance.destroy(); restoreDom(fx.prior);}
});
test(' retained cancelled fact outranks untyped historical answer after result quarantine', async () => {
    const historical = {status: 'cancelled', phase: 'cancelled', ts: '2026-09-09T09:02:00Z', provenance: 'canonical_task_result_after_finalization'};
    const fx = makeInstance([
        {task_id: 'past', is_progress: true, text: 'Work before cancellation', ts: '2026-09-09T09:00:00Z', historical_terminal: historical},
        {task_id: 'past', role: 'assistant', text: 'Preserved partial answer', ts: '2026-09-09T09:02:00Z', historical_terminal: historical},
    ]);
    try {
        await fx.instance.refreshHistory({revision: 1});
        fx.instance.hydrateStateSnapshot({active_chat_activities: [], active_chat_activities_complete: false, supervisor_ready: true}, Infinity, 1);
        assert.equal(fx.card('past').dataset.finished, '0');
        fx.instance.hydrateStateSnapshot({active_chat_activities: [], active_chat_activities_complete: true, supervisor_ready: true}, Infinity, 2);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(fx.card('past').querySelector('[data-live-phase]').textContent, 'Cancelled');
    } finally {fx.instance.destroy(); restoreDom(fx.prior);}
});
for (const via of ['log', 'detail']) test(`child terminal ${via} retains the producer model observation`, async () => {
    const observation = {source: 'usable_solve_response', used_model: 'fallback', requested_model: 'initial',
        used_local: false, requested_use_local: false, llm_call_id: 'call', provider: 'openrouter'};
    const fx = makeInstance([], {child: {task_id: 'child', status: 'completed', model_execution: observation}});
    try {
        await fx.instance.refreshHistory({revision: 1});
        fx.handlers.get('chat')({chat_id: 1, role: 'assistant', is_progress: true, content: 'child working',
            task_id: 'child', subagent_task_id: 'child', parent_task_id: 'root', delegation_role: 'subagent',
            subagent_role: 'reader', subagent_event: 'running', model: 'initial'});
        fx.handlers.get('log')({chat_id: 1, data: {type: 'task_done', task_id: via === 'log' ? 'child' : 'root', status: 'completed',
            ...(via === 'log' ? {model_execution: observation} : {})}});
        await new Promise(resolve => setImmediate(resolve));
        assert.match(fx.card('child').querySelector('[data-live-meta]').innerHTML, /Last solve response: fallback/);
    } finally {fx.instance.destroy(); restoreDom(fx.prior);}
});
