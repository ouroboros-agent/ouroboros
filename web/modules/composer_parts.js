/**
 * Ordered composer parts + the reversible `[context:]` marker codec.
 *
 * ONE module owns the context-capture grammar so every producer (chat composer,
 * Changes dock, Files dock) and every consumer (chat rendering, history replay)
 * read the same bytes. The marker is a TEXT convention: nothing about the chat
 * transport, contracts.py, or the agent's system prompt changes. The agent sees
 * self-describing natural language it can act on with the tools it already has.
 *
 * Grammar
 * -------
 *   [context: <path> L<start>-L<end>]
 *   ```
 *   <the selected lines, verbatim>
 *   ```
 *   [context: <path>]
 *
 * A selection of at most MAX_CHIP_LINES lines is inlined VERBATIM in a fenced
 * block right after its marker: the agent has the exact bytes with zero extra
 * tool rounds, and the marker names the exact referent. The fence length is the
 * longest backtick run in the content plus one (minimum 3), so content that
 * itself contains fences can never terminate the block early. A selection over
 * the cap, or a whole-file chip, serializes as the bare `[context: <path>]`
 * marker and the agent reads the file itself.
 *
 * Reversibility is the invariant: `parseContent(serializeParts(parts))`
 * re-serializes to the identical string. A path that cannot round-trip (it
 * contains a newline or `]`, or is empty) is REFUSED at capture time —
 * `makeChipPart` returns null — rather than silently producing a marker that
 * parses back as prose. Anything that is not an exactly-formed marker line
 * (`[context:foo]`, leading spaces, trailing text) stays plain text.
 *
 * Producer contract (`makeChipPart` content)
 * ------------------------------------------
 * `content` is EXACTLY the lines the range names: LF separators, no trailing
 * newline, so `content.split('\n').length === lineEnd - lineStart + 1`. CRLF and
 * ONE trailing newline — what a raw editor selection or file slice usually hands
 * over — are normalized HERE so no producer has to repeat that arithmetic.
 * Content that still disagrees with the range after normalization is DROPPED and
 * the chip stays ranged-but-bare (the agent reads exactly that span itself),
 * because a marker whose claimed line count its own fence contradicts is refused
 * a compact chip by the renderer anyway: building it would only manufacture a
 * message that has to be shown as raw grammar.
 *
 * This module is pure with respect to the network: no fetch, no transport, no
 * send logic. `createComposerParts` is a thin DOM mount over the same core.
 */

import { escapeHtml, escapeHtmlAttr } from './utils.js';

/** Selections longer than this are handed to the agent as a bare marker. */
export const MAX_CHIP_LINES = 200;

