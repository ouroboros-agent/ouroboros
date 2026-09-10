// Activity dashboard subtab (P4): a single observability + minimal-control view for
// cron/scheduled tasks, what is running/queued now, and background consciousness.
// Management is DIRECT mechanical control via existing APIs (cancel a task, enable/
// disable/delete a MANUAL schedule, start/stop background consciousness). Skill-managed
// schedules are READ-ONLY ("managed by skill") because the lifecycle resync would
// overwrite a direct toggle (supervisor/queue.py) — control those via the skill itself.

import { fetchJson } from './api_client.js';
import { setInlineStatus } from './ui_helpers.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { taskCancelPending } from './log_events.js';
import {
    ACTION_HURRY,
    ACTION_RESUME,
    TASK_CONTROL_TRIGGER_LABEL,
    hurryTaskAction,
    openTaskControlMenu,
    requestStop,
    resumeTaskAction,
    taskControlBusy,
} from './task_control_menu.js';
import { showToast } from './toast.js';

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

const getJson = (url) => fetchJson(url, { cache: 'no-store' });

// A schedule synced from a skill manifest is reconciled from skill readiness, so a
// direct enable/disable/delete here would be temporary/misleading — show it read-only.
function isSkillManaged(s) {
    return Boolean(s && (String(s.source || '') === 'skill_manifest' || String(s.skill || '')));
}

