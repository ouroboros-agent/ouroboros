"""Production Main cards and motion over static gateway data, without a runtime."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
WEB = Path(__file__).resolve().parents[1] / "web"
BOOT = """<script type="module">
import {createChatInstance} from '/static/modules/chat.js';
const handlers=new Map();let connected=true;
const ws={on(type,fn){const rows=handlers.get(type)||[];rows.push(fn);handlers.set(type,rows);return()=>{};},
    isConnected:()=>connected,send(){}};
window.emit=(type,row)=>(handlers.get(type)||[]).forEach(fn=>fn(row));
window.connection=value=>{connected=value;window.emit(value?'open':'close');};
window.chat=createChatInstance({ws,state:{activePage:'chat',projectChatIds:new Set([7,8]),unreadCount:0},
    updateUnreadBadge(){},openSettingsTab(){},openDashboardTab(){},chatId:1,
    mountEl:document.getElementById('content'),stateSnapshots:{begin:()=>({generation:1,requestedAt:Date.now()}),
    gate(){window.censusAsked=(window.censusAsked||0)+1;return Promise.resolve(this.begin());},isCurrent:()=>true,
    apply(request,data){window.chat?.hydrateStateSnapshot(data);window.censusRead=(window.censusRead||0)+1;}}});
document.getElementById('reconnect-overlay').hidden=true;
document.getElementById('reconnect-overlay').style.display='none';
window.openCount=0;
window.addEventListener('ouro:open-project',event=>{window.openedProject=event.detail;window.openCount++;});
</script>"""


@pytest.fixture(params=[
    ("chromium", "light", 1360), ("chromium", "dark", 390),
    ("webkit", "light", 390), ("webkit", "dark", 1360),
], ids=["chromium-light-desktop", "chromium-dark-mobile", "webkit-light-mobile", "webkit-dark-desktop"])
def project_chat(request):
    engine, theme, width = request.param
    if os.environ.get("OUROBOROS_RUN_UI_SMOKE") != "1":
        pytest.skip("Set OUROBOROS_RUN_UI_SMOKE=1 to run the browser flow")
    playwright = pytest.importorskip("playwright.sync_api")
    rows = [
        {"role": "system", "system_type": "project_started", "chat_id": 1,
         "task_id": "creation", "project_id": "alpha", "project_name": "Project Alpha",
         "task_name": "Review runner › integration", "text": "Project Alpha › Review runner › integration · Started",
         "ts": "2026-10-08T13:57:33Z"},
        {"role": "system", "system_type": "project_handoff", "chat_id": 1,
         "task_id": "transfer", "project_id": "beta", "project_name": "Project Beta",
         "handoff_id": "transfer-beta", "task_name": "A second independent work item",
         "text": "Project Beta › A second independent work item", "ts": "2026-10-08T13:58:00Z"},
    ]
    state = {"supervisor_ready": True, "active_chat_activities_complete": True,
             "active_chat_activities": [
                 {"activity_id": task, "project_id": project, "chat_id": chat, "phase": "working"}
                 for task, project, chat in [("creation", "alpha", 7), ("transfer", "beta", 8)]
             ]}
    conversions, details, requests = {}, {}, []
    history = {"messages": rows, "progress": [], "has_more": False, "page_cursor": "work-entries",
               "next_cursor": None, "window": {"complete": True}, "coverage": {
                   "v": 1, "view": "work", "upper": {"chat": 2, "progress": 0}, "spans": {
                       "chat": {"from": 0, "to": 2, "chain": "work", "gaps": []}, "progress": None}}}
    chat_id = request.node.callspec.params.get("chat_id", 1)
    boot = BOOT.replace("chatId:1,", f"chatId:{chat_id},asPanel:{str(chat_id != 1).lower()},")
    html = (WEB / "index.html").read_text(encoding="utf-8").replace(
        '<script type="module" src="/static/app.js"></script>', boot)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return
            self.path = self.path.removeprefix("/static")
            super().do_GET()

    def respond(route):
        path = urlparse(route.request.url).path
        data = history if path == "/api/chat/history" else state if path == "/api/state" else {}
        if path == "/api/projects/from-task":
            payload = route.request.post_data_json
            requests.append(payload)
            data = conversions[payload["task_id"]]
        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[-1]
            data = details.get(task_id, {"task_id": task_id, "status": "running"})
        route.fulfill(content_type="application/json", body=json.dumps(data))

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": 850}, has_touch=width < 980, timezone_id="UTC")
                page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)});")
                page.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
                    value: {writeText: async text => {window.copiedText = text;}}});""")
                page.route("**/api/**", respond)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                yield {"page": page, "expect": playwright.expect, "rows": rows, "state": state,
                       "conversions": conversions, "details": details, "requests": requests,
                       "engine": engine, "theme": theme, "width": width}
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _evidence_dir(tmp_path):
    path = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _shell_geometry(card):
    return card.evaluate("""root => {
        const bounds = root.getBoundingClientRect(), css = getComputedStyle(root);
        const measure = selector => {
            const node = root.querySelector(selector), box = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return {left: box.left - bounds.left, top: box.top - bounds.top,
                right: box.right - bounds.left, bottom: box.bottom - bounds.top,
                width: box.width, height: box.height, overflow: node.scrollWidth > node.clientWidth + 1,
                font: style.fontSize, line: style.lineHeight, weight: style.fontWeight};
        };
        return {width: bounds.width, height: bounds.height, overflow: root.scrollWidth > root.clientWidth + 1,
            surface: Object.fromEntries(['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
                'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
                'borderColor', 'borderRadius', 'backgroundColor', 'boxShadow'].map(key => [key, css[key]])),
            title: measure('.project-handoff-title'), phase: measure('.project-handoff-phase'),
            reference: measure('[data-intent="open-project"]'), footer: measure('.project-handoff-footer'),
            name: measure('.chat-live-project-name'), time: measure('.msg-time'), copy: measure('.chat-message-copy')};
    }""")


