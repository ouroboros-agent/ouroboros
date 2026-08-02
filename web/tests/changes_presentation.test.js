import assert from 'node:assert/strict';
import test from 'node:test';

import {
    HEAD_DRIFT_NOTICE,
    diffBanners,
    diffSummaryMeta,
    requestEditsParts,
    requestEditsPrefix,
    taskProjectBadge,
    taskShortTitle,
} from '../modules/changes.js';
import { makeChipPart, serializeParts } from '../modules/composer_parts.js';
import { parsePatch } from '../modules/patch_parse.js';

test('a task badge prefers its OWN project scope over a post-hoc binding', () => {
    const badge = taskProjectBadge(
        { task_id: 't1', project_id: 'proj_own' },
        {
            taskBindings: { t1: { project_id: 'proj_bound' } },
            projects: [{ id: 'proj_own', name: 'Own Room' }, { id: 'proj_bound', name: 'Bound Room' }],
        },
    );
    assert.deepEqual(badge, { projectId: 'proj_own', label: 'Own Room' });
});

test('a task bound only through /api/state.task_bindings still gets a badge', () => {
    const badge = taskProjectBadge(
        { task_id: 't2' },
        { taskBindings: { t2: { project_id: 'proj_x' } }, projects: [{ id: 'proj_x', name: 'Retry Backoff' }] },
    );
    assert.deepEqual(badge, { projectId: 'proj_x', label: 'Retry Backoff' });
});

test('a project beyond the capped sidebar list falls back to its id, not to nothing', () => {
    const badge = taskProjectBadge({ task_id: 't3', project_id: 'proj_uncapped' }, { projects: [] });
    assert.deepEqual(badge, { projectId: 'proj_uncapped', label: 'proj_uncapped' });
});

test('an unscoped task has no badge', () => {
    assert.equal(taskProjectBadge({ task_id: 't4' }, { taskBindings: { other: { project_id: 'p' } } }), null);
    assert.equal(taskProjectBadge({ task_id: 't5', project_id: '   ' }, {}), null);
    assert.equal(taskProjectBadge({}, {}), null);
});

test('the short title prefers title, then objective, then description, then id', () => {
    assert.equal(taskShortTitle({ title: 'Fix retry backoff' }), 'Fix retry backoff');
    assert.equal(taskShortTitle({ objective: 'Tighten the loop' }), 'Tighten the loop');
    assert.equal(taskShortTitle({ description: 'multi\n line   text' }), 'multi line text');
    assert.equal(taskShortTitle({ task_id: 'abc123' }), 'abc123');
    assert.equal(taskShortTitle({}), 'task');
    const long = taskShortTitle({ title: 'x'.repeat(200) });
    assert.equal(long.length, 72);
    assert.ok(long.endsWith('…'));
});

test('the summary meta reports the lifecycle instead of a fake zero-file count', () => {
    const parsed = parsePatch([
        'diff --git a/a.py b/a.py',
        '--- a/a.py',
        '+++ b/a.py',
        '@@ -1,1 +1,2 @@',
        ' keep',
        '+added',
    ].join('\n'));
    assert.equal(diffSummaryMeta({ status: 'ready' }, parsed), '1 file · +1 −0');
    assert.equal(diffSummaryMeta({ status: 'empty' }, { files: [], added: 0, removed: 0 }), 'no changes');
    assert.equal(
        diffSummaryMeta({ status: 'pending' }, { files: [], added: 0, removed: 0 }),
        'waiting for the task to finalize its changes',
    );
    assert.equal(diffSummaryMeta({ status: 'blocked' }, null), 'diff unavailable');
});

test('banners disclose pending, blocked and drift with the locked drift wording', () => {
    assert.deepEqual(diffBanners({ status: 'ready', blockers: [], head_advanced: false }), []);

    const pending = diffBanners({ status: 'pending', blockers: [] });
    assert.equal(pending.length, 1);
    assert.equal(pending[0].tone, 'pending');

    const blocked = diffBanners({ status: 'blocked', blockers: ['baseline_missing', 'candidate_scan_failed'] });
    assert.equal(blocked[0].tone, 'blocked');
    assert.equal(blocked[0].detail, 'baseline_missing, candidate_scan_failed');

    const drift = diffBanners({ status: 'ready', head_advanced: true, blockers: ['baseline_stale'] });
    assert.equal(drift[0].tone, 'drift');
    assert.equal(drift[0].text, HEAD_DRIFT_NOTICE);
    // Drift wording is a boolean disclosure: no commit counts, no ownership claim.
    assert.ok(!/\d/.test(HEAD_DRIFT_NOTICE));
    assert.ok(!/only|exclusive|owns/i.test(HEAD_DRIFT_NOTICE));
    // Remaining blockers are still surfaced as evidence, not swallowed.
    assert.equal(drift[1].tone, 'evidence');
    assert.equal(drift[1].detail, 'baseline_stale');
});

test('a blocked diff does not ALSO repeat its blockers as evidence', () => {
    const rows = diffBanners({ status: 'blocked', blockers: ['baseline_missing'] });
    assert.deepEqual(rows.map((row) => row.tone), ['blocked']);
});

test('request edits prepends the task line and keeps the dock order', () => {
    const task = { task_id: 'task-9', title: 'Fix retry backoff' };
    assert.equal(requestEditsPrefix(task), 'Re task task-9 ("Fix retry backoff"): ');

    const chip = makeChipPart({ path: 'ouroboros/loop.py', lineStart: 10, lineEnd: 11, content: 'a\nb' });
    const parts = requestEditsParts(task, [chip, { type: 'text', text: 'this retry is wrong' }]);
    assert.deepEqual(parts.map((part) => part.type), ['text', 'chip', 'text']);
    assert.equal(parts[0].text, 'Re task task-9 ("Fix retry backoff"): ');
    // The serialized handoff is ordinary message text — chips keep their marker.
    assert.equal(serializeParts(parts), [
        'Re task task-9 ("Fix retry backoff"): ',
        '[context: ouroboros/loop.py L10-L11]',
        '```',
        'a',
        'b',
        '```',
        'this retry is wrong',
    ].join('\n'));
});

test('the parts builder is pure: an empty dock yields only the task line', () => {
    // The "did the owner actually ask for something" guard lives in the dock
    // handler (an empty dock is a focus no-op, never a bare task-line message);
    // this builder stays a pure projection of whatever it is handed.
    const parts = requestEditsParts({ task_id: 't' }, []);
    assert.deepEqual(parts.map((part) => part.type), ['text']);
    assert.deepEqual(requestEditsParts({ task_id: 't' }, null).length, 1);
});
