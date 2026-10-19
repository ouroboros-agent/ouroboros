// The sidebar's activity marks read each Project room in the server's work projection (`rooms`): its mark,
// amber while something waits for the owner, and its word with the reason of its first work. The rooms
// here are the server's own output (fixtures/work_projection_cases.json).
import test from 'node:test';
import assert from 'node:assert/strict';
import {
    IDLE_SUMMARY,
    buildProjectActivityIndex,
    reconcileProjectRooms,
    summarizeRoom,
    waitingModel,
} from '../modules/project_activity.js';
import { SERVER_CASES } from './work_fixture.js';

// One server body whose case room is re-keyed to a Project chat id.
function body(name, chatId = 42, extra = {}) {
    const served = structuredClone(SERVER_CASES[name]);
    const rooms = Object.fromEntries(Object.values(served.rooms).map((room) => [String(chatId), room]));
    return { supervisor_ready: true, ...served, rooms, ...extra };
}
const summary = (name) => {
    const served = SERVER_CASES[name];
    return summarizeRoom(Object.values(served.rooms)[0], served.works);
};

test('the mark is the server room\'s: moving work, still queue, one dot, a ring; only work moves', () => {
    assert.deepEqual([summary('held_continue').state, summary('held_continue').motion], ['working', true]);
    assert.deepEqual([summary('paused_by_owner').state, summary('paused_by_owner').motion], ['dot', false]);
    assert.deepEqual([summary('sleeping').state, summary('model_access').state], ['dot', 'dot']);
    assert.deepEqual([summary('unknown').state, summary('unknown').motion], ['unknown', false]);
    const queued = summarizeRoom({ activity: 'queued', mark: 'still', turn: [], amber: false, works: [] }, []);
    assert.deepEqual([queued.state, queued.motion, queued.label], ['queued', false, 'Queued']);
    assert.equal(summarizeRoom({ activity: 'idle', mark: 'none' }), IDLE_SUMMARY);
    assert.equal(summarizeRoom(null), IDLE_SUMMARY);
});

test('amber is the owner\'s turn alone: questions and owner actions, never a Pause by the owner or Ouroboros', () => {
    assert.equal(summary('direct_question').amber, true);
    assert.equal(summary('spending_limit').amber, true);
    assert.equal(summary('model_access').amber, true);
    assert.equal(summary('paused_by_owner').amber, false);
    assert.equal(summary('paused_by_mind').amber, false);
    assert.equal(summary('held_continue').amber, false);
});

test('the label is the room word and the reason of its first work, in its short reason words', () => {
    assert.equal(summary('held_continue').label, 'Working · Continue starts when 3 helpers of the earlier attempt finish');
    assert.equal(summary('paused_by_owner').label, 'Paused · by you');
    assert.equal(summary('paused_by_mind').label, 'Paused · by Ouroboros');
    assert.equal(summary('sleeping').label, 'Waiting · sleeping');
    assert.equal(summary('direct_question').label, 'Waiting for your answer', 'the word already says the question');
    assert.equal(summary('queued_and_pausing').label, 'Pausing… · by you');
    assert.equal(summary('unknown').label, 'Activity unconfirmed');
});

test('the accessible name says what waits for the owner whenever the word does not: amber is never colour alone', () => {
    assert.equal(summary('spending_limit').label, 'Paused · spending limit · Something here waits for you');
    assert.equal(summary('model_access').label,
        'Waiting · The model needs you: sign in or restore access · Something here waits for you');
    // A question in one work while another works: the room's word is Working, and the question is said beside it.
    const room = { activity: 'working', transition: 'finalizing', call: '', mark: 'moving', amber: true, works: ['f', 'q'],
        turn: [{ kind: 'question', task_id: 'q', quiz_id: 'q1' }] };
    assert.equal(summarizeRoom(room, []).label, 'Finalizing… · A question waits for your answer');
    // Nothing waits for the owner, or the word already says the question: nothing is added.
    assert.equal(summarizeRoom({ ...room, amber: false, turn: [] }, []).label, 'Finalizing…');
    assert.equal(summary('direct_question').label, 'Waiting for your answer');
});

