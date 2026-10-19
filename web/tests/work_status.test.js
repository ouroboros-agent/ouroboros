// One work, one status in the browser (work_status.js) over the server's real projection: the cases in
// fixtures/work_projection_cases.json are what gateway/work_projection.works_snapshot builds for them
// (tests/test_work_projection_web_fixture.py keeps the two in step). The client says those facts in
// short words, keeps what an incomplete body omitted, and lays each work's controls on the shared doors.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { applyPayload, CODE_PREFIX, currentPayload, setMissTransport } from '../modules/i18n.js';
import {
    ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_RESUME, ACTION_STOP_EARLIER, ACTION_STOP_NOW,
    TASK_CONTROL_LABELS, requestStopAll,
} from '../modules/task_control_menu.js';
import { pausePhaseLabel, taskActivityPresentation } from '../modules/task_phase_chip.js';
import {
    activityWord, applyCardWorkViews, cardControls, cardMenuActions, cardWorkView, emptyWorkState,
    ownStopPress, reasonBeside, reasonText, reconcileWorkState, resumeTargets, roomStatusView, workControls,
    workMenuActions, workQuestion, workRowView,
} from '../modules/work_status.js';
import { SERVER_REASONS } from './work_fixture.js';

setMissTransport(() => Promise.resolve(null));
const CASES = JSON.parse(readFileSync(new URL('./fixtures/work_projection_cases.json', import.meta.url), 'utf8')).cases;
// One /api/state body for a case, as the server sends it (supervisor ready unless said otherwise).
const body = (name, extra = {}) => ({ supervisor_ready: true, ...structuredClone(CASES[name]), ...extra });
const work = (name, index = 0) => structuredClone(CASES[name].works[index]);
const stateOf = (name, chatId = 42, extra = {}) => reconcileWorkState(emptyWorkState(), body(name, extra), chatId);

test('the room says one short word for every activity, transition and exact call', () => {
    const words = {
        held_continue: 'Working', working_call: 'Sending', paused_by_owner: 'Paused', spending_limit: 'Paused',
        model_access: 'Waiting', sleeping: 'Waiting', queued_and_pausing: 'Pausing…', finalizing_failed: 'Finalizing…',
        unknown: 'Activity unconfirmed', ended_with_runs: 'Working',
    };
    for (const [name, word] of Object.entries(words)) assert.equal(roomStatusView(stateOf(name)).text, word, name);
    // A waiting room whose turn is a question says so in its word; its ink is the amber (no second mark).
    const asked = roomStatusView(stateOf('direct_question', 1));
    assert.deepEqual([asked.text, asked.tone, asked.amber, asked.dots], ['Waiting for your answer', 'wait', '', 'one']);
    for (const [call, word] of [['preparing', 'Preparing'], ['thinking', 'Thinking'], ['working', 'Working'], ['', 'Working']]) {
        assert.equal(activityWord({ activity: 'working', transition: 'none', call }), word, call);
    }
    assert.equal(activityWord({ activity: 'queued' }), 'Queued');
    assert.equal(activityWord({ activity: 'idle' }), 'Online');
    assert.equal(activityWord({ activity: 'something new' }), 'Activity unconfirmed');
});

test('the mark follows the server: moving work, still queue, one dot, a ring; amber only for the owner', () => {
    const view = (name, chatId = 42) => roomStatusView(stateOf(name, chatId));
    assert.deepEqual([view('held_continue').dots, view('held_continue').tone, view('held_continue').amber], ['moving', 'live', '']);
    assert.equal(view('held_continue').showDots, true);
    assert.deepEqual([view('paused_by_owner').dots, view('paused_by_owner').tone, view('paused_by_owner').amber], ['one', 'held', '']);
    assert.equal(view('paused_by_mind').amber, '', "Ouroboros's own Pause is not amber");
    assert.deepEqual([view('spending_limit').tone, view('spending_limit').amber], ['held', 'action']);
    assert.deepEqual([view('model_access').tone, view('model_access').amber], ['held', 'action']);
    assert.deepEqual([view('unknown').tone, view('unknown').mark], ['unknown', 'ring']);
    const pausing = roomStatusView(stateOf('queued_and_pausing'));
    assert.deepEqual([pausing.text, pausing.tone, pausing.dots], ['Pausing…', 'held', 'moving'],
        'a Pause still settling: neutral ink, the server\'s moving mark');
    const queued = roomStatusView({ room: { activity: 'queued', mark: 'still', turn: [], amber: false } });
    assert.deepEqual([queued.text, queued.dots, queued.tone], ['Queued', 'still', 'queued']);
    // A working room with a question elsewhere keeps its word and turns its moving mark amber.
    const mixed = roomStatusView({ room: { activity: 'working', mark: 'moving', amber: true, call: '',
        turn: [{ kind: 'question', task_id: 'x', quiz_id: 'q' }] } });
    assert.deepEqual([mixed.text, mixed.dots, mixed.amber], ['Working', 'moving', 'question']);
});

