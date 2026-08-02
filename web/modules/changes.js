/**
 * Changes screen: review one task's diff and ask for edits.
 *
 * Left rail = the recent task list (newest first) with a project badge; middle =
 * the file list of the selected task, derived from the patch bytes by
 * `patch_parse.js`; main pane = the unified or split renderer over the same
 * parsed hunks; bottom dock = an ordered composer-parts field whose "Request
 * edits" hands the parts to the chat controller.
 *
 * Two honesty rules shape this module:
 *   1. The file list, per-file status and +/- counts come from the SAME patch the
 *      renderer shows — never a second server-side stat source that could
 *      disagree with what the owner is reading.
 *   2. A diff the server could not produce is disclosed, never smoothed over: a
 *      `pending` task says its artifacts are not finalized, a `blocked` one names
 *      its typed blockers, and drift says HEAD moved — no page ever implies "no
 *      changes" when the truth is "we could not tell".
 *
 * There is deliberately NO approve action here (owner-locked scope): the only
 * action is asking the agent for edits.
 */

import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { escapeHtmlAttr, escapeHtmlText as escapeHtml } from './utils.js';
import { apiClient } from './api_client.js';
import { createComposerParts, makeTextPart, normalizeParts } from './composer_parts.js';
import { fileStatusLetter, parsePatch, splitRows, unifiedRows } from './patch_parse.js';

const TASK_RAIL_LIMIT = 60;

/** The one drift sentence (owner-locked wording). */
export const HEAD_DRIFT_NOTICE = 'HEAD differs from the task baseline; showing the '
    + 'current projection for paths attributed during the task window';

// ---------------------------------------------------------------------------
// Pure presentation helpers (node-tested)
// ---------------------------------------------------------------------------

/**
 * Resolve a task's project badge.
 *
 * `task.project_id` is the task's OWN stored scope and wins. A task that was
 * bound to a project after the fact (the "turn into project" path) carries the
 * binding only in `/api/state.task_bindings`, so that is the fallback. A project
 * whose name is not in the (capped) sidebar summary still shows its id rather
 * than nothing — an unnamed badge is better than a silently unscoped task.
 *
 * @returns {{projectId: string, label: string}|null}
 */
export function taskProjectBadge(task, { taskBindings = {}, projects = [] } = {}) {
    const taskId = String(task?.task_id || task?.id || '');
    const own = String(task?.project_id || '').trim();
    const bound = String((taskBindings && taskBindings[taskId] && taskBindings[taskId].project_id) || '').trim();
    const projectId = own || bound;
    if (!projectId) return null;
    const row = (Array.isArray(projects) ? projects : []).find((p) => p && p.id === projectId);
    return { projectId, label: String((row && row.name) || projectId) };
}

/** Short human title for a task row / the "Re task …" line. */
export function taskShortTitle(task) {
    const candidates = [task?.title, task?.objective, task?.description, task?.text];
    for (const value of candidates) {
        const text = String(value || '').trim().replace(/\s+/g, ' ');
        if (text) return text.length > 72 ? `${text.slice(0, 71)}…` : text;
    }
    return String(task?.task_id || task?.id || 'task');
}

/**
 * The rail/header meta line for one loaded diff: `N files · +A −R`.
 * A diff that is not `ready` reports its lifecycle instead of a fake `0 files`.
 */
export function diffSummaryMeta(diff, parsed) {
    const status = String(diff?.status || '');
    if (status === 'pending') return 'waiting for the task to finalize its changes';
    if (status === 'blocked') return 'diff unavailable';
    if (status === 'empty') return 'no changes';
    const files = parsed?.files?.length || 0;
    return `${files} file${files === 1 ? '' : 's'} · +${parsed?.added || 0} −${parsed?.removed || 0}`;
}

/**
 * Banner rows for one diff response: pending / blocked / drift.
 * Returns [] when there is nothing to disclose. Tone drives the CSS token only.
 */
