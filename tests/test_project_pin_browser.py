"""Actual sidebar + Project gateway/storage; controlled non-project census, no owner state.

The existing shell server supplies assets and non-project APIs. Project list/update
requests exercise the real gateway and registry in a private TestClient drive.
Pin and Unpin are the first actions of a row's ⋯; pinned rows lead the one keyed list
and one step of air parts them from the rest, with no caption, glyph or column.
"""
import json
from urllib.parse import urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros.gateway.router import collect_routes
from ouroboros import projects_registry as registry
from tests.test_subscription_setup_browser import subscription_ui  # noqa: F401
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401
from tests._shared import producer_evidence_dir
from tests._work_projection_shared import with_work_fields

pytestmark = pytest.mark.ui_browser

LONG = "Old project with a long descriptive title"
# Visible rows in order: their gap to the row above, whether they lead or follow the pins, their full names.
GROUPS = """() => {
    const items = [...document.querySelectorAll('#nav-projects-list > .nav-project-item:not([hidden])')];
    return items.map((item, i) => {
        const row = item.querySelector('.nav-project-row'), label = row.querySelector('.nav-row-label');
        return {id: item.dataset.navProjectId, pinned: item.hasAttribute('data-pinned'),
                after: item.hasAttribute('data-after-pins'), height: row.getBoundingClientRect().height,
                gap: i ? item.getBoundingClientRect().top - items[i - 1].getBoundingClientRect().bottom : null,
                title: row.title, name: row.getAttribute('aria-label'),
                ellipsized: label.scrollWidth > label.clientWidth};
    });
}"""


@pytest.fixture(params=[("chromium", 1280, "dark"), ("webkit", 390, "light")],
                ids=["chromium-1280-dark", "webkit-390-light-touch"])
def pins_ui(request, monkeypatch, tmp_path):
    engine, width, theme = request.param
    monkeypatch.setenv("OUROBOROS_UI_BROWSER_ENGINE", engine)
    ui = request.getfixturevalue("subscription_ui")
    root = tmp_path / "pin-data"
    for pid, name in (("older", LONG), ("newer", "Recent project")):
        registry.create_project(root, pid, name=name, origin="owner_ui")
    registry.update_project(root, "older", last_active_at="2026-01-01T00:00:00Z")
    app = Starlette(routes=collect_routes(data_dir=root))
    app.state.drive_root = root
    control = {"fail": False, "hold": False, "held": [], "hold_writes": False, "held_writes": [],
               "writes": [], "unread": set(), "activities": []}
    pages = []
    with TestClient(app) as client:
        def forward(route, path, body):
            response = client.post(path, json=body)
            route.fulfill(status=response.status_code, content_type="application/json", body=response.text)

        def respond(route):
            path = urlparse(route.request.url).path
            if path == "/api/state":
                rows = registry.projects_summary(root, include_pinned=True)
                for project in rows:
                    if project['id'] in control['unread']:
                        project['visible_revision'] = 2
                # The sidebar's live marks read the server's projection of the census rows (its rooms).
                state = with_work_fields({"sha": "pin-fixture", "supervisor_ready": True,
                                          "active_chat_activities_complete": True,
                                          "active_chat_activities": control['activities'], "projects": rows,
                                          "project_chat_ids": [p["chat_id"] for p in rows]})
                if control["hold"]:
                    control["held"].append((route, json.dumps(state)))
                    return
                route.fulfill(content_type="application/json", body=json.dumps(state))
            elif path.startswith("/api/projects/") and route.request.method == "POST":
                body = route.request.post_data_json
                control["writes"].append(body)
                if control["fail"]:
                    route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic write outage"}')
                elif control["hold_writes"]:
                    control["held_writes"].append((route, path, body))
                else:
                    forward(route, path, body)
            elif path == "/api/projects":
                response = client.get(path)
                route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
            else:
                route.fallback()

        def new_page(height=900):
            page = ui["new_page"](viewport={"width": width, "height": height}, has_touch=width < 720)
            pages.append(page)
            page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
            page.route("**/api/**", respond)
            page.clock.install()
            page.goto(ui["url"])
            page.locator('.nav-project-item').first.wait_for()
            open_drawer(page, width)
            return page

        page = new_page()
        try:
            yield {**ui, "page": page, "make_page": new_page, "root": root, "control": control, "forward": forward,
                   "width": width, "engine": engine, "theme": theme, "client": client,
                   "tag": f"{engine}-{width}-{theme}"}
        finally:
            # Stop route callbacks and polling before their TestClient drive closes.
            for page in pages:
                page.close()


