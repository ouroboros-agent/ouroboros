"""Phone shell interactions: layer inert, Back, Escape, focus, the drawer drag, inline links, a short phone.

Each case turns a CR1 reviewer probe around: it asserts the fixed behaviour and, where a guard is involved, that
the guard still fires. Uses the static-server/API fixture (test_subscription_setup_browser); never starts the
runtime or reads live data. Synthetic CDP touch is Chromium's input pipeline, not a claim about an iPhone WebView.
"""
from __future__ import annotations

import json

import pytest

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import PROJECTS, save_evidence, seed_page, settle, sidebar_metrics

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.fixture(params=['webkit', 'chromium'])
def shell_ui(request, monkeypatch):
    monkeypatch.setenv('OUROBOROS_UI_BROWSER_ENGINE', request.param)
    ui = request.getfixturevalue('subscription_ui')
    ui['engine'] = request.param
    return ui


@pytest.fixture
def chromium_ui(request, monkeypatch):
    # CDP Input.dispatchTouchEvent exists only in Chromium; WebKit has no equivalent, so these cases run there only.
    monkeypatch.setenv('OUROBOROS_UI_BROWSER_ENGINE', 'chromium')
    ui = request.getfixturevalue('subscription_ui')
    ui['engine'] = 'chromium'
    return ui


def evidence(tmp_path, name, facts):
    save_evidence(tmp_path, name, facts)
    print(name, json.dumps(facts, indent=1))


def boot(page, ui):
    page.goto(ui['url'] + '/')
    page.wait_for_selector('#chat-input', state='attached')
    page.wait_for_selector('#nav-projects-list [data-project-id]', state='attached')
    settle(page)


def ids(page):
    return page.evaluate('window.__ouroLayers.ids()')