test('only the client facts overlay the room: socket closed, own send, start, own Stop press', () => {
    const working = stateOf('held_continue');
    assert.deepEqual(roomStatusView(working, { connected: false }),
        { kind: 'offline', text: 'Reconnecting…', tone: 'offline', dots: 'one', showDots: false, amber: '', mark: '' });
    const idle = emptyWorkState();
    assert.equal(roomStatusView(idle).text, 'Online');
    assert.deepEqual([roomStatusView(idle, { ready: false }).text, roomStatusView(idle, { ready: false }).tone],
        ['Starting…', 'starting']);
    // The owner's own unconfirmed send outranks a quiet room, never work the room already does.
    assert.equal(roomStatusView(idle, { sending: true }).text, 'Sending');
    assert.equal(roomStatusView(stateOf('paused_by_owner'), { sending: true }).text, 'Sending');
    assert.equal(roomStatusView(working, { sending: true }).text, 'Working');
    // The owner's own Stop press on every working work says Cancelling… or Finalizing… until the server catches up.
    const records = new Map([['c', { cancelPendingPolicy: 'immediate' }]]);
    assert.equal(ownStopPress(working, records), 'immediate');
    assert.equal(roomStatusView(working, { stopping: 'immediate' }).text, 'Cancelling…');
    assert.equal(roomStatusView(working, { stopping: 'finalize' }).text, 'Finalizing…');
    assert.equal(ownStopPress(working, new Map()), '');
    assert.equal(ownStopPress(stateOf('paused_by_owner'), new Map([['o', { cancelPendingPolicy: 'immediate' }]])), '',
        'a press on work that does not work changes no word');
});

test('reasons read their code through the catalog; counts stay parameters; an unknown code reads its sentence', () => {
    const held = work('held_continue');
    assert.equal(reasonText(held.because[0]), 'Continue starts when 3 helpers of the earlier attempt finish');
    assert.equal(reasonText({ code: 'brand_new_reason', text: 'A sentence no client knows yet' }), 'A sentence no client knows yet');
    assert.equal(reasonText({ code: '', text: 'raw' }), 'raw');
    assert.equal(reasonText(null), '');
    // Short reason words stand beside a word; a code without one keeps its sentence.
    assert.equal(reasonBeside(work('paused_by_owner').because[0]), 'by you');
    assert.equal(reasonBeside(work('paused_by_mind').because[0]), 'by Ouroboros');
    assert.equal(reasonBeside(work('spending_limit').because[0]), 'spending limit');
    assert.equal(reasonBeside(work('sleeping').because[0]), 'sleeping');
    assert.equal(reasonBeside({ code: 'saved_on_stop', text: 'Saved when the app stopped; Resume continues it' }), 'app stopped');
    assert.equal(reasonBeside(work('model_access').because[0]), 'The model needs you: sign in or restore access');
    const prior = currentPayload();
    try {
        applyPayload({ language: 'ru', english: false, entries: {
            'Continue starts when {count} helpers of the earlier attempt finish':
                { text: 'Продолжение начнётся, когда закончат {count} помощника прошлой попытки' },
            [CODE_PREFIX + 'work.because.earlier_attempt_finishing']: { text: 'Продолжение начнётся, когда закончит помощник' },
            [CODE_PREFIX + 'work.because.model_access']: { text: 'Модели нужен вход' },
            [CODE_PREFIX + 'work.short.by_you']: { text: 'вами' },
        } });
        // The count is a parameter of the translated template, never a number baked into a translation.
        assert.equal(reasonText(held.because[0]), 'Продолжение начнётся, когда закончат 3 помощника прошлой попытки');
        assert.equal(reasonText({ ...held.because[0], count: 18, text: 'Continue starts when 18 helpers of the earlier attempt finish' }),
            'Продолжение начнётся, когда закончат 18 помощника прошлой попытки');
        assert.equal(reasonText({ ...held.because[0], count: 1 }), 'Продолжение начнётся, когда закончит помощник');
        assert.equal(reasonText(work('model_access').because[0]), 'Модели нужен вход');
        assert.equal(reasonBeside(work('paused_by_owner').because[0]), 'вами');
        assert.equal(reasonText({ code: 'brand_new_reason', text: 'Unknown sentence' }), 'Unknown sentence');
    } finally { applyPayload(prior); }
});

