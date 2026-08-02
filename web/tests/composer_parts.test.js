import assert from 'node:assert/strict';
import test from 'node:test';

import {
    MAX_CHIP_LINES,
    chipLabel,
    chipPathIsRepresentable,
    clearParts,
    fenceFor,
    makeChipPart,
    normalizeParts,
    parseContent,
    popLast,
    pushChip,
    pushText,
    serializeParts,
} from '../modules/composer_parts.js';

const chip = (over = {}) => makeChipPart({ path: 'ouroboros/loop.py', lineStart: 10, lineEnd: 12, ...over });
// A capture whose bytes really are the lines its range names — the producer
// contract every chip-with-content fixture below has to satisfy.
const captured = (content, lineStart = 10) => makeChipPart({
    path: 'ouroboros/loop.py',
    lineStart,
    lineEnd: lineStart + content.split('\n').length - 1,
    content,
});

test('interleaved text and chips serialize in order and parse back exactly', () => {
    let parts = pushText([], 'look at this');
    parts = pushChip(parts, captured('a = 1\nb = 2'));
    parts = pushText(parts, 'then fix the retry');
    parts = pushChip(parts, makeChipPart({ path: 'README.md' }));

    const text = serializeParts(parts);
    assert.equal(text, [
        'look at this',
        '[context: ouroboros/loop.py L10-L11]',
        '```',
        'a = 1',
        'b = 2',
        '```',
        'then fix the retry',
        '[context: README.md]',
    ].join('\n'));

    const parsed = parseContent(text);
    assert.deepEqual(parsed, parts);
    // Reversibility invariant: the codec is a string-level fixpoint.
    assert.equal(serializeParts(parsed), text);
});

test('a whole-file chip carries no range and no inlined bytes', () => {
    const whole = makeChipPart({ path: 'docs/ARCHITECTURE.md', content: 'ignored without a range' });
    assert.deepEqual(whole, { type: 'chip', path: 'docs/ARCHITECTURE.md' });
    assert.equal(serializeParts([whole]), '[context: docs/ARCHITECTURE.md]');
});

test('fence length escalates past the longest backtick run in the content', () => {
    assert.equal(fenceFor('plain'), '```');
    assert.equal(fenceFor('a ``` b'), '````');
    assert.equal(fenceFor('~~~\n`````\n'), '``````');

    const nested = 'before\n```\nnested\n```\nafter';
    const text = serializeParts([captured(nested)]);
    assert.equal(text, [
        '[context: ouroboros/loop.py L10-L14]',
        '````',
        'before',
        '```',
        'nested',
        '```',
        'after',
        '````',
    ].join('\n'));
    assert.deepEqual(parseContent(text), [captured(nested)]);
});

test('a selection over the inline cap keeps its range but drops the bytes', () => {
    const under = 'x\n'.repeat(MAX_CHIP_LINES - 1) + 'x';
    assert.equal(under.split('\n').length, MAX_CHIP_LINES);
    assert.ok(serializeParts([captured(under)]).includes('```'));

    const over = 'x\n'.repeat(MAX_CHIP_LINES) + 'x';
    assert.equal(over.split('\n').length, MAX_CHIP_LINES + 1);
    // The range is true information: the agent reads exactly that span itself.
    assert.equal(
        serializeParts([captured(over)]),
        `[context: ouroboros/loop.py L10-L${10 + MAX_CHIP_LINES}]`,
    );
});

// --- producer contract: content must BE the range's lines --------------------

test('a CRLF capture is normalized to LF and still chips', () => {
    // A raw editor/file slice arrives with \r\n; the codec's grammar is LF, and
    // making every producer strip \r would be four places to get it wrong.
    const part = makeChipPart({
        path: 'a.py', lineStart: 1, lineEnd: 3, content: 'one\r\ntwo\r\nthree',
    });
    assert.equal(part.content, 'one\ntwo\nthree');
    const text = serializeParts([part]);
    assert.equal(text, '[context: a.py L1-L3]\n```\none\ntwo\nthree\n```');
    assert.equal(serializeParts(parseContent(text)), text);
});

test('ONE trailing newline on a capture is stripped, not counted as a line', () => {
    // `lines.slice(a, b).join('\n') + '\n'` is the natural way to cut a file, and
    // that trailing byte would otherwise make a 3-line capture claim 4 lines.
    const part = makeChipPart({ path: 'a.py', lineStart: 1, lineEnd: 3, content: 'one\ntwo\nthree\n' });
    assert.equal(part.content, 'one\ntwo\nthree');
    assert.match(serializeParts([part]), /```\none\ntwo\nthree\n```$/);
    // CRLF + trailing CRLF together, the Windows version of the same slice.
    assert.equal(
        makeChipPart({ path: 'a.py', lineStart: 1, lineEnd: 2, content: 'one\r\ntwo\r\n' }).content,
        'one\ntwo',
    );
    // TWO trailing newlines are a real blank final line: it is kept and counted.
    assert.equal(
        makeChipPart({ path: 'a.py', lineStart: 1, lineEnd: 3, content: 'one\ntwo\n\n' }).content,
        'one\ntwo\n',
    );
});