const MARKER_RE = /^\[context: ([^\]\n]+?)(?: L(\d+)-L(\d+))?\]$/;
const FENCE_RE = /^(`{3,})$/;

function isPositiveInt(value) {
    return Number.isInteger(value) && value > 0;
}

/** A path is representable only if the marker grammar can round-trip it. */
export function chipPathIsRepresentable(path) {
    const raw = typeof path === 'string' ? path : '';
    if (!raw.trim()) return false;
    if (raw !== raw.trim()) return false;
    if (/[\n\r\]]/.test(raw)) return false;
    // A path whose own tail looks like the line-range suffix would parse back as
    // a different (path, range) pair — refuse it rather than mislabel the bytes.
    return !/ L\d+-L\d+$/.test(raw);
}

/**
 * Build a chip part, or return null when the path cannot round-trip. Callers
 * disclose the refusal to the owner instead of capturing a lossy marker.
 *
 * `content` is normalized and then held to the producer contract documented at
 * the top of this module: CRLF becomes LF, ONE trailing newline is stripped, and
 * bytes that still do not span exactly `lineStart..lineEnd` are dropped.
 */
export function makeChipPart({ path, lineStart = null, lineEnd = null, content = null } = {}) {
    if (!chipPathIsRepresentable(path)) return null;
    const start = isPositiveInt(lineStart) ? lineStart : null;
    const end = isPositiveInt(lineEnd) ? lineEnd : null;
    const hasRange = start !== null && end !== null && end >= start;
    const chip = { type: 'chip', path };
    if (hasRange) {
        chip.lineStart = start;
        chip.lineEnd = end;
        // Content is only meaningful with a range to locate it; a whole-file chip
        // never carries bytes (the agent reads the file).
        if (typeof content === 'string' && content !== '') {
            const normalized = content.replace(/\r\n/g, '\n').replace(/\n$/, '');
            // The range is the CLAIM; these bytes are the evidence for it. When they
            // disagree, keep the claim (it came from the real selection) and drop the
            // bytes rather than emit a fence the renderer must refuse to fold.
            if (normalized !== '' && normalized.split('\n').length === end - start + 1) {
                chip.content = normalized;
            }
        }
    }
    return chip;
}

export function makeTextPart(text) {
    const value = typeof text === 'string' ? text : '';
    return value ? { type: 'text', text: value } : null;
}

/** Human label for a chip: `name · N lines`, or `name` for a whole file. */
export function chipLabel(chip) {
    const path = String(chip?.path || '');
    const name = path.split('/').filter(Boolean).pop() || path;
    const start = Number(chip?.lineStart);
    const end = Number(chip?.lineEnd);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return name;
    const count = Math.max(1, end - start + 1);
    return `${name} · ${count} line${count === 1 ? '' : 's'}`;
}

// ---------------------------------------------------------------------------
// Parts reducer (pure; every op returns a NEW list)
// ---------------------------------------------------------------------------

/**
 * Adjacent text parts are merged, because the serialized form cannot tell them
 * apart — keeping the list normalized is what makes the codec reversible.
 */
export function normalizeParts(parts) {
    const out = [];
    for (const part of Array.isArray(parts) ? parts : []) {
        if (!part || (part.type !== 'text' && part.type !== 'chip')) continue;
        if (part.type === 'chip') {
            if (!chipPathIsRepresentable(part.path)) continue;
            out.push(part);
            continue;
        }
        const text = typeof part.text === 'string' ? part.text : '';
        if (!text) continue;
        const last = out[out.length - 1];
        if (last && last.type === 'text') out[out.length - 1] = { type: 'text', text: `${last.text}\n${text}` };
        else out.push({ type: 'text', text });
    }
    return out;
}

export function pushText(parts, text) {
    const part = makeTextPart(text);
    if (!part) return normalizeParts(parts);
    return normalizeParts([...(parts || []), part]);
}

export function pushChip(parts, chip) {
    const part = chip && chip.type === 'chip' ? chip : makeChipPart(chip || {});
    if (!part) return normalizeParts(parts);
    return normalizeParts([...(parts || []), part]);
}

/** Backspace-in-empty-input semantics: drop the trailing part. */
export function popLast(parts) {
    const list = normalizeParts(parts);
    list.pop();
    return list;
}

export function clearParts() {
    return [];
}

// ---------------------------------------------------------------------------
// Codec
// ---------------------------------------------------------------------------

function longestBacktickRun(text) {
    let best = 0;
    for (const match of String(text).matchAll(/`+/g)) {
        if (match[0].length > best) best = match[0].length;
    }
    return best;
}

export function fenceFor(content) {
    return '`'.repeat(Math.max(3, longestBacktickRun(content) + 1));
}

function serializeChip(chip) {
    const path = String(chip.path);
    const hasRange = isPositiveInt(chip.lineStart) && isPositiveInt(chip.lineEnd);
    const content = hasRange && typeof chip.content === 'string' && chip.content !== '' ? chip.content : null;
    if (content !== null) {
        const lineCount = content.split('\n').length;
        if (hasRange && lineCount <= MAX_CHIP_LINES) {
            const fence = fenceFor(content);
            return `[context: ${path} L${chip.lineStart}-L${chip.lineEnd}]\n${fence}\n${content}\n${fence}`;
        }
        // Over the inline cap: keep the (true) range, drop the bytes — the agent
        // reads exactly that span itself instead of getting a truncated excerpt.
        return `[context: ${path} L${chip.lineStart}-L${chip.lineEnd}]`;
    }
    if (hasRange) return `[context: ${path} L${chip.lineStart}-L${chip.lineEnd}]`;
    return `[context: ${path}]`;
}

/** Ordered parts -> the exact content string that is sent, stored and replayed. */
export function serializeParts(parts) {
    return normalizeParts(parts)
        .map((part) => (part.type === 'chip' ? serializeChip(part) : part.text))
        .join('\n');
}

/**
 * The exact inverse of `serializeParts`. Unrecognized or malformed marker-like
 * lines are returned as plain text, so prose that merely resembles a marker
 * survives untouched.
 */
