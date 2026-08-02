/**
 * Task inspector: the right-side panel for ONE task.
 *
 * It registers itself as the `inspector` kind against the app-level right-panel
 * state machine, so opening it closes the project panel and vice versa (one slot,
 * mutually exclusive kinds). It opens on the `ouro:inspect-task` event a live
 * chat card dispatches — the card needs no knowledge of this module.
 *
 * Two tabs only (owner-locked): Changes (the diff endpoint's patch, parsed by
 * `patch_parse.js`, one row per file) and Cost. The Cost tab shows ONLY fields
 * that are actually persisted on the task result, and it distinguishes
 * "unavailable" from "$0.00": a ledger that could not be read must never render
 * as a convincing zero. The footer carries the parsed +N −N, the task cost, and
 * elapsed from the durable `duration_sec` scalar.
 */

import { apiClient } from './api_client.js';
// The drift sentence is ONE owner-facing fact: both diff surfaces read it from
// the Changes module rather than each spelling its own wording.
import { HEAD_DRIFT_NOTICE } from './changes.js';
import { fileStatusLetter, parsePatch } from './patch_parse.js';

const TERMINAL_STATUSES = new Set([
    'completed', 'failed', 'cancelled', 'rejected_duplicate', 'cancel_requested',
]);

/** Number that is genuinely present, else null (0 is a real value). */
function optionalFiniteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
}

export function taskIsRunning(task) {
    return !TERMINAL_STATUSES.has(String(task?.status || '').toLowerCase());
}

