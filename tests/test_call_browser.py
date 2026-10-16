"""Fake microphone/socket smoke through the real Main and Project composers.

The browser runs the real AudioContext, AudioWorklet, PCM codec and playback.
Only getUserMedia's source and the dedicated call socket/admission are stubs;
the ordinary app socket and room lifecycle run against the isolated test server.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ("tests.test_ui_smoke_playwright",)

FAKE_CALL = r"""(() => {
    const NativeSocket = window.WebSocket;
    const probe = window.__callTest = { sockets: [], frames: [], streams: [], micContexts: [] };
    const mediaDevices = { getUserMedia: async () => {
        if (probe.deny) throw new DOMException('Test permission denied', 'NotAllowedError');
        const context = new AudioContext();
        const oscillator = context.createOscillator();
        const output = context.createMediaStreamDestination();
        oscillator.frequency.value = 220;
        oscillator.connect(output); oscillator.start(); await context.resume();
        probe.streams.push(output.stream); probe.micContexts.push(context);
        for (const track of output.stream.getTracks()) {
            const stop = track.stop.bind(track);
            track.stop = () => { stop(); oscillator.stop(); context.close(); };
        }
        return output.stream;
    } };
    // WebKit may return a fresh MediaDevices wrapper; replace the getter's value,
    // so every capture request uses the oscillator and never the native device.
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: mediaDevices });
    class CallSocket {
        constructor(url) {
            this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = [];
            probe.sockets.push(this);
            setTimeout(() => { if (this.readyState !== 3) { this.readyState = 1; this.onopen?.(); } }, 0);
        }
        receive(data) { this.onmessage?.({ data: typeof data === 'object' && !(data instanceof ArrayBuffer) ? JSON.stringify(data) : data }); }
        send(data) {
            if (typeof data !== 'string') {
                const view = new DataView(data);
                probe.frames.push({ magic: [view.getUint8(0), view.getUint8(1)], version: view.getUint8(2),
                    kind: view.getUint8(3), epoch: view.getUint16(4, true), seq: view.getUint32(6, true),
                    rate: view.getUint32(10, true), bytes: data.byteLength - 16 });
                return;
            }
            const msg = JSON.parse(data); this.sent.push(msg);
            if (msg.type === 'hello') {
                this.rate = msg.rate;
                this.receive({ type: 'state', state: 'live' });
            }
        }
        speech(item, epoch, ms = 600) {
            this.receive({ type: 'caption', speaker: 'ouroboros', item_id: item, stream: 0, epoch,
                text: '<b>Caption is literal</b>', final: false });
            const samples = Math.round(this.rate * 0.02);
            for (let seq = 0; seq < ms / 20; seq++) {
                const wire = new ArrayBuffer(16 + samples * 2), view = new DataView(wire);
                view.setUint8(0, 79); view.setUint8(1, 67); view.setUint8(2, 1); view.setUint8(3, 2);
                view.setUint16(4, epoch, true); view.setUint32(6, seq, true);
                view.setUint32(10, this.rate, true); view.setUint16(14, 0, true);
                for (let i = 0; i < samples; i++) view.setInt16(16 + i * 2, 1000, true);
                this.receive(wire);
            }
        }
        close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
    }
    function Socket(url, protocols) {
        return new URL(url, location.href).pathname.startsWith('/ws/call/')
            ? new CallSocket(url) : new NativeSocket(url, protocols);
    }
    Socket.prototype = NativeSocket.prototype;
    Object.setPrototypeOf(Socket, NativeSocket);
    window.WebSocket = Socket;
})();"""


def _socket(page, script):
    return page.evaluate("() => { const socket = __callTest.sockets.at(-1); " + script + " }")


def _ended(page):
    page.wait_for_function("__callTest.streams.every(stream => stream.getTracks().every(track => track.readyState === 'ended'))")
    page.wait_for_function("__callTest.micContexts.every(context => context.state === 'closed')")


@pytest.mark.serial
@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_call_fake_media_in_main_and_project(direct_server_with_data, engine):
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.projects_registry import create_project

    server = direct_server_with_data
    project = create_project(server["data_dir"], "voice-room", name="Voice room")
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(server["data_dir"].parent)))
    evidence.mkdir(parents=True, exist_ok=True)
    admissions, errors = [], []

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script(FAKE_CALL)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def admit(route):
                admissions.append(route.request.post_data_json)
                route.fulfill(json={"call_id": f"test-{len(admissions)}", "ws_path": f"/ws/call/test-{len(admissions)}"})

            page.route("**/api/call/open", admit)
            page.goto(server["url"], wait_until="domcontentloaded")
            main = page.locator("#page-chat")
            call = main.locator(".call-button")
            expect(call).to_be_enabled(timeout=30000)
            assert call.evaluate("el => el.previousElementSibling.id") == "chat-attach"
            main.locator(".call-choose").click()
            main.locator(".call-engine").select_option("google")
            main.locator(".call-model").fill("owner-live-model")
            main.locator(".call-voice").fill("owner-voice")
            call.click()
            expect(main.locator(".call-state")).to_have_text("Listening", timeout=15000)
            assert page.evaluate("__callTest.streams.length") == 1
            page.wait_for_function("__callTest.frames.length > 2")
            assert admissions[-1]["chat_id"] == 1 and admissions[-1]["engine"] == "google"
            assert admissions[-1]["model"] == "owner-live-model" and admissions[-1]["voice"] == "owner-voice"
            assert admissions[-1]["client_message_id"]
            hello = _socket(page, "return socket.sent[0];")
            frames = page.evaluate("__callTest.frames")
            assert hello["protocol"] == 1 and hello["role"] == "owner" and hello["mic"] and hello["speaker"]
            assert all(f["magic"] == [79, 67] and f["version"] == 1 and f["kind"] == 1
                       and f["rate"] == hello["rate"] and f["bytes"] > 0 for f in frames)
            assert frames[1]["seq"] > frames[0]["seq"]
            _socket(page, "socket.speech('first', 0);")
            expect(main.locator(".call-captions")).to_have_text("Ouroboros: <b>Caption is literal</b>")
            assert main.locator(".call-captions b").count() == 0
            page.wait_for_function("__callTest.sockets.at(-1).sent.some(m => m.type === 'played' && m.item_id === 'first' && m.played_ms > 0)")
            # Flush while some samples are still queued, then deliberately deliver stale PCM.
            _socket(page, "socket.receive({type: 'flush', epoch: 1}); socket.speech('stale', 0); socket.speech('second', 1, 200);")
            page.wait_for_function("__callTest.sockets.at(-1).sent.some(m => m.type === 'played' && m.item_id === 'second')")
            played = _socket(page, "return socket.sent.filter(m => m.type === 'played');")
            assert not any(m["item_id"] == "stale" for m in played)
            assert all(0 < m["played_ms"] <= (600 if m["item_id"] == "first" else 200) for m in played)
            main.locator(".call-mute").click()
            expect(main.locator(".call-mute")).to_have_text("Unmute")
            assert page.evaluate("__callTest.streams.at(-1).getAudioTracks().every(track => !track.enabled)")
            count = page.evaluate("__callTest.frames.length")
            page.wait_for_timeout(100)  # More than four capture packets; muted sends must stay absent.
            assert page.evaluate("__callTest.frames.length") == count
            main.locator(".call-mute").click()
            page.wait_for_function("count => __callTest.frames.length > count", arg=count)
            _socket(page, "socket.receive({type: 'state', state: 'reconnecting', reason: 'engine_error:transport'});")
            expect(main.locator(".call-state")).to_have_text("Reconnecting…")
            expect(main.locator(".call-reason")).to_have_text("The voice connection dropped.")
            expect(main.locator(".call-reason")).to_have_attribute("title", "engine_error:transport")
            _socket(page, "socket.receive({type: 'state', state: 'live'});")
            for width in [1280, 390]:
                page.set_viewport_size({"width": width, "height": 900})
                assert main.locator(".chat-text-row").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                assert main.locator(".call-strip").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                page.screenshot(path=str(evidence / f"call-{engine}-{width}.png"), animations="disabled")
            main.locator(".call-leave").click()
            _ended(page)
            assert _socket(page, "return socket.sent.at(-1).type;") == "bye"

            # A phone uses the same control and device-rate worklet.
            call.click()
            expect(main.locator(".call-state")).to_have_text("Listening")
            main.locator(".call-leave").click()
            _ended(page)
            page.set_viewport_size({"width": 1280, "height": 900})
            page.locator('.nav-project-row[data-project-id="voice-room"]').click()
            room = page.locator("#panel-pchat-voice-room")
            expect(room.locator(".call-button")).to_be_enabled()
            room.locator(".call-button").click()
            expect(room.locator(".call-state")).to_have_text("Listening")
            assert admissions[-1]["chat_id"] == project["chat_id"]
            page.click("#project-panel-close")
            _ended(page)
            assert _socket(page, "return socket.readyState;") == 3
            assert page.locator("#panel-pchat-voice-room").count() == 0

            page.evaluate("__callTest.deny = true")
            call.click()
            expect(main.locator(".call-state")).to_have_text("Call failed")
            expect(main.locator(".call-reason")).to_contain_text("Microphone access was denied")
            page.evaluate("__callTest.deny = false")
            call.click()
            expect(main.locator(".call-state")).to_have_text("Listening")
            _socket(page, "socket.onerror();")
            expect(main.locator(".call-state")).to_have_text("Call failed")
            _ended(page)
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.serial
@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_call_feature_refusal_and_settings_draft(direct_server_with_data, engine):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script("Object.defineProperty(window, 'isSecureContext', {value: false});")
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            call = page.locator("#page-chat .call-button")
            expect(call).to_be_disabled(timeout=30000)
            expect(page.locator("#page-chat .call-reason")).to_contain_text("HTTPS or localhost")
            assert call.get_attribute("aria-describedby")
            # Settings go through the existing draft serializer, with no hardcoded model choices.
            page.set_viewport_size({"width": 1280, "height": 900})
            page.click('[data-nav-page="settings"]')
            expect(page.locator("#settings-status")).to_have_text("Settings refreshed", timeout=30000)
            page.click('[data-nav-page="chat"]')
            expect(call).to_be_disabled()
            expect(page.locator("#page-chat .call-reason")).to_be_visible()
            expect(page.locator("#page-chat .call-reason")).to_contain_text("HTTPS or localhost")
            page.click('[data-nav-page="settings"]')
            page.click('[data-settings-tab="models"]')
            for name in ["call-max-minutes", "presence-call-max-minutes"]:
                expect(page.locator("#s-" + name)).to_have_value("")
            page.locator("#s-call-engine").select_option("google")
            page.locator("#s-call-model-google").fill("future-owner-model")
            page.locator("#s-call-voice-google").fill("future-owner-voice")
            page.locator("#s-call-native-search").select_option("true")
            page.locator("#s-call-max-minutes").fill("17")
            page.locator("#s-presence-call-max-minutes").fill("-1")
            page.click("#btn-save-settings")
            expect(page.locator("#s-presence-call-max-minutes")).to_have_attribute("aria-invalid", "true")
            page.locator("#s-presence-call-max-minutes").fill("")
            saves = []

            def saved(route):
                saves.append(route.request.post_data_json)
                route.fulfill(json={"ok": False, "error": "Synthetic save refusal", "saved": False})

            page.route("**/api/settings", saved, times=1)
            page.click("#btn-save-settings")
            page.wait_for_function("document.querySelector('#settings-status').textContent.includes('Synthetic save refusal')")
            assert saves[-1]["OUROBOROS_CALL_ENGINE"] == "google"
            assert saves[-1]["OUROBOROS_CALL_MODEL_GOOGLE"] == "future-owner-model"
            assert saves[-1]["OUROBOROS_CALL_VOICE_GOOGLE"] == "future-owner-voice"
            assert saves[-1]["OUROBOROS_CALL_NATIVE_SEARCH"] == "true"
            assert saves[-1]["OUROBOROS_CALL_MAX_MINUTES"] == "17"
            assert saves[-1]["OUROBOROS_PRESENCE_CALL_MAX_MINUTES"] == ""
            expect(page.locator("#s-call-model-google")).to_have_value("future-owner-model")
            page.click('[data-settings-tab="secrets"]')
            secret = page.locator('#s-secret-google-api-key')
            expect(secret).to_have_attribute("type", "password")
            secret.fill("synthetic-google-secret")
            page.locator('.secret-toggle[data-target="s-secret-google-api-key"]').click()
            expect(page.locator('#s-secret-google-api-key-reveal')).to_have_text("synthetic-google-secret")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator(".settings-scroll").evaluate("el => el.scrollWidth <= el.clientWidth + 1")

            telegram = browser.new_page()
            telegram.add_init_script("window.Telegram = {WebApp: {}};")
            telegram.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            telegram.locator("#chat-attach").wait_for()
            telegram.evaluate("() => import('/static/modules/call.js')")
            assert telegram.locator(".call-button").count() == 0
        finally:
            browser.close()
