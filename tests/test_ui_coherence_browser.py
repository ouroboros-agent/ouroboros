"""Focused shell integration against real documents/modules and synthetic API data.

These tests cover geometry and interactions, not native host bridges or backend work.
The existing subscription fixture owns the static server, browser and cleanup.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from tests import test_subscription_setup_browser as setup_browser

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def open_app(ui, path="/"):
    page = ui["page"]
    origin = urlparse(ui["url"]).netloc
    page.route("**/*", lambda route: route.fallback()
               if urlparse(route.request.url).netloc == origin else route.abort())
    page.goto(ui["url"] + path)
    page.wait_for_selector("#chat-input", state="attached")
    return page


def hit_box(locator):
    return locator.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,
            reachable: !!hit && (hit === el || el.contains(hit)), hit:hit?.id || hit?.className};
    }""")


@pytest.mark.parametrize("width", [390, 1440])
def test_chat_header_decoration_does_not_clip_menu_and_system_actions_keep_gap(subscription_ui, width):
    ui = subscription_ui
    page = ui["page"]
    page.set_viewport_size({"width": width, "height": 600})
    rows = [{"role": "system", "text": text, "markdown": markdown,
             "system_type": "project_completion_summary", "project_id": "fixture-project",
             "project_name": "Fixture project", "ts": f"2026-09-09T00:00:0{i}Z"}
            for i, (text, markdown) in enumerate([
                ("Plain completion text\nSecond line.", False),
                ("**Markdown completion**\n\nA second paragraph.", True)])]
    rows.extend({"role": "assistant", "text": f"Reading line {i}. " + "Useful content continues below the header. " * 6,
                 "ts": f"2026-09-09T00:01:{i:02}Z"} for i in range(12))
    page.route("**/api/chat/history*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"messages": rows, "progress": []})))
    open_app(ui)
    page.wait_for_selector(".system-message-actions")
    actions = page.locator("#chat-messages .system-message-actions")
    assert actions.count() == 2
    for action in actions.all():
        metrics = action.evaluate("""el => {
            const prose = el.previousElementSibling;
            return {previous:prose.className, nested:!!el.closest('.message'),
                gap:el.getBoundingClientRect().top - prose.getBoundingClientRect().bottom};
        }""")
        assert metrics["previous"] == "message" and not metrics["nested"]
        assert metrics["gap"] == pytest.approx(12, abs=0.5), metrics
    header = page.locator(".chat-page-header")
    paint = header.evaluate("""el => ({mask:getComputedStyle(el).maskImage,
        overflow:getComputedStyle(el).overflow, fade:getComputedStyle(el,'::before').maskImage,
        blur:getComputedStyle(el,'::before').backdropFilter,
        pointer:getComputedStyle(el,'::before').pointerEvents})""")
    assert paint["mask"] == "none" and paint["overflow"] == "visible", paint
    assert "gradient" in paint["fade"] and "blur" in paint["blur"], paint
    assert paint["pointer"] == "none"
    page.locator("#chat-messages").evaluate("el=>el.scrollTop=0")
    setup_browser.capture(page, f"shell-chat-actions-{width}")
    page.locator("#chat-messages").evaluate("el=>el.scrollTop=160")
    page.locator(".chat-header-more > summary").click()
    for item in page.locator(".chat-header-menu-item").all():
        box = hit_box(item)
        assert box["reachable"] and box["bottom"] <= 600 and box["right"] <= width, box
    setup_browser.capture(page, f"shell-chat-menu-{width}")
    page.locator(".chat-header-more > summary").click()


def test_scroll_fade_tracks_edges_late_content_and_stops_after_disposal(subscription_ui):
    page = open_app(subscription_ui)
    # Mount only a bounded scroll region; its observer and mask are the production ones.
    page.evaluate("""async () => {
        const {bindScrollFade} = await import('/static/modules/scroll_fade.js');
        const el = document.createElement('div');
        el.id='shell-fade-probe'; el.className='scroll-fade-y';
        el.style.cssText='position:fixed;z-index:100;left:20px;top:160px;width:300px;height:100px;overflow:auto;background:#17131a';
        const child=document.createElement('div'); child.textContent='First visible line';
        el.append(child); document.body.append(el);
        window.shellFade={el,child,dispose:bindScrollFade(el)};
    }""")
    state = """() => {const e=shellFade.el;return [e.hasAttribute('data-scroll-above'),e.hasAttribute('data-scroll-below')]}"""
    page.wait_for_function("""() => getComputedStyle(shellFade.el).getPropertyValue('--scroll-fade-top').trim()==='0px'""")
    assert page.evaluate(state) == [False, False]
    page.evaluate("shellFade.child.style.height='500px'")
    page.wait_for_function("shellFade.el.hasAttribute('data-scroll-below')")
    assert page.evaluate(state) == [False, True]
    page.evaluate("shellFade.el.scrollTop=200")
    page.wait_for_function("shellFade.el.hasAttribute('data-scroll-above')")
    assert page.evaluate(state) == [True, True]
    setup_browser.capture(page, "shell-scroll-middle")
    page.evaluate("shellFade.el.scrollTop=shellFade.el.scrollHeight")
    page.wait_for_function("!shellFade.el.hasAttribute('data-scroll-below')")
    assert page.evaluate(state) == [True, False]
    page.evaluate("shellFade.el.replaceChildren(document.createTextNode('New short content'))")
    page.wait_for_function("!shellFade.el.hasAttribute('data-scroll-above')")
    assert page.evaluate(state) == [False, False]
    page.evaluate("""() => {shellFade.dispose();shellFade.el.append(shellFade.child);
        shellFade.child.style.height='1000px';shellFade.el.scrollTop=100;
        shellFade.el.dispatchEvent(new Event('scroll'));}""")
    page.wait_for_timeout(100)  # beyond the observer/rAF turns that would repaint without disposal
    assert page.evaluate(state) == [False, False]
    page.evaluate("shellFade.el.remove()")


def test_matrix_respects_reduced_motion_changes_and_page_cleanup(subscription_ui):
    page = subscription_ui["page"]
    page.emulate_media(reduced_motion="reduce")
    open_app(subscription_ui)
    page.wait_for_selector("#matrix-rain", state="attached")
    sample = "document.querySelector('#matrix-rain').toDataURL()"
    first = page.evaluate(sample)
    page.wait_for_timeout(200)
    assert page.evaluate(sample) == first
    setup_browser.capture(page, "shell-rain-reduced")
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_function("before => document.querySelector('#matrix-rain').toDataURL() !== before", arg=first)
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_timeout(100)
    frozen = page.evaluate(sample)
    page.wait_for_timeout(200)
    assert page.evaluate(sample) == frozen
    page.evaluate("""() => {window.shellRain=document.querySelector('#matrix-rain');
        window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false}));}""")
    assert page.locator("#matrix-rain").count() == 0
    before = page.evaluate("[shellRain.width,shellRain.height,shellRain.toDataURL()]")
    page.set_viewport_size({"width": 700, "height": 400})
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_timeout(200)
    assert page.evaluate("[shellRain.width,shellRain.height,shellRain.toDataURL()]") == before


@pytest.mark.parametrize("height", [260, 600])
def test_photo_menu_escapes_gallery_crop_downloads_and_cleans_up(subscription_ui, height):
    page = subscription_ui["page"]
    page.set_viewport_size({"width": 390, "height": height})
    page.route("**/api/chat/history*", lambda route: route.fulfill(content_type="application/json", body=json.dumps({
        "messages": [{"role": "assistant", "text": "Photo fixture is ready", "ts": "2026-09-09T00:00:00Z"}]})))
    open_app(subscription_ui)
    page.get_by_text("Photo fixture is ready", exact=True).wait_for()
    # Actual media renderer, gallery grouping, menu and CSS. Callbacks only bind it
    # to this document's real message region; no copied photo/menu markup.
    page.evaluate("""async () => {
        const {createChatMedia}=await import('/static/modules/chat_media.js');
        const feed=document.querySelector('#chat-messages');
        const canvas=document.createElement('canvas');canvas.width=32;canvas.height=12;
        const c=canvas.getContext('2d');c.fillStyle='#7ba1c4';c.fillRect(0,0,32,12);
        const image=canvas.toDataURL('image/png').split(',')[1];
        const media=createChatMedia({chatSessionId:'shell-proof',durableChatMediaUrl:()=>'',
            formatMsgTime:()=>null,senderLabel:()=> 'Fixture',stampNodeTimestamp:()=>{},
            insertMessageNode:node=>feed.append(node)});
        window.shellPhoto={media,add:()=>{for(let i=0;i<2;i++){
            const msg={type:'photo',task_id:'shell-gallery',role:'assistant',image_base64:image,mime:'image/png'};
            media.buildGallery('photos',msg,media.buildMediaBubble(msg));
        }}};
        shellPhoto.add();
    }""")
    trigger = page.locator(".chat-photo-actions summary").first
    trigger.scroll_into_view_if_needed()
    page.evaluate("() => new Promise(r => requestAnimationFrame(()=>requestAnimationFrame(r)))")
    page.evaluate("""() => {
        window.shellPhotoEvents=[];
        for (const type of ['scroll','focusin','click']) window.addEventListener(type,e=>
            shellPhotoEvents.push({type,target:e.target?.id||e.target?.className||e.target?.tagName,
                scroll:document.querySelector('#chat-messages').scrollTop,
                active:document.activeElement?.outerHTML.slice(0,100),time:performance.now()}),true);
    }""")
    trigger.click()
    menu = page.locator('body > .chat-photo-menu[role="menu"]')
    setup_browser.capture(page, f"shell-photo-open-{height}")
    assert menu.is_visible(), json.dumps(page.evaluate("shellPhotoEvents"))
    assert menu.evaluate("el=>el.closest('.chat-gallery-item')===null")
    assert page.locator(".chat-gallery-item").first.evaluate("el=>getComputedStyle(el).overflow") == "hidden"
    for item in menu.locator('[role="menuitem"]').all():
        item.scroll_into_view_if_needed()
        box = hit_box(item)
        assert box["reachable"] and box["x"] >= 0 and box["right"] <= 390 and box["bottom"] <= height, box
    page.keyboard.press("End")
    assert menu.locator('[data-photo-action="copy"]').evaluate("el=>document.activeElement===el")
    setup_browser.capture(page, f"shell-photo-menu-{height}")
    page.keyboard.press("Escape")
    assert not menu.count()
    assert trigger.evaluate("el=>document.activeElement===el")
    trigger.click()
    with page.expect_download() as download:
        page.locator('body > .chat-photo-menu [data-photo-action="download"]').click()
    assert download.value.suggested_filename == "image.png"
    assert download.value.failure() is None
    assert not menu.count()
    trigger.click()
    assert menu.is_visible()
    page.evaluate("shellPhoto.media.reset()")
    assert not menu.count() and page.locator(".chat-gallery-item").count() == 0
    page.evaluate("shellPhoto.add()")
    page.locator(".chat-photo-actions summary").first.click()
    assert menu.is_visible()
    page.evaluate("shellPhoto.media.destroy()")
    assert not menu.count() and page.locator(".chat-gallery-item").count() == 0


def test_settings_footer_controls_are_reachable_at_content_breakpoints(subscription_ui):
    page = open_app(subscription_ui, "/#settings")
    page.wait_for_selector("#btn-save-settings")
    # Exercise the existing footer's geometry in each meaningful controller state.
    # Domain save/validation transitions belong to the forms track's separate flows.
    states = [
        ("clean", "", False, False), ("dirty", "Unsaved changes", False, False),
        ("saving", "Saving…", True, False),
        ("error", "Error: Could not save your settings. Your edits are still available.", False, False),
        ("restart", "Saved. Restart required before the new runtime settings take effect.", False, True),
    ]
    for width in [640, 641, 760, 768, 980, 981]:
        page.set_viewport_size({"width": width, "height": 600})
        if width <= 640:
            page.wait_for_function("document.querySelector('#primary-sidebar').getBoundingClientRect().right<=1")
        for sidebar in [220, 280]:
            page.evaluate("value => document.documentElement.style.setProperty('--sidebar-width',value+'px')", sidebar)
            for name, message, busy, restart in states:
                page.evaluate("""s => {
                    document.querySelector('#settings-status').textContent=s.message;
                    document.querySelector('#settings-unsaved-indicator').classList.toggle('is-visible',s.name==='dirty');
                    document.querySelector('#btn-save-settings').disabled=s.busy;
                    document.querySelector('#btn-restart-now').hidden=!s.restart;
                }""", {"name": name, "message": message, "busy": busy, "restart": restart})
                for selector in ["#btn-reload-settings", "#btn-save-settings"] + (["#btn-restart-now"] if restart else []):
                    box = hit_box(page.locator(selector))
                    valid = box["reachable"] and box["x"] >= 0 and box["right"] <= width + 1 and box["bottom"] <= 600
                    if not valid:
                        setup_browser.capture(page, f"shell-footer-failure-{width}-{sidebar}-{name}")
                    assert valid, json.dumps([width, sidebar, name, selector, box])
                if sidebar == 280 and name in {"dirty", "restart"}:
                    setup_browser.capture(page, f"shell-footer-{width}-{name}")


def test_short_sidebar_has_one_scroll_and_cost_cards_keep_local_table_overflow(subscription_ui):
    ui = subscription_ui
    page = ui["page"]
    projects = [{"id": f"project-{i}", "name": f"Project {i} with a descriptive title", "chat_id": 100+i,
                 "lifecycle": "active", "visible_revision": 0} for i in range(12)]
    page.route("**/api/projects", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"projects": projects})))
    page.route("**/api/state", lambda route: route.fulfill(content_type="application/json", body=json.dumps({
        "supervisor_ready": True, "active_chat_activities": [], "projects": projects,
        "project_chat_ids": [p["chat_id"] for p in projects]})))
    data = {"total_cost": 27.4, "total_calls": 42, "accounting": {"available": True,
            "accounted_usd": 27.4, "confirmed_usd": 25, "reserved_usd": 2,
            "unresolved_upper_bound_usd": 0.4, "unknown_unmetered": 1,
            "limit_usd": 200, "cost_final": False},
            "by_model": {"provider/a-long-model-name-for-real-table-overflow": {"calls": 42, "cost": 27.4}}}
    page.route("**/api/cost-breakdown", lambda route: route.fulfill(content_type="application/json", body=json.dumps(data)))
    page.set_viewport_size({"width": 844, "height": 320})
    open_app(ui)
    page.wait_for_selector(".nav-project-row")
    sidebar = page.locator("#primary-sidebar")
    scrolling = sidebar.evaluate("""el => [...el.querySelectorAll('*')].filter(e =>
        ['auto','scroll'].includes(getComputedStyle(e).overflowY) && e.scrollHeight>e.clientHeight+1).map(e=>e.className)""")
    assert scrolling == ["sidebar-scroll"], scrolling
    nav = page.locator('[data-nav-page="settings"]')
    nav.scroll_into_view_if_needed()
    assert hit_box(nav)["reachable"]
    setup_browser.capture(page, "shell-sidebar-short-bottom")
    page.locator('[data-nav-page="dashboard"]').click()
    page.locator('[data-dashboard-tab="costs"]').click()
    page.wait_for_function("document.querySelector('#cost-confirmed').textContent==='$25.00'")
    for width in [390, 641, 768, 981]:
        page.set_viewport_size({"width": width, "height": 600})
        if width <= 640:
            page.wait_for_function("document.querySelector('#primary-sidebar').getBoundingClientRect().right<=1")
        for card in page.locator(".costs-stats-grid > .stat-card").all():
            card.scroll_into_view_if_needed()
            box = hit_box(card)
            valid = box["reachable"] and box["x"] >= 0 and box["right"] <= width + 1
            if not valid:
                setup_browser.capture(page, f"shell-cost-failure-{width}")
            assert valid, json.dumps([width, box])
        for table in page.locator(".costs-tables-grid > div").all():
            assert table.evaluate("el=>getComputedStyle(el).overflowX") == "auto"
            assert table.evaluate("el=>el.getBoundingClientRect().right<=innerWidth+1")
        if width == 390:
            first = page.locator("#cost-by-model").locator("..")
            assert first.evaluate("el => el.scrollWidth > el.clientWidth"), "long table should scroll locally"
            first.evaluate("el=>el.scrollLeft=el.scrollWidth")
            assert first.evaluate("el=>el.scrollLeft>0")
        page.locator(".costs-stats-grid").scroll_into_view_if_needed()
        setup_browser.capture(page, f"shell-costs-{width}")


