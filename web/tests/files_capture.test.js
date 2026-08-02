/**
 * Selection → line-range mapping (plan §5.1) plus the chip the Files page builds
 * from it. The mapper is a PURE function over boundary shapes, so the pinned
 * cases run without a DOM: the DOM layer's only job is resolving each boundary
 * to a `data-line-number` row and handing the numbers over.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { selectionLineRange } from '../modules/files.js';
import { makeChipPart, serializeParts } from '../modules/composer_parts.js';

const range = (startLine, startOffset, endLine, endOffset) =>
    selectionLineRange({ startLine, startOffset, endLine, endOffset });

test('a forward selection spans both boundary lines inclusively', () => {
    assert.deepEqual(range(10, 4, 14, 7), { lineStart: 10, lineEnd: 14 });
});

test('a backward selection is ordered, not rejected', () => {
    // Dragging upward puts focus before anchor; both directions must name the
    // same range.
    assert.deepEqual(range(14, 7, 10, 4), { lineStart: 10, lineEnd: 14 });
    assert.deepEqual(range(14, 7, 10, 4), range(10, 4, 14, 7));
});

test('an end boundary at offset 0 excludes that line', () => {
    // The caret sits BEFORE line 15's first character, so line 15 is not selected.
    assert.deepEqual(range(10, 0, 15, 0), { lineStart: 10, lineEnd: 14 });
    // One character into the line puts it back in.
    assert.deepEqual(range(10, 0, 15, 1), { lineStart: 10, lineEnd: 15 });
    // Backward selection ending at offset 0 of the later line: same rule.
    assert.deepEqual(range(15, 0, 10, 2), { lineStart: 10, lineEnd: 14 });
});

test('a single line stays a single line', () => {
    assert.deepEqual(range(7, 0, 7, 12), { lineStart: 7, lineEnd: 7 });
    assert.deepEqual(range(7, 12, 7, 0), { lineStart: 7, lineEnd: 7 });
    // Offset 0 on the SAME line never empties the range.
    assert.deepEqual(range(7, 0, 7, 1), { lineStart: 7, lineEnd: 7 });
});

test('a collapsed selection captures nothing', () => {
    assert.equal(range(7, 5, 7, 5), null);
    assert.equal(range(1, 0, 1, 0), null);
});

test('an offset-0-only span that would collapse below its start is refused', () => {
    // Start at line 9 offset 0, end at line 9's own start via the next row: after
    // excluding the boundary line there is nothing left to name.
    assert.equal(range(9, 0, 9, 0), null);
});

test('non-resolving boundaries are refused rather than guessed', () => {
    assert.equal(selectionLineRange(), null);
    assert.equal(selectionLineRange({}), null);
    assert.equal(range(null, 0, 4, 2), null);
    assert.equal(range(0, 0, 4, 2), null);
    assert.equal(range(-3, 0, 4, 2), null);
    assert.equal(range(2.5, 0, 4, 2), null);
    assert.equal(range(2, 0, undefined, 2), null);
});

test('negative or non-numeric offsets read as the line start', () => {
    assert.deepEqual(range(4, -5, 6, 3), { lineStart: 4, lineEnd: 6 });
    assert.deepEqual(range(4, 'x', 6, 3), { lineStart: 4, lineEnd: 6 });
    // A non-numeric END offset reads as 0 -> the last line is excluded.
    assert.deepEqual(range(4, 1, 6, 'x'), { lineStart: 4, lineEnd: 5 });
});

test('the mapped range drives the chip: full lines, verbatim, in the marker', () => {
    const lines = [
        'class ToolExecutor:',
        '',
        '    async def run(self, call):',
        '        return await self._dispatch(call)',
    ];
    const mapped = range(1, 6, 3, 9);
    assert.deepEqual(mapped, { lineStart: 1, lineEnd: 3 });

    const chip = makeChipPart({
        path: '/Users/o/ouroboros/tools.py',
        lineStart: mapped.lineStart,
        lineEnd: mapped.lineEnd,
        content: lines.slice(mapped.lineStart - 1, mapped.lineEnd).join('\n'),
    });
    assert.equal(serializeParts([chip]), [
        '[context: /Users/o/ouroboros/tools.py L1-L3]',
        '```',
        'class ToolExecutor:',
        '',
        '    async def run(self, call):',
        '```',
    ].join('\n'));
});

test('an unrepresentable path yields no chip (the page discloses instead)', () => {
    assert.equal(makeChipPart({ path: 'weird]name.py', lineStart: 1, lineEnd: 2, content: 'x' }), null);
    assert.equal(makeChipPart({ path: '', lineStart: 1, lineEnd: 2, content: 'x' }), null);
});