def _assert_shared_shell(cards, *, narrow):
    measurements = [_shell_geometry(card) for card in cards]
    baseline = measurements[0]
    for result in measurements:
        assert result["surface"] == baseline["surface"]
        for dimension in ("width", "height"):
            assert result[dimension] == pytest.approx(baseline[dimension], abs=1)
        assert result["surface"]["paddingTop"] == "12px"
        assert result["surface"]["paddingLeft"] == "16px"
        assert not result["overflow"]
        for part in ("title", "phase", "reference", "footer", "name", "time", "copy"):
            geometry = result[part]
            assert geometry["left"] >= 16 and geometry["right"] <= result["width"] - 16
            assert geometry["top"] >= 12 and geometry["bottom"] <= result["height"] - 12
            if part not in ("reference", "name"):
                assert not geometry["overflow"], (part, result)
            for dimension in ("left", "top", "width", "height"):
                assert geometry[dimension] == pytest.approx(baseline[part][dimension], abs=1), part
            assert geometry["font"] == baseline[part]["font"]
            assert geometry["line"] == baseline[part]["line"]
        # Direction A: a light title over one destination row. The reference fills that row
        # beside the recorded time and Copy (on a narrow card, the whole row above them).
        assert result["title"]["weight"] == "500"
        reference, footer, time, copy = (result[part] for part in ("reference", "footer", "time", "copy"))
        assert reference["left"] == pytest.approx(footer["left"], abs=1)
        assert reference["height"] >= 36, "the destination keeps a full pointer target"
        assert time["right"] <= copy["left"] + 1
        assert copy["right"] == pytest.approx(footer["right"], abs=1)
        if narrow:
            assert reference["width"] == pytest.approx(footer["width"], abs=1)
            assert time["top"] >= reference["bottom"] - 1
        else:
            assert time["left"] >= reference["right"] - 1
            assert reference["width"] >= footer["width"] - time["width"] - copy["width"] - 2 * 8 - 2
            for part in (time, copy):
                assert part["top"] + part["height"] / 2 == pytest.approx(reference["top"] + reference["height"] / 2, abs=2)
        # A long name reads on two lines at the reading size, never a smaller font.
        assert result["name"]["font"] == "14px"
        assert result["name"]["height"] == pytest.approx(2 * float(result["name"]["line"].removesuffix("px")), abs=2)
        if narrow:
            assert result["phase"]["bottom"] < result["title"]["top"]
            assert result["phase"]["left"] == pytest.approx(result["title"]["left"], abs=1)
        else:
            assert result["title"]["right"] < result["phase"]["left"]
            assert result["phase"]["top"] < result["title"]["top"] + float(result["title"]["line"].removesuffix("px"))
    return measurements


def _emit_convertible_work(page, task_id, title):
    page.evaluate("""({task, title}) => {
        emit('chat', {chat_id: 1, task_id: task, role: 'assistant', is_progress: true,
            content: 'Inspecting the requested work', ts: '2026-10-08T13:56:00Z'});
        emit('task_named', {task_id: task, suggested_name: title});
    }""", {"task": task_id, "title": title})
    return page.locator(f'.chat-live-card[data-task-id="{task_id}"]')


