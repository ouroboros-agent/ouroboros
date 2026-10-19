// Server-shaped work projection fields (`/api/state` works, rooms, works_complete) for the chat and room tests.
// The real shapes are the cases in fixtures/work_projection_cases.json, built by the server itself
// (tests/test_work_projection_web_fixture.py); these builders state one room the way the server sends it
// for a test's census rows, so a test pins what the client says for that room, never a client derivation.
import { readFileSync } from 'node:fs';

const SERVER = JSON.parse(readFileSync(new URL('./fixtures/work_projection_cases.json', import.meta.url), 'utf8'));
export const SERVER_CASES = SERVER.cases;
// Every reason sentence the server can send, by code: a counted one at 1 and at 3 ('' for an uncounted one).
export const SERVER_REASONS = SERVER.reasons;

// The mark the server's room rollup gives each activity (gateway/work_status.roll_up_room; the fixture pins it).
const MARKS = { working: 'moving', queued: 'still', waiting: 'dot', paused: 'dot', unknown: 'ring' };

/** One room as the server rolls it up: its activity speaks, its mark follows, amber while it is the owner's turn. */
export function room(activity, extra = {}) {
    return { activity, transition: 'none', call: '', turn: [], amber: Boolean(extra.turn?.length),
        mark: MARKS[activity] || 'none', confirmed: activity !== 'unknown', works: [], ...extra };
}

/** One work for one census row: its episode, members and room; what it does and offers as the test states it. */
export function work(row, extra = {}) {
    const id = String(row.activity_id);
    return { work_id: id, room: row.chat_id ?? 1, name: '', kind: row.kind === 'managed_task' ? 'task' : 'direct',
        project_id: String(row.project_id || ''), episode: { task_id: id, live: true }, activity: 'working',
        transition: 'none', call: '', because: [], turn: [], release: 'self', paused_by: null,
        members: { working: 0, waiting: 0, delegated: 0, ids: [id] }, controls: [], since: row.started_at || 0,
        confirmed: true, ...(row.client_message_id ? { client_message_id: row.client_message_id } : {}), ...extra };
}

/** The work fields of one body for one chat: its works and its one room (absent while the room is idle). */
export function projection(chatId, works = [], activeRoom = null, complete = true) {
    return {
        works, works_complete: complete, works_gaps: [], work_summary: { counts: {}, turn: 0, confirmed: complete },
        rooms: activeRoom ? { [String(chatId)]: { ...activeRoom, works: works.map((item) => item.work_id) } } : {},
    };
}