def open_drawer(page, width):
    if width < 720:
        page.locator('#page-chat [data-mobile-nav-toggle]').click()
        page.locator('#primary-sidebar.open').wait_for()


def expect():
    return pytest.importorskip("playwright.sync_api").expect


def row(page, pid):
    return page.locator(f'.nav-project-item[data-nav-project-id="{pid}"]')


def menu(page):
    return page.locator('body > .project-row-menu')


def order(page):
    return page.locator('#nav-projects-list > *').evaluate_all('els => els.map(el => el.dataset.navProjectId)')


def refresh(page):
    page.clock.fast_forward(21_000)


def until(page, predicate, rounds=200):
    """Route callbacks run while Playwright is called; round-trip until one has recorded what the test needs."""
    for _ in range(rounds):
        if predicate():
            return
        page.evaluate("0")
    raise AssertionError("the route callback never recorded the request")


def choose(page, pid, label):
    """Press ⋯ on a row and its first action, which must read ``label``."""
    row(page, pid).locator('.nav-project-kebab').click()
    item = menu(page).locator('[data-prm="pin"]')
    expect()(item).to_have_text(label)
    item.click()
    expect()(menu(page)).to_have_count(0)


def assert_one_boundary(groups):
    """Pins lead; the first row shown after them keeps one step of air (8px + the list's 1px gap), nothing else."""
    pinned = [g["pinned"] for g in groups]
    assert pinned == sorted(pinned, reverse=True), groups
    for index, group in enumerate(groups):
        boundary = index > 0 and groups[index - 1]["pinned"] and not group["pinned"]
        assert group["after"] is boundary, (group, groups)
        if index:
            assert group["gap"] == pytest.approx(9 if boundary else 1, abs=0.5), (group, groups)


def assert_flat_and_unlabelled(page):
    """One flat keyed list: every child a Project row, and the word Pinned only in names, never drawn."""
    children = page.locator('#nav-projects-list > *').evaluate_all(
        'els => els.map(el => [el.className, el.dataset.navProjectId || ""])')
    assert children and all(cls.startswith('nav-project-item') and pid for cls, pid in children), children
    assert len({pid for _, pid in children}) == len(children)
    assert "Pinned" not in page.locator('#primary-sidebar').inner_text()
    assert page.locator('#primary-sidebar svg').evaluate_all('els => els.filter(el => el.closest(".nav-project-item")'
                                                            ' && !el.closest(".nav-project-kebab")).length') == 0