/** `1m 12s` / `4.5s` / '' — never a fabricated 0 for a missing value. */
export function formatElapsed(durationSec) {
    const seconds = optionalFiniteNumber(durationSec);
    if (seconds === null || seconds < 0) return '';
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${Math.round(seconds - minutes * 60)}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * Cost-tab rows from the PERSISTED task-result fields only.
 *
 * Same discipline as the chat card's `taskCostMeta`: money is rendered from
 * explicit accounting evidence, an `unavailable` ledger says so, an unsettled
 * reading is labelled pending, and a field the record does not carry is omitted
 * instead of being shown as zero.
 *
 * @returns {Array<{label: string, value: string, tone?: string}>}
 */
export function inspectorCostRows(task = {}) {
    const rows = [];
    const has = (key) => Object.prototype.hasOwnProperty.call(task || {}, key);
    const status = String(task.cost_accounting_status || '').toLowerCase();
    const unavailable = status === 'unavailable';
    const cost = optionalFiniteNumber(task.cost_usd);
    const finalKnown = task.cost_final === true;

    if (unavailable) {
        rows.push({ label: 'Task cost', value: 'Unavailable', tone: 'unavailable' });
    } else if (cost === null) {
        rows.push({
            label: 'Task cost',
            value: has('cost_usd') || status ? 'Pending' : 'Unavailable',
            tone: 'pending',
        });
    } else {
        rows.push({
            label: 'Task cost',
            value: `$${cost.toFixed(2)}${finalKnown ? '' : ' (pending)'}`,
            tone: finalKnown ? 'final' : 'pending',
        });
    }

    const rounds = optionalFiniteNumber(task.total_rounds);
    if (rounds !== null) rows.push({ label: 'LLM rounds', value: String(Math.trunc(rounds)) });
    const promptTokens = optionalFiniteNumber(task.prompt_tokens);
    const completionTokens = optionalFiniteNumber(task.completion_tokens);
    if (promptTokens !== null || completionTokens !== null) {
        rows.push({
            label: 'Tokens in/out',
            value: `${promptTokens === null ? '—' : Math.trunc(promptTokens)}`
                + ` / ${completionTokens === null ? '—' : Math.trunc(completionTokens)}`,
        });
    }
    const reserved = optionalFiniteNumber(task.reserved_usd);
    if (reserved !== null && reserved > 0) {
        rows.push({ label: 'Reserved', value: `$${reserved.toFixed(2)}` });
    }
    const unresolved = optionalFiniteNumber(task.unresolved_upper_bound_usd);
    if (unresolved !== null && unresolved > 0) {
        rows.push({ label: 'Unresolved ≤', value: `$${unresolved.toFixed(2)}`, tone: 'pending' });
    }
    const unmetered = optionalFiniteNumber(task.unknown_unmetered);
    if (unmetered !== null && unmetered > 0) {
        rows.push({ label: 'Unmetered calls', value: String(Math.trunc(unmetered)), tone: 'pending' });
    }
    if (status && !unavailable) {
        rows.push({
            label: 'Accounting',
            value: finalKnown ? 'final' : 'pending',
            tone: finalKnown ? 'final' : 'pending',
        });
    }
    return rows;
}

/** Footer projection: files +N −N, task cost, elapsed. */
export function inspectorFooter(task = {}, parsed = null) {
    const costRow = inspectorCostRows(task)[0];
    const elapsed = formatElapsed(task.duration_sec);
    return {
        added: parsed?.added || 0,
        removed: parsed?.removed || 0,
        files: parsed?.files?.length || 0,
        cost: costRow ? costRow.value : 'Unavailable',
        elapsed: elapsed || 'Unavailable',
    };
}

function element(tag, className, text = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
}

export function initTaskInspector(ctx = {}) {
    const { registerRightPanel, openRightPanel, closeRightPanel, subscribeState } = ctx;
    if (typeof registerRightPanel !== 'function') return null;

    const backdrop = element('div', 'inspector-backdrop');
    backdrop.hidden = true;
    const panel = element('aside', 'inspector-panel');
    panel.id = 'task-inspector';
    panel.hidden = true;
    panel.innerHTML = `
        <div class="inspector-bar">
            <div class="inspector-title" data-inspector-title>Task</div>
            <button type="button" class="inspector-close" data-inspector-close
                title="Close inspector" aria-label="Close task inspector">×</button>
        </div>
        <div class="inspector-tabs" role="tablist" aria-label="Task inspector views">
            <button type="button" class="inspector-tab active" data-inspector-tab="changes" role="tab" aria-selected="true">Changes</button>
            <button type="button" class="inspector-tab" data-inspector-tab="cost" role="tab" aria-selected="false">Cost</button>
        </div>
        <div class="inspector-body scroll-fade-y" data-inspector-body></div>
        <div class="inspector-footer" data-inspector-footer></div>
    `;
    const host = document.getElementById('app') || document.body;
    host.append(backdrop, panel);

    const titleEl = panel.querySelector('[data-inspector-title]');
    const bodyEl = panel.querySelector('[data-inspector-body]');
    const footerEl = panel.querySelector('[data-inspector-footer]');

    const view = { taskId: '', task: null, diff: null, parsed: null, tab: 'changes', error: '' };
    let openState = false;

    function paintChangesTab() {
        bodyEl.textContent = '';
        if (!view.diff) {
            bodyEl.appendChild(element('div', 'inspector-empty', view.error || 'Loading changes…'));
            return;
        }
        const status = String(view.diff.status || '');
        if (status !== 'ready' || !view.parsed?.files?.length) {
            const note = {
                pending: 'Changes are not finalized yet.',
                empty: 'This task changed nothing.',
                blocked: 'No trustworthy diff can be shown.',
            }[status] || 'No changes to show.';
            bodyEl.appendChild(element('div', 'inspector-empty', note));
            const blockers = Array.isArray(view.diff.blockers) ? view.diff.blockers.filter(Boolean) : [];
            if (blockers.length) bodyEl.appendChild(element('code', 'inspector-blockers', blockers.join(', ')));
            return;
        }
        if (view.diff.head_advanced) {
            bodyEl.appendChild(element('div', 'inspector-drift', HEAD_DRIFT_NOTICE));
        }
        for (const file of view.parsed.files) {
            const letter = fileStatusLetter(file);
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'inspector-file-row';
            row.dataset.inspectorFile = file.path;
            row.title = file.path;
            row.append(
                element('span', `inspector-file-status is-${letter.toLowerCase()}`, letter),
                element('span', 'inspector-file-path', file.path),
            );
            const counts = element('span', 'inspector-file-counts');
            if (file.binary) {
                counts.appendChild(element('span', 'inspector-file-binary', 'bin'));
            } else {
                counts.append(
                    element('span', 'changes-add', `+${file.added}`),
                    element('span', 'changes-del', `−${file.removed}`),
                );
            }
            row.appendChild(counts);
            bodyEl.appendChild(row);
        }
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'inspector-open-full';
        open.dataset.inspectorOpenFull = '1';
        open.textContent = 'Open full diff →';
        bodyEl.appendChild(open);
    }

    function paintCostTab() {
        bodyEl.textContent = '';
        if (!view.task) {
            bodyEl.appendChild(element('div', 'inspector-empty', view.error || 'Loading task…'));
            return;
        }
        const rows = inspectorCostRows(view.task);
        for (const row of rows) {
            const line = element('div', 'inspector-cost-row');
            if (row.tone) line.dataset.tone = row.tone;
            line.append(
                element('span', 'inspector-cost-label', row.label),
                element('span', 'inspector-cost-value', row.value),
            );
            bodyEl.appendChild(line);
        }
        bodyEl.appendChild(element(
            'div', 'inspector-cost-note',
            'Only values the task record actually persisted are shown.',
        ));
    }

    function paintFooter() {
        const footer = inspectorFooter(view.task || {}, view.parsed);
        footerEl.textContent = '';
        const files = element('span', 'inspector-footer-files');
        files.append(
            element('span', 'changes-add', `+${footer.added}`),
            element('span', 'changes-del', `−${footer.removed}`),
        );
        files.title = `${footer.files} file${footer.files === 1 ? '' : 's'} changed`;
        footerEl.append(
            files,
            element('span', 'inspector-footer-cost', footer.cost),
            element('span', 'inspector-footer-elapsed', footer.elapsed),
        );
    }

    function paint() {
        titleEl.textContent = view.taskId || 'Task';
        titleEl.title = view.taskId;
        panel.querySelectorAll('[data-inspector-tab]').forEach((tab) => {
            const active = tab.dataset.inspectorTab === view.tab;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        if (view.tab === 'cost') paintCostTab();
        else paintChangesTab();
        paintFooter();
    }

    async function load(taskId) {
        const id = String(taskId || '');
        const [taskResult, diffResult] = await Promise.allSettled([
            apiClient.task(id),
            apiClient.taskDiff(id),
        ]);
        if (view.taskId !== id) return;
        if (taskResult.status === 'fulfilled') {
            view.task = taskResult.value || {};
            view.error = '';
        } else {
            view.task = view.task || null;
            view.error = `Task unavailable: ${taskResult.reason?.message || 'request failed'}`;
        }
        if (diffResult.status === 'fulfilled') {
            view.diff = diffResult.value || null;
            view.parsed = parsePatch(view.diff?.patch || '');
        } else {
            view.diff = {
                status: 'blocked',
                blockers: [diffResult.reason?.message || 'request failed'],
                patch: '',
            };
            view.parsed = { files: [], added: 0, removed: 0 };
        }
        paint();
    }

    function setOpen(open) {
        openState = Boolean(open);
        document.body.classList.toggle('inspector-open', openState);
        if (openState) {
            panel.hidden = false;
            backdrop.hidden = false;
            requestAnimationFrame(() => {
                panel.classList.add('open');
                backdrop.classList.add('open');
            });
        } else {
            panel.classList.remove('open');
            backdrop.classList.remove('open');
            panel.hidden = true;
            backdrop.hidden = true;
        }
    }

    const unregister = registerRightPanel('inspector', {
        mount({ taskId = '', tab = '' } = {}) {
            const id = String(taskId || '');
            if (!id) return false;
            if (id !== view.taskId) {
                view.taskId = id;
                view.task = null;
                view.diff = null;
                view.parsed = null;
                view.error = '';
            }
            if (tab === 'cost' || tab === 'changes') view.tab = tab;
            setOpen(true);
            paint();
            load(id);
            return true;
        },
        unmount() {
            setOpen(false);
        },
    });

    panel.addEventListener('click', (event) => {
        if (event.target.closest('[data-inspector-close]')) {
            if (typeof closeRightPanel === 'function') closeRightPanel();
            else setOpen(false);
            return;
        }
        const tab = event.target.closest('[data-inspector-tab]');
        if (tab) {
            view.tab = tab.dataset.inspectorTab === 'cost' ? 'cost' : 'changes';
            paint();
            return;
        }
        const fileRow = event.target.closest('[data-inspector-file]');
        if (fileRow) {
            window.dispatchEvent(new CustomEvent('ouro:open-changes', {
                detail: { taskId: view.taskId, filePath: fileRow.dataset.inspectorFile },
            }));
            return;
        }
        if (event.target.closest('[data-inspector-open-full]')) {
            window.dispatchEvent(new CustomEvent('ouro:open-changes', { detail: { taskId: view.taskId } }));
        }
    });

    backdrop.addEventListener('click', () => {
        if (typeof closeRightPanel === 'function') closeRightPanel();
        else setOpen(false);
    });

    window.addEventListener('ouro:inspect-task', (event) => {
        const taskId = String(event?.detail?.taskId || event?.detail?.task_id || '');
        if (!taskId) return;
        if (typeof openRightPanel === 'function') openRightPanel('inspector', { taskId });
        else {
            view.taskId = taskId;
            setOpen(true);
            paint();
            load(taskId);
        }
    });

    // The inspector belongs to the Chat page; the full Changes screen replaces it.
    window.addEventListener('ouro:page-shown', (event) => {
        if (!openState) return;
        if (event?.detail?.page === 'chat') return;
        if (typeof closeRightPanel === 'function') closeRightPanel();
        else setOpen(false);
    });

    // Live refresh rides the ONE app-owned /api/state poll: while the task runs its
    // cost/rounds and its diff keep moving, and a finished task stops re-fetching.
    if (typeof subscribeState === 'function') {
        subscribeState(() => {
            if (!openState || !view.taskId) return;
            if (view.task && !taskIsRunning(view.task)) return;
            load(view.taskId);
        });
    }

    return {
        panel,
        backdrop,
        destroy() {
            unregister();
            panel.remove();
            backdrop.remove();
        },
        snapshot: () => ({ ...view, open: openState }),
    };
}