def test_conversion_replay_and_creation_share_the_whole_project_shell(project_chat, tmp_path):
    page, expect = project_chat["page"], project_chat["expect"]
    rows, state = project_chat["rows"], project_chat["state"]
    title = "Review runner integration, preserve the complete long work title and its visible hierarchy " * 3
    title = title.strip()
    name = "Project with a deliberately long name that uses its available line and remains fully accessible " * 3
    name = name.strip()
    ts = "2026-10-08T13:58:00Z"
    for row in rows:
        row.update(task_name=title, project_name=name, ts=ts)
    page.reload()
    page.emulate_media(reduced_motion="reduce")
    start = page.locator('[data-system-type="project_started"]')
    replay = page.locator('[data-handoff-id="transfer-beta"]')
    expect(start.locator('.project-handoff-title')).to_have_text(title)
    cards = [start, replay]
    saved_rows = []
    for order in ("after", "before"):
        task_id, project_id, handoff_id = f"converted-{order}", f"room-{order}", f"handoff-{order}"
        card = _emit_convertible_work(page, task_id, title)
        original_position = card.get_attribute("data-ts")
        assert original_position
        state["active_chat_activities"].append({"activity_id": task_id, "chat_id": 1, "phase": "working"})
        page.evaluate("s=>chat.hydrateStateSnapshot(s)", state)
        project_chat["conversions"][task_id] = {
            "project": {"id": project_id, "name": name, "chat_id": 9},
            "binding": {"project_id": project_id}, "handoff_id": handoff_id, "handoff_receipt": "durable"}
        receipt = {"role": "system", "system_type": "project_handoff", "chat_id": 1,
                   "task_id": task_id, "project_id": project_id, "project_name": name,
                   "handoff_id": handoff_id, "task_name": title, "text": "Stored transfer", "ts": ts}
        if order == "before":
            page.evaluate("r=>emit('chat',{...r,content:r.text})", receipt)
        card.locator('[data-turn-into-project]').click()
        expect(card).to_have_attribute("data-project-created", "1")
        assert card.locator('[data-turn-into-project]').count() == 0
        if order == "after":
            expect(card.locator('.msg-time')).to_be_hidden()
            page.evaluate("r=>emit('chat',{...r,content:r.text})", receipt)
        expect(card.locator('.msg-time')).to_have_attribute("title", "Oct 8, 2026 at 13:58")
        expect(page.locator(f'.chat-bubble[data-handoff-id="{handoff_id}"]')).to_be_hidden()
        assert card.get_attribute("data-ts") == original_position, "folding must not move the converted transcript node"
        saved_rows.append(receipt)
        cards.append(card)
    assert [request["task_id"] for request in project_chat["requests"]] == ["converted-after", "converted-before"]
    page.wait_for_function("""() => {
        const cards = [...document.querySelectorAll('.project-handoff:not([hidden])')];
        return cards.every(card => ['backgroundColor', 'borderColor', 'boxShadow'].every(
            key => getComputedStyle(card)[key] === getComputedStyle(cards[0])[key]));
    }""")
    measurements = _assert_shared_shell(cards, narrow=project_chat["width"] < 980)
    keyboard_trace = []
    for card in cards:
        assert card.locator('.message, .sender').count() == 0, "every host mounts the same content shell"
        # Creation, replay and conversion alike: the reference is the one door and stop.
        assert card.evaluate("e=>['role','tabindex','aria-label'].map(name=>e.getAttribute(name))") == [None, None, None]
        expect(card.locator('.project-handoff-title')).to_have_text(title)
        reference = card.locator('[data-intent="open-project"]')
        expect(reference).to_have_attribute("aria-label", f"Open project {name}")
        # Clamped at two lines; the whole name stays in the accessible name, the tooltip and Copy.
        assert reference.locator('.chat-live-project-name').evaluate("e=>e.scrollHeight>e.clientHeight+1")
        expect(reference).to_have_attribute("title", f"Open project {name}")
        reference.focus()
        expect(reference).to_be_focused()
        page.keyboard.press("Tab")
        keyboard_trace.append(page.evaluate("""() => ({tag: document.activeElement.tagName,
            id: document.activeElement.id, className: document.activeElement.className})"""))
        # As in the segmented-control WebKit flow, establish keyboard modality
        # explicitly; platform Tab preferences need not visit native buttons.
        page.keyboard.press("Shift")
        reference.focus()
        expect(reference).to_be_focused()
        ring = reference.evaluate("""e=>{const s=getComputedStyle(e), r=e.getBoundingClientRect(),
            p=e.closest('.project-handoff').getBoundingClientRect(); return {visible:e.matches(':focus-visible'),
            width:s.outlineWidth, offset:s.outlineOffset,
            clearance:Math.min(r.left-p.left,p.right-r.right,r.top-p.top,p.bottom-r.bottom)};}""")
        assert ring == {"visible": True, "width": "2px", "offset": "2px", "clearance": ring["clearance"]}
        assert ring["clearance"] >= 4
        page.keyboard.press("Enter")
        assert page.evaluate("openedProject.project.id") == card.get_attribute("data-project-id")
        assert page.evaluate("openedProject.task_id") == card.get_attribute("data-task-id")
        page.evaluate("copiedText = ''")
        card.locator('.chat-message-copy').focus()
        expect(card.locator('.chat-message-copy')).to_be_focused()
        page.keyboard.press("Enter")
        page.wait_for_function("expected=>window.copiedText===expected", arg=f"{title}\n{name}")
    evidence = _evidence_dir(tmp_path)
    suffix = "-".join(str(project_chat[key]) for key in ("engine", "theme", "width"))
    page.locator('#toast-stack .toast').evaluate_all("nodes=>nodes.forEach(node=>node.click())")
    expect(page.locator('#toast-stack .toast')).to_have_count(0)
    page.screenshot(path=str(evidence / f"project-shell-{suffix}.png"), animations="disabled")
    for label, card in (("started", start), ("replay", replay), ("converted", cards[2])):
        card.screenshot(path=str(evidence / f"project-shell-{suffix}-{label}.png"), animations="disabled")
    (evidence / f"project-shell-{suffix}.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
    (evidence / f"project-keyboard-{suffix}.json").write_text(json.dumps(keyboard_trace, indent=2), encoding="utf-8")
    if project_chat["width"] >= 980:
        # A narrow pane on a desktop must stack too: the component, not viewport, decides.
        page.locator('#chat-messages').evaluate("e=>e.style.width='420px'")
        _assert_shared_shell(cards, narrow=True)
        cards[2].screenshot(path=str(evidence / f"project-shell-{suffix}-narrow-pane.png"), animations="disabled")
        page.locator('#chat-messages').evaluate("e=>e.style.removeProperty('width')")

    # Known outcomes and unfinished finalization retain both chips after conversion.
    fact = state["active_chat_activities"][-2]
    fact.update(phase="finalizing", root_phase_checkpoint={"post_task_synthesis": "running"})
    for status, label in (("failed", "Failed"), ("completed", "Done")):
        fact["status"] = status
        page.evaluate("s=>chat.hydrateStateSnapshot(s)", state)
        expect(cards[2].locator('.chat-live-phase').first).to_have_text(label)
        expect(cards[2].locator('.chat-live-phase-secondary')).to_have_text("Finalizing…")

    # Finishing BEFORE conversion used to let the task's red shell override Project ink.
    finished = _emit_convertible_work(page, "finished", title)
    terminal = {"task_id": "finished", "chat_id": 1, "type": "task_done", "status": "completed",
                "root_phase_checkpoint": {"post_task_synthesis": "completed"}, "ts": "2026-10-08T14:00:00Z"}
    project_chat["details"]["finished"] = terminal
    page.evaluate("data=>emit('log',{chat_id:1,data})", terminal)
    expect(finished).to_have_attribute("data-finished", "1")
    project_chat["conversions"]["finished"] = {"project": {"id": "finished-room", "name": name},
        "binding": {"project_id": "finished-room"}, "handoff_id": "finished-handoff", "handoff_receipt": "durable"}
    finished.locator('[data-turn-into-project]').click()
    expect(finished.locator('.project-handoff-title')).to_have_text(title)
    page.wait_for_function("""() => {
        const card = document.querySelector('[data-task-id="finished"].project-handoff');
        const reference = document.querySelector('[data-system-type="project_started"]');
        return card && ['backgroundColor','borderColor','boxShadow'].every(key=>getComputedStyle(card)[key]===getComputedStyle(reference)[key]);
    }""")
    assert _shell_geometry(finished)["surface"] == _shell_geometry(start)["surface"]
    assert page.evaluate("""() => {
        const copies = [...document.querySelectorAll('.chat-live-card.project-handoff .chat-message-copy')];
        chat.destroy(); window.copiedText = '';
        copies.forEach(button => button.click());
        return copies.length;
    }""") == 3
    assert page.evaluate("copiedText") == "", "retired converted cards must release their copy listeners"
    rows.extend(saved_rows)
    page.reload()
    for receipt in saved_rows:
        restored = page.locator(f'[data-handoff-id="{receipt["handoff_id"]}"]')
        expect(restored).to_be_visible()
        expect(restored.locator('.msg-time')).to_have_attribute("title", "Oct 8, 2026 at 13:58")
        restored.locator('.chat-message-copy').click()
        page.wait_for_function("expected=>window.copiedText===expected", arg=f"{title}\n{name}")


def test_project_work_entries_share_live_phase_title_and_motion(project_chat, tmp_path):
    page, expect = project_chat["page"], project_chat["expect"]
    rows, state = project_chat["rows"], project_chat["state"]
    engine, theme, width = (project_chat[key] for key in ("engine", "theme", "width"))
    start = page.locator('[data-system-type="project_started"]')
    transfer = page.locator('[data-system-type="project_handoff"]')
    expect(start.locator('.chat-live-phase')).to_have_text("Working")
    expect(transfer.locator('.chat-live-phase')).to_have_text("Working")
    assert start.get_attribute("data-handoff-id") is None
    assert start.locator('.project-handoff-title').inner_text() == rows[0]["task_name"]
    assert start.locator('[data-intent="open-project"]').count() == 1
    assert "System" not in start.inner_text() and "Started" not in start.inner_text()
    assert start.evaluate("e=>getComputedStyle(e).backgroundColor") == transfer.evaluate(
        "e=>getComputedStyle(e).backgroundColor")
    chip = start.locator('.chat-live-phase')
    assert chip.evaluate("e=>getComputedStyle(e).animationName") == "thinking-pulse"
    first = chip.evaluate("e=>getComputedStyle(e).opacity")
    page.wait_for_function("previous=>getComputedStyle(document.querySelector('[data-system-type=project_started] .chat-live-phase')).opacity!==previous", arg=first)
    page.emulate_media(reduced_motion="reduce")
    assert chip.evaluate("e=>getComputedStyle(e).animationName") == "none"
    page.emulate_media(reduced_motion="no-preference")

    for phase, extra, label, moving in [
        ("queued", {}, "Queued", "0"),
        ("working", {"owner_wait": {"owner_wait_state": "waiting"}}, "Waiting for your answer", "0"),
        ("working", {"required_question": {"quiz_state": "answered", "owner_wait_state": "resumed"}}, "Working", "1"),
        ("working", {"required_question_unavailable": True}, "Activity unconfirmed", "0"),
    ]:
        activity = {"activity_id": "creation", "phase": phase, **extra}
        page.evaluate("a=>chat.hydrateStateSnapshot({supervisor_ready:true,active_chat_activities_complete:true,active_chat_activities:[a]})", activity)
        expect(chip).to_have_text(label)
        assert chip.get_attribute("data-motion") == moving
        assert chip.evaluate("e=>getComputedStyle(e).animationName") == ("thinking-pulse" if moving == "1" else "none")
    page.evaluate("s=>chat.hydrateStateSnapshot(s)", state)
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"project-work-{engine}-{theme}-{width}.png"))
    assert start.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
    start.locator('[data-intent="open-project"]').click()
    assert page.evaluate("openedProject.task_id") == "creation"
    assert page.evaluate("openedProject.project.id") == "alpha"

    page.reload()
    expect(start.locator('.chat-live-phase')).to_have_text("Working")
    assert start.locator('.chat-live-phase').evaluate("e=>getComputedStyle(e).animationName") == "thinking-pulse"
    receipt = {**rows[0], "system_type": "project_handoff", "handoff_id": "transfer-alpha"}
    rows.append(receipt)
    page.evaluate("r=>emit('chat',{...r,content:r.text})", receipt)
    expect(start).to_be_hidden()
    assert page.locator('.project-handoff:not([hidden])').count() == 2
    page.reload()
    expect(start).to_have_count(1)
    expect(start).to_be_hidden()
    expect(page.locator('.project-handoff:not([hidden])')).to_have_count(2)

    # Exercise hydration and disconnect through the actual chat,
    # without pre-populating private record outcome flags.
    page.evaluate("""()=>emit('chat',{chat_id:1,task_id:'observed',role:'assistant',
        is_progress:true,content:'Reviewing the current work',ts:'2026-10-08T14:00:00Z'})""")
    child_frame = {"chat_id": 1, "task_id": "child", "role": "assistant", "is_progress": True,
                   "content": "Inspecting a related file", "ts": "2026-10-08T14:00:01Z",
                   "subagent_task_id": "child", "parent_task_id": "observed",
                   "delegation_role": "subagent", "subagent_event": "running", "subagent_role": "scout"}
    page.evaluate("r=>emit('chat',r)", child_frame)
    child_chip = page.locator('.chat-live-card[data-task-id="child"] [data-live-phase]')
    expect(child_chip).to_have_text("Working")
    card = page.locator('.chat-live-card[data-task-id="observed"]')
    outcome = card.locator('[data-live-phase]').first
    secondary = card.locator('[data-live-phase-secondary]').first
    fact = {"activity_id": "observed", "chat_id": 1, "kind": "managed_task",
            "phase": "finalizing", "status": "failed",
            "root_phase_checkpoint": {"post_task_synthesis": "running"}}
    page.evaluate("a=>chat.hydrateStateSnapshot({supervisor_ready:true,active_chat_activities_complete:true,active_chat_activities:[a]})", fact)
    expect(outcome).to_have_text("Failed")
    expect(secondary).to_have_text("Finalizing…")
    assert secondary.get_attribute("data-motion") == "1"
    fact["status"] = "completed"
    page.evaluate("a=>chat.hydrateStateSnapshot({supervisor_ready:true,active_chat_activities_complete:true,active_chat_activities:[a]})", fact)
    expect(outcome).to_have_text("Done")
    state.update(active_chat_activities=[], active_chat_activities_complete=False)
    page.evaluate("connection(false)")
    expect(outcome).to_have_text("Done")
    expect(secondary).to_have_text("Activity unconfirmed")
    assert secondary.get_attribute("data-motion") == "0"
    assert secondary.evaluate("e=>getComputedStyle(e).animationName") == "none"
    expect(child_chip).to_have_text("Activity unconfirmed")
    page.evaluate("connection(true)")
    # The reconnect reads the census at once (chat.js onWs 'open') and this harness applies
    # each read whenever it lands: an incomplete census marks every held activity unconfirmed
    # (chat_activity.js), so a read landing after the snapshot below would rightly undo it.
    page.wait_for_function("() => window.censusRead === window.censusAsked")
    expect(secondary).to_have_text("Activity unconfirmed")
    # The host's census is complete again; every later read (the 3 s Main poll) says so too.
    state.update(active_chat_activities=[fact], active_chat_activities_complete=True)
    page.evaluate("a=>chat.hydrateStateSnapshot({supervisor_ready:true,active_chat_activities_complete:true,active_chat_activities:[a]})", fact)
    expect(secondary).to_have_text("Finalizing…")
    assert secondary.get_attribute("data-motion") == "1"
    expect(child_chip).to_have_text("Activity unconfirmed")
    page.evaluate("r=>emit('chat',r)", child_frame)
    expect(child_chip).to_have_text("Working")
    assert child_chip.get_attribute("data-motion") == "1"

    ended = {**fact, "activity_id": "late-card", "status": "cancelled"}
    page.evaluate("a=>chat.hydrateStateSnapshot({supervisor_ready:true,active_chat_activities_complete:true,active_chat_activities:[a]})", ended)
    page.evaluate("""()=>emit('chat',{chat_id:1,task_id:'late-card',role:'assistant',
        is_progress:true,narration:true,content:'Earlier progress arriving later',ts:'2026-10-08T14:00:00Z'})""")
    late = page.locator('.chat-live-card[data-task-id="late-card"]')
    expect(late.locator('[data-live-title]')).to_have_text("Earlier progress arriving later")
    expect(late.locator('[data-live-phase]')).to_have_text("Cancelled")
    assert late.locator('[data-live-phase]').get_attribute("data-motion") == "0"
    assert late.get_attribute("data-finished") == "1"


