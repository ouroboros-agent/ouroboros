/**
 * One work, one status in the browser (DESIGN "Room work status"): readers over the `/api/state` work projection
 * (ouroboros/gateway/work_status.py; shapes in work_types.js). The server groups every live member into works, rolls
 * works into rooms and decides each work's controls with their exact targets; this module says those facts in the
 * product's short words, keeps what an incomplete body did not list again, and lays a work's controls on the shared
 * task-control doors. It derives no word, mark or control of its own. Four facts only this client knows stay
 * overlays: the socket closed (Reconnecting…), the owner's own unconfirmed send (Sending), the supervisor not ready
 * yet (Starting…) and the owner's own Stop press on a card (Cancelling… / Finalizing…).
 */
import { fmt, tr } from './i18n.js';
import { taskPresentation, taskTerminalPhase } from './log_events.js';
import { startingStatus } from './host_starting_status.js';
import { chipWord, shortReason } from './task_phase_chip.js';
import {
    ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_RESUME, ACTION_STOP_EARLIER, ACTION_STOP_NOW, REUSABLE_TASK_IDS,
    hurryTaskAction, openTaskControlMenu, openTaskControls, requestStop, requestStopAll, resumeTaskAction, taskControlActions,
    taskControlBusy,
} from './task_control_menu.js';

// A known outcome keeps its status colour; an unfinished lifecycle (Finalizing…) is said beside it.
export const OUTCOME_TONES = { done: 'done', warn: 'caution', cancelled: 'caution', error: 'failed', timeout: 'failed',
    lifecycle_error: 'failed' };
const IDLE_ROOM = Object.freeze({ activity: 'idle', transition: 'none', call: '', turn: [], amber: false, mark: 'none',
    confirmed: true, works: [] });
const CALL_WORDS = { preparing: 'preparing', sending: 'sending', thinking: 'thinking' };
const list = (value) => (Array.isArray(value) ? value : []);
const asked = (state) => list(state?.turn).some((item) => item?.kind === 'question');

/** The state one chat instance shows before its first body: an idle room and no works. */
export function emptyWorkState() {
    return { room: IDLE_ROOM, works: [], episodes: new Map(), members: new Set(), attempts: new Map(), complete: false };
}

// What an incomplete body did not list again: still shown, never as confirmed work and never with controls.
const unconfirmedWork = (work) => ({ ...work, activity: 'unknown', transition: 'none', call: '', because: [],
    turn: [], controls: [], confirmed: false });

/**
 * One body applied to an instance's work state. `room` and `works` are this chat's (`works[].room`, keyed as the
 * server keys `rooms`); `episodes` maps every work's current episode task id to its work in EVERY room (a Main
 * portal's work runs in its Project) and `members` holds every member id; `attempts` is each census row's attempt,
 * so a control opened for one attempt of an episode is not pressed on the next. Only a complete supervisor-ready
 * body read over an open socket proves absence; an incomplete one keeps the works it omitted, unconfirmed. A body
 * without the projection, or a failed read (null), changes nothing.
 */
export function reconcileWorkState(prior, data, chatId, connected = true) {
    const before = prior || emptyWorkState();
    if (!data || typeof data !== 'object' || !Array.isArray(data.works)) return before;
    const complete = Boolean(connected) && data.works_complete === true && data.supervisor_ready === true;
    const chat = String(chatId);
    const all = data.works.filter((work) => work && typeof work === 'object' && work.work_id);
    const listed = all.filter((work) => String(work.room || 0) === chat);
    const ids = new Set(listed.map((work) => work.work_id));
    const kept = complete ? [] : before.works.filter((work) => !ids.has(work.work_id)).map(unconfirmedWork);
    const served = data.rooms && typeof data.rooms === 'object' ? data.rooms[chat] : null;
    const room = served && typeof served === 'object' ? served
        : kept.length ? { ...IDLE_ROOM, activity: 'unknown', mark: 'ring', confirmed: false, works: kept.map((w) => w.work_id) }
            : { ...IDLE_ROOM, confirmed: complete };
    const episodes = new Map();
    const members = new Set();
    for (const work of all) {
        if (work.episode?.task_id) episodes.set(String(work.episode.task_id), work);
        for (const id of list(work.members?.ids)) members.add(String(id));
    }
    const attempts = new Map(list(data.active_chat_activities).filter((row) => row?.activity_id)
        .map((row) => [String(row.activity_id), row.task_attempt ?? null]));
    return { room, works: [...listed, ...kept], episodes, members, attempts, complete };
}

