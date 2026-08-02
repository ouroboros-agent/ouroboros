import assert from 'node:assert/strict';
import test from 'node:test';

import {
    HEAD_DRIFT_NOTICE,
    NO_BASELINE_NOTICE,
    diffBanners,
    diffChipDecision,
    diffLacksBaselineOnly,
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
    assert.equal(
        diffSummaryMeta({ status: 'blocked', blockers: ['candidate_scan_failed'] }, null),
        'diff unavailable',
    );
    // A task that simply never had a baseline is not a broken read (C5).
    assert.equal(diffSummaryMeta({ status: 'blocked', blockers: ['baseline_missing'] }, null), 'no diff baseline recorded');
    assert.equal(diffSummaryMeta({ status: 'blocked' }, null), 'no diff baseline recorded');
});

test('banners disclose pending, blocked and drift with the locked drift wording', () => {
    assert.deepEqual(diffBanners({ status: 'ready', blockers: [], head_advanced: false }), []);

    const pending = diffBanners({ status: 'pending', blockers: [] });
    assert.equal(pending.length, 1);
    assert.equal(pending[0].tone, 'pending');

    const blocked = diffBanners({ status: 'blocked', blockers: ['baseline_missing', 'candidate_scan_failed'] });
    assert.equal(blocked[0].tone, 'blocked');
    assert.equal(blocked[0].detail, 'baseline_missing, candidate_scan_failed');

    const drift = diffBanners({
        status: 'ready', source: 'mutation_baseline', head_advanced: true, blockers: ['baseline_stale'],
    });
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
    const rows = diffBanners({ status: 'blocked', blockers: ['candidate_scan_failed'] });
    assert.deepEqual(rows.map((row) => row.tone), ['blocked']);
});

test('a missing baseline reads as a neutral absence, not an untrustworthy diff', () => {
    // C5: `baseline_missing` (or a blocked answer with no blockers at all) is the
    // ORDINARY case for a task that never touched the repo. Alarming copy there
    // teaches the owner to ignore the banner that matters.
    assert.equal(diffLacksBaselineOnly({ status: 'blocked', blockers: ['baseline_missing'] }), true);
    assert.equal(diffLacksBaselineOnly({ status: 'blocked', blockers: [] }), true);
    assert.equal(diffLacksBaselineOnly({ status: 'blocked' }), true);
    assert.equal(diffLacksBaselineOnly({ status: 'blocked', blockers: ['baseline_missing', 'x'] }), false);
    assert.equal(diffLacksBaselineOnly({ status: 'blocked', blockers: ['patch_too_large'] }), false);
    assert.equal(diffLacksBaselineOnly({ status: 'empty', blockers: ['baseline_missing'] }), false);

    const rows = diffBanners({ status: 'blocked', blockers: ['baseline_missing'] });
    assert.deepEqual(rows, [{ tone: 'neutral', text: NO_BASELINE_NOTICE }]);
    // No blocker code is shown: there is nothing here for the owner to act on.
    assert.equal(NO_BASELINE_NOTICE, 'No diff baseline was recorded for this task');
    assert.ok(!/trust|fail|error/i.test(NO_BASELINE_NOTICE));
});

test('drift is disclosed for the LIVE projection only, never for a durable patch', () => {
    // C3: `head_advanced` means "the repo moved away from the base this patch is
    // taken against". A workspace patch is durable bytes captured at its own base,
    // so the sentence would describe a repo the patch does not depend on.
    const live = diffBanners({ status: 'ready', source: 'mutation_baseline', head_advanced: true, blockers: [] });
    assert.deepEqual(live.map((row) => row.tone), ['drift']);
    const durable = diffBanners({ status: 'ready', source: 'workspace_patch', head_advanced: true, blockers: [] });
    assert.deepEqual(durable, []);
    // An absent source is not a licence to claim drift either.
    assert.deepEqual(diffBanners({ status: 'ready', head_advanced: true, blockers: [] }), []);
});

test('the task rail is capped at a reviewable length, not a task log', async () => {
    // U5: 30 rows. Pinned in the source so the cap cannot drift back upward
    // unnoticed (the constant is module-private on purpose).
    const source = await import('node:fs/promises')
        .then((fs) => fs.readFile(new URL('../modules/changes.js', import.meta.url), 'utf8'));
    assert.match(source, /const TASK_RAIL_LIMIT = 30;/);
});

// ---------------------------------------------------------------------------
// ⌘L capture: the pure selection -> chip decision (U1)
// ---------------------------------------------------------------------------

test('no selection captures the whole file: no range, no bytes', () => {
    assert.deepEqual(
        diffChipDecision({ path: 'ouroboros/loop.py', rows: [] }),
        { path: 'ouroboros/loop.py', lineStart: null, lineEnd: null, content: null },
    );
    assert.deepEqual(diffChipDecision({ path: 'a.py' }).lineStart, null);
    assert.deepEqual(diffChipDecision().content, null);
});

test('a selection is named by its NEW-side line numbers and keeps the lines verbatim', () => {
    const decision = diffChipDecision({
        path: 'ouroboros/loop.py',
        rows: [
            { newNumber: '17', text: '     def run(self):' },
            { newNumber: '', text: '-        window = COOLDOWN_S' },
            { newNumber: '18', text: '+        window = self._window_for(provider)' },
        ],
    });
    // The range spans the boundary rows' NEW numbers; the interior deletion has no
    // new number of its own and does not narrow the range.
    assert.equal(decision.lineStart, 17);
    assert.equal(decision.lineEnd, 18);
    // Verbatim means verbatim: the +/-/space prefixes survive, nothing is trimmed.
    assert.equal(decision.content, [
        '     def run(self):',
        '-        window = COOLDOWN_S',
        '+        window = self._window_for(provider)',
    ].join('\n'));
});

test('a deletion-bounded selection omits the range rather than naming wrong lines', () => {
    const decision = diffChipDecision({
        path: 'ouroboros/loop.py',
        rows: [
            { newNumber: '', text: '-        window = COOLDOWN_S' },
            { newNumber: '', text: '-        return window' },
        ],
    });
    assert.equal(decision.lineStart, null);
    assert.equal(decision.lineEnd, null);
    // The content is kept by the decision; the codec then applies its own rule
    // (bytes only under a range), so the chip degrades to the bare marker instead
    // of pointing at lines that no longer exist in the file.
    assert.equal(decision.content, '-        window = COOLDOWN_S\n-        return window');
    const chip = makeChipPart(decision);
    assert.deepEqual(chip, { type: 'chip', path: 'ouroboros/loop.py' });
    assert.equal(serializeParts([chip]), '[context: ouroboros/loop.py]');
});

test('a range serializes with the selected diff lines inlined verbatim', () => {
    const chip = makeChipPart(diffChipDecision({
        path: 'a/b.py',
        rows: [
            { newNumber: 4, text: '+first' },
            { newNumber: 5, text: '+second' },
        ],
    }));
    assert.equal(serializeParts([chip]), [
        '[context: a/b.py L4-L5]',
        '```',
        '+first',
        '+second',
        '```',
    ].join('\n'));
});

test('a single selected row is a one-line range, not a whole-file chip', () => {
    const decision = diffChipDecision({ path: 'x.py', rows: [{ newNumber: 9, text: '+only' }] });
    assert.equal(decision.lineStart, 9);
    assert.equal(decision.lineEnd, 9);
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