# The glyph tile as painted: its fill, its ink and both against what lies beneath them.
_MARK_MATERIAL = """buttons => {
    const rgba = value => { const [r, g, b, a = 1] = String(value).match(/[\\d.]+/g).map(Number); return {r, g, b, a}; };
    const over = (top, base) => Object.fromEntries(['r', 'g', 'b'].map(k => [k, top[k] * top.a + base[k] * (1 - top.a)]));
    const probe = document.body.appendChild(document.createElement('i'));
    probe.style.color = 'var(--bg-primary)';
    const page = rgba(getComputedStyle(probe).color);
    probe.style.color = 'var(--project)';
    const ink = getComputedStyle(probe).color;
    probe.remove();
    const painted = node => {
        const layers = [];
        for (let el = node; el; el = el.parentElement) layers.unshift(rgba(getComputedStyle(el).backgroundColor));
        return layers.reduce((base, layer) => ({...over(layer, base), a: 1}), page);
    };
    const lum = c => ['r', 'g', 'b'].map(k => c[k] / 255).map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
    const contrast = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
    return buttons.map(button => {
        const icon = button.querySelector('.chat-live-project-icon'), name = button.querySelector('.chat-live-project-name');
        const tile = painted(icon), beneath = painted(icon.parentElement);
        return {host: button.classList.contains('chat-live-bound-pointer') ? 'footer' : button.closest('.project-handoff') ? 'entry' : 'capsule', ink,
            fill: rgba(getComputedStyle(icon).backgroundColor).a,
            glyph: getComputedStyle(icon).color, name: getComputedStyle(name).color,
            glyph_on_tile: contrast(rgba(getComputedStyle(icon).color), tile),
            tile_on_beneath: contrast(tile, beneath),
            name_on_beneath: contrast(rgba(getComputedStyle(name).color), beneath)};
    });
}"""


