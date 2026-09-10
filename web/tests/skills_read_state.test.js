import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { setImmediate as nextTurn } from 'node:timers/promises';

import { renderInstalledSkillCard, renderSkillHubBadges } from '../modules/skill_card_renderer.js';
import { lifecycleFor } from '../modules/marketplace.js';
import { lifecycleCardClassFor, lifecycleSpinnerFor } from '../modules/lifecycle_card.js';
import { hubListingRowFor, hubSyncVerdict } from '../modules/hub_sync.js';
import { escapeHtmlAttr, isRateLimitError, renderHubCard } from '../modules/utils.js';

// Run the production controllers against their network/DOM boundaries. No
// copied renderer, browser globals or process-wide fetch mutation is needed.
function source(file, from, until) {
    const text = readFileSync(new URL(`../modules/${file}.js`, import.meta.url), 'utf8');
    const start = text.indexOf(from);
    const end = until ? text.indexOf(until, start) : text.length;
    assert.ok(start >= 0 && end > start, `${file}: source boundaries`);
    return text.slice(start, end).replace(/^export /, '');
}

function node() {
    return {
        innerHTML: '', textContent: '', hidden: true, isConnected: true,
        className: '', dataset: {}, handlers: {},
        classList: { add() {}, remove() {} },
        addEventListener(name, handler) { this.handlers[name] = handler; },
        removeEventListener(name, handler) { if (this.handlers[name] === handler) delete this.handlers[name]; },
        querySelectorAll: () => [],
        setAttribute() {},
    };
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

const demo = {
    name: 'demo', source: 'external', payload_root: 'skills/external/demo',
    version: '1.0.0', review_status: 'clean', review_gate: { executable_review: true },
    permissions: [], grants: { all_granted: true },
};

function skillsReader(overrides = {}) {
    const status = node(), empty = node(), list = node(), badge = node();
    badge.dataset.skillHubBadges = 'demo';
    let writes = 0, html = '', cards = [];
    const patches = [];
    const cardFor = value => ({ ...node(), dataset: { skill: /data-skill="([^"]+)"/.exec(value)[1] } });
    list.querySelectorAll = selector => selector.includes('.skills-card') ? cards : [badge];
    list.insertBefore = card => { cards.unshift(card); };
    Object.defineProperty(list, 'firstElementChild', { get: () => cards[0] || null });
    Object.defineProperty(list, 'innerHTML', {
        get: () => html,
        set: value => { writes += 1; html = value; cards = [...value.matchAll(/<article[^>]+>/g)].map(row => cardFor(row[0])); },
    });
    const apiClient = {
        state: async () => ({ github_token_configured: true }),
        extensions: async () => ({ skills: [demo], live: {} }),
        skillLifecycleQueue: async () => ({ events: [] }),
        ...overrides,
    };
    const context = vm.createContext({
        apiClient, renderInstalledSkillCard, renderSkillHubBadges,
        patchInstalledSkillEnrichment: (card, skill) => { patches.push({ card, skill }); },
        LIFECYCLE_VISIBLE_STATUSES: new Set(['queued', 'running', 'failed']),
        document: {
            getElementById: id => id === 'skills-status' ? status : null,
            createElement: () => ({ content: {}, set innerHTML(value) { this.content.firstElementChild = cardFor(value); } }),
        },
        hubCatalog: { byName: new Map(), available: false },
        loadHubCatalog: () => Promise.resolve(), sortSkillsForDisplay: rows => rows,
    });
    vm.runInContext(`let skillsRenderGeneration = 0; let skillsSnapshot = null;
        ${source('skills', 'async function fetchSkills(', '\nfunction updateQueueBadges')}
        function updateQueueBadges() {}
        ${source('skills', 'async function renderSkillsList(', '\nasync function postWithFeedback')}`, context);
    return { context, apiClient, status, empty, list, badge, patches, writes: () => writes,
        render: interactions => context.renderSkillsList(list, empty, new Set(), new Set(), interactions) };
}

test('primary failure preserves previous cards and is never a successful empty list', async () => {
    const view = skillsReader({ extensions: async () => { throw new Error('HTTP 503'); } });
    await assert.rejects(view.context.fetchSkills(), /HTTP 503/);
    await view.render();
    assert.equal(view.empty.hidden, true);
    assert.equal(view.list.innerHTML, '');
    assert.match(view.status.textContent, /Could not load installed skills.*503/);

    view.apiClient.extensions = async () => ({ skills: [demo], live: {} });
    await view.render();
    const previous = view.list.innerHTML;
    view.apiClient.extensions = async () => { throw new Error('offline'); };
    await view.render();
    assert.equal(view.list.innerHTML, previous);
    assert.match(view.status.textContent, /Showing the previous list/);

    view.apiClient.extensions = async () => ({ skills: [], live: {} });
    await view.render();
    assert.equal(view.list.innerHTML, '');
    assert.equal(view.empty.hidden, false);
    assert.equal(view.status.textContent, '');
    view.apiClient.extensions = async () => { throw new Error('offline again'); };
    await view.render();
    assert.equal(view.empty.hidden, true, 'an old empty response is not the current availability claim');
});

test('malformed primary data fails, optional state/queue failures retain known facts', async () => {
    const view = skillsReader({ skillLifecycleQueue: async () => ({
        active: { id: 'disable-1', target: 'demo', kind: 'disable', status: 'running' }, events: [],
    }) });
    await view.render();
    view.apiClient.state = async () => { throw new Error('settings offline'); };
    view.apiClient.skillLifecycleQueue = async () => { throw new Error('queue offline'); };
    await view.render();
    assert.match(view.list.innerHTML, /Disabling/);
    assert.match(view.list.innerHTML, /data-submit-disabled="false"/);
    assert.match(view.status.textContent, /Skill settings could not be refreshed/);
    assert.match(view.status.textContent, /Lifecycle progress could not be refreshed/);
    view.apiClient.extensions = async () => ({ error: 'invalid response' });
    await assert.rejects(view.context.fetchSkills(), /Installed skills response is unavailable/);
});

test('state, queue and both pending never delay the primary render or its completion', async () => {
    for (const held of [['state'], ['queue'], ['state', 'queue']]) {
        const state = deferred(), queue = deferred();
        const view = skillsReader({
            state: () => held.includes('state') ? state.promise : Promise.resolve({ github_token_configured: true }),
            skillLifecycleQueue: () => held.includes('queue') ? queue.promise : Promise.resolve({ events: [] }),
        });
        let completed = false;
        const rendering = view.render().then(() => { completed = true; });
        await nextTurn();
        try {
            assert.equal(completed, true, `${held}: primary render must complete independently`);
            assert.match(view.list.innerHTML, /data-skill="demo"/);
            assert.match(view.status.textContent, /loading/);
            assert.doesNotMatch(view.list.innerHTML, /Configure GITHUB_TOKEN/, 'pending is not known absence');
        } finally {
            state.resolve({ github_token_configured: true }); queue.resolve({ events: [] });
            await rendering; await nextTurn();
        }
        assert.equal(view.status.textContent, '');
        assert.equal(view.writes(), 1, 'late facts never repaint the whole list');
    }
});

test('late queue settlement merges raw primary rows, clearing prior running annotations', async () => {
    const queued = { events: [{ id: 'disable', target: 'demo', kind: 'disable', status: 'running' }] };
    const view = skillsReader({ skillLifecycleQueue: async () => queued });
    await view.render();
    const queue = deferred();
    view.apiClient.skillLifecycleQueue = () => queue.promise;
    await view.render();
    assert.match(view.list.innerHTML, /Disabling/);
    queue.resolve({ events: [{ ...queued.events[0], status: 'done' }] });
    await nextTurn();
    const updated = view.patches.at(-1).skill;
    assert.equal(updated.name, 'demo');
    assert.equal(Object.hasOwn(updated, 'lifecycle_pending'), false);
    assert.equal(Object.hasOwn(demo, 'lifecycle_pending'), false, 'raw extensions stay unannotated');
    assert.equal(view.writes(), 2);
});

test('late optional failures are named and older generation responses are ignored', async () => {
    const state = deferred(), queue = deferred();
    const view = skillsReader({ state: () => state.promise, skillLifecycleQueue: () => queue.promise });
    await view.render();
    state.reject(new Error('settings offline')); queue.reject(new Error('queue offline'));
    await nextTurn();
    assert.match(view.status.textContent, /Skill settings could not be refreshed/);
    assert.match(view.status.textContent, /Lifecycle progress could not be refreshed/);
    assert.equal(view.writes(), 1);
    const oldState = deferred(), oldQueue = deferred();
    view.apiClient.state = () => oldState.promise; view.apiClient.skillLifecycleQueue = () => oldQueue.promise;
    await view.render();
    view.apiClient.state = async () => ({ github_token_configured: true });
    view.apiClient.skillLifecycleQueue = async () => ({ events: [] });
    await view.render();
    const count = view.patches.length;
    oldState.resolve({ github_token_configured: false }); oldQueue.reject(new Error('old failed read'));
    await nextTurn();
    assert.equal(view.patches.length, count);
    assert.equal(view.status.textContent, '');
});

test('primary replacement closes menus after the read, whereas a failed read leaves them attached', async () => {
    const view = skillsReader();
    await view.render();
    for (const failed of [false, true]) {
        const primary = deferred();
        view.apiClient.extensions = () => primary.promise;
        let closes = 0;
        const rendering = view.render({ beforeReplace: () => { closes += 1; } });
        await nextTurn();
        assert.equal(closes, 0, 'a menu opened during this request still has its owner');
        if (failed) primary.reject(new Error('offline'));
        else primary.resolve({ skills: [demo], live: {} });
        await rendering;
        assert.equal(closes, failed ? 0 : 1);
    }
});

test('legacy publish keeps unknown, confirmed absent and present token facts distinct', () => {
    for (const value of [undefined, false, true]) {
        const html = renderInstalledSkillCard(demo, new Set(), new Set(), {}, { githubTokenConfigured: value });
        assert.equal(html.includes('Configure GITHUB_TOKEN'), value === false);
        assert.equal(html.includes('GitHub token status unavailable'), value === undefined);
        assert.equal(html.includes('data-submit-disabled="false"'), value === true);
    }
    const html = renderInstalledSkillCard({ ...demo, submit_hub: { visible: true, task_start_allowed: true } });
    assert.match(html, /data-submit-disabled="false"/, 'authoritative backend admission outranks optional state');
});

test('late older success and failure cannot replace the current list or its status', async () => {
    for (const fails of [false, true]) {
        const old = deferred(), fresh = deferred();
        const reads = [old, fresh];
        const view = skillsReader({ extensions: () => reads.shift().promise });
        const first = view.render(), second = view.render();
        fresh.resolve({ skills: [{ ...demo, name: 'current' }], live: {} });
        await second;
        if (fails) old.reject(new Error('old failure'));
        else old.resolve({ skills: [{ ...demo, name: 'obsolete' }], live: {} });
        await first;
        assert.match(view.list.innerHTML, /current/);
        assert.doesNotMatch(view.list.innerHTML, /obsolete/);
        assert.equal(view.status.textContent, '');
    }
});

test('late optional Hub badges update without recreating the skill card', async () => {
    const catalog = deferred();
    const view = skillsReader({ extensions: async () => ({ skills: [{
        ...demo, source: 'ouroboroshub', payload_root: 'skills/ouroboroshub/demo',
    }], live: {} }) });
    view.context.loadHubCatalog = () => catalog.promise;
    await view.render();
    const card = view.list.innerHTML;
    view.context.hubCatalog.available = true;
    view.context.hubCatalog.byName.set('demo', { sanitized_name: 'demo', slug: 'demo', latest_version: '2.0.0' });
    catalog.resolve();
    await nextTurn();
    assert.match(view.badge.innerHTML, /Update available/);
    assert.equal(view.list.innerHTML, card);
    assert.equal(view.writes(), 1, 'only badge contents may change after catalog enrichment');
});

test('one identical lifecycle/load failure renders once; distinct causes remain visible', () => {
    const common = { ...demo, lifecycle_status: 'failed', lifecycle_error: 'same failure', load_error: 'same failure' };
    assert.equal(renderInstalledSkillCard(common).split('same failure').length - 1, 1);
    assert.equal(renderInstalledSkillCard({ ...common, lifecycle_virtual: true, description: 'same failure' })
        .split('same failure').length - 1, 1);
    const html = renderInstalledSkillCard({ ...common, load_error: 'different failure' });
    assert.match(html, /same failure/);
    assert.match(html, /different failure/);
});

test('a primary read failure during owner attestation produces feedback without submitting', async () => {
    const container = node(), messages = [];
    const context = vm.createContext({
        document: { addEventListener() {} }, window: { addEventListener() {} },
        fetchSkills: async () => { throw new Error('inventory offline'); },
        showToast: message => { messages.push(message); },
    });
    vm.runInContext(source('skills', 'function attachActionHandlers(', '\nfunction activateTab'), context);
    context.attachActionHandlers(container, () => {}, new Set(), new Set());
    const button = { dataset: { skill: 'demo' }, classList: { contains: name => name === 'skills-attest-review' } };
    await container.handlers.click({ target: {
        closest: selector => selector === 'button[data-skill]' ? button : null,
    } });
    assert.deepEqual(messages, ['demo: inventory offline']);
});

test('cancelled skill actions retain their card; confirmed effects still refresh it', async () => {
    for (const kind of ['skills-delete-local', 'skills-uninstall', 'skills-submit-hub', 'skills-grant', 'review', 'repair']) {
        const container = node();
        let renders = 0, writes = 0;
        const context = vm.createContext({
            fetchSkills: async () => ({ skills: [{ ...demo, content_hash: 'a'.repeat(64) }] }),
            openConfirmDialog: async () => false,
            runSkillPublishFlow: async () => ({ started: false }),
            apiClient: { deleteSkill: async () => { writes += 1; return { ok: true }; } },
            postWithFeedback: async () => { writes += 1; return { ok: true }; },
            emitSkillLifecycle() {}, showToast() {},
        });
        vm.runInContext(source('skills', 'function attachActionHandlers(', '\nfunction activateTab'), context);
        context.attachActionHandlers(container, () => { renders += 1; }, new Set(), new Set());
        const primary = ['review', 'repair'].includes(kind);
        const button = { dataset: { skill: 'demo', skillAction: kind, keys: 'KEY' },
            classList: { contains: name => name === kind } };
        const event = { target: { closest: selector => (
            primary ? selector === '[data-skill-action]' : selector === 'button[data-skill]'
        ) ? button : null } };
        await container.handlers.click(event);
        assert.equal(writes, 0, `${kind}: cancellation has no side effect`);
        assert.equal(renders, 0, `${kind}: cancellation does not replace the focus return target`);
        assert.equal(button.disabled, false);

        if (kind === 'skills-delete-local' || kind === 'skills-uninstall') {
            context.openConfirmDialog = async () => true;
            await container.handlers.click(event);
            assert.equal(writes, 1, `${kind}: confirmation preserves the existing operation`);
            assert.equal(renders, 1);
        }
    }
});

test('failed lifecycle helpers do not claim computation and unknown installed state offers no Install', () => {
    assert.equal(lifecycleSpinnerFor({ failed: true }), '');
    assert.equal(lifecycleCardClassFor({ failed: true }), 'marketplace-card');
    assert.match(lifecycleSpinnerFor({ failed: false }), /spinner/);
    assert.equal(lifecycleFor({}, null, null).action, 'install', 'a confirmed absent skill is installable');
    const unknown = lifecycleFor({}, null, { failed: true, retry_action: 'install' }, { installedUnavailable: true });
    assert.equal(unknown.action, '');
    assert.equal(unknown.disabled, true);
    assert.doesNotMatch(unknown.label, /Not installed/);
});

test('ClawHub primary and optional installed reads have independent outcomes', async () => {
    const context = vm.createContext({ AbortController, setTimeout, clearTimeout, console: { warn() {} } });
    vm.runInContext(source('marketplace', 'async function loadInstalled(', '\nasync function runSearch'), context);
    context.fetchJson = async path => {
        if (path.endsWith('/installed')) throw new Error('primary failed');
        return { skills: [] };
    };
    const unavailable = await context.loadInstalled();
    assert.equal(unavailable.available, false);
    assert.equal(unavailable.map, null);
    context.fetchJson = async path => {
        if (path.endsWith('/extensions')) throw new Error('optional failed');
        return { skills: [{ ...demo, provenance: { slug: 'demo' } }] };
    };
    const partial = await context.loadInstalled();
    assert.equal(partial.available, true);
    assert.equal(partial.map.get('demo').name, 'demo');
    assert.equal(partial.enrichmentAvailable, false);
});

function catalogPane(kind) {
    const selectors = kind === 'marketplace'
        ? ['#mp-query', '#mp-only-official', '[data-mp-search]', '#mp-results', '#mp-pagination', '#mp-status']
        : ['#oh-query', '#oh-results', '#oh-status', '[data-oh-search]'];
    const nodes = Object.fromEntries(selectors.map(selector => [selector, node()]));
    const pane = { ...node(), querySelector: selector => nodes[selector] };
    return { pane, nodes };
}

test('ClawHub retains catalog results on failure, updates installed availability, and recovers through Refresh', async () => {
    const { pane, nodes } = catalogPane('marketplace');
    let failed = false, filtered = false, onPending;
    const pending = new Map();
    const paints = [];
    const context = vm.createContext({
        AbortController, setTimeout, clearTimeout, URLSearchParams, isRateLimitError,
        paneTemplate: () => '', getPendingBySlug: () => pending,
        getPending: slug => pending.get(slug),
        setPending: (slug, value) => { pending.set(slug, value); onPending(); },
        document: { getElementById: id => nodes[`#${id}`] },
        startLifecyclePoller: callback => { onPending = callback; return () => {}; },
        runSearch: async () => { if (failed) throw new Error('catalog offline'); return { results: filtered ? [] : [{ slug: 'demo' }] }; },
        loadInstalled: async () => ({ available: !failed, map: new Map(), enrichmentAvailable: true }),
        renderResults: (host, rows, installed, count, facts) => {
            host.innerHTML = rows.map(row => row.slug + (pending.get(row.slug)?.message || '')).join(',');
            host.querySelectorAll = () => rows.map(row => ({ dataset: { slug: row.slug } }));
            paints.push({ count, unavailable: facts.installedUnavailable });
        },
        renderPagination() {},
    });
    vm.runInContext(source('marketplace', 'function installErrorCopy(', '\nconst safeExternalUrl')
        + source('marketplace', 'function showStatus(', '\nasync function loadInstalled')
        + source('marketplace', 'export function initMarketplace('), context);
    await context.initMarketplace(pane);
    assert.equal(nodes['#mp-results'].innerHTML, 'demo');
    failed = true;
    await pane._marketplaceRefresh();
    assert.equal(nodes['#mp-results'].innerHTML, 'demo');
    assert.equal(paints.at(-1).unavailable, true);
    assert.match(nodes['#mp-status'].textContent, /catalog offline.*Showing previous results/);
    onPending();
    assert.match(nodes['#mp-status'].textContent, /catalog offline/);
    failed = false;
    await pane._marketplaceRefresh();
    assert.equal(paints.at(-1).unavailable, false);
    assert.doesNotMatch(nodes['#mp-status'].textContent, /offline/);

    // The request may outlive its visible row when the owner searches again.
    for (const rowGone of [true, false]) {
        filtered = false;
        await pane._marketplaceRefresh();
        const install = deferred();
        context.jsonPost = () => install.promise;
        const button = { dataset: { slug: 'demo', mpAction: 'install' } };
        const action = nodes['#mp-results'].handlers.click({ target: {
            closest: selector => selector === '[data-mp-action]' ? button : null,
        } });
        filtered = rowGone;
        await pane._marketplaceRefresh();
        install.reject(new Error('install failed after filtering'));
        await action;
        const status = nodes['#mp-status'].textContent;
        const cards = nodes['#mp-results'].innerHTML;
        assert.equal((status + cards).split('install failed after filtering').length - 1, 1);
        assert.equal(status.includes('install failed after filtering'), rowGone);
    }
});

test('Hub failed listing never becomes Install, and failed catalog keeps useful rows without duplicate errors', async () => {
    const { pane, nodes } = catalogPane('hub');
    let catalogFailed = true, listingFailed = true, filtered = false, onPending, install;
    const pending = new Map();
    nodes['#oh-results'].querySelectorAll = () => nodes['#oh-results'].innerHTML.includes('data-slug="demo"')
        ? [{ dataset: { slug: 'demo' } }] : [];
    const context = vm.createContext({
        URLSearchParams, setTimeout, clearTimeout, hubListingRowFor, hubSyncVerdict,
        escapeHtml: escapeHtmlAttr, renderHubCard,
        template: () => '', getPending: slug => pending.get(slug),
        setPending: (slug, value) => { pending.set(slug, value); onPending(); },
        startLifecyclePoller: callback => { onPending = callback; return () => {}; },
        fetchJson: async (path, options) => {
            if (options?.method === 'POST') return install.promise;
            if (path.startsWith('/api/extensions')) {
                if (listingFailed) throw new Error('listing offline');
                return { skills: [] };
            }
            if (catalogFailed) throw new Error('catalog offline');
            return { results: filtered ? [] : [{ slug: 'demo', sanitized_name: 'demo', latest_version: '1.0.0' }] };
        },
    });
    vm.runInContext(source('ouroboroshub', 'function adoptHint(', '\nfunction controlsTemplate')
        + source('ouroboroshub', 'export function initOuroborosHub('), context);
    await context.initOuroborosHub(pane);
    onPending();
    assert.equal(nodes['#oh-results'].innerHTML, '', 'first failure is not an empty result');
    catalogFailed = false;
    await pane._ouroboroshubRefresh();
    assert.match(nodes['#oh-results'].innerHTML, /Hub facts unavailable/);
    assert.doesNotMatch(nodes['#oh-results'].innerHTML, /data-oh-action="install"/);
    listingFailed = false;
    await pane._ouroboroshubRefresh();
    assert.match(nodes['#oh-results'].innerHTML, /data-oh-action="install"/);
    catalogFailed = true;
    await pane._ouroboroshubRefresh();
    assert.match(nodes['#oh-results'].innerHTML, /demo/);
    assert.doesNotMatch(nodes['#oh-results'].innerHTML, /catalog offline|data-oh-action="install"/);
    assert.match(nodes['#oh-status'].textContent, /catalog offline.*Showing previous results/);
    catalogFailed = false;
    for (const rowGone of [true, false]) {
        filtered = false;
        await pane._ouroboroshubRefresh();
        install = deferred();
        const button = { dataset: { ohSlug: 'demo', ohAction: 'install' } };
        const action = nodes['#oh-results'].handlers.click({ target: {
            closest: selector => selector === '[data-oh-action]' ? button : null,
        } });
        filtered = rowGone;
        await pane._ouroboroshubRefresh();
        install.reject(new Error('hub install failed after filtering'));
        await action;
        const status = nodes['#oh-status'].textContent;
        const cards = nodes['#oh-results'].innerHTML;
        assert.equal((status + cards).split('hub install failed after filtering').length - 1, 1);
        assert.equal(status.includes('hub install failed after filtering'), rowGone);
    }
});

test('Skills header Refresh and page revisit call the currently selected catalog', async () => {
    const ids = ['content', 'skills-list', 'skills-empty', 'skills-refresh'];
    const nodes = Object.fromEntries(ids.map(id => [id, node()]));
    nodes.content.appendChild = () => {};
    const tabs = ['installed', 'marketplace', 'ouroboroshub'].map(tab => ({ ...node(), dataset: { tab } }));
    const calls = [], listeners = {};
    const context = vm.createContext({
        document: {
            createElement: () => ({ firstElementChild: {} }),
            getElementById: id => nodes[id], querySelector: () => node(),
        },
        window: {
            addEventListener: (event, callback) => { listeners[event] = callback; },
            removeEventListener: event => { delete listeners[event]; },
        },
        skillsPageTemplate: () => '', activateTab() {}, loadHubCatalog() {},
        attachActionHandlers: () => ({ closeMenus() {}, destroy() {} }),
        bindTabStrip: (strip, { onChange }) => {
            tabs.forEach(tab => { tab.handlers.click = () => onChange(tab.dataset.tab, tab); });
            return { select() {}, destroy() {} };
        },
        renderSkillsList: async () => { calls.push('installed'); },
        renderMarketplacePane: async () => { calls.push('marketplace'); },
        renderOuroborosHubPane: async () => { calls.push('ouroboroshub'); },
        setTimeout: callback => callback(), console,
        showToast: message => { throw new Error(message); },
    });
    vm.runInContext(source('skills', 'export function initSkills('), context);
    context.initSkills({});
    for (const tab of tabs) {
        tab.handlers.click();
        await nextTurn();
        calls.length = 0;
        await nodes['skills-refresh'].handlers.click();
        assert.deepEqual(calls, [tab.dataset.tab]);
        calls.length = 0;
        listeners['ouro:page-shown']({ detail: { page: 'skills' } });
        await nextTurn();
        assert.deepEqual(calls, [tab.dataset.tab]);
    }
    const older = deferred(), current = deferred();
    context.renderMarketplacePane = () => older.promise;
    context.renderOuroborosHubPane = () => current.promise;
    tabs[1].handlers.click();
    tabs[2].handlers.click();
    older.resolve();
    await nextTurn();
    assert.equal(nodes['skills-refresh'].disabled, true, 'older tab request does not finish the current refresh');
    current.resolve();
    await nextTurn();
    assert.equal(nodes['skills-refresh'].disabled, false);
});
