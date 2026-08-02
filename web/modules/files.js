/**
 * Files — READ-ONLY code browser with context capture (redesign decision 18).
 *
 * The page is a tree rail + a highlighted viewer + a bottom dock:
 *
 *   • the rail lazily lists directories (one `/api/files/list` per expand) and
 *     replaces the old breadcrumb strip; the viewer header carries the compact
 *     current path instead;
 *   • the viewer renders text files as ONE ROW PER LINE, each row carrying
 *     `data-line-number` — that attribute is the substrate the selection→range
 *     mapping resolves against, so a capture names real line numbers rather
 *     than a guess derived from character offsets;
 *   • the dock is a `composer_parts` instance: ⌘L (or the buttons) appends a
 *     context chip, comments are typed between chips, and Enter hands the
 *     ORDERED parts to the chat controller — navigation to Chat happens only
 *     after that handoff succeeds, so a failed send never loses the draft.
 *
 * Everything that MUTATED the filesystem is gone from the UI: the editor and
 * ⌘S save, new file / new directory, upload and the drag-drop overlay, the
 * copy/move clipboard, delete, and the context menu. The backend write
 * endpoints are untouched (frozen contract) — this module simply never calls
 * them, and with no editor there is no unsaved state, so the files page no
 * longer registers a `setBeforePageLeave` navigation guard.
 *
 * Reads consumed here: `/api/files/list`, `/api/files/read` (+ its
 * `content_url` for image/PDF previews) and `/api/files/download`.
 */

import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { escapeHtmlAttr } from './utils.js';
import { apiFetch } from './api_client.js';
import { downloadViaHostBridge } from './ui_helpers.js';
import { showToast } from './toast.js';
import { createComposerParts, makeChipPart } from './composer_parts.js';
import { highlightLine, languageForPath } from './code_highlight.js';

/** Rows rendered by the "Go to file…" filter before it stops listing. */
const FILTER_RESULT_LIMIT = 200;