def test_project_glyph_is_a_tinted_mark_never_a_solid_tile(project_chat):
    """DESIGN "Design bible" (principle 3): hue says identity, fill strength importance. The
    glyph only identifies the Project, so in each theme it is project ink on a tint,
    readable, and lighter than the name it marks: never a second button in the row."""
    page, expect = project_chat["page"], project_chat["expect"]
    for answer in ("The review runner reports every reviewer in words.", None):
        row = {"role": "system", "system_type": "project_completion_summary", "chat_id": 1,
               "task_id": f"ended-{answer is None}", "project_id": "alpha", "project_name": "Project Alpha",
               "text": "Project Alpha · Done", "ts": "2026-10-08T14:01:00Z"}
        if answer:
            row["completion_answer"] = answer
        page.evaluate("r=>emit('chat',{...r,content:r.text})", row)
    expect(page.locator('.system-message-actions [data-intent="open-project"]')).to_have_count(2)
    # app.js renderBoundProjectPointer mounts this same footer on bound Main roots.
    card = _emit_convertible_work(page, "bound", "An existing bound task")
    card.evaluate("""async card => {
        const {projectReference} = await import('/static/modules/project_reference.js');
        card.append(projectReference({id: 'alpha', name: 'Project Alpha'}, {layout: 'footer'}));
    }""")
    # The two entries come from the initial history read, which can land after the live rows.
    expect(page.locator('.project-handoff [data-intent="open-project"]')).to_have_count(2)
    marks = page.locator('[data-intent="open-project"]').evaluate_all(_MARK_MATERIAL)
    assert sorted(mark["host"] for mark in marks) == ["capsule", "capsule", "entry", "entry", "footer"]
    for mark in marks:
        assert 0 < mark["fill"] < 1, mark
        assert mark["glyph"] == mark["name"] == mark["ink"], mark
        assert mark["glyph_on_tile"] >= 3, mark
        assert mark["tile_on_beneath"] * 2 < mark["name_on_beneath"], mark