/** A work's or a room's one short word, from its activity, transition and exact call (the cards' chip words). */
export function activityWord(state = {}) {
    switch (state.activity) {
    case 'working':
        if (state.transition === 'finalizing') return chipWord('finalizing');
        if (state.transition === 'pausing') return chipWord('pausing');
        return chipWord(CALL_WORDS[state.call] || 'working');
    case 'queued': return chipWord('queued');
    case 'waiting': return asked(state) ? chipWord('ownerWait') : chipWord('waiting');
    case 'paused': return chipWord('paused');
    case 'idle': return 'Online';
    default: return chipWord('unconfirmed');
    }
}

// A counted reason says its number in the catalog's template, so a translation keeps the count. These are the
// server's sentences (gateway/work_status.py REASON_TEXT); the web fixture pins them to the server's own.
const COUNTED_REASONS = {
    earlier_attempt_finishing: (n) => (n === 1
        ? tr('work.because.earlier_attempt_finishing', 'Continue starts when the helper of the earlier attempt finishes')
        : fmt('Continue starts when {count} helpers of the earlier attempt finish', { count: n, n })),
    earlier_runs_finishing: (n) => (n === 1
        ? tr('work.because.earlier_runs_finishing', 'Continue starts when the external run of the earlier attempt finishes')
        : fmt('Continue starts when {count} external runs of the earlier attempt finish', { count: n, n })),
    helpers_running: (n) => (n === 1 ? tr('work.because.helpers_running', '1 helper still working')
        : fmt('{count} helpers still working', { count: n, n })),
    delegated_running: (n) => (n === 1 ? tr('work.because.delegated_running', '1 external run still working')
        : fmt('{count} external runs still working', { count: n, n })),
};

/** One reason in the owner's words: the server's code read through the catalog, its sentence as the English (a
 *  code this client has never seen still reads its sentence). A count stays a parameter of its template; a counted
 *  code this client does not know keeps the server's sentence as it is. */
export function reasonText(reason) {
    if (!reason || typeof reason !== 'object') return '';
    const code = String(reason.code || '');
    const text = String(reason.text || code);
    if (!code) return text;
    if (Number.isInteger(reason.count)) return COUNTED_REASONS[code] ? COUNTED_REASONS[code](reason.count) : text;
    return tr(`work.because.${code}`, text);
}

/** The reason said beside a word: its short word where one exists (Paused · by you), else its sentence. */
export const reasonBeside = (reason) => shortReason(reason?.code) || reasonText(reason);

/** The reason said beside `word`, '' when the word already says it (an answer wait's own words, in any language). */
export function reasonBesideWord(reason, word) {
    if (!reason || (reason.code === 'owner_answer' && word === chipWord('ownerWait'))) return '';
    const said = reasonBeside(reason);
    return said === word ? '' : said;
}

/** What waits for the owner in a room ('question' | 'action'), '' when nothing does or its own word says so. */
export const roomAmber = (room) => (!room?.amber || (room.activity === 'waiting' && asked(room)) ? ''
    : asked(room) ? 'question' : 'action');

/** The amber mark said in words, for the room's accessible name: the colour is never the only way it is told. */
export const amberNote = (kind) => (kind === 'question' ? tr('work.amber.question', 'A question waits for your answer')
    : kind === 'action' ? tr('work.amber.action', 'Something here waits for you') : '');

/**
 * The room's status in its bar: the server's room in short words, with the client's own facts on top. Tone is the
 * word's ink (live, queued, wait for the owner's own question, held, unknown, starting, offline, online); `dots` the
 * one mark after it (moving, still or one dot); `amber` says what waits for the owner (question | action), which turns
 * the mark amber; `showDots` is the transcript's typing row. `stopping` is the owner's own Stop press on every
 * working work of the room ('finalize' | 'immediate'); until the host is `ready`, an idle room says its start in the
 * words of the host's `boot` phases (host_starting_status.js).
 */