test('every reason the server can send reads in the server\'s own sentence; a counted one in its template', () => {
    assert.ok(Object.keys(SERVER_REASONS).length > 10);
    for (const [code, forms] of Object.entries(SERVER_REASONS)) {
        for (const [count, text] of Object.entries(forms)) {
            const reason = count ? { code, text, count: Number(count) } : { code, text };
            assert.equal(reasonText(reason), text, `${code} ${count}`);
        }
    }
    // A counted code this client does not know keeps the server's sentence: no translation bakes its number.
    assert.equal(reasonText({ code: 'brand_new_count', count: 2, text: '2 brand new things' }), '2 brand new things');
});

test('the reason beside a word is never the one the word already says, in any language', () => {
    const asked = work('direct_question');
    assert.deepEqual([workRowView(asked).word, workRowView(asked).secondary], ['Waiting for your answer', '']);
    const prior = currentPayload();
    try {
        applyPayload({ language: 'ru', english: false, entries: {
            [CODE_PREFIX + 'task.chip.waiting_for_answer']: { text: 'Ждёт вашего ответа' } } });
        // The word is translated and the server's sentence is not: still nothing beside the word.
        assert.deepEqual([workRowView(asked).word, workRowView(asked).secondary], ['Ждёт вашего ответа', '']);
        // Another reason beside the same word still shows.
        assert.equal(workRowView(work('model_access')).secondary, 'The model needs you: sign in or restore access');
    } finally { applyPayload(prior); }
});

test('the owner\'s own Stop press on a work\'s card says itself on the row at once; work that does not work keeps its word', () => {
    const live = work('working_call');
    assert.deepEqual(['word', 'secondary', 'tone', 'dots'].map((key) => workRowView(live, { stopping: 'finalize' })[key]),
        ['Finalizing…', '', 'live', 'moving']);
    assert.equal(workRowView(live, { stopping: 'immediate' }).word, 'Cancelling…');
    assert.equal(workRowView(live).word, 'Sending');
    assert.equal(workRowView(work('paused_by_owner'), { stopping: 'immediate' }).word, 'Paused');
});

test('a reusable slot gets no control by its id; a real target keeps its own', () => {
    const slot = { controls: [{ action: 'stop', target: 'active' }, { action: 'pause', target: 'active' },
        { action: 'stop', target: 't-1' }] };
    assert.deepEqual(workControls(slot), { stop: ['t-1'], stopEarlier: [], pause: '', resume: '', hurry: '', wrapUp: '' });
});