def test_entry_free_area_presses_its_reference_while_text_and_copy_stay_their_own(project_chat):
    page, expect = project_chat["page"], project_chat["expect"]
    rows, touch = project_chat["rows"], project_chat["width"] < 980
    start = page.locator('[data-system-type="project_started"]')
    transfer = page.locator('[data-system-type="project_handoff"]')
    expect(transfer.locator('.chat-live-phase')).to_have_text("Working")

    def press(x, y):
        if touch:
            page.touchscreen.tap(x, y)
        else:
            page.mouse.click(x, y)

    def free_point(entry):
        # The card padding under the destination row is neither text nor a control (clear of the rounded corner,
        # which takes no pointer events).
        box = entry.bounding_box()
        return box["x"] + 16, box["y"] + box["height"] - 6

    for entry, task_id, project_id in ((start, "creation", "alpha"), (transfer, "transfer", "beta")):
        # The entry adds no stop and no name: the reference is the one door, keyboard included.
        assert entry.evaluate("e=>['role','tabindex','aria-label'].map(name=>e.getAttribute(name))") == [None, None, None]
        before = page.evaluate("openCount")
        press(*free_point(entry))
        assert page.evaluate("openCount") == before + 1
        assert page.evaluate("[openedProject.task_id, openedProject.project.id]") == [task_id, project_id]

    title = transfer.locator('.project-handoff-title')
    before = page.evaluate("openCount")
    if touch:
        title.tap()
    else:
        title.click()
        title.dblclick()
        assert page.evaluate("String(getSelection()).trim()"), "a double click selects a word of the title"
        box = title.bounding_box()
        page.mouse.move(box["x"] + 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(*free_point(transfer), steps=8)
        page.mouse.up()
        assert page.evaluate("String(getSelection())"), "a drag from the title into free area selects"
        # Displacement alone is a drag too, with nothing selected.
        x, y = free_point(transfer)
        page.mouse.move(x + 40, y)
        page.mouse.down()
        page.mouse.move(x + 70, y, steps=4)
        page.mouse.up()
    transfer.locator('.chat-message-copy').click()
    page.wait_for_function("expected=>window.copiedText===expected",
                           arg=f"{rows[1]['task_name']}\n{rows[1]['project_name']}")
    assert page.evaluate("openCount") == before, "text, drags and Copy never navigate"

    # A duplicate delivery of the same receipt leaves one press per click.
    page.evaluate("r=>emit('chat',{...r,content:r.text})", rows[1])
    visible = page.locator('[data-system-type="project_handoff"]:not([hidden])')
    expect(visible).to_have_count(1)
    press(*free_point(visible))
    assert page.evaluate("openCount") == before + 1


# Test-only: the pointer listeners project_handoff.js holds on the document, so a release is
# observable in every engine apart from the page's other document listeners.
_HANDOFF_DOCUMENT_LISTENERS = """(() => {
    const live = [], add = EventTarget.prototype.addEventListener, remove = EventTarget.prototype.removeEventListener;
    const capture = options => Boolean(typeof options === 'object' ? options?.capture : options);
    const index = (type, fn, options) => live.findIndex(row => row.type === type && row.fn === fn && row.capture === capture(options));
    EventTarget.prototype.addEventListener = function (type, fn, options) {
        if (this === document && type.startsWith('pointer') && /project_handoff\\.js/.test(new Error().stack)
            && index(type, fn, options) < 0) live.push({type, fn, capture: capture(options)});
        return add.call(this, type, fn, options);
    };
    EventTarget.prototype.removeEventListener = function (type, fn, options) {
        const at = this === document ? index(type, fn, options) : -1;
        if (at >= 0) live.splice(at, 1);
        return remove.call(this, type, fn, options);
    };
    window.handoffListeners = () => live.map(row => `${row.type}${row.capture ? ':capture' : ''}`).sort();
})();"""
_PATH = ["pointercancel:capture", "pointermove:capture", "pointerup:capture"]


def test_a_press_begun_on_a_control_or_text_or_gone_astray_never_navigates(project_chat, tmp_path):
    """A press released three pixels off Copy used to reach the card as a free-area click, and a
    drag out and back was judged only by where it ended (review rl-20261012T144241-d2842d826b1f)."""
    page, expect = project_chat["page"], project_chat["expect"]
    engine, theme, width = (project_chat[key] for key in ("engine", "theme", "width"))
    page.add_init_script(_HANDOFF_DOCUMENT_LISTENERS)
    page.reload()
    transfer = page.locator('[data-system-type="project_handoff"]')
    expect(transfer.locator('.chat-live-phase')).to_have_text("Working")
    page.evaluate("window.copiedText = ''")
    trace = []

    def edges(locator):
        box = locator.bounding_box()
        return box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]

    def hit(point, selector):
        return page.evaluate("([x, y, s]) => Boolean(document.elementFromPoint(x, y)?.matches(s))", [*point, selector])

    def gesture(label, *points, steps=1):
        before = page.evaluate("openCount")
        page.mouse.move(*points[0])
        page.mouse.down()
        armed = page.evaluate("handoffListeners()")
        for point in points[1:]:
            page.mouse.move(*point, steps=steps)
        page.mouse.up()
        trace.append({"gesture": label, "points": points, "opened": page.evaluate("openCount") - before,
                      "selection": page.evaluate("String(getSelection())"), "armed": armed,
                      "after": page.evaluate("handoffListeners()")})

    left, top, right, bottom = edges(transfer)
    free = (left + 16, bottom - 6)  # in the padding, clear of the rounded corner (which takes no pointer events)
    free_area = '.project-handoff, .project-handoff-heading, .project-handoff-footer'
    feed_top = page.locator('#chat-messages').bounding_box()["y"]
    c_left, c_top, c_right, c_bottom = edges(transfer.locator('.chat-message-copy'))
    r_left, r_top, r_right, r_bottom = edges(transfer.locator('[data-intent="open-project"]'))
    t_left, t_top, t_right, t_bottom = edges(transfer.locator('.project-handoff-title'))
    # Three pixels off a control or text, the browser sends the click to the card's free area.
    origins = (
        ("copy", (c_right - 1, (c_top + c_bottom) / 2), (c_right + 2, (c_top + c_bottom) / 2), '.chat-message-copy, .chat-message-copy *'),
        ("reference", (r_left + 1, (r_top + r_bottom) / 2), (r_left - 2, (r_top + r_bottom) / 2), '[data-intent], [data-intent] *'),
        ("title", (t_right - 1, (t_top + t_bottom) / 2), (t_right + 2, (t_top + t_bottom) / 2), '.project-handoff-title'),
    )
    assert hit(free, free_area)
    assert not hit((free[0], feed_top - 8), '#chat-messages, #chat-messages *')
    for label, start, end, origin in origins:
        assert hit(start, origin) and hit(end, free_area), label
    assert page.evaluate("handoffListeners()") == [], "nothing is followed until a press arms"

    # An ordinary click wobbles a few pixels and still opens its Project.
    gesture("wobble", free, (free[0] + 4, free[1]), (free[0] + 2, free[1] + 1))
    # Out and back to where it began is a drag: a hundred pixels beyond the card, or past the
    # feed in one jump each way, where only the document sees the pointer at all.
    gesture("out-and-back", free, (free[0], free[1] + 100), free, steps=10)
    gesture("beyond-feed", free, (free[0], feed_top - 8), free)
    # A press begun on a control or on text stays theirs: never a second way to navigate.
    for label, start, end, _ in origins:
        gesture(label, start, end)
    # Afterwards a plain press on free area still opens.
    gesture("plain", free)
    copied = page.evaluate("copiedText")
    evidence = _evidence_dir(tmp_path)
    suffix = f"{engine}-{theme}-{width}"
    (evidence / f"project-press-{suffix}.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    transfer.screenshot(path=str(evidence / f"project-press-{suffix}.png"), animations="disabled")

    for row in trace:
        assert not row["selection"].strip(), ("no case may lean on a selection", row)
    assert {row["gesture"]: row["opened"] for row in trace} == {
        "wobble": 1, "out-and-back": 0, "beyond-feed": 0, "copy": 0, "reference": 0, "title": 0, "plain": 1}
    assert copied == "", "Copy released off itself copies nothing"
    for row in trace:
        # Only a press armed on free area follows its pointer, and every release lets it go.
        assert row["armed"] == ([] if row["gesture"] in ("copy", "reference", "title") else _PATH), row
        assert row["after"] == [], row
    if width < 980:
        before = page.evaluate("openCount")
        page.touchscreen.tap(*free)
        page.wait_for_function("n => openCount === n", arg=before + 1)
        if engine == "chromium":
            # A finger wobbles too, followed under its own pointer identity. A second tap inside
            # the double-tap time is a repeat (detail 2), which never navigates: let it pass.
            page.wait_for_timeout(1000)
            cdp = page.context.new_cdp_session(page)
            for kind, points in (("touchStart", [free]), ("touchMove", [(free[0] + 3, free[1])]), ("touchEnd", [])):
                cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [{"x": x, "y": y} for x, y in points]})
            page.wait_for_function("n => openCount === n", arg=before + 2)
        assert page.evaluate("handoffListeners()") == []
    # Destroyed mid-press: the release navigates nowhere and the document keeps no listener of it.
    page.mouse.move(*free)
    page.mouse.down()
    assert page.evaluate("handoffListeners()") == _PATH
    before = page.evaluate("openCount")
    page.evaluate("chat.destroy()")
    assert page.evaluate("handoffListeners()") == []
    page.mouse.up()
    assert page.evaluate("openCount") == before
