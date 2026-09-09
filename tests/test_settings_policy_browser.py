"""Real Settings save/reload proof for the three independent policy controls."""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401


@pytest.mark.ui_browser
def test_access_and_review_round_trip_without_losing_restart_truth(direct_server_with_data):
    from playwright.sync_api import sync_playwright

    fixture = direct_server_with_data
    evidence = pathlib.Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(fixture["data_dir"].parent)))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        try:
            def open_behavior():
                page.goto(fixture["url"], wait_until="domcontentloaded")
                page.locator('[data-nav-page="settings"]').click()
                page.locator('[data-settings-tab="behavior"]').click()
                page.wait_for_function("() => document.querySelector('#btn-save-settings')?.disabled === false")

            open_behavior()
            page.locator('[data-runtime-mode-group] [data-effort-value="cyber_pro"]').click()
            page.locator('[data-enforcement-group] [data-effort-value="blocking"]').click()
            assert page.locator('#s-runtime-mode').input_value() == "cyber_pro"
            assert page.locator('#s-review-enforcement').input_value() == "blocking"
            assert page.locator('[data-enforcement-group] [data-effort-value="blocking"]').is_enabled()
            page.locator('#btn-save-settings').click()
            page.locator('[data-confirm-ok]').click()
            page.wait_for_function("() => !document.querySelector('#btn-save-settings').disabled && (document.querySelector('#settings-status').textContent || '').includes('restart required')")
            stored = json.loads((fixture["data_dir"] / "settings.json").read_text())
            assert stored["OUROBOROS_RUNTIME_MODE"] == "cyber_pro"
            assert stored["OUROBOROS_REVIEW_ENFORCEMENT"] == "blocking"

            open_behavior()
            assert page.locator('#s-runtime-mode').input_value() == "cyber_pro"
            assert page.locator('#s-review-enforcement').input_value() == "blocking"
            access = page.locator('[data-policy-state="access"]')
            assert "Saved: Cyber Pro" in access.inner_text()
            assert "Current process: Light" in access.inner_text()
            assert "Next task: Cyber Pro" in access.inner_text()
            assert "Restart required" in access.inner_text()
            access.scroll_into_view_if_needed()
            page.screenshot(path=str(evidence / "settings-policy-reload-desktop.png"))
        finally:
            browser.close()