test('a complete ready body clears absent rooms; a partial, unready or unavailable one keeps them unconfirmed', () => {
    const first = reconcileProjectRooms(new Map(), body('held_continue'));
    assert.equal(first.complete, true);
    assert.equal(first.rooms.get('42').state, 'working');
    for (const data of [
        { ...body('held_continue'), works: [], rooms: {}, works_complete: false },
        { ...body('held_continue'), works: [], rooms: {}, supervisor_ready: false },
        { ...body('held_continue'), works: [], rooms: {}, works_complete: 1 },
        { works_complete: true, supervisor_ready: true, rooms: {} },  // no works array
        {}, undefined,
    ]) {
        const next = reconcileProjectRooms(first.rooms, data);
        assert.equal(next.complete, false, JSON.stringify(data));
        assert.deepEqual(next.rooms.get('42'), { state: 'unknown', motion: false, amber: false,
            word: 'Activity unconfirmed', label: 'Activity unconfirmed' }, 'kept, unconfirmed and still');
    }
    const empty = reconcileProjectRooms(first.rooms, { supervisor_ready: true, works: [], rooms: {}, works_complete: true });
    assert.equal(empty.rooms.size, 0);
});

test('a partial body marks only its omissions unknown; a room it lists describes itself; inputs stay untouched', () => {
    const initial = reconcileProjectRooms(new Map(), body('held_continue', 42));
    const partialBody = { ...body('paused_by_owner', 43), works_complete: false };
    const partial = reconcileProjectRooms(initial.rooms, partialBody);
    assert.equal(partial.rooms.get('42').state, 'unknown');
    assert.deepEqual([partial.rooms.get('43').state, partial.rooms.get('43').label], ['dot', 'Paused · by you']);
    assert.equal(initial.rooms.get('42').state, 'working', 'the previous map is not mutated');
    const restored = reconcileProjectRooms(partial.rooms, body('held_continue', 42));
    assert.equal(restored.rooms.get('42').motion, true);
    assert.equal(restored.rooms.has('43'), false, 'a complete body proves the paused room idle once it is not listed');
});

test('the index reads Project rooms only; the collapsed header shows the strongest mark, amber when any is', () => {
    const rooms = new Map([
        ['42', summary('paused_by_owner')], ['43', summary('spending_limit')], ['1', summary('held_continue')],
    ]);
    const index = buildProjectActivityIndex(rooms, [42, 43, 44]);
    assert.deepEqual([...index.byChat.keys()], ['42', '43']);
    assert.deepEqual([index.aggregate.state, index.aggregate.motion, index.aggregate.amber, index.aggregate.label],
        ['dot', false, true, 'Paused'], 'Main\'s own room never moves the Projects header');
    const moving = buildProjectActivityIndex(new Map([['42', summary('held_continue')], ['43', summary('unknown')]]), ['42', '43']);
    assert.deepEqual([moving.aggregate.state, moving.aggregate.motion, moving.aggregate.label],
        ['working', true, 'Working · Activity unconfirmed']);
    assert.equal(buildProjectActivityIndex(new Map(), [42]).aggregate, IDLE_SUMMARY);
    assert.equal(buildProjectActivityIndex().aggregate, IDLE_SUMMARY);
});

test('model waits use the existing current-attempt rule (Activity reads it)', () => {
    const row = { phase: 'working', task_attempt: 2,
        model_waits: { previous: { wait_id: 'previous', revision: 1, task_attempt: 1, state: 'waiting', reason: 'quota' } } };
    assert.equal(waitingModel(row), false);
    row.model_waits.current = { wait_id: 'current', revision: 1, task_attempt: 2, state: 'waiting', reason: 'auth' };
    assert.equal(waitingModel(row), true);
    row.model_waits.current.state = 'resolved';
    assert.equal(waitingModel(row), false);
    // A malformed wait row (no id, revision or reason) is dropped exactly as the chat card drops it.
    assert.equal(waitingModel({ task_attempt: 1, model_waits: { w: { state: 'waiting', task_attempt: 1 } } }), false);
});
