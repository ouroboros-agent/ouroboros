"""A retained desktop DOM must not make the next shell stylesheet squeeze Main.

The former shell had an in-flow #divider before #content. Drive the real CSS
through Chromium against that old document shape and the current one; no server
or stored chat data is needed for this geometry regression.
"""

from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.mark.ui_browser
def test_retained_divider_cannot_take_the_main_grid_track():
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    ui = (WEB / "ui.css").read_text(encoding="utf-8")
    composition = (WEB / "style.css").read_text(encoding="utf-8")
    for legacy in (False, True):
        shell = f"""<!doctype html><html data-layout="docked"><head>
            <style>{ui}</style><style>{composition}</style></head><body>
            <div id="app"><nav id="primary-sidebar">Main Chat</nav>
            {'<div id="divider"></div>' if legacy else ''}
            <main id="content">Main transcript</main>
            <div id="project-panel-backdrop" class="project-panel-backdrop" hidden></div>
            <aside id="project-panel" class="project-panel" hidden></aside></div></body></html>"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1536, "height": 816})
                page.set_content(shell)
                measured = page.evaluate("""() => {
                    const sidebar = document.querySelector('#primary-sidebar').getBoundingClientRect();
                    const main = document.querySelector('#content').getBoundingClientRect();
                    const divider = document.querySelector('#divider');
                    return {left: main.left, right: main.right, width: main.width,
                        sidebarRight: sidebar.right,
                        dividerDisplay: divider && getComputedStyle(divider).display,
                        tracks: getComputedStyle(document.querySelector('#app')).gridTemplateColumns};
                }""")
                assert abs(measured["left"] - measured["sidebarRight"]) <= 1, measured
                assert measured["width"] >= 1000 and measured["right"] <= 1537, measured
                assert len(measured["tracks"].split()) == 2, measured
                if legacy:
                    assert measured["dividerDisplay"] == "none", measured

                page.evaluate("""() => {
                    for (const id of ['project-panel-backdrop', 'project-panel']) {
                        const el = document.getElementById(id);
                        el.hidden = false;
                        el.classList.add('open');
                    }
                }""")
                panel = page.evaluate("""() => {
                    const main = document.querySelector('#content').getBoundingClientRect();
                    const panel = document.querySelector('#project-panel').getBoundingClientRect();
                    return {mainLeft: main.left, mainWidth: main.width,
                        panelLeft: panel.left, panelRight: panel.right};
                }""")
                assert panel["mainLeft"] == measured["left"] and panel["mainWidth"] == measured["width"], panel
                assert panel["panelLeft"] >= measured["sidebarRight"] and panel["panelRight"] <= 1537, panel

                page.evaluate("document.documentElement.dataset.layout = 'stack'")
                page.set_viewport_size({"width": 414, "height": 700})
                narrow = page.evaluate("""() => {
                    const main = document.querySelector('#content').getBoundingClientRect();
                    return {left: main.left, right: main.right, width: main.width};
                }""")
                assert narrow["left"] == 0 and 400 <= narrow["width"] <= 414, narrow
                layer = page.locator('#project-panel').bounding_box()
                assert layer and abs(layer['x']) <= 1 and layer['width'] >= 400, layer
            finally:
                browser.close()