test('a held Continue with three helpers of its failed earlier attempt: one working row with the reason, Stop and Stop earlier attempt, no Resume', () => {
    const state = stateOf('held_continue');
    assert.equal(state.works.length, 1, 'one work, one row: the Continue and its earlier helpers are inside it');
    const row = workRowView(state.works[0]);
    assert.deepEqual(row, { word: 'Working', secondary: 'Continue starts when 3 helpers of the earlier attempt finish',
        tone: 'live', dots: 'moving', amber: false, focus: '' });
    const grouped = workControls(state.works[0]);
    assert.deepEqual(grouped, { stop: ['c', 'p'], stopEarlier: ['p'], pause: '', resume: '', hurry: '', wrapUp: '' });
    assert.deepEqual(workMenuActions(grouped), [ACTION_STOP_EARLIER, ACTION_STOP_NOW]);
    assert.equal(TASK_CONTROL_LABELS[ACTION_STOP_EARLIER], 'Stop earlier attempt');
    // The held Continue's own card says the work's word and reason, and offers no Resume.
    const card = cardControls(state, 'c');
    assert.deepEqual([card.resume, card.menu, Boolean(card.work)], [false, true, true]);
    assert.deepEqual(cardWorkView(state.works[0]), { phase: 'working', text: 'Working',
        secondary: 'Continue starts when 3 helpers of the earlier attempt finish', classes: 'working',
        motion: true, waiting: false });
    // A count says itself the same from one helper to eighteen; the structure does not change.
    const many = { ...state.works[0], because: [{ code: 'earlier_attempt_finishing', count: 18,
        text: 'Continue starts when 18 helpers of the earlier attempt finish' }] };
    assert.deepEqual(Object.keys(workRowView(many)), Object.keys(row));
    assert.equal(workRowView(many).tone, row.tone);
});

test('rows: their word, the reason beside it, tone and amber from the work alone', () => {
    const row = (name, index = 0) => workRowView(work(name, index));
    assert.deepEqual(row('paused_by_owner'), { word: 'Paused', secondary: 'by you', tone: 'held', dots: '', amber: false, focus: '' });
    assert.deepEqual(row('spending_limit'), { word: 'Paused', secondary: 'spending limit', tone: 'held', dots: '', amber: true,
        focus: '' });
    assert.deepEqual(row('model_access'), { word: 'Waiting', secondary: 'The model needs you: sign in or restore access',
        tone: 'held', dots: '', amber: true, focus: '' });
    // The word already says the owner's question: the reason does not repeat it.
    assert.deepEqual(row('direct_question'), { word: 'Waiting for your answer', secondary: '', tone: 'wait', dots: '',
        amber: false, focus: '' });
    // A Pause still settling: the owner's own Pause keeps the neutral ink while the work still moves.
    assert.deepEqual(['word', 'secondary', 'tone', 'dots'].map((key) => row('queued_and_pausing', 0)[key]),
        ['Pausing…', 'by you', 'held', 'moving']);
    assert.deepEqual(['word', 'tone', 'dots'].map((key) => row('queued_and_pausing', 1)[key]), ['Queued', 'queued', 'still']);
    assert.deepEqual(['tone', 'dots'].map((key) => row('working_call')[key]), ['live', 'moving']);
    // An outcome stays beside Finalizing…; unknown stays unknown.
    assert.deepEqual(row('finalizing_failed'), { word: 'Failed', secondary: 'Finalizing…', tone: 'failed', dots: '', amber: false,
        focus: '' });
    assert.deepEqual(['word', 'tone', 'dots'].map((key) => row('unknown')[key]), ['Activity unconfirmed', 'unknown', '']);
    assert.deepEqual([row('ended_with_runs').word, row('ended_with_runs').secondary], ['Working', '2 external runs still working']);
    assert.deepEqual(workQuestion(work('direct_question')), { taskId: 'd', quizId: 'q1' });
    assert.equal(workQuestion(work('model_access')), null, 'an access wait is not an answer wait');
});

test('the focus line is the work\'s own update_focus text under its state, and none without one', () => {
    const focused = { ...work('working_call'), focus: { text: '  Release 7.7 · code merged · waiting for 7 signed files ', at: 1 } };
    assert.equal(workRowView(focused).focus, 'Release 7.7 · code merged · waiting for 7 signed files');
    assert.equal(workRowView(work('working_call')).focus, '');
    assert.equal(workRowView({ ...work('working_call'), focus: { text: '   ', at: 1 } }).focus, '');
});