def test_mcp_transport_fields_use_named_shared_controls_without_changing_drafts(subscription_ui):
    page = open_app(subscription_ui, '/#settings')
    page.locator('[data-settings-tab="advanced"]').click()
    servers = [dict(id='web', name='HTTP server', transport='streamable_http', url='https://example.test/mcp',
                    auth_token='***', auth_header='Authorization', custom_option='retained'),
               dict(id='local', name='Local server', transport='stdio', command='npx', args=['-y','server'],
                    env={'PORT':'8080'}, env_from_settings={'TOKEN':'TOKEN_KEY'})]
    page.evaluate('''async servers=>{
        const mcp=await import('/static/modules/mcp_settings.js');
        mcp.applyMcpSettings({MCP_ENABLED:true,MCP_SERVERS:servers,MCP_TOOL_TIMEOUT_SEC:60});
        window.mcpProof=mcp;
    }''', servers)
    cards=page.locator('[data-mcp-card]')
    assert cards.count()==2
    for control in cards.locator('[data-mcp-field]:not([type="checkbox"])').all():
        assert 'ui-control' in control.get_attribute('class')
        assert control.get_attribute('aria-label').startswith('MCP server ')
        assert control.evaluate('el=>Array.from(el.labels||[]).length') == 1
    page.get_by_label('MCP server 1: Server URL', exact=True).fill('https://edited.test/mcp')
    draft=page.evaluate('mcpProof.collectMcpSettings()')
    assert draft['MCP_SERVERS'][0]['url']=='https://edited.test/mcp'
    assert draft['MCP_SERVERS'][0]['auth_token']=='***'
    assert draft['MCP_SERVERS'][0]['custom_option']=='retained'
    assert draft['MCP_SERVERS'][1]['args']==['-y','server']
    assert draft['MCP_SERVERS'][1]['env_from_settings']=={'TOKEN':'TOKEN_KEY'}
    assert page.evaluate('mcpProof.validateMcpSettings().length') == 0
    field = page.get_by_label('MCP server 2: Environment (JSON, optional)', exact=True)
    for invalid in ['{unfinished', '[]', '5', '{"PORT":8080}', '{"":"value"}']:
        field.fill(invalid)
        assert page.evaluate('mcpProof.validateMcpSettings().map(e=>e.input.dataset.mcpField)') == ['env']
        assert field.input_value() == invalid
    field.fill('null')
    assert page.evaluate('mcpProof.validateMcpSettings().length') == 0  # backend accepts omitted maps
    field.fill('{"PORT":"8080"}')
    assert page.evaluate('mcpProof.validateMcpSettings().length') == 0
    capture = setup_browser.capture
    cards.first.scroll_into_view_if_needed()
    capture(page,'shell-mcp-shared-fields')
