"""Real isolated Skills page: native platform visibility and retained OFF.

This is a macOS/Linux UI consumer, not native Windows/COM qualification. A
Windows seed left by an old unfiltered launcher is deliberately present on disk.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from urllib.request import urlopen

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401


@pytest.mark.ui_browser
@pytest.mark.serial
@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Unix-host visibility journey")
def test_skills_shows_native_unix_and_keeps_owner_off(direct_server_with_data, tmp_path):  # noqa: F811
    from ouroboros.launcher_bootstrap import _seed_skills_into
    from ouroboros.skill_loader import load_enabled

    pw = pytest.importorskip("playwright.sync_api")
    host = direct_server_with_data
    drive, repo, url = host["data_dir"], host["repo_dir"], host["url"]
    host["stop_server"]()
    _seed_skills_into(repo / "skills", drive / "skills", logging.getLogger(__name__))
    foreign = drive / "skills" / "native" / "windows_computer_use"
    shutil.copytree(repo / "skills" / "windows_computer_use", foreign, dirs_exist_ok=True)
    (foreign / ".seed-origin").write_text("seeded by an earlier launcher\n", encoding="utf-8")
    assert load_enabled(drive, "unix_computer_use") is True
    host["start_server"]()

    def skills():
        with urlopen(url + "/api/extensions", timeout=30) as response:
            return {row["name"]: row for row in json.load(response)["skills"]}

    rows = skills()
    assert "windows_computer_use" not in rows
    assert rows["unix_computer_use"]["enabled"] is True
    evidence = Path(os.environ.get("OUROBOROS_UI_SCREENSHOT_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    print("SKILLS_VISUAL_EVIDENCE " + str(evidence), flush=True)
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.goto(url + "/#skills")
            card = page.locator('.skills-card[data-skill="unix_computer_use"]')
            card.wait_for(timeout=30000)
            toggle = card.locator(".skills-toggle")
            assert toggle.is_checked()
            assert page.locator('.skills-card[data-skill="windows_computer_use"]').count() == 0
            page.screenshot(path=str(evidence / "skills-native-on.png"))
            with page.expect_response(lambda response: response.request.method == "POST" and
                                      response.url.endswith("/api/skills/unix_computer_use/toggle")) as saved:
                card.locator("label.skills-switch").click()
            assert saved.value.ok and saved.value.json()["enabled"] is False
            assert load_enabled(drive, "unix_computer_use") is False
            # A subsequent launcher seed must not reinterpret OFF as no choice.
            host["stop_server"]()
            _seed_skills_into(repo / "skills", drive / "skills", logging.getLogger(__name__))
            host["start_server"]()
            page.goto(url + "/#skills")
            card.wait_for(timeout=30000)
            assert not toggle.is_checked()
            assert page.locator('.skills-card[data-skill="windows_computer_use"]').count() == 0
            assert "windows_computer_use" not in skills()
            page.screenshot(path=str(evidence / "skills-native-off-after-reseed.png"))
        finally:
            browser.close()