def test_menu_pin_reorders_keyed_rows_without_opening_or_reading_across_reload_and_windows(pins_ui, tmp_path):
    ui, page = pins_ui, pins_ui["page"]
    evidence = producer_evidence_dir(tmp_path, "project-pin", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    # No pins: the ordinary list, no boundary; ⋯ offers Pin before Rename and Delete.
    assert order(page) == ["newer", "older"]
    assert page.locator('#nav-projects-list [data-pinned], #nav-projects-list [data-after-pins]').count() == 0
    page.evaluate("window.savedKebab = document.querySelector('[data-nav-project-id=older] .nav-project-kebab')")
    row(page, "older").locator('.nav-project-kebab').focus()
    page.keyboard.press("Enter")
    expect()(menu(page)).to_be_visible()
    assert menu(page).locator('[role=menuitem]').all_inner_texts() == ["Pin", "Rename…", "Delete project…"]
    assert page.evaluate("document.activeElement.dataset.prm") == "pin"
    page.screenshot(path=str(evidence / f"menu-pin-{ui['tag']}.png"), animations="disabled")
    page.keyboard.press("Enter")
    expect()(menu(page)).to_have_count(0)
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    assert order(page) == ["older", "newer"]
    # The same keyed node moved and kept the keyboard.
    assert page.evaluate("savedKebab === document.activeElement && savedKebab.isConnected")
    assert registry.get_project(ui["root"], "older")["pinned"] is True
    assert ui["control"]["writes"] == [{"pinned": True}]
    # Pinning neither opens nor reads the Project.
    assert page.locator('.nav-project-row.active').count() == 0
    assert page.locator('#project-panel').is_hidden()
    assert not [body for path, body in ui["posts"]
                if path == "/api/ui/preferences" and "project_seen_revision" in (body or {})]
    groups = page.evaluate(GROUPS)
    assert_one_boundary(groups)
    assert [g["id"] for g in groups] == ["older", "newer"]
    assert groups[0]["title"] == groups[0]["name"] == f"{LONG} · Pinned"
    assert groups[1]["title"] == "Recent project"
    assert_flat_and_unlabelled(page)
    page.locator('#primary-sidebar').screenshot(path=str(evidence / f"expanded-{ui['tag']}.png"), animations="disabled")
    # Reload reads the pin from the registry.
    page.reload()
    page.locator('.nav-project-item').first.wait_for()
    open_drawer(page, ui["width"])
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    assert order(page) == ["older", "newer"]
    # Another window unpins; this one follows at its next state read, and the last pin takes its air with it.
    second = ui["make_page"]()
    expect()(row(second, "older")).to_have_attribute("data-pinned", "")
    choose(second, "older", "Unpin")
    expect()(row(second, "older")).not_to_have_attribute("data-pinned", "")
    refresh(page)
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")
    assert order(page) == ["newer", "older"]
    assert page.locator('#nav-projects-list [data-after-pins]').count() == 0
    assert row(page, "older").locator('.nav-project-row').get_attribute("title") == LONG
    # Rename remains a partial update and does not pin the room again.
    response = ui["client"].post('/api/projects/older/update', json={"name": "Renamed"})
    assert response.status_code == 200 and response.json()["project"]["pinned"] is False
    refresh(page)
    expect()(row(page, "older").locator('.nav-row-label')).to_have_text("Renamed")


def test_collapsed_list_keeps_pins_and_unpins_in_place(pins_ui, tmp_path):
    ui, page = pins_ui, pins_ui["page"]
    evidence = producer_evidence_dir(tmp_path, "project-pin", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    choose(page, "older", "Pin")
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    page.locator('#nav-projects-toggle').click()
    expect()(row(page, "older")).to_be_visible()
    expect()(row(page, "newer")).to_be_hidden()
    show_all = page.locator('#nav-projects-show-all')
    expect()(show_all).to_have_text("Show all 2")
    # Nothing shown follows the pins, so no air waits before "Show all".
    assert page.locator('#nav-projects-list [data-after-pins]').count() == 0
    page.screenshot(path=str(evidence / f"collapsed-{ui['tag']}.png"), animations="disabled")
    kebab = row(page, "older").locator('.nav-project-kebab')
    if ui["width"] < 720:  # coarse pointer: the one visible control keeps its own touch target
        box = kebab.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
    assert page.locator('#primary-sidebar').evaluate("e => e.scrollWidth <= e.clientWidth")
    # Unpin from the collapsed list by keyboard: the row stays while it holds focus, then waits behind "Show all".
    kebab.focus()
    page.keyboard.press("Enter")
    expect()(menu(page).locator('[data-prm="pin"]')).to_have_text("Unpin")
    page.keyboard.press("Enter")
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")
    assert registry.get_project(ui["root"], "older")["pinned"] is False
    expect()(row(page, "older")).to_be_visible()
    assert kebab.evaluate("el => el === document.activeElement")
    page.locator('[data-nav-page="chat"]').focus()
    expect()(row(page, "older")).to_be_hidden()
    expect()(show_all).to_be_visible()
    expect()(show_all).to_have_text("Show all 2")
    show_all.click()
    expect()(row(page, "older")).to_be_visible()
    assert page.locator('#nav-projects-list [data-pinned], #nav-projects-list [data-after-pins]').count() == 0


def test_failed_and_pending_saves_are_honest_and_late_reads_cannot_undo_them(pins_ui):
    ui, page, control = pins_ui, pins_ui["page"], pins_ui["control"]
    control["fail"] = True
    choose(page, "older", "Pin")
    expect()(page.locator('#toast-stack')).to_contain_text("Could not save project pin: Synthetic write outage")
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")
    assert registry.get_project(ui["root"], "older")["pinned"] is False
    assert len(control["writes"]) == 1
    control["fail"] = False
    # A save in flight reads as such in ⋯ and cannot be pressed twice; Rename and Delete stay available.
    control["hold_writes"] = True
    choose(page, "older", "Pin")
    until(page, lambda: len(control["held_writes"]) == 1)
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")  # never shown before it is saved
    row(page, "older").locator('.nav-project-kebab').click()
    pending = menu(page).locator('[data-prm="pin"]')
    expect()(pending).to_have_text("Pinning…")
    expect()(pending).to_have_attribute("aria-disabled", "true")
    assert page.evaluate("document.activeElement.dataset.prm") == "rename"
    pending.dispatch_event("click")
    expect()(menu(page)).to_be_visible()
    assert len(control["writes"]) == 2
    assert menu(page).locator('[aria-disabled="true"]').count() == 1
    page.keyboard.press("Escape")
    expect()(menu(page)).to_have_count(0)
    # A state read requested before the confirmation cannot undo it.
    control["hold"] = True
    with page.expect_request(lambda request: urlparse(request.url).path == '/api/state'):
        refresh(page)
    until(page, lambda: bool(control["held"]))
    for route, path, body in control["held_writes"]:
        ui["forward"](route, path, body)
    control["held_writes"].clear()
    control["hold_writes"] = False
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    control["hold"] = False
    for route, body in control["held"]:
        route.fulfill(content_type="application/json", body=body)
    control["held"].clear()
    with page.expect_response(lambda response: urlparse(response.url).path == '/api/state'):
        refresh(page)
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    assert order(page) == ["older", "newer"]
    assert registry.get_project(ui["root"], "older")["pinned"] is True
    choose(page, "older", "Unpin")
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")
    assert control["writes"][-1] == {"pinned": False}


@pytest.mark.parametrize("succeeded", [True, False], ids=["saved", "refused"])
def test_menu_opened_during_save_retires_pending_state_at_settlement(pins_ui, succeeded):
    ui, page, control = pins_ui, pins_ui["page"], pins_ui["control"]
    control["hold_writes"] = True
    choose(page, "older", "Pin")
    until(page, lambda: len(control["held_writes"]) == 1)
    kebab = row(page, "older").locator('.nav-project-kebab')
    kebab.click()
    expect()(menu(page).locator('[data-prm="pin"]')).to_have_text("Pinning…")
    assert page.evaluate("document.activeElement.dataset.prm") == "rename"
    route, path, body = control["held_writes"].pop()
    control["hold_writes"] = False
    if succeeded:
        ui["forward"](route, path, body)
    else:
        route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic write outage"}')
        expect()(page.locator('#toast-stack')).to_contain_text("Synthetic write outage")
    # Leave the pending menu open through completion: no stale disabled action survives.
    expect()(menu(page)).to_have_count(0)
    assert kebab.evaluate("el => el === document.activeElement")
    kebab.click()
    action = menu(page).locator('[data-prm="pin"]')
    expect()(action).to_have_text("Unpin" if succeeded else "Pin")
    assert action.get_attribute("aria-disabled") is None
    assert len(control["writes"]) == 1
    assert registry.get_project(ui["root"], "older")["pinned"] is succeeded


def test_pins_keep_unread_live_rows_menu_anchor_other_windows_and_deleting(pins_ui):
    ui, page, control = pins_ui, pins_ui["page"], pins_ui["control"]
    control["unread"].add("newer")
    control["activities"] = [{"activity_id": "work", "project_id": "newer",
                              "chat_id": registry.get_project(ui["root"], "newer")["chat_id"],
                              "kind": "managed_task", "phase": "working"}]
    refresh(page)
    expect()(row(page, "newer").locator('.nav-unread-dot')).to_be_visible()
    expect()(row(page, "newer")).to_have_attribute("data-live", "")
    choose(page, "older", "Pin")
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    assert order(page) == ["older", "newer"]
    expect()(row(page, "newer").locator('.nav-unread-dot')).to_be_visible()
    assert "Unread" in row(page, "newer").locator('.nav-project-row').get_attribute("aria-label")
    # Collapsed keeps the pin and the live row, the air between them.
    page.locator('#nav-projects-toggle').click()
    expect()(row(page, "newer")).to_be_visible()
    groups = page.evaluate(GROUPS)
    assert [g["id"] for g in groups] == ["older", "newer"]
    assert_one_boundary(groups)
    # Another window unpins while this one's ⋯ is open on that row: the menu follows its moved anchor, and its
    # label still names what it does, so pressing it never flips the pin back.
    kebab = row(page, "older").locator('.nav-project-kebab')
    kebab.click()
    expect()(menu(page).locator('[data-prm="pin"]')).to_have_text("Unpin")
    page.evaluate("window.savedMenuAnchor = document.querySelector('[data-nav-project-id=older] .nav-project-kebab')")
    original_anchor = kebab.bounding_box()
    ui["client"].post('/api/projects/older/update', json={"name": "Changed elsewhere", "pinned": False})
    refresh(page)
    expect()(row(page, "older").locator('.nav-row-label')).to_have_text("Changed elsewhere")
    assert order(page) == ["newer", "older"]
    assert page.evaluate("savedMenuAnchor.isConnected && savedMenuAnchor"
                         " === document.querySelector('[data-nav-project-id=older] .nav-project-kebab')")
    expect()(menu(page)).to_be_visible()
    anchor, popup = kebab.bounding_box(), menu(page).bounding_box()
    assert anchor['y'] != original_anchor['y']  # a real reorder, not a same-position repaint
    assert abs(popup['y'] - (anchor['y'] + anchor['height'] + 4)) <= 1
    with page.expect_response(lambda response: response.url.endswith('/api/projects/older/update')):
        menu(page).locator('[data-prm="pin"]').click()
    assert control["writes"][-1] == {"pinned": False}
    assert registry.get_project(ui["root"], "older")["pinned"] is False
    expect()(row(page, "older")).not_to_have_attribute("data-pinned", "")
    page.locator('#nav-projects-toggle').click()
    control['unread'].clear()
    refresh(page)
    expect()(row(page, 'newer').locator('.nav-unread-dot')).to_have_count(0)
    # A deleting row keeps its one trailing fact and no ⋯, so nothing can pin it.
    registry.update_project(ui["root"], "older", pinned=True)
    registry.begin_project_deletion(ui["root"], "older")
    refresh(page)
    expect()(row(page, "older").locator('.nav-project-row')).to_be_disabled()
    expect()(row(page, "older").locator('.nav-project-deleting-status')).to_have_text("Deleting…")
    assert row(page, "older").locator('.nav-project-kebab').count() == 0
    title = row(page, "older").locator('.nav-project-row').get_attribute("title")
    assert title.startswith("Changed elsewhere · Pinned — Deleting…"), title


def test_many_pins_order_within_groups_long_names_and_short_windows(pins_ui, tmp_path):
    ui, page, root, client = pins_ui, pins_ui["page"], pins_ui["root"], pins_ui["client"]
    evidence = producer_evidence_dir(tmp_path, "project-pin", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    names = {f"p{i}": f"Fictional research room {i} with a deliberately long descriptive name" for i in range(10)}
    for i, (pid, name) in enumerate(names.items()):
        registry.create_project(root, pid, name=name, origin="owner_ui")
        registry.update_project(root, pid, last_active_at=f"2026-03-{i + 1:02}T00:00:00Z")
    for pid in ("p1", "p4", "p7"):
        assert client.post(f'/api/projects/{pid}/update', json={"pinned": True}).json()["project"]["pinned"] is True
    ui["control"]["unread"].update({"p1", "p8"})
    refresh(page)
    expect()(row(page, "p7")).to_have_attribute("data-pinned", "")
    # Pins lead; unread, then recency orders within each group.
    expected = ["p1", "p7", "p4", "p8", "newer", "p9", "p6", "p5", "p3", "p2", "p0", "older"]
    assert order(page) == expected
    groups = page.evaluate(GROUPS)
    assert [g["id"] for g in groups] == expected
    assert_one_boundary(groups)
    assert_flat_and_unlabelled(page)
    by_id = {g["id"]: g for g in groups}
    assert by_id["p1"]["title"] == f"{names['p1']} · Pinned · Unread" and by_id["p1"]["ellipsized"]
    assert by_id["p8"]["title"] == f"{names['p8']} · Unread"
    expect()(row(page, "p1").locator('.nav-unread-dot')).to_be_visible()
    assert page.locator('#primary-sidebar').evaluate("e => e.scrollWidth <= e.clientWidth")
    page.locator('#primary-sidebar').screenshot(path=str(evidence / f"many-{ui['tag']}.png"), animations="disabled")
    page.locator('#nav-projects-toggle').click()
    expect()(page.locator('#nav-projects-show-all')).to_have_text("Show all 12")
    collapsed = page.evaluate(GROUPS)
    assert [g["id"] for g in collapsed] == ["p1", "p7", "p4"]
    assert_one_boundary(collapsed)
    page.locator('#nav-projects-toggle').click()
    # A short window keeps the boundary in the whole column's scroll, rows compacted only under a fine pointer.
    page.set_viewport_size({"width": ui["width"], "height": 520})
    page.wait_for_function("() => getComputedStyle(document.getElementById('nav-projects-list'))"
                           ".overflowY === 'visible'")
    short = page.evaluate(GROUPS)
    assert_one_boundary(short)
    assert {g["height"] for g in short} == {44 if ui["width"] < 720 else 28}
    assert page.locator('#primary-sidebar').evaluate(
        "e => e.scrollHeight > e.clientHeight && e.scrollWidth <= e.clientWidth")
    page.screenshot(path=str(evidence / f"short-{ui['tag']}.png"), animations="disabled")


def test_menu_repositions_after_a_hidden_pin_is_revealed(pins_ui, tmp_path):
    ui, page, control = pins_ui, pins_ui["page"], pins_ui["control"]
    control["activities"] = [{"activity_id": "work", "project_id": "newer",
                              "chat_id": registry.get_project(ui["root"], "newer")["chat_id"],
                              "kind": "managed_task", "phase": "working"}]
    refresh(page)
    expect()(row(page, "newer")).to_have_attribute("data-live", "")
    page.locator('#nav-projects-toggle').click()
    expect()(row(page, "older")).to_be_hidden()
    kebab = row(page, "newer").locator('.nav-project-kebab')
    kebab.click()
    original_anchor = kebab.bounding_box()
    response = ui["client"].post('/api/projects/older/update', json={"pinned": True})
    assert response.status_code == 200
    refresh(page)
    expect()(row(page, "older")).to_be_visible()
    expect()(menu(page)).to_be_visible()
    anchor, popup = kebab.bounding_box(), menu(page).bounding_box()
    assert anchor['y'] != original_anchor['y']
    assert abs(popup['y'] - (anchor['y'] + anchor['height'] + 4)) <= 1
    assert_one_boundary(page.evaluate(GROUPS))
    evidence = producer_evidence_dir(tmp_path, "pin-menu", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    page.screenshot(path=str(evidence / f"collapsed-menu-{ui['tag']}.png"), animations="disabled")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pin_real_server_updates_other_window_and_survives_reload(direct_server_with_data, engine, tmp_path):  # noqa: F811
    """No API/WS interception: real isolated server, public routes, two independent clients."""
    from playwright.sync_api import sync_playwright
    from tests._dialogue import advance_room_revision

    server = direct_server_with_data
    root, url = server["data_dir"], server["url"]
    registry.create_project(root, "pin-old", name="Archive research project")
    registry.create_project(root, "pin-new", name="Recent unread project")
    registry.update_project(root, "pin-old", last_active_at="2026-01-01T00:00:00Z")
    revision = advance_room_revision(root, project_id="pin-new")["visible_revision"]
    evidence = producer_evidence_dir(tmp_path, "pin-real-server", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            first = browser.new_page(viewport={"width": 1172, "height": 875})
            second = browser.new_page(viewport={"width": 1172, "height": 875})
            for page in (first, second):
                page.goto(url, wait_until="domcontentloaded")
                expect()(row(page, "pin-new").locator('.nav-unread-dot')).to_be_visible(timeout=30000)
            choose(first, "pin-old", "Pin")
            expect()(row(first, "pin-old")).to_have_attribute("data-pinned", "")
            expect()(row(second, "pin-old")).to_have_attribute("data-pinned", "", timeout=30000)
            for page in (first, second):
                assert order(page) == ["pin-old", "pin-new"]
                expect()(row(page, "pin-new").locator('.nav-unread-dot')).to_be_visible()
                assert page.locator('#project-panel').is_hidden()
                assert_flat_and_unlabelled(page)
                assert_one_boundary(page.evaluate(GROUPS))
            first.reload()
            expect()(row(first, "pin-old")).to_have_attribute("data-pinned", "", timeout=30000)
            first.screenshot(path=str(evidence / f"real-server-{engine}.png"))
            choose(second, "pin-old", "Unpin")
            expect()(row(first, "pin-old")).not_to_have_attribute("data-pinned", "", timeout=30000)
            assert order(first) == ["pin-new", "pin-old"]
            assert registry.get_project(root, "pin-old")["pinned"] is False
            assert next(p for p in registry.projects_summary(root) if p["id"] == "pin-new")["visible_revision"] == revision
        finally:
            browser.close()


@pytest.mark.parametrize("height", [900, 500])
@pytest.mark.parametrize("keyboard", [True, False], ids=["keyboard", "pointer"])
def test_local_pin_reveal_respects_input_and_visible_scroll_region(pins_ui, height, keyboard):
    ui, page, root = pins_ui, pins_ui["page"], pins_ui["root"]
    page.set_viewport_size({"width": ui["width"], "height": height})
    for i in range(35):
        registry.create_project(root, f"scroll-{i}", name=f"Research {i}", origin="owner_ui")
    refresh(page)
    expect()(page.locator('.nav-project-item')).to_have_count(37)
    kebab = row(page, "older").locator('.nav-project-kebab')
    kebab.scroll_into_view_if_needed()
    scroll = "() => ['primary-sidebar', 'nav-projects-list'].map(id => document.getElementById(id).scrollTop)"
    if keyboard:
        kebab.focus()
        page.keyboard.press("Enter")
    elif ui["width"] < 720:
        kebab.tap()
    else:
        kebab.click()
    expect()(menu(page).locator('[data-prm="pin"]')).to_have_text("Pin")
    before = page.evaluate(scroll)
    if keyboard:
        page.keyboard.press("Enter")
    elif ui["width"] < 720:
        menu(page).locator('[data-prm="pin"]').tap()
    else:
        menu(page).locator('[data-prm="pin"]').click()
    expect()(row(page, "older")).to_have_attribute("data-pinned", "")
    if keyboard:
        assert kebab.evaluate("el => el === document.activeElement")
        assert kebab.evaluate("""el => {
            const r = el.getBoundingClientRect();
            return r.top >= 0 && r.bottom <= innerHeight
                && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
        }"""), "keyboard focus must remain visible and unobscured by the sticky head"
    else:
        # Native anchoring can shift by the moved row plus the new spacing boundary,
        # but must not follow that row hundreds of pixels to its new position.
        allowance = kebab.evaluate("el => el.getBoundingClientRect().height + parseFloat(getComputedStyle(el).getPropertyValue('--space-2')) + 3")
        assert max(abs(a - b) for a, b in zip(before, page.evaluate(scroll))) <= allowance, "pointer pin must not jump the list"
    expect()(page.locator('#toast-stack').filter(has_text="Could not save project pin")).to_have_count(0)