export function roomStatusView(state, { connected = true, ready = true, boot = null, sending = false, stopping = '' } = {}) {
    const view = (kind, text, tone, dots = 'one', extra = {}) => ({ kind, text, tone, dots, showDots: dots !== 'one',
        amber: '', mark: '', ...extra });
    if (!connected) return view('offline', 'Reconnecting…', 'offline');
    const room = state?.room || IDLE_ROOM;
    const mark = String(room.mark || 'none');
    const amber = roomAmber(room);
    if (room.activity !== 'working' && sending) return view('thinking', chipWord('sending'), 'live', 'moving', { amber });
    switch (room.activity) {
    case 'working':  // a Pause still settling keeps the neutral ink; its mark moves while work stops
        return view('thinking', stopping ? chipWord(stopping === 'finalize' ? 'finalizing' : 'cancelling') : activityWord(room),
            room.transition === 'pausing' && !stopping ? 'held' : 'live', mark === 'moving' ? 'moving' : 'one', { amber, mark });
    case 'queued': return view('thinking', activityWord(room), 'queued', 'still', { amber, mark });
    // The word itself says the owner's question: its ink is the amber, not a second mark.
    case 'waiting': return asked(room) ? view('online', activityWord(room), 'wait', 'one', { mark })
        : view('online', activityWord(room), 'held', 'one', { amber, mark });
    case 'paused': return view('online', activityWord(room), 'held', 'one', { amber, mark });
    case 'unknown': return view('online', activityWord(room), 'unknown', 'one', { mark: 'ring' });
    default: return ready ? view('online', 'Online', 'online') : { ...view('starting', 'Starting…', 'starting'), ...startingStatus(boot) };
    }
}

/** The owner's own Stop press on every working work of the room (a card's pending policy), else ''. */
export function ownStopPress(state, records) {
    const working = list(state?.works).filter((work) => work.activity === 'working' && work.confirmed);
    const policies = new Set(working.map((work) => records?.get?.(String(work.episode?.task_id || ''))?.cancelPendingPolicy || ''));
    return working.length && policies.size === 1 ? [...policies][0] : '';
}

/**
 * One work's row in the room's Shade: its word (an outcome beside Finalizing…), the reason beside it, the row's tone,
 * its dots (moving while it works, a Pause still settling included; still while queued; else one dot) and whether its
 * mark turns amber (something waits for the owner). Paused by the owner or by Ouroboros is not amber. `stopping` is
 * the owner's own Stop press on the work's card ('finalize' | 'immediate'), said at once as the card says it.
 */
export function workRowView(work = {}, { stopping = '' } = {}) {
    if (stopping && work.activity === 'working' && work.confirmed !== false) {
        return { word: chipWord(stopping === 'finalize' ? 'finalizing' : 'cancelling'), secondary: '', tone: 'live',
            dots: 'moving', amber: list(work.turn).length > 0, focus: '' };
    }
    const outcome = work.transition === 'finalizing' && work.outcome
        ? taskPresentation(taskTerminalPhase({ status: work.outcome })) : null;
    const word = outcome ? outcome.headline : activityWord(work);
    const reason = reasonBesideWord(list(work.because)[0], word);
    const secondary = [outcome ? chipWord('finalizing') : '', reason].filter(Boolean).join(' · ');
    const pausing = work.activity === 'working' && work.transition === 'pausing';
    const tone = outcome ? OUTCOME_TONES[outcome.phase] || 'live'
        : !work.confirmed || work.activity === 'unknown' ? 'unknown'
            : work.activity === 'waiting' && asked(work) ? 'wait'
                : work.activity === 'working' && !pausing ? 'live' : work.activity === 'queued' ? 'queued' : 'held';
    const dots = outcome || tone === 'unknown' ? '' : work.activity === 'working' ? 'moving' : work.activity === 'queued' ? 'still' : '';
    // The work's own focus line: what its author says it is doing, under the state (update_focus).
    const focus = String(work.focus?.text || '').trim();
    return { word, secondary, tone, dots, amber: tone !== 'wait' && list(work.turn).length > 0, focus };
}