test('controls group by action with exact targets; Resume is a button, a pending Stop leaves its escalation', () => {
    const running = workControls(work('working_call'));
    assert.deepEqual(workMenuActions(running), [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
    assert.deepEqual(workMenuActions(running, { cancelPending: true }), [ACTION_STOP_NOW]);
    const paused = workControls(work('paused_by_owner'));
    assert.equal(paused.resume, 'o');
    assert.deepEqual(workMenuActions(paused), [ACTION_STOP_NOW], 'Resume stands as its own button');
    assert.deepEqual(workMenuActions(paused, { resume: true }), [ACTION_RESUME, ACTION_STOP_NOW]);
    assert.deepEqual(workMenuActions(workControls(work('direct_question'))), [ACTION_PAUSE, ACTION_STOP_NOW],
        'a direct turn waiting for its answer can be paused and stopped, as its doors take it');
    assert.deepEqual(workControls({ controls: [{ action: 'teleport', target: 'x' }, { action: 'stop', target: '' }] }),
        { stop: [], stopEarlier: [], pause: '', resume: '', hurry: '', wrapUp: '' }, 'unknown or targetless actions are not offered');
    assert.deepEqual([...resumeTargets(body('paused_by_owner'))], ['o']);
    assert.deepEqual([...resumeTargets(body('held_continue'))], [], 'a held Continue is never resumed by the owner');
});

test('a card menu reads its work, else its own task controls without Resume', () => {
    const state = stateOf('paused_by_owner');
    assert.deepEqual(cardMenuActions(state, { groupId: 'o' }), [ACTION_STOP_NOW]);
    assert.equal(cardControls(state, 'o').resume, true);
    assert.deepEqual(cardMenuActions(state, { groupId: 'loose' }, { phase: 'budget_paused' }), [ACTION_STOP_NOW]);
    assert.deepEqual(cardMenuActions(state, { groupId: 'loose' }, { phase: 'working' }),
        [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
    assert.equal(cardControls(state, 'loose').resume, false);
});

test('the grouped Stop reaches every target in order and says one combined result', async () => {
    const asked = [], toasts = [];
    const ok = await requestStopAll(['c', 'p', 'c', ''], ACTION_STOP_NOW, {
        stopOne: async (target, action) => { asked.push([target, action]); },
        toast: (...args) => toasts.push(args) });
    assert.equal(ok, true);
    assert.deepEqual(asked, [['c', ACTION_STOP_NOW], ['p', ACTION_STOP_NOW]]);
    assert.deepEqual(toasts, []);
    const failing = await requestStopAll(['c', 'p'], ACTION_STOP_NOW, {
        stopOne: async (target) => { if (target === 'p') throw Object.assign(new Error('busy'), { status: 409 }); },
        toast: (...args) => toasts.push(args) });
    assert.equal(failing, false);
    assert.deepEqual(toasts, [['Stop failed for 1 of 2 parts of this work: busy', 'error']]);
    toasts.length = 0;
    // A target that already ended is settled, not a failure.
    assert.equal(await requestStopAll(['gone'], ACTION_STOP_NOW, {
        stopOne: async () => { throw Object.assign(new Error('missing'), { status: 404 }); },
        toast: (...args) => toasts.push(args) }), true);
    assert.deepEqual(toasts, []);
});

test('an incomplete body keeps what it omitted, unconfirmed and without controls; only a complete one clears', () => {
    const seen = stateOf('held_continue');
    const partial = reconcileWorkState(seen, { supervisor_ready: true, works: [], rooms: {}, works_complete: false }, 42);
    assert.equal(partial.complete, false);
    assert.deepEqual(partial.works.map((item) => [item.work_id, item.activity, item.confirmed, item.controls.length]),
        [['p', 'unknown', false, 0]]);
    assert.deepEqual([partial.room.activity, partial.room.mark, roomStatusView(partial).text], ['unknown', 'ring', 'Activity unconfirmed']);
    // Not ready, or read over a closed socket: no absence is proved either.
    assert.equal(reconcileWorkState(seen, { ...body('held_continue'), works: [], rooms: {}, supervisor_ready: false }, 42).works.length, 1);
    assert.equal(reconcileWorkState(seen, { ...body('held_continue'), works: [], rooms: {} }, 42, false).works.length, 1);
    const cleared = reconcileWorkState(partial, { supervisor_ready: true, works: [], rooms: {}, works_complete: true }, 42);
    assert.deepEqual([cleared.works.length, cleared.room.activity, roomStatusView(cleared).text], [0, 'idle', 'Online']);
    // A failed read or a body without the projection changes nothing.
    assert.equal(reconcileWorkState(seen, null, 42), seen);
    assert.equal(reconcileWorkState(seen, { active_chat_activities: [] }, 42), seen);
    // Nothing seen before and nothing listed: the room is quiet, never invented work.
    assert.equal(roomStatusView(reconcileWorkState(emptyWorkState(), { works: [], works_complete: false }, 42)).text, 'Online');
});

test('a room reads only its own works; every room feeds the card index and the member ids', () => {
    const merged = { supervisor_ready: true, works_complete: true,
        works: [...CASES.held_continue.works, ...CASES.direct_question.works],
        rooms: { ...CASES.held_continue.rooms, ...CASES.direct_question.rooms },
        active_chat_activities: [{ activity_id: 'c', task_attempt: 2 }] };
    const main = reconcileWorkState(emptyWorkState(), merged, 1);
    assert.deepEqual(main.works.map((item) => item.work_id), ['d']);
    assert.equal(main.room.activity, 'waiting');
    // A Main portal's work runs in its Project: its card still finds that work.
    assert.equal(main.episodes.get('c').work_id, 'p');
    assert.deepEqual([...main.members].sort(), ['c', 'd', 'h0', 'h1', 'h2']);
    assert.equal(main.attempts.get('c'), 2);
    assert.equal(reconcileWorkState(emptyWorkState(), merged, 42).works[0].work_id, 'p');
});

test('a work card says its work\'s word and short reason; plain running, finalizing and unconfirmed work keep their own', () => {
    const view = (name, index = 0) => cardWorkView(work(name, index));
    assert.deepEqual([view('paused_by_owner').text, view('paused_by_owner').classes], ['Paused · by you', 'paused']);
    assert.equal(view('paused_by_mind').text, 'Paused · by Ouroboros');
    assert.deepEqual([view('spending_limit').text, view('spending_limit').classes], ['Paused · spending limit', 'warn']);
    assert.equal(view('sleeping').text, 'Waiting · sleeping');
    assert.deepEqual([view('model_access').text, view('model_access').secondary],
        ['Waiting', 'The model needs you: sign in or restore access']);
    assert.deepEqual([view('queued_and_pausing', 0).text, view('queued_and_pausing', 0).motion], ['Pausing… · by you', false]);
    assert.equal(view('working_call'), null, 'its own model-call words already say Sending');
    assert.equal(view('finalizing_failed'), null, 'its outcome beside Finalizing… is its own');
    assert.equal(view('unknown'), null);
    assert.equal(view('ended_with_runs'), null, 'an ended episode keeps its own outcome');
    // The chip presenter takes the view, but a disconnected card still says Activity unconfirmed.
    const workView = view('paused_by_owner');
    assert.equal(taskActivityPresentation({ phase: 'budget_paused', workView }).text, 'Paused · by you');
    assert.equal(taskActivityPresentation({ phase: 'budget_paused', workView }).className, 'chat-live-phase paused');
    assert.equal(taskActivityPresentation({ phase: 'unknown', workView }).text, 'Activity unconfirmed');
    const records = new Map([['o', { groupId: 'o' }], ['child', { groupId: 'o', isSubagent: true }]]);
    assert.equal(applyCardWorkViews(records, stateOf('paused_by_owner')), true);
    assert.equal(records.get('o').workView.text, 'Paused · by you');
    assert.equal(records.get('child').workView, undefined, 'a helper card keeps its own presenter');
    assert.equal(applyCardWorkViews(records, stateOf('paused_by_owner')), false, 'an equal view is no change');
});

test('the census pause words are the short reason words, and only the projection may say who paused', () => {
    assert.equal(pausePhaseLabel('budget_paused', 'budget'), 'Paused · spending limit');
    assert.equal(pausePhaseLabel('budget_paused', 'restart'), 'Paused · app stopped');
    assert.equal(pausePhaseLabel('budget_paused', 'authority'), 'Paused · technical problem');
    assert.equal(pausePhaseLabel('budget_paused', 'sleep'), 'Waiting · sleeping');
    // The census `owner` cause also covers Ouroboros's own Pause: it never claims "by you".
    assert.equal(pausePhaseLabel('budget_paused', 'owner'), 'Paused');
    assert.equal(pausePhaseLabel('budget_pausing', 'owner'), 'Pausing…');
});