export function initActivity({ mount, ws } = {}) {
    if (!mount) return { refresh: () => {} };
    let busy = false;
    let refreshRevision = 0;
    mount.innerHTML = `<div class="activity-scroll">
        <div class="activity-section" data-activity-section="queue"><h3 class="activity-h">Running &amp; queued</h3></div>
        <div class="activity-section" data-activity-section="background"><h3 class="activity-h">Background</h3></div>
        <div class="activity-section" data-activity-section="schedules"><h3 class="activity-h">Scheduled</h3></div>
    </div>`;
    const sections = ['queue', 'background', 'schedules'].map((name) => {
        const root = mount.querySelector(`[data-activity-section="${name}"]`);
        const status = document.createElement('div');
        status.className = 'ui-status activity-read-status';
        status.setAttribute('role', 'status');
        const content = document.createElement('div');
        content.className = 'activity-section-content';
        root.append(status, content);
        return { root, status, content, loaded: false };
    });

    function renderQueue(queue) {
        if (!Array.isArray(queue?.running) || !Array.isArray(queue?.pending)) throw new Error('Queue unavailable');
        const { running, pending } = queue;
        // #322: the snapshot already carries the pause truth — a member's own
        // _budget_pause row, or a root fence covering its tree.
        const fencedRoots = new Set(
            ((queue && queue.budget_root_fences) || [])
                .filter((f) => f && ['active', 'paused'].includes(String(f.status || '')))
                .map((f) => String(f.root_task_id || '')));
        const rowBudgetPaused = (q, t, kind) => kind === 'pending' && Boolean(
            (t && t._budget_pause)
            || fencedRoots.has(String((t && (t.root_task_id || t.id)) || q.id || '')));
        const row = (q, kind) => {
            const t = (q && q.task) || {};
            const id = esc(q.id || t.id || '');
            const label = esc(t.title || t.objective || t.text || q.type || id || 'task');
            const rt = kind === 'running' && q.runtime_sec != null ? ` · ${Math.round(q.runtime_sec)}s` : '';
            const paused = rowBudgetPaused(q, t, kind);
            const kindLabel = paused ? 'paused (budget)' : kind;
            const meta = `${esc(kindLabel)}${q.type ? ` · ${esc(q.type)}` : ''}${rt}`;
            return `<div class="activity-row">
                <div class="activity-row-main">
                    <span class="activity-name">${label}</span>
                    <span class="activity-sub">${meta}</span>
                </div>
                <div class="activity-row-actions">
                    <button type="button" class="btn btn-xs btn-danger" data-act="task-control" data-id="${id}"${paused ? ' data-budget-paused="1"' : ''}>${esc(TASK_CONTROL_TRIGGER_LABEL)}</button>
                </div>
            </div>`;
        };
        const parts = [...running.map((q) => row(q, 'running')), ...pending.map((q) => row(q, 'pending'))];
        return parts.length ? parts.join('') : '<div class="activity-empty">Nothing running or queued.</div>';
    }

    function renderBg(stateData) {
        if (typeof stateData?.bg_consciousness_enabled !== 'boolean') throw new Error('Background state unavailable');
        const enabled = stateData.bg_consciousness_enabled;
        const bg = (stateData && stateData.bg_consciousness_state) || {};
        const detail = esc(bg.detail || bg.last_idle_reason || (enabled ? 'running' : 'disabled'));
        return `<div class="activity-row">
            <div class="activity-row-main">
                <span class="activity-name">Background consciousness</span>
                <span class="activity-sub">${enabled ? 'enabled' : 'disabled'}${detail ? ` · ${detail}` : ''}</span>
            </div>
            <div class="activity-row-actions">
                <button type="button" class="btn btn-xs btn-default" data-act="bg-toggle" data-enabled="${enabled ? '1' : '0'}"${ws ? '' : ' disabled'}>${enabled ? 'Stop' : 'Start'}</button>
            </div>
        </div>`;
    }

    function renderSchedules(data) {
        if (!Array.isArray(data?.tasks)) throw new Error('Schedules unavailable');
        const tasks = data.tasks;
        if (!tasks.length) return '<div class="activity-empty">No scheduled tasks.</div>';
        return tasks.map((s) => {
            const managed = isSkillManaged(s);
            const trigger = s.trigger || {};
            const once = String(trigger.type || 'cron') === 'once';
            // One-shot rows have no cron: show the fire instant + a "one-shot" tag.
            const timing = once
                ? `one-shot · at/after ${esc(trigger.run_at || '')}`
                : esc(trigger.expr || s.cron || '');
            const next = esc(s.next_run_at || '');
            const enabled = s.enabled !== false;
            const id = esc(s.id || '');
            const sub = `${timing}${next ? ` · next ${next}` : ''}${managed && s.skill ? ` · ${esc(s.skill)}` : ''}`;
            const actions = managed
                ? '<span class="activity-tag">managed by skill</span>'
                : `<button type="button" class="btn btn-xs btn-default" data-act="schedule-toggle" data-id="${id}">${enabled ? 'Disable' : 'Enable'}</button>
                   <button type="button" class="btn btn-xs btn-danger" data-act="schedule-delete" data-id="${id}">Delete</button>`;
            return `<div class="activity-row${enabled ? '' : ' off'}">
                <div class="activity-row-main">
                    <span class="activity-name">${esc(s.name || s.id || 'schedule')}</span>
                    <span class="activity-sub">${sub}</span>
                </div>
                <div class="activity-row-actions">${actions}</div>
            </div>`;
        }).join('');
    }

    async function refresh() {
        const revision = ++refreshRevision;
        sections.forEach(({ root, status, loaded }) => {
            root.setAttribute('aria-busy', 'true');
            setInlineStatus(status, loaded ? 'Refreshing… Previously loaded values shown.' : 'Loading…');
        });
        const results = await Promise.allSettled([
            // This view renders only the queue; queue_only skips the whole
            // task-results scan server-side (v6.9x P2).
            getJson('/api/tasks?queue_only=1'),
            getJson('/api/state'),
            getJson('/api/schedules'),
        ]);
        if (revision !== refreshRevision) return;
        const renderers = [(data) => renderQueue(data?.queue), renderBg, renderSchedules];
        sections.forEach((section, index) => {
            const { root, status, content } = section;
            root.removeAttribute('aria-busy');
            try {
                const result = results[index];
                if (result.status === 'rejected') throw result.reason;
                content.innerHTML = renderers[index](result.value);
                section.loaded = true;
                setInlineStatus(status, '');
            } catch {
                setInlineStatus(status, section.loaded
                    ? 'Could not refresh. Previously loaded values shown; current state is unknown. Reopen Activity to try again.'
                    : 'Could not load. Current state is unknown. Reopen Activity to try again.', 'error');
            }
        });
    }

    async function findSchedule(id) {
        const data = await getJson('/api/schedules');
        const tasks = (data && Array.isArray(data.tasks)) ? data.tasks : [];
        return tasks.find((s) => String(s.id) === String(id)) || null;
    }

    mount.addEventListener('click', async (event) => {
        const btn = event.target.closest('[data-act]');
        if (!btn || busy) return;
        const act = btn.dataset.act;
        const id = btn.dataset.id || '';
        if (act === 'task-control') {
            // S3 (Q2/HQ1): owner product-wide parity — the SAME three-action
            // dropdown as the Chat card (one shared module: same actions,
            // endpoint bindings, request-id retry, and typed refusals).
            // Dismissing the menu continues the run. The durable detail decides
            // whether a cancel intent is pending (then only the hard escalation
            // is offered and hurry is never shown).
            let stored = null;
            try {
                stored = await getJson(`/api/tasks/${encodeURIComponent(id)}`);
            } catch (exc) {
                if (exc?.status !== 404) showToast(`Could not refresh task state: ${exc?.message || exc}`, 'error');
            }
            openTaskControlMenu(btn, {
                cancelPending: taskCancelPending(stored),
                budgetPaused: btn.dataset.budgetPaused === '1',
                busy: taskControlBusy(id),
                onAction: async (action) => {
                    busy = true;
                    try {
                        if (action === ACTION_HURRY) {
                            // Local toast acknowledgement only — never a chat message.
                            await hurryTaskAction(id);
                            return;
                        }
                        if (action === ACTION_RESUME) {
                            await resumeTaskAction(id);
                            return;
                        }
                        // Same declared semantics as the chat card (v6.82): the
                        // task AND its live subtree, so stopping an orchestrator
                        // never orphans its running subagents. Soft stop answers
                        // 202 with the intent open; immediate answers after the
                        // teardown — either way the refresh shows honest state.
                        await requestStop(id, action);
                    } catch (exc) {
                        // A 404 is the documented completion race (the run
                        // finished on its own); the refresh tells that story.
                        if (exc?.status !== 404) {
                            showToast(`Action failed: ${exc?.message || exc}`, 'error');
                        }
                    } finally {
                        busy = false;
                        await refresh();
                    }
                },
            });
            return;
        }
        busy = true;
        btn.disabled = true;
        try {
            if (act === 'schedule-delete') {
                const confirmedDelete = await openConfirmDialog({
                    title: 'Delete schedule',
                    body: 'Delete this schedule?',
                    confirmLabel: 'Delete',
                    danger: true,
                });
                if (!confirmedDelete) return;
                await fetchJson(`/api/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' });
            } else if (act === 'schedule-toggle') {
                // Read-modify-write the FULL record (upsert replaces by id; never drop
                // timezone/trigger/task/source) with the flipped enabled flag.
                const rec = await findSchedule(id);
                if (rec) {
                    await fetchJson('/api/schedules', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ...rec, enabled: !(rec.enabled !== false) }),
                    });
                }
            } else if (act === 'bg-toggle') {
                const on = btn.dataset.enabled === '1';
                // Reuse the existing direct control command (same as the chat header
                // toggle); /bg is a control slash-command, not a chat message to the agent.
                ws?.send?.({ type: 'command', cmd: `/bg ${on ? 'stop' : 'start'}` });
                await new Promise((resolve) => setTimeout(resolve, 400));
            }
        } catch (exc) {
            // A 404 is the documented completion race (the run finished on its own)
            // and the refresh below tells that story. Anything else is a real
            // failure — a refused cancel must not read as a silent no-op click.
            if (exc?.status !== 404) {
                showToast(`Action failed: ${exc?.message || exc}`, 'error');
            }
        } finally {
            busy = false;
            btn.disabled = false;
            await refresh();
        }
    });

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event?.detail?.tab === 'activity') refresh();
    });

    return { refresh };
}
