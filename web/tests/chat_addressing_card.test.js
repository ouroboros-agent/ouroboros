import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { taskTerminalSummary } from '../modules/log_events.js';
import { installDom, restoreDom, walkCard } from './chat_dom_fixture.js';

const TS = '2026-09-12T12:00:00Z';
const TASK = 'ordinary-turn';

function fixture(history = []) {
    const { prior, mount } = installDom(async (url) => ({ ok: true, json: async () =>
        String(url).startsWith('/api/chat/history')
            ? { messages: history, window: { complete: true } }
            : { active_direct_turns: [] } }));
    const handlers = new Map();
    const ws = { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); },
        isConnected: () => true, send() {} };
    const instance = createChatInstance({ ws,
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
        updateUnreadBadge() {}, chatId: 1, idPrefix: 'chat', mountEl: mount,
        stateSnapshots: { begin: () => ({ generation: 1, requestedAt: Date.now() }),
            isCurrent: () => true, apply() {} },
    });
    const messages = document.byId.get('chat-messages');
    return { instance, messages,
        card: () => walkCard(messages, TASK),
        emit: (type, row) => handlers.get(type)({ chat_id: 1, ts: TS, ...row }),
        log: (row) => handlers.get('log')({ chat_id: 1, data: { task_id: TASK, ts: TS, ...row } }),
        close() { instance.destroy(); restoreDom(prior); },
    };
}

const ownerRow = { role: 'user', content: 'Please work on this', text: 'Please work on this',
    client_message_id: 'owner-message', ts: TS, chat_id: 1 };
const annotation = { annotation_type: 'routing_ack', client_message_id: 'owner-message',
    action: 'promote_chat_to_task', status: 'scheduled', target: 'managed-root',
    target_title: 'Requested work' };
const final = { task_id: TASK, role: 'assistant', content: 'The task is scheduled.',
    text: 'The task is scheduled.', task_terminal_status: 'completed', tool_calls: 1,
    outcome_axes: { execution: { status: 'ok' } }, reason_code: 'final_message',
    accounted_upper_bound_usd: 0.75, cost_final: true, cost_accounting_status: 'available' };

for (const tool of ['promote_chat_to_task', 'route_to_project', 'steer_task']) {
    test(`${tool}-only keeps the owner annotation without a live or terminal card`, () => {
        const f = fixture();
        try {
            f.emit('chat', ownerRow);
            f.log({ type: 'task_started' });
            f.log({ type: 'tool_call_started', tool });
            f.emit('message_annotation', { ...annotation, action: tool });
            f.log({ type: 'tool_call_finished', tool, is_error: false });
            assert.equal(Boolean(f.card()), false);
            const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
            assert.ok(owner?.querySelector('.msg-routing-annotation'), 'the receipt is on the original message');
            f.emit('chat', final);
            f.log({ ...final, type: 'task_done', status: 'completed' });
            assert.equal(Boolean(f.card()), false, 'terminal aggregate cannot reinterpret addressing as work');
            assert.equal(f.messages.children.filter((n) => n.classList.contains('assistant')
                && /The task is scheduled/.test(n.innerHTML)).length, 1, 'authored answer remains visible');
        } finally { f.close(); }
    });
}

test('read_file followed by promote reveals work and keeps the full reported count and cost', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'read_file' });
        const card = f.card();
        assert.ok(card);
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        f.emit('chat', { ...final, tool_calls: 2 });
        assert.equal(f.card(), card);
        assert.equal(card.dataset.finished, '1');
        assert.match(card.querySelector('[data-live-meta]').innerHTML, /2 tool calls/);
        assert.match(card.querySelector('[data-live-meta]').innerHTML, /\$0\.75/);
    } finally { f.close(); }
});

test('failed steer reveals a card through the ordinary typed error path', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'steer_task' });
        assert.equal(Boolean(f.card()), false);
        f.log({ type: 'tool_call_finished', tool: 'steer_task', is_error: true, error: 'Target unavailable' });
        assert.ok(f.card());
        f.log({ type: 'task_done', status: 'failed', reason_code: 'tool_failure',
            outcome_axes: { execution: { status: 'failed' } } });
        assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'error');
    } finally { f.close(); }
});

for (const type of ['task_metrics_event', 'task_eval']) {
    test(`late ${type} aggregate cannot reveal an addressing-only card`, () => {
        const f = fixture();
        try {
            f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
            f.emit('chat', final);
            f.log({ ...final, type, tool_calls: 1 });
            assert.equal(Boolean(f.card()), false);
            f.log({ ...final, type, tool_calls: 2, outcome_axes: undefined });
            assert.ok(f.card(), 'a larger aggregate still reveals previously unobserved work');
        } finally { f.close(); }
    });
}