def wait_quietly(page, expression, timeout=3_000):
    """Wait for a condition; a defect that never meets it is recorded by the facts that follow, not a timeout."""
    try:
        page.wait_for_function(expression, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - the facts read next are the observation
        return False


def open_project(page, index=0):
    page.evaluate("p => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail: {project: p}}))",
                  PROJECTS[index])
    page.wait_for_function("() => window.__ouroLayers.top() === 'project'")
    page.wait_for_timeout(300)


def open_drawer(page):
    page.locator('[data-mobile-nav-toggle]:visible').first.click()
    page.wait_for_function("() => window.__ouroLayers.top() === 'drawer'")
    page.wait_for_timeout(250)


def open_confirm(page):
    page.evaluate("""async () => {
        const m = await import('/static/modules/confirm_dialog.js');
        window.__confirmResult = 'pending';
        m.openConfirmDialog({title: 'Stop this work?', body: 'shell fixture', danger: true})
            .then((r) => { window.__confirmResult = JSON.stringify(r); });
    }""")
    page.wait_for_selector('.confirm-dialog-backdrop')


def open_reader(page):
    page.evaluate("""async () => {
        const m = await import('/static/modules/document_reader.js');
        window.__reader = m.createDocumentReader({});
        window.__reader.open({filename: 'notes.md', kind: 'markdown', meta: '1 KB',
                              reader: {url: '/api/files/read?path=notes.md'}});
    }""")
    page.wait_for_selector('dialog.document-reader[open]')
    page.wait_for_timeout(300)


def sync_project_rows(page):
    """The next /api/state snapshot changes one row (Project 5 has a new visible message). The 20 s poll would
    deliver it; the projects_changed frame (a backend-created project) asks for the same snapshot now."""
    rows = [dict(p) for p in PROJECTS]
    rows[5]['visible_revision'] = 3
    state = {'supervisor_ready': True, 'active_chat_activities': [], 'projects': rows,
             'project_chat_ids': [p['chat_id'] for p in rows]}
    page.route('**/api/state', lambda route: route.fulfill(json=state))
    page.evaluate("window.__ouroWs.emit('projects_changed', {})")
    page.wait_for_function("() => document.querySelector('#nav-projects-count')?.textContent === '1'")
    page.wait_for_timeout(300)


SURFACES = {
    'confirm': {'surface': '.confirm-dialog-backdrop',
                'control': '.confirm-dialog .marketplace-modal-actions [data-confirm-cancel]'},
    'reader': {'surface': 'dialog.document-reader', 'control': 'dialog.document-reader [data-reader-action="close"]'},
}
OVER_ROOM = """sel => {
    const s = document.querySelector(sel.surface), c = document.querySelector(sel.control);
    const room = {contentInert: Boolean(document.getElementById('content').closest('[inert]')),
                  panelInert: Boolean(document.getElementById('project-panel').closest('[inert]')),
                  layers: window.__ouroLayers.ids()};
    if (!s || !c) return {...room, open: false};
    const r = c.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {...room, open: true, inert: Boolean(s.closest('[inert]')),
            hitIsControl: Boolean(hit) && (hit === c || c.contains(hit)),
            hit: hit ? hit.tagName + '.' + [...hit.classList].join('.') : null};
}"""


@pytest.mark.parametrize('surface', list(SURFACES))
def test_a_dialog_over_a_phone_project_stays_live_after_a_state_sync(shell_ui, tmp_path, surface):
    """Layer inert stops at the app shell: a body-level dialog survives the row change; the room stays inert."""
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        open_project(page)
        open_confirm(page) if surface == 'confirm' else open_reader(page)
        facts['before_sync'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        sync_project_rows(page)
        facts['after_sync'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        try:
            page.locator(SURFACES[surface]['control']).click(timeout=2_500)
            facts['click'] = 'clicked'
        except Exception as exc:  # noqa: BLE001 - the observation is the point
            facts['click'] = f'failed: {type(exc).__name__}'
        page.wait_for_timeout(300)
        facts['after_click'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        facts['result'] = page.evaluate('window.__confirmResult ?? null')
    finally:
        evidence(tmp_path, f'shell-dialog-after-sync-{surface}-{shell_ui["engine"]}', facts)
        page.close()
    for phase in ('before_sync', 'after_sync'):
        assert facts[phase]['open'] and not facts[phase]['inert'] and facts[phase]['hitIsControl'], facts
    # The guard still fires: the room outside the open Project layer stays inert, the panel stays live.
    assert facts['after_sync']['contentInert'] and not facts['after_sync']['panelInert'], facts
    assert facts['click'] == 'clicked' and not facts['after_click']['open'], facts
    assert facts['after_click']['layers'] == ['project'] and facts['after_click']['contentInert'], facts
    if surface == 'confirm':
        assert facts['result'] == 'false', facts


# A Back that leaves the app lands on about:blank: the facts say so instead of throwing.
BACK_FACTS = """() => ({layers: window.__ouroLayers?.ids() ?? 'left the app',
    drawer: Boolean(document.getElementById('primary-sidebar')?.classList.contains('open')),
    dialog: Boolean(document.querySelector('.new-project-dialog, .confirm-dialog')),
    result: window.__confirmResult ?? null})"""


def press_back(page, via):
    page.evaluate('Telegram.WebApp.BackButton.callback()' if via == 'telegram' else 'history.back()')


@pytest.mark.parametrize('via', ['history', 'telegram'])
@pytest.mark.parametrize('case', ['new-project-over-drawer', 'confirm-over-project'])
def test_back_closes_the_topmost_dialog_before_its_layer(shell_ui, tmp_path, case, via):
    layer = 'drawer' if case == 'new-project-over-drawer' else 'project'
    page = seed_page(shell_ui, 390, 844, telegram=via == 'telegram')
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        if layer == 'drawer':
            open_drawer(page)
            page.locator('#nav-projects-add').click()
            page.locator('[data-np-name]').fill('Retained draft')
        else:
            open_project(page)
            open_confirm(page)
        wait_quietly(page, 'window.__ouroLayers?.historyArmed()')
        facts['open'] = page.evaluate(BACK_FACTS)
        press_back(page, via)
        wait_quietly(page, "() => !document.querySelector('.new-project-dialog, .confirm-dialog') "
                           "|| !window.__ouroLayers?.size()")
        page.wait_for_timeout(200)
        facts['first_back'] = page.evaluate(BACK_FACTS)
        wait_quietly(page, 'window.__ouroLayers?.historyArmed()')
        press_back(page, via)
        wait_quietly(page, '() => !window.__ouroLayers?.size()')
        page.wait_for_timeout(200)
        facts['second_back'] = page.evaluate(BACK_FACTS)
    finally:
        evidence(tmp_path, f'shell-back-{case}-{via}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['open']['layers'] == [layer] and facts['open']['dialog'], facts
    assert facts['first_back']['layers'] == [layer] and not facts['first_back']['dialog'], facts
    assert facts['first_back']['drawer'] is (layer == 'drawer'), facts
    if layer == 'project':
        assert facts['first_back']['result'] == 'false', facts
    assert facts['second_back']['layers'] == [] and not facts['second_back']['drawer'], facts


@pytest.mark.parametrize('width,height', [(1280, 860), (390, 844)])
def test_escape_in_a_composer_capsule_closes_only_the_capsule(shell_ui, tmp_path, width, height):
    page = seed_page(shell_ui, width, height)
    facts = {'engine': shell_ui['engine'], 'viewport': [width, height]}
    try:
        boot(page, shell_ui)
        open_project(page)
        head = page.locator('#project-panel .chat-capsule-head').first
        head.click()
        page.wait_for_timeout(250)
        facts['capsule_before'] = head.get_attribute('aria-expanded')
        page.keyboard.press('Escape')
        page.wait_for_timeout(400)
        facts['capsule_after'] = head.get_attribute('aria-expanded') if head.count() else 'gone'
        facts['layers_after'] = ids(page)
        # Nothing inside takes the next Escape: it is the room's again (the guard is not a swallow-all).
        page.keyboard.press('Escape')
        page.wait_for_timeout(400)
        facts['layers_second'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-escape-capsule-{shell_ui["engine"]}-{width}', facts)
        page.close()
    assert facts['capsule_before'] == 'true', facts
    assert facts['capsule_after'] == 'false' and facts['layers_after'] == ['project'], facts
    assert facts['layers_second'] == [], facts


WHO = "(() => { const a = document.activeElement; return a ? a.tagName + '#' + a.id + '.' + [...a.classList].slice(0, 3).join('.') : null; })()"


@pytest.mark.parametrize('how', ['enter', 'tap'])
def test_a_project_opened_from_the_phone_drawer_takes_focus(shell_ui, tmp_path, how):
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine'], 'how': how}
    def press(locator):  # a keyboard user or a finger
        if how == 'tap':
            return locator.tap()
        locator.focus()
        page.keyboard.press('Enter')

    try:
        boot(page, shell_ui)
        press(page.locator('[data-mobile-nav-toggle]:visible').first)
        page.wait_for_function("() => window.__ouroLayers.top() === 'drawer'")
        page.wait_for_timeout(250)
        press(page.locator('#nav-projects-list [data-project-id]').first)
        page.wait_for_function("() => window.__ouroLayers.top() === 'project'")
        page.wait_for_timeout(400)
        facts['focus'] = page.evaluate(WHO)
        facts['inside_panel'] = page.evaluate("document.getElementById('project-panel').contains(document.activeElement)")
        facts['layers'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-focus-{how}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['layers'] == ['project'] and facts['inside_panel'] is True, facts


def swipe(page, start, end, steps=12):
    cdp = page.context.new_cdp_session(page)
    try:
        for i in range(steps + 1):
            point = {'x': start[0] + (end[0] - start[0]) * i / steps,
                     'y': start[1] + (end[1] - start[1]) * i / steps, 'id': 1}
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart' if i == 0 else 'touchMove', 'touchPoints': [point]})
            page.wait_for_timeout(16)
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_timeout(450)
    finally:
        cdp.detach()


DRAWER = """() => ({open: document.getElementById('primary-sidebar').classList.contains('open'),
                    layers: window.__ouroLayers.ids()})"""
UNDER = "([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.tagName + '#' + e.id + '.' + e.className : null; }"


@pytest.mark.parametrize('room', ['main', 'project'])
def test_the_drawer_follows_a_finger_on_a_phone_room(chromium_ui, tmp_path, room):
    page = seed_page(chromium_ui, 390, 844)
    under = [] if room == 'main' else ['project']  # the drawer opens over an open Project room
    facts = {'room': room}
    try:
        boot(page, chromium_ui)
        if room == 'project':
            open_project(page)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        box = page.locator('#chat-messages' if room == 'main' else '#project-panel-body').bounding_box()
        y = box['y'] + box['height'] / 2
        facts['under'] = page.evaluate(UNDER, [60, y])
        for name, start, end in [('vertical_up', (200, y + 120), (200, y - 120)),
                                 ('vertical_down', (200, y - 120), (200, y + 120))]:
            swipe(page, start, end)
            facts[name] = page.evaluate(DRAWER)
            if facts[name]['open']:
                page.evaluate('window.__ouroLayers.pop()')  # keep the next case independent
                page.wait_for_timeout(300)
        swipe(page, (60, y), (330, y))
        facts['open_drag'] = page.evaluate(DRAWER)
        if facts['open_drag']['open']:
            swipe(page, (300, y), (20, y))  # starts on the open drawer
            facts['close_drag'] = page.evaluate(DRAWER)
            swipe(page, (60, y), (330, y))
            swipe(page, (370, y), (60, y))  # starts on the scrim's visible strip
            facts['scrim_close_drag'] = page.evaluate(DRAWER)
    finally:
        evidence(tmp_path, f'shell-drawer-drag-{room}', facts)
        page.close()
    assert facts['coarse'] is True, facts
    # The guard: a vertical finger scrolls the feed and never opens the drawer, in either direction.
    assert facts['vertical_up'] == facts['vertical_down'] == {'open': False, 'layers': under}, facts
    assert facts['open_drag'] == {'open': True, 'layers': under + ['drawer']}, facts
    assert facts['close_drag'] == {'open': False, 'layers': under}, facts
    assert facts['scrim_close_drag'] == {'open': False, 'layers': under}, facts


WORD = """bar => {
    const t = document.querySelector(bar + ' .rw-trigger:not([hidden])');
    const b = t?.querySelector('.status-badge');
    if (!t || !b) return null;
    const shown = getComputedStyle(b).display !== 'none';
    return {shown, cut: shown && b.scrollWidth > b.clientWidth + 1, word: t.dataset.word || '', title: t.title,
            mark: [...t.querySelectorAll('.rw-mark, .rw-dots')].some((m) => getComputedStyle(m).display !== 'none')};
}"""


@pytest.mark.parametrize('room,width', [('main', 390), ('project', 390), ('main', 1280)])
def test_the_status_word_is_whole_or_absent(shell_ui, tmp_path, room, width):
    page = seed_page(shell_ui, width, 844)
    facts = {'engine': shell_ui['engine'], 'room': room, 'width': width}
    try:
        boot(page, shell_ui)
        if room == 'project':
            open_project(page)  # the fixture's long Project name leaves the word no room on a phone
        page.wait_for_timeout(300)
        facts['word'] = page.evaluate(WORD, '#project-panel .project-panel-bar' if room == 'project' else '.chat-page-header')
    finally:
        evidence(tmp_path, f'shell-status-word-{room}-{width}-{shell_ui["engine"]}', facts)
        page.close()
    word = facts['word']
    assert word and word['mark'] and word['title'], facts
    assert word['cut'] is False, facts  # never a fragment such as "Wor…"
    if width >= 720:
        assert word['shown'] is True, facts  # a bar with room keeps the word


INLINE_LINK = """() => {
    const a = document.querySelector('.settings-section-copy a[href="https://clawhub.ai"]');
    const range = document.createRange();
    range.selectNodeContents(a.parentElement);
    const tops = [...range.getClientRects()].filter((q) => q.width && q.height).map((q) => q.top).sort((x, y) => x - y);
    const lines = tops.reduce((acc, t) => (acc.length && t - acc[acc.length - 1] < 3 ? acc : [...acc, t]), []);
    return {display: getComputedStyle(a).display, height: Math.round(a.getBoundingClientRect().height),
            lines: lines.length, gaps: lines.slice(1).map((t, i) => Math.round((t - lines[i]) * 10) / 10)};
}"""
TARGET = """sel => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const r = e.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const hit = [[cx - 21, cy], [cx + 21, cy], [cx, cy - 21], [cx, cy + 21]].every(([x, y]) => {
        const t = document.elementFromPoint(x, y);
        return Boolean(t) && (t === e || e.contains(t));
    });
    return {width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10, hit,
            display: getComputedStyle(e).display};
}"""


def open_settings_panel_of(page, selector):
    panel = page.evaluate("s => document.querySelector(s)?.closest('[data-settings-panel]')?.dataset.settingsPanel || ''",
                          selector)
    page.locator(f'[data-settings-tab="{panel}"]').first.click()
    page.locator(selector).first.scroll_into_view_if_needed()
    settle(page)
    return panel


def test_an_inline_link_in_settings_copy_reads_as_text_while_a_standalone_link_keeps_a_finger_target(shell_ui, tmp_path):
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine']}
    standalone = '.about-links a[href]'
    try:
        boot(page, shell_ui)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        open_drawer(page)
        page.locator('[data-nav-page="settings"]').click()
        page.locator('#page-settings').wait_for(state='visible')
        facts['inline_panel'] = open_settings_panel_of(page, '.settings-section-copy a[href="https://clawhub.ai"]')
        facts['inline'] = page.evaluate(INLINE_LINK)
        facts['standalone_panel'] = open_settings_panel_of(page, standalone)
        facts['standalone'] = page.evaluate(TARGET, standalone)
    finally:
        evidence(tmp_path, f'shell-inline-link-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['coarse'] is True, facts
    inline = facts['inline']
    assert inline['display'] == 'inline', facts
    assert inline['lines'] >= 3 and max(inline['gaps']) - min(inline['gaps']) <= 2, facts
    target = facts['standalone']
    assert target and (target['hit'] or (target['width'] >= 43.9 and target['height'] >= 43.9)), facts


def test_a_short_phone_drawer_has_one_scroller_the_whole_column(shell_ui, tmp_path):
    page = seed_page(shell_ui, 390, 570)  # the 30-Project fixture of the gate
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        facts['projects'] = page.locator('#nav-projects-list [data-project-id]').count()
        facts['scrollers'] = sidebar_metrics(page)  # opens the drawer
        facts['layers'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-short-phone-sidebar-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['coarse'] is True and facts['projects'] == len(PROJECTS) and facts['layers'] == ['drawer'], facts
    assert [row['node'] for row in facts['scrollers']] == ['primary-sidebar'], facts


def saved_history(tmp_path):
    """The real history endpoint over two saved rows, so the feed draws bubbles with Copy and Reply."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ouroboros import dialogue_store
    from ouroboros.gateway.history import make_chat_history_endpoint
    root = tmp_path / 'history'
    root.mkdir()
    for minute, (direction, role, text) in enumerate([('in', 'user', 'Please check the build before the release.'),
                                                       ('out', 'assistant', 'The build is green; nothing to fix.')]):
        dialogue_store.append(root, 'chat', {'chat_id': 1, 'user_id': 1, 'direction': direction, 'role': role,
                                             'text': text, 'ts': f'2026-10-18T10:0{minute}:00Z'})
    client = TestClient(Starlette(routes=[Route('/api/chat/history', make_chat_history_endpoint(root))]))

    def respond(route):
        response = client.get('/' + route.request.url.split('/', 3)[3])
        route.fulfill(status=response.status_code, content_type='application/json', body=response.content)
    return client, respond


ACTION_ROWS = """() => [...document.querySelectorAll('#chat-messages .chat-message-actions')]
    .filter((row) => row.offsetParent && row.children.length >= 2)
    .map((row) => [...row.children].filter((c) => c.offsetParent).map((c) => {
        const r = c.getBoundingClientRect();
        return {name: c.className, top: Math.round(r.top * 10) / 10, bottom: Math.round(r.bottom * 10) / 10};
    }))"""


@pytest.mark.parametrize('width', [1280, 390])
def test_copy_and_reply_sit_side_by_side_on_one_line(shell_ui, tmp_path, width):
    page = seed_page(shell_ui, width, 860)
    client, respond = saved_history(tmp_path)
    facts = {'engine': shell_ui['engine'], 'width': width}
    try:
        page.route('**/api/chat/history**', respond)
        boot(page, shell_ui)
        page.wait_for_selector('#chat-messages .chat-message-actions .chat-message-copy', timeout=20_000)
        facts['rows'] = page.evaluate(ACTION_ROWS)
    finally:
        evidence(tmp_path, f'shell-copy-reply-{width}-{shell_ui["engine"]}', facts)
        page.close()
        client.close()
    assert facts['rows'], facts
    for row in facts['rows']:
        assert len(row) >= 2 and any('copy' in c['name'] for c in row) and any('reply' in c['name'] for c in row), facts
        assert max(c['top'] for c in row) - min(c['top'] for c in row) <= 0.5, facts
        assert max(c['bottom'] for c in row) - min(c['bottom'] for c in row) <= 0.5, facts


TITLE_BAND = """band => {
    // launcher_window_chrome.py's own press filter: a plain press off any control moves the window.
    const CONTROLS = 'a[href], button, input, select, textarea, summary, label, iframe, video, audio, '
        + '[contenteditable]:not([contenteditable="false"]), [tabindex]:not([tabindex="-1"]), [draggable="true"], '
        + '[role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [role="checkbox"], '
        + '[role="radio"], [role="switch"], [role="slider"], [role="textbox"]';
    const left = document.getElementById('primary-sidebar').getBoundingClientRect().right;
    const caught = {};
    for (let y = 4; y < band; y += 6) {
        for (let x = left + 4; x < innerWidth - 8; x += 12) {
            const t = document.elementFromPoint(x, y);
            const control = t?.closest(CONTROLS);
            if (control) caught[control.tagName.toLowerCase() + '.' + [...control.classList].slice(0, 2).join('.')] = y;
        }
    }
    const tab = document.querySelector('.page.active .app-tab');
    return {caught, firstTabTop: tab ? Math.round(tab.getBoundingClientRect().top) : null};
}"""


@pytest.mark.parametrize('page_name', ['dashboard', 'settings'])
def test_the_desktop_title_band_over_a_page_moves_the_window(shell_ui, tmp_path, page_name):
    """macOS desktop window: the transparent title bar's band over a page holds no control, its tabs start below."""
    page = seed_page(shell_ui, 1280, 860)
    facts = {'engine': shell_ui['engine'], 'page': page_name}
    try:
        boot(page, shell_ui)
        # What launcher_window_chrome.py writes into the document once the integrated title bar is applied.
        page.evaluate("""() => { const root = document.documentElement; root.dataset.windowChrome = 'integrated';
            root.style.setProperty('--window-chrome-top', '52px'); root.style.setProperty('--window-chrome-start', '78px'); }""")
        page.locator(f'[data-nav-page="{page_name}"]').click()
        page.locator(f'#page-{page_name}').wait_for(state='visible')
        settle(page)
        facts['band'] = page.evaluate(TITLE_BAND, 52)
    finally:
        evidence(tmp_path, f'shell-title-band-{page_name}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['band']['caught'] == {}, facts
    assert facts['band']['firstTabTop'] is not None and facts['band']['firstTabTop'] >= 52, facts


def live_state(project_ids):
    """The /api/state snapshot with work running in these Projects (the census the sidebar's dots read)."""
    return {'supervisor_ready': True, 'projects': PROJECTS, 'project_chat_ids': [p['chat_id'] for p in PROJECTS],
            'active_chat_activities_complete': True,
            'active_chat_activities': [{'activity_id': f'work-{pid}', 'root_task_id': f'work-{pid}', 'project_id': pid,
                                        'phase': 'working'} for pid in project_ids]}


SIDEBAR_FACTS = """() => {
    const side = document.getElementById('primary-sidebar');
    const rows = [...side.querySelectorAll('#nav-projects-list > .nav-project-item')].filter((e) => !e.hidden);
    const zone = document.getElementById('nav-projects').getBoundingClientRect();
    const files = side.querySelector('[data-nav-page="files"]').getBoundingClientRect();
    const all = document.getElementById('nav-projects-show-all');
    return {rows: rows.map((e) => e.querySelector('[data-project-id]').dataset.projectId),
            gapToTools: Math.round(files.top - zone.bottom), filesTop: Math.round(files.top),
            sidebarBottom: Math.round(side.getBoundingClientRect().bottom),
            showAll: all && !all.hidden ? all.textContent.trim() : null,
            expanded: document.getElementById('nav-projects-toggle').getAttribute('aria-expanded')};
}"""


@pytest.mark.parametrize('width,height', [(1108, 680), (390, 844)])
def test_collapsed_projects_keep_the_live_ones_and_the_tools_follow(shell_ui, tmp_path, width, height):
    page = seed_page(shell_ui, width, height)
    live = [PROJECTS[3]['id'], PROJECTS[7]['id']]
    facts = {'engine': shell_ui['engine'], 'size': [width, height]}
    try:
        page.route('**/api/state', lambda route: route.fulfill(json=live_state(live)))
        boot(page, shell_ui)
        page.wait_for_function("() => document.querySelectorAll('#nav-projects-list > .nav-project-item[data-live]').length === 2",
                               timeout=20_000)
        if width < 720:
            open_drawer(page)
        facts['open'] = page.evaluate(SIDEBAR_FACTS)
        page.locator('#nav-projects-toggle').click()
        settle(page)
        facts['collapsed'] = page.evaluate(SIDEBAR_FACTS)
        page.locator('#nav-projects-show-all').click()
        settle(page)
        facts['show_all'] = page.evaluate(SIDEBAR_FACTS)
    finally:
        evidence(tmp_path, f'shell-sidebar-collapse-{width}-{shell_ui["engine"]}', facts)
        page.close()
    collapsed = facts['collapsed']
    assert collapsed['expanded'] == 'false' and sorted(collapsed['rows']) == sorted(live), facts
    assert collapsed['showAll'] == f'Show all {len(PROJECTS)}', facts
    # The tools follow the Projects zone: no empty room between them (the free room is under the last row).
    assert collapsed['gapToTools'] <= 24 and collapsed['filesTop'] < collapsed['sidebarBottom'] - 200, facts
    assert facts['open']['expanded'] == 'true' and len(facts['open']['rows']) == len(PROJECTS), facts
    assert facts['show_all']['expanded'] == 'true' and len(facts['show_all']['rows']) == len(PROJECTS), facts
    assert facts['show_all']['showAll'] is None, facts


DOT_FACTS = """() => {
    const dot = document.querySelector('#nav-projects-list .nav-unread-dot');
    const marker = document.querySelector('#nav-projects-list .nav-activity-marker[data-state="working"]');
    const row = marker?.closest('.nav-project-row');
    const ds = dot ? getComputedStyle(dot) : null;
    const probe = document.createElement('span');
    probe.style.color = 'var(--red)';
    document.body.append(probe);
    const red = getComputedStyle(probe).color;
    probe.remove();
    return {dot: ds && {background: ds.backgroundColor, ring: ds.boxShadow}, red,
            working: marker ? getComputedStyle(marker.querySelector('span')).backgroundColor : null,
            rowInk: row ? getComputedStyle(row).color : null};
}"""


def test_sidebar_dots_are_a_red_unread_ring_and_working_dots_in_the_row_ink(shell_ui, tmp_path):
    page = seed_page(shell_ui, 1108, 680)
    unread = [dict(p, visible_revision=3) if i == 2 else p for i, p in enumerate(PROJECTS)]
    state = dict(live_state([PROJECTS[5]['id']]), projects=unread)
    facts = {'engine': shell_ui['engine']}
    try:
        page.route('**/api/state', lambda route: route.fulfill(json=state))
        page.route('**/api/projects', lambda route: route.fulfill(json={'projects': unread}))
        boot(page, shell_ui)
        page.wait_for_function("() => document.querySelector('#nav-projects-list .nav-unread-dot') && "
                               "document.querySelector('#nav-projects-list .nav-activity-marker[data-state=\"working\"]')",
                               timeout=20_000)
        facts.update(page.evaluate(DOT_FACTS))
    finally:
        evidence(tmp_path, f'shell-sidebar-dots-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['dot']['background'] == facts['red'], facts
    assert facts['dot']['ring'] not in ('', 'none'), facts
    assert facts['working'] == facts['rowInk'], facts


PANEL_FACTS = """() => {
    const panel = document.getElementById('project-panel').getBoundingClientRect();
    const side = document.getElementById('primary-sidebar').getBoundingClientRect();
    const handle = document.querySelector('#project-panel > .resize-handle');
    return {panel: Math.round(panel.width), left: Math.round(panel.left), sidebarRight: Math.round(side.right),
            handle: Boolean(handle && handle.getClientRects().length),
            fill: document.documentElement.hasAttribute('data-project-fill')};
}"""


@pytest.mark.parametrize('width', [1108, 800])
def test_the_project_panel_keeps_its_own_width_beside_main(shell_ui, tmp_path, width):
    """The owner's widths (sidebar 282, panel 581): beside Main at its own width with its handle; a window too
    narrow for it gets a panel as wide as the content area, never over the sidebar."""
    page = seed_page(shell_ui, width, 680)
    facts = {'engine': shell_ui['engine'], 'width': width}
    try:
        page.route('**/api/ui/preferences', lambda route: route.fulfill(
            json={'sidebar_width': 282, 'project_panel_width': 581, 'project_seen_revision': {}}))
        boot(page, shell_ui)
        page.wait_for_function("() => getComputedStyle(document.documentElement).getPropertyValue('--project-panel-width').trim() === '581px'",
                               timeout=20_000)
        open_project(page)
        settle(page)
        facts.update(page.evaluate(PANEL_FACTS))
    finally:
        evidence(tmp_path, f'shell-project-panel-{width}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['fill'] is False and facts['handle'] is True, facts
    content = width - facts['sidebarRight']
    assert facts['panel'] == min(581, content), facts
    assert facts['left'] >= facts['sidebarRight'], facts


FOCUS = """() => { const a = document.activeElement;
    return a === document.body ? 'body' : a.id || (a.dataset.projectId ? 'project:' + a.dataset.projectId
        : a.dataset.navPage ? 'page:' + a.dataset.navPage : a.className); }"""


def test_the_keyboard_stays_in_the_projects_when_a_collapse_hides_its_control(shell_ui, tmp_path):
    page = seed_page(shell_ui, 1108, 680)
    live = [PROJECTS[3]['id'], PROJECTS[7]['id']]
    current = {'state': live_state(live)}
    facts = {'engine': shell_ui['engine']}
    hidden = "id => document.querySelector(`[data-project-id=\"${id}\"]`).closest('[hidden]') !== null"
    try:
        page.route('**/api/state', lambda route: route.fulfill(json=current['state']))
        boot(page, shell_ui)
        page.wait_for_function("() => document.querySelectorAll('#nav-projects-list > .nav-project-item[data-live]').length === 2",
                               timeout=20_000)
        page.locator('#nav-projects-toggle').click()
        facts['show_all_name'] = page.locator('#nav-projects-show-all').get_attribute('aria-label')
        page.locator('#nav-projects-show-all').focus()
        page.keyboard.press('Enter')
        settle(page)
        facts['after_show_all'] = page.evaluate(FOCUS)
        page.keyboard.press('Tab')
        facts['next_tab'] = page.evaluate(FOCUS)
        # Collapse again, put the keyboard on a live row, then its work ends in a complete census: the row stays
        # under the keyboard and goes behind "Show all" once focus leaves the Projects.
        page.locator('#nav-projects-toggle').click()
        page.locator(f'[data-project-id="{live[0]}"]').focus()
        current['state'] = live_state(live[1:])
        page.evaluate("() => window.__ouroWs.emit('projects_changed', {})")
        page.wait_for_function(f"() => !document.querySelector('[data-project-id=\"{live[0]}\"]').closest('.nav-project-item').hasAttribute('data-live')",
                               timeout=20_000)
        settle(page)
        facts['focused_row_hidden'] = page.evaluate(hidden, live[0])
        facts['focus_while_quiet'] = page.evaluate(FOCUS)
        page.locator('[data-nav-page="chat"]').focus()
        settle(page)
        facts['row_hidden_after_leave'] = page.evaluate(hidden, live[0])
    finally:
        evidence(tmp_path, f'shell-sidebar-collapse-focus-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['show_all_name'] == f'Show all {len(PROJECTS)} Projects', facts
    assert facts['after_show_all'].startswith('project:'), facts
    assert facts['next_tab'] not in ('page:files', 'body'), facts
    assert facts['focused_row_hidden'] is False and facts['focus_while_quiet'] == f'project:{live[0]}', facts
    assert facts['row_hidden_after_leave'] is True, facts


def test_collapsed_projects_follow_the_census_and_keep_the_open_one(shell_ui, tmp_path):
    page = seed_page(shell_ui, 1108, 680)
    current = {'state': live_state([PROJECTS[3]['id']])}
    facts = {'engine': shell_ui['engine']}
    rows = "() => [...document.querySelectorAll('#nav-projects-list > .nav-project-item')].filter((e) => !e.hidden).map((e) => e.querySelector('[data-project-id]').dataset.projectId)"
    try:
        page.route('**/api/state', lambda route: route.fulfill(json=current['state']))
        boot(page, shell_ui)
        page.wait_for_function("() => document.querySelectorAll('#nav-projects-list > .nav-project-item[data-live]').length === 1",
                               timeout=20_000)
        page.locator('#nav-projects-toggle').click()
        facts['one'] = page.evaluate(rows)
        for name, ids_now in (('three', [PROJECTS[3]['id'], PROJECTS[5]['id'], PROJECTS[9]['id']]), ('none', [])):
            current['state'] = live_state(ids_now)
            page.evaluate("() => window.__ouroWs.emit('projects_changed', {})")
            page.wait_for_function(f"() => document.querySelectorAll('#nav-projects-list > .nav-project-item[data-live]').length === {len(ids_now)}",
                                   timeout=20_000)
            settle(page)
            facts[name] = page.evaluate(rows)
        # The open Project stays in a collapsed list though nothing runs there; once it closes it goes.
        open_project(page, 12)
        settle(page)
        facts['open'] = page.evaluate(rows)
        page.keyboard.press('Escape')
        page.wait_for_function("() => window.__ouroLayers.ids().length === 0")
        page.locator('[data-nav-page="chat"]').focus()
        settle(page)
        facts['closed'] = page.evaluate(rows)
    finally:
        evidence(tmp_path, f'shell-sidebar-collapse-census-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['one'] == [PROJECTS[3]['id']], facts
    assert sorted(facts['three']) == sorted([PROJECTS[3]['id'], PROJECTS[5]['id'], PROJECTS[9]['id']]), facts
    assert facts['none'] == [], facts
    assert facts['open'] == [PROJECTS[12]['id']], facts
    assert facts['closed'] == [], facts