/** The question a row's View question goes to: the first question in the work's turn, by its exact quiz id. */
export function workQuestion(work) {
    const question = list(work?.turn).find((item) => item?.kind === 'question' && item.quiz_id && item.task_id);
    return question ? { taskId: String(question.task_id), quizId: String(question.quiz_id) } : null;
}

/**
 * What the card of a work's current episode says (one work, one status): the work's word with its short
 * reason in the chip, or its reason sentence beside it. Plain running work, finalizing work (its outcome beside
 * Finalizing…) and unconfirmed work keep the card's own words; so does a card whose episode is not live.
 */
export function cardWorkView(work) {
    if (!work || !work.confirmed || !work.episode?.live || work.activity === 'unknown' || work.transition === 'finalizing') return null;
    const first = list(work.because)[0];
    if (work.activity === 'working' && work.transition === 'none' && !first) return null;
    // Reviewers already launched finish beside a settled Pause, visibly: the chip keeps saying so.
    const reviews = work.activity === 'paused' && list(work.because).some((entry) => entry?.code === 'review_work_finishing');
    const word = [activityWord(work), shortReason(first?.code), reviews ? chipWord('reviewWorkFinishing') : '']
        .filter(Boolean).join(' · ');
    const sentence = first && !shortReason(first.code) ? reasonBesideWord(first, activityWord(work)) : '';
    const running = work.activity === 'working';
    const tone = list(work.turn).length ? 'warn' : running ? (work.transition === 'pausing' ? 'working waiting' : 'working')
        : work.activity;
    return {
        phase: running ? 'working' : work.activity, text: word, secondary: sentence, classes: tone,
        motion: running && work.transition === 'none', waiting: !running || work.transition !== 'none',
    };
}

/** Every card's work view from one state; true when a card's view changed (its chip repaints with its next write). */
export function applyCardWorkViews(records, state) {
    let changed = false;
    for (const record of records?.values?.() || []) {
        if (record?.isSubagent) continue;
        const view = cardWorkView(state?.episodes?.get(String(record.groupId || '')));
        if (JSON.stringify(view) !== JSON.stringify(record.workView || null)) changed = true;
        record.workView = view;
    }
    return changed;
}

/**
 * A work's controls, one press each: Stop (every episode root that still owns live work, in the server's order),
 * Stop earlier attempt (each earlier root that does) and the current episode's Pause, Resume, Hurry and Wrap up.
 * Unknown actions are not offered.
 */
export function workControls(work) {
    const out = { stop: [], stopEarlier: [], pause: '', resume: '', hurry: '', wrapUp: '' };
    for (const control of list(work?.controls)) {
        // A reusable slot (`active`) hosts many cycles: no control reaches one of them by that id.
        const target = String(control?.target || '').trim();
        if (!target || REUSABLE_TASK_IDS.has(target)) continue;
        const many = { stop: out.stop, stop_earlier: out.stopEarlier }[control.action];
        if (many) { if (!many.includes(target)) many.push(target); continue; }
        const one = { pause: 'pause', resume: 'resume', hurry: 'hurry', wrap_up: 'wrapUp' }[control.action];
        if (one && !out[one]) out[one] = target;
    }
    return out;
}

/** The confirmed work whose live current episode is `taskId` in one body, else null. */
export function workOfEpisode(data, taskId) {
    const id = String(taskId || '');
    return list(data?.works).find((work) => work?.confirmed !== false && work.episode?.live
        && String(work.episode.task_id || '') === id) || null;
}

/** Every task a confirmed work's Resume control names in one body: where Activity may offer Resume. */
export function resumeTargets(data) {
    return new Set(list(data?.works).filter((work) => work?.confirmed !== false)
        .map((work) => workControls(work).resume).filter(Boolean));
}