export function parseContent(text) {
    const raw = typeof text === 'string' ? text : '';
    if (!raw) return [];
    const lines = raw.split('\n');
    const parts = [];
    let pending = [];
    const flushText = () => {
        if (!pending.length) return;
        const joined = pending.join('\n');
        pending = [];
        if (joined) parts.push({ type: 'text', text: joined });
    };
    for (let i = 0; i < lines.length; i += 1) {
        const match = MARKER_RE.exec(lines[i]);
        if (!match || !chipPathIsRepresentable(match[1])) {
            pending.push(lines[i]);
            continue;
        }
        const chip = makeChipPart({
            path: match[1],
            lineStart: match[2] ? Number(match[2]) : null,
            lineEnd: match[3] ? Number(match[3]) : null,
        });
        if (!chip) {
            pending.push(lines[i]);
            continue;
        }
        // A fenced block on the NEXT line belongs to this marker when it closes.
        // Only a RANGED marker can own inlined bytes (that is the only form the
        // serializer emits a fence for), so a fence after a whole-file marker
        // stays ordinary text and the string still round-trips.
        const fence = chip.lineStart ? FENCE_RE.exec(lines[i + 1] || '') : null;
        if (fence) {
            let close = -1;
            for (let j = i + 2; j < lines.length; j += 1) {
                if (lines[j] === fence[1]) { close = j; break; }
            }
            if (close > i + 1) {
                chip.content = lines.slice(i + 2, close).join('\n');
                i = close;
            }
        }
        flushText();
        parts.push(chip);
    }
    flushText();
    return normalizeParts(parts);
}

// ---------------------------------------------------------------------------
// Thin DOM mount (no transport, no send logic)
// ---------------------------------------------------------------------------

/**
 * Render `parts` as inline chips before a live input inside `container`.
 *
 * @param {object} options
 * @param {HTMLElement} options.container host element (gets `.composer-parts`)
 * @param {HTMLElement} options.input     the live input/textarea, kept last
 * @param {Function} [options.onChange]   called with the new parts list
 */
export function createComposerParts({ container, input, onChange = null } = {}) {
    if (!container || !input) throw new Error('createComposerParts needs container + input');
    let parts = [];
    container.classList.add('composer-parts', 'composer-parts-host');

    const emit = () => { if (typeof onChange === 'function') onChange(getParts()); };

    function paint() {
        container.querySelectorAll('[data-composer-part]').forEach((node) => node.remove());
        parts.forEach((part, index) => {
            const node = document.createElement('span');
            node.dataset.composerPart = String(index);
            if (part.type === 'chip') {
                const label = chipLabel(part);
                node.className = 'composer-part-chip';
                node.title = part.path;
                node.innerHTML = `<span class="composer-part-chip-label">${escapeHtml(label)}</span>`
                    + `<button type="button" class="composer-part-remove"`
                    + ` data-composer-part-remove="${escapeHtmlAttr(String(index))}"`
                    + ` title="Remove ${escapeHtmlAttr(label)}"`
                    + ` aria-label="Remove ${escapeHtmlAttr(label)}">×</button>`;
            } else {
                node.className = 'composer-part-text';
                node.textContent = part.text;
            }
            container.insertBefore(node, input);
        });
    }

    function setParts(next) {
        parts = normalizeParts(next);
        paint();
        return getParts();
    }

    function getParts() {
        return parts.map((part) => ({ ...part }));
    }

    /** The typed draft becomes a text part, so chip/comment order is preserved. */
    function commitDraft() {
        const draft = input.value;
        if (!draft) return getParts();
        input.value = '';
        parts = pushText(parts, draft);
        paint();
        return getParts();
    }

    function addChip(chip) {
        commitDraft();
        parts = pushChip(parts, chip);
        paint();
        emit();
        focus();
        return getParts();
    }

    function clear() {
        parts = clearParts();
        input.value = '';
        paint();
        emit();
        return getParts();
    }

    function focus() {
        try { input.focus(); } catch {}
    }

    const onRemoveClick = (event) => {
        const button = event.target.closest?.('[data-composer-part-remove]');
        if (!button || !container.contains(button)) return;
        const index = Number(button.getAttribute('data-composer-part-remove'));
        if (!Number.isInteger(index) || index < 0 || index >= parts.length) return;
        parts = normalizeParts(parts.filter((_, i) => i !== index));
        paint();
        emit();
        focus();
    };

    const onKeyDown = (event) => {
        if (event.key !== 'Backspace') return;
        if (input.value !== '' || !parts.length) return;
        event.preventDefault();
        parts = popLast(parts);
        paint();
        emit();
    };

    container.addEventListener('click', onRemoveClick);
    input.addEventListener('keydown', onKeyDown);

    return {
        getParts,
        setParts,
        addChip,
        commitDraft,
        clear,
        focus,
        /** Everything currently in the field, typed draft included. */
        serialize() {
            const draft = input.value;
            const all = draft ? pushText(parts, draft) : parts;
            return serializeParts(all);
        },
        destroy() {
            container.removeEventListener('click', onRemoveClick);
            input.removeEventListener('keydown', onKeyDown);
            container.querySelectorAll('[data-composer-part]').forEach((node) => node.remove());
            container.classList.remove('composer-parts', 'composer-parts-host');
        },
    };
}