export function diffBanners(diff) {
    const rows = [];
    const status = String(diff?.status || '');
    const blockers = Array.isArray(diff?.blockers) ? diff.blockers.filter(Boolean) : [];
    if (status === 'pending') {
        rows.push({
            tone: 'pending',
            text: 'This task has not finalized its changes yet. The diff appears once its '
                + 'artifacts are written.',
        });
    }
    if (status === 'blocked') {
        rows.push({
            tone: 'blocked',
            text: 'No trustworthy diff can be shown for this task.',
            detail: blockers.join(', '),
        });
    }
    if (diff?.head_advanced) {
        rows.push({ tone: 'drift', text: HEAD_DRIFT_NOTICE });
    }
    if (status !== 'blocked' && blockers.length) {
        rows.push({ tone: 'evidence', text: 'Attribution notes', detail: blockers.join(', ') });
    }
    return rows;
}

/** The plain-text task line prepended to a "Request edits" handoff. */
export function requestEditsPrefix(task) {
    const taskId = String(task?.task_id || task?.id || '');
    return `Re task ${taskId} ("${taskShortTitle(task)}"): `;
}

/**
 * Ordered parts for the handoff: the task line, then everything in the dock.
 * The dock's own order is preserved — a chip/comment interleaving is the message.
 */
