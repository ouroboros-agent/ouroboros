"""Browser regressions for generated subagent preview identity preservation."""
from __future__ import annotations

import copy
import json

import pytest

from tests.test_subscription_setup_browser import subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def _watch_previews(page):
    page.evaluate("performance.clearResourceTimings()")


def _wait_for_two_previews(page):
    page.wait_for_function("""
        () => performance.getEntriesByType('resource')
            .filter(entry => entry.name.includes('/api/onboarding/subagents/preview')).length >= 2
    """)


def test_settings_same_preview_keeps_live_row_handler_and_payload(subscription_ui):
    ui, page = subscription_ui, subscription_ui["page"]
    roster = copy.deepcopy(ui["fixture"]["preview"]["available_subagents"])
    ui["settings"]["OUROBOROS_SUBAGENTS"] = ""
    ui["settings"]["_meta"]["available_subagents"] = {"source": "undecided", "candidate": roster}
    saves = []

    def settings_route(route):
        if route.request.method == "POST":
            saves.append(route.request.post_data_json)
            route.fulfill(content_type="application/json", body=json.dumps({
                "status": "saved", "saved": True, "restart_required": False,
            }))
        else:
            route.fulfill(content_type="application/json", body=json.dumps(ui["settings"]))

    page.route("**/api/settings", settings_route)
    _watch_previews(page)
    page.goto(ui["url"] + "/#settings")
    with page.expect_response("**/api/onboarding/subagents/preview"):
        page.locator('[data-settings-tab="agents"]').click()
    page.wait_for_selector("[data-subagent-row]")
    _wait_for_two_previews(page)
    page.evaluate("window.__rowBefore = document.querySelector('[data-subagent-row]')")
    row = page.locator("[data-subagent-row]").first
    field = row.locator('[data-subagent-field="recommended_use"]')
    field.fill("SETTINGS EDIT 123")
    assert page.evaluate("() => document.activeElement === document.querySelector('[data-subagent-field=recommended_use]')")
    assert page.evaluate("() => window.__rowBefore === document.querySelector('[data-subagent-row]')")
    with page.expect_response("**/api/settings"):
        page.locator("#btn-save-settings").click()
    assert saves
    payload = saves[-1]["OUROBOROS_SUBAGENTS"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["items"][0]["recommended_use"] == "SETTINGS EDIT 123"


def test_wizard_same_preview_keeps_live_row_handler_and_finish_payload(subscription_ui):
    ui, page = subscription_ui, subscription_ui["page"]
    _watch_previews(page)
    page.goto(ui["url"] + "/onboarding")
    page.wait_for_selector("#quick-start-btn:not([hidden])")
    page.click("#next-btn")
    page.wait_for_selector('[data-model-role="main"]')
    page.locator("details:has(#onboarding-available-subagents) > summary").click()
    page.wait_for_selector("#onboarding-available-subagents [data-subagent-row]")
    _wait_for_two_previews(page)
    page.evaluate("window.__rowBefore = document.querySelector('#onboarding-available-subagents [data-subagent-row]')")
    field = page.locator("#onboarding-available-subagents [data-subagent-field='recommended_use']").first
    field.fill("WIZARD EDIT 456")
    assert page.evaluate("() => window.__rowBefore === document.querySelector('#onboarding-available-subagents [data-subagent-row]')")
    page.click("#next-btn")
    page.wait_for_selector("#reviewer-slots-section", state="attached")
    page.click("#next-btn")
    page.wait_for_selector('[data-collapse="api-budget"]')
    page.click("#next-btn")
    page.wait_for_selector(".summary-card")
    with page.expect_response("**/api/onboarding/complete"):
        page.click("#next-btn")
    writes = [body for path, body in ui["posts"] if path == "/api/onboarding/complete"]
    assert len(writes) == 1
    payload = writes[0]["OUROBOROS_SUBAGENTS"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["items"][0]["recommended_use"] == "WIZARD EDIT 456"