test('content that still disagrees with its range is DROPPED, not shipped', () => {
    // The range came from the real selection, so it is the claim worth keeping;
    // the bytes are what cannot be trusted. A ranged-bare chip sends the agent to
    // read exactly that span instead of a fence contradicting its own label.
    const short = makeChipPart({ path: 'a.py', lineStart: 10, lineEnd: 12, content: 'only one line' });
    assert.deepEqual(short, { type: 'chip', path: 'a.py', lineStart: 10, lineEnd: 12 });
    assert.equal(serializeParts([short]), '[context: a.py L10-L12]');

    const long = makeChipPart({ path: 'a.py', lineStart: 1, lineEnd: 2, content: 'a\nb\nc\nd' });
    assert.equal('content' in long, false);
    assert.equal(serializeParts([long]), '[context: a.py L1-L2]');

    // A capture that is nothing but a newline normalizes to empty: no bytes.
    assert.equal('content' in makeChipPart({ path: 'a.py', lineStart: 1, lineEnd: 1, content: '\n' }), false);
});

test('malformed and lookalike markers stay plain text', () => {
    const cases = [
        '[context:no-space.py]',
        ' [context: leading-space.py]',
        '[context: trailing.py] and more prose',
        '[context: two.py][context: markers.py]',
        '[context: ]',
        'CONTEXT: not-a-marker.py',
    ];
    for (const raw of cases) {
        assert.deepEqual(parseContent(raw), [{ type: 'text', text: raw }], raw);
        assert.equal(serializeParts(parseContent(raw)), raw, raw);
    }
});

test('a half-written range is read as part of the path, and still round-trips', () => {
    // The grammar allows spaces in a path, so `L10-L` is not a range — it is the
    // tail of the named file. The referent stays exactly the bytes the owner
    // captured, which is the property the codec has to guarantee.
    const raw = '[context: bad.py L10-L]';
    assert.deepEqual(parseContent(raw), [{ type: 'chip', path: 'bad.py L10-L' }]);
    assert.equal(serializeParts(parseContent(raw)), raw);
});

test('a fenced block after a whole-file marker is ordinary text, not captured bytes', () => {
    const text = '[context: README.md]\n```\nnot mine\n```';
    assert.deepEqual(parseContent(text), [
        { type: 'chip', path: 'README.md' },
        { type: 'text', text: '```\nnot mine\n```' },
    ]);
    assert.equal(serializeParts(parseContent(text)), text);
});

test('an unclosed fence after a marker leaves the fence as text', () => {
    const text = '[context: a.py L1-L2]\n```\nstill open';
    assert.deepEqual(parseContent(text), [
        { type: 'chip', path: 'a.py', lineStart: 1, lineEnd: 2 },
        { type: 'text', text: '```\nstill open' },
    ]);
    assert.equal(serializeParts(parseContent(text)), text);
});

test('paths that cannot round-trip are refused at capture time', () => {
    assert.equal(makeChipPart({ path: 'has]bracket.py' }), null);
    assert.equal(makeChipPart({ path: 'has\nnewline.py' }), null);
    assert.equal(makeChipPart({ path: 'has\rreturn.py' }), null);
    assert.equal(makeChipPart({ path: '   ' }), null);
    assert.equal(makeChipPart({ path: ' untrimmed.py' }), null);
    assert.equal(makeChipPart({}), null);
    // A path whose own tail mimics the range suffix would parse back as a
    // different (path, range) pair.
    assert.equal(makeChipPart({ path: 'weird L1-L2' }), null);
    assert.equal(chipPathIsRepresentable('ouroboros/loop.py'), true);
    // A refused chip never enters the list.
    assert.deepEqual(pushChip([], { path: 'bad]path' }), []);
});

test('an invalid range degrades to a whole-file chip rather than lying', () => {
    assert.deepEqual(makeChipPart({ path: 'a.py', lineStart: 5, lineEnd: 2 }), { type: 'chip', path: 'a.py' });
    assert.deepEqual(makeChipPart({ path: 'a.py', lineStart: 0, lineEnd: 3 }), { type: 'chip', path: 'a.py' });
    assert.deepEqual(makeChipPart({ path: 'a.py', lineStart: 1.5, lineEnd: 3 }), { type: 'chip', path: 'a.py' });
});

test('reducer: pushText merges, popLast/backspace drops the trailing part, clear empties', () => {
    let parts = pushText([], 'first');
    parts = pushText(parts, 'second');
    assert.deepEqual(parts, [{ type: 'text', text: 'first\nsecond' }]);

    parts = pushChip(parts, chip());
    parts = pushText(parts, 'tail');
    assert.equal(parts.length, 3);

    parts = popLast(parts);
    assert.deepEqual(parts.map((p) => p.type), ['text', 'chip']);
    parts = popLast(parts);
    assert.deepEqual(parts.map((p) => p.type), ['text']);
    parts = popLast(parts);
    assert.deepEqual(parts, []);
    // Popping an empty list is a no-op, not a throw (backspace keeps working).
    assert.deepEqual(popLast(parts), []);
    assert.deepEqual(clearParts(), []);

    // Empty typed drafts never become parts.
    assert.deepEqual(pushText([], ''), []);
    assert.deepEqual(pushText([], null), []);
});

test('normalizeParts drops junk and keeps chip/text order', () => {
    const parts = normalizeParts([
        null,
        { type: 'bogus' },
        { type: 'text', text: '' },
        { type: 'text', text: 'a' },
        { type: 'text', text: 'b' },
        { type: 'chip', path: 'bad]path' },
        chip(),
    ]);
    assert.deepEqual(parts, [{ type: 'text', text: 'a\nb' }, chip()]);
    assert.deepEqual(parseContent(''), []);
    assert.deepEqual(serializeParts(undefined), '');
});

test('chip labels name the file and the honest line count', () => {
    assert.equal(chipLabel(chip()), 'loop.py · 3 lines');
    assert.equal(chipLabel(makeChipPart({ path: 'a/b.py', lineStart: 4, lineEnd: 4 })), 'b.py · 1 line');
    assert.equal(chipLabel(makeChipPart({ path: 'README.md' })), 'README.md');
});