export function requestEditsParts(task, dockParts) {
    const prefix = makeTextPart(requestEditsPrefix(task));
    return normalizeParts([prefix, ...(Array.isArray(dockParts) ? dockParts : [])].filter(Boolean));
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

function renderShell() {
    return `
        ${renderPageHeader({
            title: 'Changes',
            icon: PAGE_ICONS.changes || '',
            actionsHtml: '<button class="btn btn-default" data-changes-refresh>Refresh</button>',
        })}
        <div class="changes-layout">
            <aside class="changes-rail">
                <div class="changes-rail-head">
                    <div class="changes-rail-title">Tasks</div>
                    <div class="changes-rail-meta" data-changes-rail-meta></div>
                </div>
                <div class="changes-task-list scroll-fade-y" data-changes-task-list></div>
                <div class="changes-file-head">
                    <div class="changes-rail-title">Files</div>
                    <div class="changes-rail-meta" data-changes-file-meta></div>
                </div>
                <div class="changes-file-list scroll-fade-y" data-changes-file-list></div>
            </aside>
            <section class="changes-main">
                <div class="changes-main-head">
                    <div class="changes-path" data-changes-path></div>
                    <div class="changes-counts" data-changes-counts></div>
                    <div class="ui-segment-group changes-mode" role="group" aria-label="Diff view">
                        <button type="button" class="ui-segment active" data-changes-mode="unified">Unified</button>
                        <button type="button" class="ui-segment" data-changes-mode="split">Split</button>
                    </div>
                </div>
                <div class="changes-banners" data-changes-banners></div>
                <div class="changes-diff scroll-fade-y" data-changes-diff></div>
                <form class="changes-dock" data-changes-dock>
                    <div class="changes-dock-field" data-changes-dock-field>
                        <input
                            type="text"
                            class="changes-dock-input"
                            data-changes-dock-input
                            placeholder="⌘L adds lines from the diff, type comments between · Enter sends"
                            aria-label="Request edits message"
                        >
                    </div>
                    <button type="submit" class="btn btn-primary" data-changes-request>Request edits</button>
                </form>
            </section>
        </div>
    `;
}

function statusClass(letter) {
    return { A: 'is-added', D: 'is-deleted', R: 'is-renamed' }[letter] || 'is-modified';
}

export function initChanges(ctx = {}) {
    const { showPage, subscribeState, getChatController } = ctx;
    const page = document.getElementById('page-changes');
    if (!page) return null;
    page.classList.add('app-page-glass');
    page.innerHTML = renderShell();

    const railMetaEl = page.querySelector('[data-changes-rail-meta]');
    const taskListEl = page.querySelector('[data-changes-task-list]');
    const fileMetaEl = page.querySelector('[data-changes-file-meta]');
    const fileListEl = page.querySelector('[data-changes-file-list]');
    const pathEl = page.querySelector('[data-changes-path]');
    const countsEl = page.querySelector('[data-changes-counts]');
    const bannersEl = page.querySelector('[data-changes-banners]');
    const diffEl = page.querySelector('[data-changes-diff]');
    const dockForm = page.querySelector('[data-changes-dock]');
    const dockField = page.querySelector('[data-changes-dock-field]');
    const dockInput = page.querySelector('[data-changes-dock-input]');

    const view = {
        tasks: [],
        taskId: '',
        task: null,
        diff: null,
        parsed: { files: [], added: 0, removed: 0 },
        filePath: '',
        mode: 'unified',
        error: '',
        loading: false,
        // The one /api/state snapshot (project names + post-hoc task bindings).
        projects: [],
        taskBindings: {},
    };

    const dock = createComposerParts({ container: dockField, input: dockInput });

    function paintTaskRail() {
        railMetaEl.textContent = view.tasks.length
            ? `${view.tasks.length} recent`
            : (view.error ? 'unavailable' : 'no tasks yet');
        taskListEl.textContent = '';
        if (view.error) {
            const row = document.createElement('div');
            row.className = 'changes-empty';
            row.textContent = view.error;
            taskListEl.appendChild(row);
            return;
        }
        if (!view.tasks.length) {
            const row = document.createElement('div');
            row.className = 'changes-empty';
            row.textContent = 'No tasks have run yet.';
            taskListEl.appendChild(row);
            return;
        }
        for (const task of view.tasks) {
            const taskId = String(task.task_id || task.id || '');
            const badge = taskProjectBadge(task, {
                taskBindings: view.taskBindings, projects: view.projects,
            });
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `changes-task-row${taskId === view.taskId ? ' active' : ''}`;
            button.dataset.changesTask = taskId;
            button.title = taskShortTitle(task);
            button.innerHTML = `
                <span class="changes-task-title">${escapeHtml(taskShortTitle(task))}</span>
                <span class="changes-task-meta">
                    <span class="changes-task-id">${escapeHtml(taskId)}</span>
                    ${badge ? `<span class="changes-task-project" title="${escapeHtmlAttr(badge.projectId)}">${escapeHtml(badge.label)}</span>` : ''}
                    <span class="changes-task-status">${escapeHtml(String(task.status || ''))}</span>
                </span>
            `;
            taskListEl.appendChild(button);
        }
    }

    function paintFileList() {
        fileMetaEl.textContent = view.diff ? diffSummaryMeta(view.diff, view.parsed) : '';
        fileListEl.textContent = '';
        if (!view.taskId) {
            const row = document.createElement('div');
            row.className = 'changes-empty';
            row.textContent = 'Pick a task to review its changes.';
            fileListEl.appendChild(row);
            return;
        }
        if (view.loading) {
            const row = document.createElement('div');
            row.className = 'changes-empty';
            row.textContent = 'Loading diff…';
            fileListEl.appendChild(row);
            return;
        }
        if (!view.parsed.files.length) {
            const row = document.createElement('div');
            row.className = 'changes-empty';
            row.textContent = diffSummaryMeta(view.diff, view.parsed);
            fileListEl.appendChild(row);
            return;
        }
        for (const file of view.parsed.files) {
            const letter = fileStatusLetter(file);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `changes-file-row${file.path === view.filePath ? ' active' : ''}`;
            button.dataset.changesFile = file.path;
            button.title = file.renamed ? `${file.oldPath} → ${file.path}` : file.path;
            button.innerHTML = `
                <span class="changes-file-status ${statusClass(letter)}">${escapeHtml(letter)}</span>
                <span class="changes-file-path">${escapeHtml(file.path)}</span>
                <span class="changes-file-counts">${
                    file.binary
                        ? '<span class="changes-file-binary">bin</span>'
                        : `<span class="changes-add">+${file.added}</span><span class="changes-del">−${file.removed}</span>`
                }</span>
            `;
            fileListEl.appendChild(button);
        }
    }

    function paintBanners() {
        bannersEl.textContent = '';
        for (const banner of view.diff ? diffBanners(view.diff) : []) {
            const row = document.createElement('div');
            row.className = 'changes-banner';
            row.dataset.tone = banner.tone;
            const text = document.createElement('span');
            text.className = 'changes-banner-text';
            text.textContent = banner.text;
            row.appendChild(text);
            if (banner.detail) {
                const detail = document.createElement('code');
                detail.className = 'changes-banner-detail';
                detail.textContent = banner.detail;
                row.appendChild(detail);
            }
            bannersEl.appendChild(row);
        }
    }

    function activeFile() {
        return view.parsed.files.find((file) => file.path === view.filePath) || null;
    }

    function paintDiff() {
        const file = activeFile();
        // With no file to name (empty / pending / blocked diff) the header keeps the
        // task's identity instead of going blank.
        pathEl.textContent = file
            ? file.path
            : (view.taskId ? taskShortTitle(view.task || { task_id: view.taskId }) : 'Changes');
        countsEl.textContent = '';
        if (file && !file.binary) {
            const add = document.createElement('span');
            add.className = 'changes-add';
            add.textContent = `+${file.added}`;
            const del = document.createElement('span');
            del.className = 'changes-del';
            del.textContent = `−${file.removed}`;
            countsEl.append(add, del);
        }
        diffEl.textContent = '';
        diffEl.dataset.mode = view.mode;
        if (!file) {
            const empty = document.createElement('div');
            empty.className = 'changes-empty changes-diff-empty';
            empty.textContent = view.taskId
                ? diffSummaryMeta(view.diff, view.parsed)
                : 'Select a task on the left to see what it changed.';
            diffEl.appendChild(empty);
            return;
        }
        if (file.binary || !file.hunks.length) {
            const note = document.createElement('div');
            note.className = 'changes-empty changes-diff-empty';
            note.textContent = file.notes.length
                ? file.notes.join(' · ')
                : 'No textual hunks for this entry.';
            diffEl.appendChild(note);
            return;
        }
        diffEl.appendChild(view.mode === 'split' ? renderSplit(file) : renderUnified(file));
    }

    function lineCell(className, text) {
        const cell = document.createElement('div');
        cell.className = className;
        cell.textContent = text;
        return cell;
    }

    function renderUnified(file) {
        const grid = document.createElement('div');
        grid.className = 'changes-unified';
        for (const row of unifiedRows(file)) {
            if (row.kind === 'hunk') {
                const header = document.createElement('div');
                header.className = 'changes-hunk';
                header.textContent = row.text;
                grid.appendChild(header);
                continue;
            }
            const line = document.createElement('div');
            line.className = `changes-row is-${row.kind}`;
            line.append(
                lineCell('changes-num', row.oldNumber),
                lineCell('changes-num', row.newNumber),
                lineCell('changes-text', row.noNewline ? `${row.text} ⏎̸` : row.text),
            );
            grid.appendChild(line);
        }
        return grid;
    }

    function renderSplit(file) {
        const grid = document.createElement('div');
        grid.className = 'changes-split';
        for (const row of splitRows(file)) {
            if (row.kind === 'hunk') {
                const header = document.createElement('div');
                header.className = 'changes-hunk';
                header.textContent = row.text;
                grid.appendChild(header);
                continue;
            }
            const line = document.createElement('div');
            line.className = 'changes-row is-split';
            const side = (cell, kind) => {
                const num = lineCell('changes-num', cell ? cell.number : '');
                const text = lineCell(`changes-text is-${cell ? cell.kind : 'none'}`, cell ? cell.text : '');
                if (!cell) {
                    num.classList.add('is-none');
                    text.classList.add('is-empty-counterpart');
                }
                num.classList.add(`is-${cell ? cell.kind : 'none'}`);
                num.dataset.side = kind;
                return [num, text];
            };
            line.append(...side(row.left, 'old'), ...side(row.right, 'new'));
            grid.appendChild(line);
        }
        return grid;
    }

    function paintAll() {
        paintTaskRail();
        paintFileList();
        paintBanners();
        paintDiff();
    }

    async function loadTasks() {
        try {
            const data = await apiClient.tasks(TASK_RAIL_LIMIT);
            view.tasks = Array.isArray(data?.tasks) ? data.tasks : [];
            view.error = '';
        } catch (err) {
            view.tasks = [];
            view.error = `Task list unavailable: ${err?.message || 'request failed'}`;
        }
        paintTaskRail();
    }

    async function selectTask(taskId, { filePath = '' } = {}) {
        const id = String(taskId || '');
        if (!id) return;
        view.taskId = id;
        view.task = view.tasks.find((task) => String(task.task_id || task.id || '') === id) || { task_id: id };
        view.diff = null;
        view.parsed = { files: [], added: 0, removed: 0 };
        view.filePath = '';
        view.loading = true;
        paintAll();
        let diff = null;
        try {
            diff = await apiClient.taskDiff(id);
        } catch (err) {
            diff = {
                status: 'blocked',
                source: '',
                blockers: [err?.message || 'request failed'],
                patch: '',
            };
        }
        if (view.taskId !== id) return;  // a newer selection won
        view.loading = false;
        view.diff = diff;
        view.parsed = parsePatch(diff?.patch || '');
        const wanted = view.parsed.files.find((file) => file.path === filePath);
        view.filePath = (wanted || view.parsed.files[0] || {}).path || '';
        paintAll();
    }

    async function requestEdits() {
        const controller = typeof getChatController === 'function' ? getChatController() : null;
        if (!controller || typeof controller.sendParts !== 'function') return;
        const dockParts = dock.commitDraft();
        // The task line alone says nothing the owner asked for: an empty dock is a
        // no-op that puts the cursor back in the field instead of sending prose.
        const asked = dockParts.some(
            (part) => part.type === 'chip' || String(part.text || '').trim(),
        );
        if (!asked) {
            dock.focus();
            return;
        }
        const parts = requestEditsParts(view.task || { task_id: view.taskId }, dockParts);
        const sent = await controller.sendParts(parts);
        // The draft is the owner's only copy until the handoff succeeded.
        if (sent === false) return;
        dock.clear();
        if (typeof showPage === 'function') await showPage('chat');
    }

    page.addEventListener('click', (event) => {
        const taskRow = event.target.closest('[data-changes-task]');
        if (taskRow && page.contains(taskRow)) {
            selectTask(taskRow.dataset.changesTask);
            return;
        }
        const fileRow = event.target.closest('[data-changes-file]');
        if (fileRow && page.contains(fileRow)) {
            view.filePath = fileRow.dataset.changesFile;
            paintFileList();
            paintDiff();
            return;
        }
        const mode = event.target.closest('[data-changes-mode]');
        if (mode && page.contains(mode)) {
            view.mode = mode.dataset.changesMode === 'split' ? 'split' : 'unified';
            page.querySelectorAll('[data-changes-mode]').forEach((button) => {
                button.classList.toggle('active', button.dataset.changesMode === view.mode);
            });
            paintDiff();
            return;
        }
        if (event.target.closest('[data-changes-refresh]')) {
            event.preventDefault();
            loadTasks().then(() => (view.taskId ? selectTask(view.taskId, { filePath: view.filePath }) : null));
        }
    });

    dockForm.addEventListener('submit', (event) => {
        event.preventDefault();
        requestEdits();
    });

    if (typeof subscribeState === 'function') {
        // Badge inputs only. Repainting the rail on every poll would reset its
        // scroll position and hover state a few times a minute for nothing, so the
        // paint is gated on the badge data actually changing.
        let knownBadgeJson = '';
        subscribeState((data) => {
            view.projects = Array.isArray(data?.projects) ? data.projects : view.projects;
            view.taskBindings = (data && data.task_bindings) || view.taskBindings;
            const json = JSON.stringify([
                view.projects.map((project) => [project?.id, project?.name]),
                Object.entries(view.taskBindings).map(([id, binding]) => [id, binding?.project_id]),
            ]);
            if (json === knownBadgeJson) return;
            knownBadgeJson = json;
            paintTaskRail();
        });
    }

    // The rail is refreshed when the page is entered: a task that finished while
    // the owner was elsewhere must be reviewable without a reload.
    window.addEventListener('ouro:page-shown', (event) => {
        if (event?.detail?.page !== 'changes') return;
        loadTasks();
        if (view.taskId) selectTask(view.taskId, { filePath: view.filePath });
    });

    // Opening a specific task (inspector → "open full diff") lands here.
    window.addEventListener('ouro:open-changes', async (event) => {
        const taskId = String(event?.detail?.taskId || '');
        if (!taskId) return;
        if (typeof showPage === 'function') await showPage('changes');
        if (!view.tasks.length) await loadTasks();
        await selectTask(taskId, { filePath: String(event?.detail?.filePath || '') });
    });

    paintAll();
    loadTasks();

    return {
        page,
        dock,
        selectTask,
        refresh: loadTasks,
        /**
         * Append a context chip to this page's dock. The seam the global ⌘L
         * handler uses to route a diff selection here without reaching into the
         * module's internals; the chip itself is built by `composer_parts`.
         */
        addChip: (chip) => dock.addChip(chip),
        /** The file currently under review, for a capture that needs its path. */
        activeFilePath: () => view.filePath,
        /** Test/inspection seam: the current view snapshot. */
        snapshot: () => ({ ...view }),
    };
}