test('a retired hidden turn is not reminted by a late terminal aggregate', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        f.emit('chat', final);
        t.mock.timers.tick(30001);
        f.log({ type: 'task_metrics_event', tool_calls: 1 });
        f.emit('chat', final);
        assert.equal(Boolean(f.card()), false);
        f.log({ type: 'tool_call_finished', tool: 'steer_task', is_error: true });
        assert.ok(f.card(), 'retirement withholds only aggregates, never a real error');
    } finally { f.close(); }
});

test('history rebuild preserves observed addressing before the final aggregate', async () => {
    const f = fixture([{ ...ownerRow, chat_annotation: annotation }]);
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        await f.instance.refreshHistory({ revision: 1 });
        f.log({ type: 'task_metrics_event', tool_calls: 1 });
        assert.equal(Boolean(f.card()), false);
        f.log({ type: 'task_metrics_event', tool_calls: 2 });
        assert.ok(f.card(), 'unobserved work remains visible after reconnect');
    } finally { f.close(); }
});

test('aggregate retains an addressing failure whose finish frame was missed offline', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'steer_task' });
        f.log({ type: 'task_metrics_event', tool_calls: 1, tool_errors: 1 });
        assert.ok(f.card(), 'the recorded tool error must remain visible');
    } finally { f.close(); }
});

test('ordinary authored progress and runtime failures stay visible', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        f.emit('chat', { task_id: TASK, role: 'assistant', is_progress: true, content: 'Inspecting the source.' });
        assert.ok(f.card(), 'real narration keeps its existing card');
        f.emit('chat', { role: 'system', system_type: 'terminal_incident', content: 'Provider outcome unknown.' });
        assert.ok(f.messages.children.some((node) => /Provider outcome unknown/.test(node.innerHTML)));
    } finally { f.close(); }
});

test('reload keeps addressing annotation and legacy answer without a synthetic card', async () => {
    const history = [{ ...ownerRow, chat_annotation: annotation },
        { ...final, ts: TS, chat_id: 1, ephemeral_decision: true }];
    const f = fixture(history);
    try {
        await f.instance.refreshHistory({ revision: 1 });
        assert.equal(Boolean(f.card()), false);
        assert.ok(f.messages.children.some((node) => /The task is scheduled/.test(node.innerHTML)));
        const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
        assert.ok(owner?.querySelector('.msg-routing-annotation'));
        await f.instance.refreshHistory({ revision: 2 });
        assert.equal(Boolean(f.card()), false);
        assert.equal(f.messages.children.filter((node) => node.dataset.clientMessageId === 'owner-message').length, 1);
    } finally { f.close(); }
});

test('an old ephemeral marker cannot manufacture terminal status', () => {
    assert.equal(taskTerminalSummary({ type: 'task_done', task_id: TASK, ephemeral_decision: true }).terminal, false);
    assert.equal(taskTerminalSummary({ type: 'task_done', task_id: TASK, status: 'completed' }).terminal, true);
});

for (const [label, counts, total, errors, visible] of [
    ['promotion only', { promote_chat_to_task: 1 }, 1, 0, false],
    ['several addressing calls', { promote_chat_to_task: 2, steer_task: 1 }, 3, 0, false],
    ['read and promote', { read_file: 1, promote_chat_to_task: 1 }, 2, 0, true],
    ['failed steering', { steer_task: 1 }, 1, 1, true],
    ['unknown tool is work', { future_tool: 1 }, 1, 0, true],
    ['incomplete counts', { promote_chat_to_task: 1 }, 2, 0, true],
    ['unknown errors', { promote_chat_to_task: 1 }, 1, null, true],
    ['legacy summary', undefined, 1, undefined, true],
]) {
    test(`cold history and late aggregate: ${label}`, async () => {
        const summary = { ...final, role: 'system', system_type: 'task_summary',
            text: 'Recorded task summary.', rounds: 2, tool_calls: total,
            ...(counts === undefined ? {} : { tool_call_counts: counts }),
            ...(errors === undefined ? {} : { tool_errors: errors }) };
        const f = fixture([{ ...ownerRow, chat_annotation: annotation }, final, summary]);
        try {
            await f.instance.refreshHistory({ revision: 1 });
            assert.equal(Boolean(f.card()), visible);
            f.log({ ...summary, type: 'task_metrics_event' });
            assert.equal(Boolean(f.card()), visible, 'late totals preserve the historical evidence');
            await f.instance.refreshHistory({ revision: 2 });
            assert.equal(Boolean(f.card()), visible);
            const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
            assert.ok(owner?.querySelector('.msg-routing-annotation'));
            assert.ok(f.messages.children.some((node) => /The task is scheduled/.test(node.innerHTML)));
            if (visible) assert.match(f.card().querySelector('[data-live-meta]').innerHTML, /\$0\.75/);
            assert.equal(summary.tool_calls, total, 'presentation never rewrites the accounting aggregate');
        } finally { f.close(); }
    });
}

