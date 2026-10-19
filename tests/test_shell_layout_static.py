"""The shell has one layout switch (docs/DESIGN.md "Shell", plan P1).

`web/theme.js` writes `html[data-layout="docked"|"stack"]` before CSS from one number pair; the
shell's sheets key off that attribute, and the shell's JS reads it through `web/modules/layout.js`.
A second width breakpoint for the shell (a media query in the sheets, a `matchMedia` in app code)
is the drift this guard closes: CSS and JS used to disagree about where "mobile" starts (640,
900 and 980 at once)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The shell's structure: what the switch moves. Component sheets may keep their own width queries.
SHELL_SELECTORS = (
    "#app", "#primary-sidebar", ".nav-drawer-backdrop", ".mobile-nav-toggle", ".project-panel",
    ".project-panel-backdrop", ".resize-handle", ".app-page-header", ".page-header", "#nav-version",
    "#content", ".nav-row", ".nav-projects", ".sidebar-", ".chat-page-header", "keyboard-open",
)
WIDTH_QUERY = re.compile(r"@media[^{]*\((?:max|min)-width:[^{]*\{")


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _media_blocks(css: str):
    """Yield the body of every width media query (balanced braces)."""
    for match in WIDTH_QUERY.finditer(css):
        depth, start = 1, match.end()
        pos = start
        while depth and pos < len(css):
            if css[pos] == "{":
                depth += 1
            elif css[pos] == "}":
                depth -= 1
            pos += 1
        yield match.group(0), css[start:pos]


def test_shell_sheets_key_structure_off_the_layout_attribute_not_width_queries():
    for rel in ("web/style.css", "web/ui.css"):
        for head, body in _media_blocks(_read(rel)):
            offenders = [sel for sel in SHELL_SELECTORS if sel in body]
            assert not offenders, f"{rel}: {head.strip()} styles shell structure {offenders}; key it off html[data-layout]"
    css = _read("web/style.css")
    assert 'html[data-layout="stack"] #app' in css
    assert 'html[data-layout="stack"] #primary-sidebar' in css
    assert 'html[data-layout="stack"] .project-panel' in css
    # Docked, the Project panel keeps its own width beside Main and never covers the sidebar (owner 18.10, 4A).
    docked_panel = css.split('html[data-layout="docked"] .project-panel {', 1)[1].split("}", 1)[0]
    assert "max-width: calc(100vw - var(--sidebar-width))" in docked_panel
    assert "data-project-fill" not in css and "data-project-fill" not in _read("web/app.js")


def test_the_layout_numbers_live_once_in_theme_js():
    theme = _read("web/theme.js")
    assert theme.count("dockedMinWidth: 720") == 1
    assert "projectBesideMainMinWidth" not in theme
    assert "root.dataset.layout = layout" in theme
    assert "window.ouroLayout = Object.freeze(" in theme
    for rel in ("web/modules/layout.js", "web/app.js", "web/modules/client_surface.js", "web/modules/page_header.js"):
        source = _read(rel)
        assert "720" not in source, f"{rel} repeats the docked width; read it from layout.js"
        assert "matchMedia('(max-width" not in source and "matchMedia('(min-width" not in source, rel
    # A sheet may size content at 720px (the Skills sheet does); it may not copy the switch into a query.
    switch_copy = re.compile(r"@(?:media|container)[^{]*\b(?:719|720)(?:\.\d+)?px")
    for rel in ("web/style.css", "web/ui.css", "web/room_work.css", "web/settings.css"):
        found = switch_copy.search(_read(rel))
        assert found is None, f"{rel} repeats the docked width in a query ({found.group(0) if found else ''}); key it off html[data-layout]"
    assert switch_copy.search("@media (min-width: 720px) { .x { y: z } }"), "the guard still recognizes a copied switch"
    for rel in ("web/app.js", "web/modules/client_surface.js"):
        assert "from './modules/layout.js'" in _read(rel) or "from './layout.js'" in _read(rel), rel
    # The switch is painted before CSS: theme.js loads first (theme.test.js pins the order too).
    html = _read("web/index.html")
    assert html.index("/static/theme.js") < html.index("/static/ui.css")