/** Episode task id → the reason a confirmed paused (or pausing) work gives: who or what holds it. */
export function pauseReasons(data) {
    return new Map(list(data?.works).filter((work) => work?.confirmed !== false
        && (work.activity === 'paused' || work.transition === 'pausing') && work.episode?.task_id && work.because?.[0]?.code)
        .map((work) => [String(work.episode.task_id), String(work.because[0].code)]));
}

/** The shared menu's items for a work's controls. Resume is its own button wherever one stands beside the menu;
 *  a pending Stop leaves only its hard escalation. */
export function workMenuActions(grouped, { cancelPending = false, resume = false } = {}) {
    if (cancelPending) return grouped.stop.length ? [ACTION_STOP_NOW] : [];
    return [grouped.wrapUp && ACTION_FINALIZE, grouped.hurry && ACTION_HURRY, grouped.pause && ACTION_PAUSE,
        resume && grouped.resume && ACTION_RESUME, grouped.stopEarlier.length && ACTION_STOP_EARLIER,
        grouped.stop.length && ACTION_STOP_NOW].filter(Boolean);
}

/**
 * What a card offers from its work: `work` when the card is a confirmed work's live current episode, `resume` when
 * that work's Resume control targets the card's own task, `menu` when the work offers anything else.
 */
export function cardControls(state, taskId) {
    const id = String(taskId || '');
    const work = state?.episodes?.get(id);
    const grouped = work && work.confirmed !== false && work.episode?.live ? workControls(work) : null;
    return { work: grouped ? work : null, resume: Boolean(grouped && grouped.resume === id),
        menu: Boolean(grouped && workMenuActions(grouped).length) };
}

/** The items a card's Stop… menu offers now: its work's controls, else the card's own task controls. */
export function cardMenuActions(state, record, activity = null) {
    const { work } = cardControls(state, record?.groupId);
    const cancelPending = Boolean(record?.cancelPendingPolicy);
    return work ? workMenuActions(workControls(work), { cancelPending })
        : taskControlActions({ cancelPending, budgetPaused: activity?.phase === 'budget_paused' });
}

/**
 * A card's Stop… menu: its work's controls when it is a work's current episode (the card's own target stops through
 * its card path, `stop`), else the card's own task controls, which never offer Resume.
 */
export function openCardControls(anchor, record, work, { activity = null, stop, afterAction = null } = {}) {
    const cancelPending = Boolean(record?.cancelPendingPolicy);
    if (!work) return openTaskControls(anchor, record?.groupId, { activity, cancelPending, stop });
    return openWorkControls(anchor, work, { cancelPending, afterAction,
        stopOne: (target, action) => (target === String(record?.groupId || '') ? stop(action) : requestStop(target, action)) });
}

/**
 * Open the shared menu on a work's controls: each action reaches the existing request owner at its exact target;
 * Stop and Stop earlier attempt stop each of their targets in order and say one combined result. `stopOne` is the
 * caller's own stop path for a target (a loaded card keeps its optimistic chip); `canAct` re-checks the current
 * controls before any effect; `afterAction` re-reads the state once an effect settled.
 */
export function openWorkControls(anchor, work, { cancelPending = false, canAct = () => true, stopOne = requestStop,
    afterAction = null, withResume = false } = {}) {
    const grouped = workControls(work);
    const actions = workMenuActions(grouped, { cancelPending, resume: withResume });
    if (!actions.length) return null;
    const stopAll = (targets, action) => requestStopAll(targets, action, { stopOne });
    return openTaskControlMenu(anchor, {
        actions, pauseTarget: grouped.pause, busy: taskControlBusy(grouped.pause || work?.episode?.task_id), canAct, afterAction,
        onAction: (action) => (action === ACTION_HURRY ? hurryTaskAction(grouped.hurry)
            : action === ACTION_RESUME ? resumeTaskAction(grouped.resume)
                : action === ACTION_FINALIZE ? stopAll([grouped.wrapUp], ACTION_FINALIZE)
                    : stopAll(action === ACTION_STOP_EARLIER ? grouped.stopEarlier : grouped.stop, ACTION_STOP_NOW)),
    });
}