test('cold client distinguishes late addressing metrics without prior tool-start frames', () => {
    const f = fixture();
    try {
        f.log({ type: 'task_metrics_event', tool_calls: 1, tool_errors: 0,
            tool_call_counts: { promote_chat_to_task: 1 } });
        assert.equal(Boolean(f.card()), false);
        f.log({ type: 'task_metrics_event', tool_calls: 2, tool_errors: 0,
            tool_call_counts: { promote_chat_to_task: 1, read_file: 1 } });
        assert.ok(f.card());
        assert.equal(f.card().dataset.finished, '0', 'live metrics alone do not conclude a turn');
    } finally { f.close(); }
});

test('recorded narration stays visible beside complete addressing-only counts', async () => {
    const f = fixture([
        { task_id: TASK, role: 'assistant', is_progress: true, text: 'Inspecting the source.', ts: TS },
        { ...final, role: 'system', system_type: 'task_summary', text: 'Recorded summary.',
            rounds: 2, tool_errors: 0, tool_call_counts: { promote_chat_to_task: 1 } },
    ]);
    try {
        await f.instance.refreshHistory({ revision: 1 });
        assert.ok(f.card());
    } finally { f.close(); }
});

test('a retired addressing turn still reveals an actual error in late metrics', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'steer_task' });
        f.emit('chat', final);
        t.mock.timers.tick(30001);
        f.log({ type: 'task_metrics_event', tool_calls: 1, tool_errors: 1,
            tool_call_counts: { steer_task: 1 } });
        assert.ok(f.card());
    } finally { f.close(); }
});

for (const [status, phase] of [['completed', 'done'], ['failed', 'error']]) {
    test(`late complete work evidence restores a retired ${status} turn`, (t) => {
        t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
        const f = fixture();
        try {
            f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
            f.emit('chat', final);
            t.mock.timers.tick(30001);
            f.log({ type: 'task_metrics_event',
                outcome_axes: { lifecycle: { status }, execution: { status: status === 'failed' ? 'failed' : 'ok' } },
                tool_calls: 2, tool_errors: 0,
                tool_call_counts: { promote_chat_to_task: 1, read_file: 1 },
                accounted_upper_bound_usd: 0.75, cost_final: true, cost_accounting_status: 'available' });
            assert.ok(f.card(), 'late proven work must remain visible');
            assert.equal(f.card().dataset.finished, '1');
            assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, phase);
            assert.match(f.card().querySelector('[data-live-meta]').innerHTML, /\$0\.75/);
        } finally { f.close(); }
    });
}

test('late delayed read start and complete metrics keep the known retired outcome', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        f.emit('chat', final);
        t.mock.timers.tick(30001);
        f.log({ type: 'tool_call_started', tool: 'read_file' });
        f.log({ type: 'task_metrics_event', tool_calls: 2, tool_errors: 0,
            tool_call_counts: { promote_chat_to_task: 1, read_file: 1 },
            outcome_axes: { lifecycle: { status: 'completed' }, execution: { status: 'ok' } },
            accounted_upper_bound_usd: 0.75, cost_final: true, cost_accounting_status: 'available' });
        assert.equal(f.card().dataset.finished, '1');
        assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'done');
        assert.match(f.card().querySelector('[data-live-meta]').innerHTML, /\$0\.75/);
    } finally { f.close(); }
});

test('late start and metrics preserve an existing retired failure', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'read_file' });
        const failed = { ...final, task_terminal_status: 'failed',
            outcome_axes: { execution: { status: 'failed' } } };
        f.emit('chat', failed);
        f.log({ ...failed, type: 'task_done', status: 'failed' });
        t.mock.timers.tick(120001);
        for (const event of [{ type: 'tool_call_started', tool: 'read_file' },
            { type: 'task_metrics_event', tool_calls: 2, tool_errors: 0,
                tool_call_counts: { read_file: 2 } }]) {
            f.log(event);
            assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'error');
            assert.equal(f.card().dataset.finished, '1');
            assert.match(f.card().querySelector('[data-live-meta]').innerHTML, /\$0\.75/);
        }
    } finally { f.close(); }
});

for (const counts of [undefined, { promote_chat_to_task: 1 }, { promote_chat_to_task: 2 }]) {
    test(`retired unknown or addressing-only metrics stay hidden: ${JSON.stringify(counts)}`, (t) => {
        t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
        const f = fixture();
        try {
            f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
            f.emit('chat', final);
            t.mock.timers.tick(30001);
            f.log({ type: 'task_metrics_event', tool_calls: 2, tool_errors: 0, tool_call_counts: counts });
            assert.equal(Boolean(f.card()), false);
        } finally { f.close(); }
    });
}
