// Pure presentation helpers for the sidebar activity marks.
//
// Each Project row reads its room in the server's work projection (`/api/state` `rooms`, keyed by the room's
// chat id; ouroboros/gateway/work_status.py): the room's mark (moving dots, still dots, one dot or a ring), amber
// while something waits for the owner, and its word with the reason of its first work. This module owns no timer,
// request, socket or durable state and derives no mark of its own; a caller supplies the previous projection when
// reconciling an incomplete body.

import { activeModelWaits, mergeModelWaits } from './model_wait.js';
import { activityWord, amberNote, reasonBesideWord, roomAmber } from './work_status.js';

/** Whether a census row's own model waits are current (Activity reads the same rule). */
export function waitingModel(row) {
    const waits = row?.model_waits;
    if (!waits || typeof waits !== 'object') return false;
    // The same admission rule the chat card applies: a malformed wait row is
    // dropped here exactly as there, so the sidebar cannot go static on a row
    // the card would never show.
    const admitted = mergeModelWaits({}, waits);
    return activeModelWaits(admitted, false, Number(row?.task_attempt) || 0).length > 0;
}

/** A room with nothing to show. */
export const IDLE_SUMMARY = Object.freeze({ state: 'idle', motion: false, amber: false, word: '', label: '' });
// The room's mark as the marker's state: three moving dots, three still dots, one dot, a hollow ring.
const MARK_STATES = { moving: 'working', still: 'queued', dot: 'dot', ring: 'unknown' };
const STATE_RANK = ['working', 'queued', 'dot', 'unknown', 'idle'];

/**
 * One room as one marker: its mark, amber, and its word with the first work's reason (no counts). The row's
 * accessible name also says what waits for the owner when the word does not, so amber is never told by colour alone.
 */
export function summarizeRoom(room, works = []) {
    if (!room || typeof room !== 'object' || room.activity === 'idle') return IDLE_SUMMARY;
    const state = MARK_STATES[room.mark] || 'unknown';
    const first = (Array.isArray(works) ? works : []).find((work) => work?.work_id === room.works?.[0]);
    const word = activityWord(room);
    const reason = state === 'unknown' ? '' : reasonBesideWord(first?.because?.[0], word);
    const note = state === 'unknown' ? '' : amberNote(roomAmber(room));
    return { state, motion: state === 'working', amber: Boolean(room.amber) && state !== 'unknown', word,
        label: [word, reason, note].filter(Boolean).join(' · ') };
}

// A room an incomplete or unavailable body did not list again: still shown, never as confirmed motion.
const unconfirmed = () => {
    const word = activityWord({ activity: 'unknown' });
    return { state: 'unknown', motion: false, amber: false, word, label: word };
};

/**
 * Reconcile one body against the previous rooms. A complete supervisor-ready body (`works_complete`) proves that
 * an unlisted room is idle; a partial, unavailable or disconnected one keeps every room it omitted, unconfirmed.
 * Rooms it lists describe their own observed state.
 */
export function reconcileProjectRooms(previous = new Map(), data = {}) {
    const prior = previous instanceof Map ? previous : new Map();
    const rooms = data?.rooms && typeof data.rooms === 'object' ? data.rooms : null;
    const complete = Boolean(rooms) && Array.isArray(data.works) && data.works_complete === true
        && data.supervisor_ready === true;
    const next = new Map();
    if (!complete) for (const chat of prior.keys()) next.set(chat, unconfirmed());
    for (const [chat, room] of Object.entries(rooms || {})) {
        const summary = summarizeRoom(room, data.works);
        if (summary.state !== 'idle') next.set(String(chat), summary);
    }
    return { rooms: next, complete };
}

/**
 * Index the Project rooms by chat id; the collapsed Projects header shows the strongest of their marks (moving over
 * still over one dot over a ring), amber when any is, and their distinct words.
 */
export function buildProjectActivityIndex(rooms = new Map(), chatIds = []) {
    const byChat = new Map();
    for (const chat of chatIds) {
        const summary = rooms instanceof Map ? rooms.get(String(chat)) : null;
        if (summary) byChat.set(String(chat), summary);
    }
    const summaries = [...byChat.values()];
    const state = summaries.map((summary) => summary.state)
        .sort((a, b) => STATE_RANK.indexOf(a) - STATE_RANK.indexOf(b))[0] || 'idle';
    const words = [...new Set(summaries.map((summary) => summary.word).filter(Boolean))];
    return {
        byChat,
        aggregate: state === 'idle' ? IDLE_SUMMARY
            : { state, motion: state === 'working', amber: summaries.some((summary) => summary.amber), word: words[0] || '',
                label: words.join(' · ') },
    };
}