function formatFileSize(size) {
    const num = Number(size);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num < 1024) return `${num} B`;
    if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
    return `${(num / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Selection boundaries -> an inclusive line range (plan §5.1). PURE: the DOM
 * layer resolves each boundary to a `data-line-number` row and passes the plain
 * shape here, which is also what the node tests exercise.
 *
 * Rules, in order:
 *   • both lines must be positive integers, else there is nothing to name;
 *   • boundaries may arrive reversed (a backward drag puts focus before anchor),
 *     so they are ORDERED here rather than trusted;
 *   • a collapsed selection (same line, same offset) captures nothing — a bare
 *     caret is not a range;
 *   • an END boundary at offset 0 of a LATER line sits before that line's first
 *     character, so that line is excluded (dragging just past a line break must
 *     not silently widen the capture by one line).
 */
export function selectionLineRange(boundaries = {}) {
    const startLine = Number(boundaries.startLine);
    const endLine = Number(boundaries.endLine);
    if (!Number.isInteger(startLine) || !Number.isInteger(endLine)) return null;
    if (startLine < 1 || endLine < 1) return null;
    const offsetOf = (value) => {
        const num = Number(value);
        return Number.isFinite(num) && num > 0 ? Math.floor(num) : 0;
    };
    let first = { line: startLine, offset: offsetOf(boundaries.startOffset) };
    let last = { line: endLine, offset: offsetOf(boundaries.endOffset) };
    if (last.line < first.line || (last.line === first.line && last.offset < first.offset)) {
        [first, last] = [last, first];
    }
    if (first.line === last.line && first.offset === last.offset) return null;
    let lineEnd = last.line;
    if (lineEnd > first.line && last.offset === 0) lineEnd -= 1;
    if (lineEnd < first.line) return null;
    return { lineStart: first.line, lineEnd };
}

/** The `data-line-number` of the row owning `node`, or null when outside `root`. */
function rowLineNumber(node, root) {
    if (!root) return null;
    const element = node instanceof Element ? node : (node?.parentElement || null);
    if (!element || !root.contains(element)) return null;
    const row = element.closest('[data-line-number]');
    if (!row || !root.contains(row)) return null;
    const line = Number(row.dataset.lineNumber);
    return Number.isInteger(line) && line > 0 ? line : null;
}

/**
 * Current window selection -> a line range inside `root`, or null.
 *
 * `getRangeAt(0)` already reports its boundaries in document order (the Range
 * API normalizes a backward anchor/focus pair); `selectionLineRange` orders them
 * again so the contract holds for any caller. BOTH boundaries must resolve to
 * rows of THIS viewer — a selection that starts in the viewer and ends in the
 * dock, the rail, or another page captures nothing rather than a wrong range.
 */
export function readViewerSelection(root) {
    const selection = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    const startLine = rowLineNumber(range.startContainer, root);
    const endLine = rowLineNumber(range.endContainer, root);
    if (startLine === null || endLine === null) return null;
    return selectionLineRange({
        startLine,
        startOffset: range.startOffset,
        endLine,
        endOffset: range.endOffset,
    });
}

export function initFiles({ state: appState, showPage, getChatController } = {}) {
    const page = document.createElement('div');
    page.id = 'page-files';
    page.className = 'page app-page-glass';
    page.innerHTML = `
        ${renderPageHeader({
            title: 'Files',
            icon: PAGE_ICONS.files,
            actionsHtml: '<button class="btn btn-default" id="files-refresh" type="button">Refresh</button>',
        })}
        <div class="files-layout">
            <section class="files-rail">
                <div class="files-rail-head">
                    <input id="files-filter" class="files-filter" type="text" placeholder="Go to file…" autocomplete="off" spellcheck="false">
                    <p class="files-rail-hint">Select code in the viewer, then ⌘L to add it to chat context.</p>
                </div>
                <nav id="files-tree" class="files-tree" aria-label="File tree"></nav>
            </section>
            <section class="files-viewer">
                <div class="files-viewer-head">
                    <div class="files-viewer-ident">
                        <div id="files-viewer-path" class="files-viewer-path">Files</div>
                        <div id="files-viewer-meta" class="files-viewer-meta"></div>
                    </div>
                    <div class="files-viewer-actions">
                        <button class="btn btn-default" id="files-download" type="button" hidden>Download</button>
                        <button class="btn btn-default files-capture-btn" id="files-capture" type="button" hidden>Add to chat <kbd class="files-kbd">⌘L</kbd></button>
                    </div>
                </div>
                <div id="files-viewer-body" class="files-viewer-body"></div>
                <button class="files-selection-btn" id="files-capture-selection" type="button" hidden>Add selection <kbd class="files-kbd">⌘L</kbd></button>
            </section>
            <div class="files-dock" data-capture-dock>
                <div class="files-dock-field">
                    <div class="files-dock-parts" id="files-dock-parts">
                        <textarea id="files-dock-input" class="files-dock-input" rows="1" placeholder="⌘L adds the selected lines · type comments between · Enter sends to chat" autocomplete="off" spellcheck="false"></textarea>
                    </div>
                    <button class="btn btn-primary files-dock-send" id="files-dock-send" type="button">Send</button>
                </div>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    const treeEl = page.querySelector('#files-tree');
    const filterEl = page.querySelector('#files-filter');
    const viewerPathEl = page.querySelector('#files-viewer-path');
    const viewerMetaEl = page.querySelector('#files-viewer-meta');
    const viewerBodyEl = page.querySelector('#files-viewer-body');
    const downloadBtn = page.querySelector('#files-download');
    const captureBtn = page.querySelector('#files-capture');
    const selectionBtn = page.querySelector('#files-capture-selection');
    const dockPartsEl = page.querySelector('#files-dock-parts');
    const dockInputEl = page.querySelector('#files-dock-input');
    const dockSendBtn = page.querySelector('#files-dock-send');
    const refreshBtn = page.querySelector('#files-refresh');

    const state = {
        rootPath: '',
        filter: '',
        /** dirPath -> { entries, loaded, loading, expanded, truncated } */
        dirs: new Map(),
        activePath: '',
        activeDisplayPath: '',
        activeName: '',
        activeIsText: false,
        activeLines: [],
        activeTruncated: false,
        selectionRange: null,
        lastSelectionRange: null,
    };

    // ---------------------------------------------------------------------
    // Tree rail
    // ---------------------------------------------------------------------

    function dirNode(path) {
        const key = path || '.';
        if (!state.dirs.has(key)) {
            state.dirs.set(key, { entries: [], loaded: false, loading: false, expanded: key === '.', truncated: false });
        }
        return state.dirs.get(key);
    }

    async function loadDir(path) {
        const key = path || '.';
        const node = dirNode(key);
        if (node.loading) return node;
        node.loading = true;
        try {
            const params = new URLSearchParams();
            // The root is requested WITHOUT a path so the backend picks its own
            // configured default root; deeper levels ask for their exact path.
            if (key !== '.') params.set('path', key);
            const query = params.toString();
            const resp = await apiFetch(`/api/files/list${query ? `?${query}` : ''}`);
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            state.rootPath = data.root_path || state.rootPath;
            node.entries = Array.isArray(data.entries) ? data.entries : [];
            node.truncated = Boolean(data.truncated);
            node.loaded = true;
        } finally {
            node.loading = false;
        }
        return node;
    }

    function treeRow({ label, depth, kind, path, expanded, active }) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = `files-tree-row files-tree-${kind}${active ? ' is-active' : ''}`;
        row.dataset.path = path;
        row.title = path;
        row.setAttribute('aria-label', kind === 'dir' ? `${path} (directory)` : path);
        if (kind === 'dir') row.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        if (active) row.setAttribute('aria-current', 'true');
        row.style.setProperty('--files-indent', String(depth));
        const twist = document.createElement('span');
        twist.className = 'files-tree-twist';
        twist.textContent = kind === 'dir' ? (expanded ? '▾' : '▸') : '';
        twist.setAttribute('aria-hidden', 'true');
        const name = document.createElement('span');
        name.className = 'files-tree-name';
        name.textContent = label;
        row.append(twist, name);
        row.addEventListener('click', () => {
            if (kind === 'dir') toggleDir(path).catch(reportError);
            else openFile(path).catch(reportError);
        });
        return row;
    }

    function treeNote(text) {
        const note = document.createElement('p');
        note.className = 'files-tree-note';
        note.textContent = text;
        return note;
    }

    function appendChildren(dirPath, depth) {
        const node = state.dirs.get(dirPath || '.');
        if (!node) return;
        for (const entry of node.entries) {
            const isDir = entry.type === 'dir';
            const child = isDir ? state.dirs.get(entry.path) : null;
            treeEl.appendChild(treeRow({
                label: isDir ? `${entry.name}/` : entry.name,
                depth,
                kind: isDir ? 'dir' : 'file',
                path: entry.path,
                expanded: Boolean(child?.expanded),
                active: !isDir && entry.path === state.activePath,
            }));
            if (isDir && child?.expanded) {
                if (child.loaded) appendChildren(entry.path, depth + 1);
                else treeEl.appendChild(treeNote('Loading…'));
            }
        }
        if (node.truncated) treeEl.appendChild(treeNote('Listing truncated by the server.'));
    }

    /** Filter matches what is already LOADED — no server-side search exists. */
    function appendFilterResults(needle) {
        const matches = [];
        for (const node of state.dirs.values()) {
            for (const entry of node.entries) {
                if (String(entry.path || '').toLowerCase().includes(needle)) matches.push(entry);
            }
        }
        matches.sort((a, b) => String(a.path).localeCompare(String(b.path)));
        for (const entry of matches.slice(0, FILTER_RESULT_LIMIT)) {
            const isDir = entry.type === 'dir';
            treeEl.appendChild(treeRow({
                label: isDir ? `${entry.path}/` : entry.path,
                depth: 0,
                kind: isDir ? 'dir' : 'file',
                path: entry.path,
                expanded: Boolean(state.dirs.get(entry.path)?.expanded),
                active: !isDir && entry.path === state.activePath,
            }));
        }
        if (!matches.length) treeEl.appendChild(treeNote('No matches in the folders opened so far.'));
        else if (matches.length > FILTER_RESULT_LIMIT) treeEl.appendChild(treeNote(`Showing the first ${FILTER_RESULT_LIMIT} of ${matches.length} matches.`));
    }

    function renderTree() {
        treeEl.replaceChildren();
        const needle = state.filter.trim().toLowerCase();
        if (needle) {
            appendFilterResults(needle);
            return;
        }
        const root = state.dirs.get('.');
        if (!root?.loaded) {
            treeEl.appendChild(treeNote(root?.loading ? 'Loading…' : 'No files listed.'));
            return;
        }
        if (!root.entries.length) {
            treeEl.appendChild(treeNote('This folder is empty.'));
            return;
        }
        appendChildren('.', 0);
    }

    async function toggleDir(path) {
        const node = dirNode(path);
        node.expanded = !node.expanded;
        renderTree();
        if (node.expanded && !node.loaded) {
            await loadDir(path);
            renderTree();
        }
    }

    // ---------------------------------------------------------------------
    // Viewer
    // ---------------------------------------------------------------------

    function setViewerHeader({ path, meta }) {
        viewerPathEl.textContent = path || 'Files';
        viewerMetaEl.textContent = meta || '';
    }

    function resetActiveFile() {
        state.activePath = '';
        state.activeDisplayPath = '';
        state.activeName = '';
        state.activeIsText = false;
        state.activeLines = [];
        state.activeTruncated = false;
        state.selectionRange = null;
        state.lastSelectionRange = null;
        downloadBtn.hidden = true;
        captureBtn.hidden = true;
        selectionBtn.hidden = true;
    }

    function showPlaceholder(text) {
        const note = document.createElement('p');
        note.className = 'files-viewer-placeholder';
        note.textContent = text;
        viewerBodyEl.replaceChildren(note);
    }

    function reportError(err) {
        const message = err instanceof Error ? err.message : String(err);
        showToast(`Files: ${message}`, 'danger');
    }

    /**
     * `content` is a PREFIX when `truncated` is true, so the row count describes
     * what is on screen ("N lines shown · preview truncated") and never claims a
     * total the client cannot know.
     */
    function previewLines(content) {
        const lines = String(content ?? '').split('\n');
        // A trailing newline produces one empty tail element that is not a line.
        if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
        return lines;
    }

    function renderCode(lines, language) {
        const code = document.createElement('div');
        code.className = 'files-code';
        lines.forEach((line, index) => {
            const row = document.createElement('div');
            row.className = 'files-code-row';
            row.dataset.lineNumber = String(index + 1);
            const number = document.createElement('span');
            number.className = 'files-code-num';
            number.textContent = String(index + 1);
            number.setAttribute('aria-hidden', 'true');
            const text = document.createElement('code');
            text.className = 'files-code-text';
            // Safe by construction: the highlighter escapes every lexeme before
            // wrapping it (web/modules/code_highlight.js).
            text.innerHTML = highlightLine(line, language);
            row.append(number, text);
            code.appendChild(row);
        });
        viewerBodyEl.replaceChildren(code);
    }

    function fileMeta(data, extra = '') {
        const size = formatFileSize(data.size);
        return [size, extra].filter(Boolean).join(' · ');
    }

    async function openFile(path) {
        const params = new URLSearchParams({ path });
        const resp = await apiFetch(`/api/files/read?${params.toString()}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        resetActiveFile();
        state.activePath = data.path || path;
        state.activeDisplayPath = data.display_path || state.activePath;
        state.activeName = data.name || String(path).split('/').filter(Boolean).pop() || 'file';
        state.activeTruncated = Boolean(data.truncated);
        downloadBtn.hidden = false;
        captureBtn.hidden = false;

        if (data.is_image && data.content_url) {
            setViewerHeader({ path: state.activeDisplayPath, meta: fileMeta(data, data.media_type || 'image') });
            const image = document.createElement('img');
            image.className = 'files-preview-image';
            image.src = data.content_url;
            image.alt = state.activeName;
            viewerBodyEl.replaceChildren(image);
        } else if (data.is_pdf && data.content_url) {
            setViewerHeader({ path: state.activeDisplayPath, meta: fileMeta(data, 'PDF preview') });
            const frame = document.createElement('div');
            frame.className = 'files-preview-frame-host';
            frame.innerHTML = `<iframe class="files-preview-frame" sandbox="allow-same-origin" src="${escapeHtmlAttr(data.content_url)}" title="${escapeHtmlAttr(state.activeName)}"></iframe>`;
            viewerBodyEl.replaceChildren(frame);
        } else if (data.is_text) {
            state.activeIsText = true;
            state.activeLines = previewLines(data.content);
            const shown = `${state.activeLines.length} lines${state.activeTruncated ? ' shown · preview truncated' : ''}`;
            setViewerHeader({ path: state.activeDisplayPath, meta: fileMeta(data, shown) });
            renderCode(state.activeLines, languageForPath(state.activeDisplayPath));
        } else {
            setViewerHeader({ path: state.activeDisplayPath, meta: fileMeta(data, 'binary or unsupported preview') });
            showPlaceholder('No text preview for this file type. Download it, or add its path to chat context.');
        }
        renderTree();
        syncSelectionButton();
    }

    // ---------------------------------------------------------------------
    // Context capture (⌘L)
    // ---------------------------------------------------------------------

    const dock = createComposerParts({ container: dockPartsEl, input: dockInputEl });

    function clearWindowSelection() {
        state.selectionRange = null;
        state.lastSelectionRange = null;
        selectionBtn.hidden = true;
        const selection = window.getSelection?.();
        try { selection?.removeAllRanges?.(); } catch {}
    }

    /**
     * Two pieces of state, because the two capture intents differ:
     *
     *   • `selectionRange` is the LIVE range. It drives the sticky button's
     *     visibility, and it is what ⌘L / "Add to chat" read — those must fall
     *     back to a whole-file chip when nothing is selected, so a remembered
     *     range would be a lie.
     *   • `lastSelectionRange` is the sticky mouseup cache the handoff asks for.
     *     Clicking the button can collapse the selection before the click handler
     *     runs (observed in Chromium even with `mousedown` default suppressed), so
     *     the button — which is only reachable WHILE a selection is visible —
     *     falls back to this. It is dropped after a capture and on file switch.
     */
    function syncSelectionButton() {
        if (!state.activeIsText || !page.classList.contains('active')) {
            state.selectionRange = null;
            selectionBtn.hidden = true;
            return;
        }
        state.selectionRange = readViewerSelection(viewerBodyEl);
        if (state.selectionRange) state.lastSelectionRange = state.selectionRange;
        selectionBtn.hidden = !state.selectionRange;
    }

    /**
     * Append a context chip to the DOCK (never straight to chat): the owner sees
     * exactly what will be sent and can type comments around it.
     */
    function capture({ selectionOnly = false } = {}) {
        if (!state.activePath) {
            showToast('Open a file first — there is nothing to add to chat context yet.', 'warn');
            return false;
        }
        const live = state.activeIsText ? (readViewerSelection(viewerBodyEl) || state.selectionRange) : null;
        // Only the selection button may fall back to the sticky cache (see
        // syncSelectionButton); ⌘L with nothing selected means "the whole file".
        const range = live || (selectionOnly ? state.lastSelectionRange : null);
        if (!range && selectionOnly) return false;
        const chip = range
            ? makeChipPart({
                path: state.activeDisplayPath,
                lineStart: range.lineStart,
                lineEnd: range.lineEnd,
                // Full lines, verbatim, from the payload the viewer rendered —
                // the codec decides itself whether they inline (≤200 lines) or
                // degrade to the bare marker.
                content: state.activeLines.slice(range.lineStart - 1, range.lineEnd).join('\n'),
            })
            : makeChipPart({ path: state.activeDisplayPath });
        if (!chip) {
            showToast(`Nothing captured: "${state.activeDisplayPath}" cannot be written as a context reference.`, 'warn');
            return false;
        }
        dock.addChip(chip);  // commits any typed draft first, then focuses the dock
        clearWindowSelection();
        return true;
    }

    async function sendDock() {
        dock.commitDraft();
        const parts = dock.getParts();
        if (!parts.length) return;
        const controller = typeof getChatController === 'function' ? getChatController() : null;
        if (!controller || typeof controller.sendParts !== 'function') {
            showToast('Chat is unavailable, so nothing was sent. Your draft is kept.', 'danger');
            return;
        }
        let sent = false;
        try {
            sent = (await controller.sendParts(parts)) !== false;
        } catch {
            sent = false;
        }
        if (!sent) {
            showToast('The message was not sent. Your draft is kept.', 'danger');
            return;
        }
        dock.clear();
        if (typeof showPage === 'function') await showPage('chat');
    }

    // ---------------------------------------------------------------------
    // Wiring
    // ---------------------------------------------------------------------

    filterEl.addEventListener('input', () => {
        state.filter = filterEl.value || '';
        renderTree();
    });

    downloadBtn.addEventListener('click', () => {
        if (!state.activePath) return;
        const params = new URLSearchParams({ path: state.activePath });
        downloadViaHostBridge(`/api/files/download?${params.toString()}`, state.activeName)
            .catch(reportError);
    });

    captureBtn.addEventListener('click', () => { capture(); });

    // The buttons are the GUARANTEED capture path (decision 10), so they must not
    // destroy the selection they are about to read: mousedown default = focus
    // change = collapsed selection.
    selectionBtn.addEventListener('mousedown', (event) => event.preventDefault());
    captureBtn.addEventListener('mousedown', (event) => event.preventDefault());
    selectionBtn.addEventListener('click', () => { capture({ selectionOnly: true }); });

    viewerBodyEl.addEventListener('mouseup', () => syncSelectionButton());
    document.addEventListener('selectionchange', () => syncSelectionButton());

    dockInputEl.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.shiftKey) return;
        event.preventDefault();
        sendDock().catch(reportError);
    });
    dockSendBtn.addEventListener('click', () => { sendDock().catch(reportError); });

    // The global ⌘L handler (app.js `[anchor:phase-C]`) knows nothing about this
    // module: it names the active page in one event and the owning page listens.
    window.addEventListener('ouro:capture-selection', (event) => {
        if (event.detail?.page !== 'files') return;
        if (!page.classList.contains('active')) return;
        capture();
    });

    refreshBtn.addEventListener('click', () => { refreshAll().catch(reportError); });

    /** Re-list every folder already open (expansion is preserved) + the file. */
    async function refreshAll() {
        const paths = [...state.dirs.entries()]
            .filter(([, node]) => node.loaded)
            .map(([path]) => path);
        for (const path of paths) await loadDir(path);
        renderTree();
        if (state.activePath) await openFile(state.activePath);
    }

    if (appState) appState.filesState = state;

    renderTree();
    loadDir('.').then(() => {
        renderTree();
        setViewerHeader({ path: state.rootPath || 'Files', meta: 'Read-only browser · pick a file in the tree' });
        showPlaceholder('Open a file from the tree to read it, download it, or add lines to chat context with ⌘L.');
    }).catch(reportError);

    return { capture, state };
}
