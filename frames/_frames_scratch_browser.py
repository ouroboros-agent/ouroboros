"""SCRATCH (lane FRAMES, audit1103 tier-1 PR; never committed): before/after frames of the PR's owner-visible lines.

The same module runs unchanged in the base and the candidate checkout. Every state is produced by THIS checkout's own
code wherever a producer exists: the real loop and terminal writer (an author's ``finish_task(action=stop)``), the real
``pr_merge`` tool over a fake ``gh`` and the real review ledger, the real supervisor park of a model sleep, the real
``server._run_supervisor`` boot path, the real task-result writer and terminal projection; rows reach the page through
this checkout's own history reader (``make_chat_history_endpoint``) and task-detail route (``api_task_get``) over a
disposable data root, the census through this checkout's own server work projection
(``tests/_work_projection_shared.work_fields`` via ``tests/test_room_work_browser.Transport``). The page is the real SPA
of this checkout (``subscription_ui``) in the bundled Chromium, light theme, ``ru-RU`` and Moscow time, 1280x800 and
390x844. Frames go to ``frames/{base|cand}/<NN>-<name>-<width>.png`` with the words each frame shows beside them as JSON.
Data that no producer writes is placed by hand with the real writer's fields; each such place says so below.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tests import test_pr_merge_receipts as merge_fixture
from tests import test_room_work_browser as rw
from tests import test_subscription_setup_browser as setup_browser

subscription_ui = setup_browser.subscription_ui
world = merge_fixture.world
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

REPO = Path(__file__).resolve().parents[1]
SIDE = os.environ.get("FRAMES_SIDE") or {"frames-base": "base", "frames-cand": "cand"}.get(REPO.name, "other")
ROOT = Path(os.environ.get("FRAMES_OUT", "/Users/anton/.claude/sprints/audit1103-impl-20261104/frames"))
OUT = ROOT / SIDE
DEBUG = ROOT / "_debug" / SIDE
SIZES = {1280: (1280, 800), 390: (390, 844)}
WIDTHS = list(SIZES)
# Other agents' batteries load this Mac heavily: every page wait gets minutes, never a different outcome.
WAIT_S = 120
WAIT_MS = WAIT_S * 1000
PROJECT = {"id": "fr-proj", "name": "Export service", "chat_id": 42, "lifecycle": "active", "visible_revision": 0}


# One afternoon of 4 Nov 2026 (UTC); the browser says it in this machine's clock (MSK, +3).
def _ts(minute, second=0):
    return f"2026-11-04T14:{minute:02d}:{second:02d}Z"


def _epoch(minute):
    return int(datetime(2026, 11, 4, 14, minute, tzinfo=timezone.utc).timestamp())


def dump(name, value):
    DEBUG.mkdir(parents=True, exist_ok=True)
    (DEBUG / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


# ------------------------------------------------------------------------------------------------
# This checkout's own readers over a disposable data root.

def history_page(root, chat_id):
    """``/api/chat/history?chat_id=…`` as this checkout's gateway answers it (``gateway/history.py``)."""
    from ouroboros.gateway.history import make_chat_history_endpoint

    return json.loads(asyncio.run(make_chat_history_endpoint(Path(root))(
        SimpleNamespace(query_params={"chat_id": str(chat_id)}))).body)


def task_detail(root, task_id):
    """``GET /api/tasks/<id>`` through this checkout's own route (``gateway/tasks.api_task_get``)."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros.gateway.tasks import api_task_get

    app = Starlette(routes=[Route("/api/tasks/{task_id}", endpoint=api_task_get, methods=["GET"])])
    app.state.drive_root = Path(root)
    with TestClient(app) as client:
        response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, (task_id, response.status_code, response.text[:400])
    return response.json()


def shown_in(page, chat_to, *, chat_from=None, times=None):
    """The page as this room's feed reads it: rows of ``chat_from`` shown in ``chat_to`` (a display remap, disclosed),
    and the run's own clock on rows the producer stamped replaced by one afternoon's time (``times``: id -> ts)."""
    page = json.loads(json.dumps(page))
    for key in ("messages", "progress"):
        for row in page.get(key) or []:
            if chat_from is not None and row.get("chat_id") == chat_from:
                row["chat_id"] = chat_to
            for stamp, ts in (times or {}).items():
                if stamp(row):
                    row["ts"] = ts
    return page


def owner_row(root, chat_id, text, cmid, minute):
    """The owner's message as the ingress stores it (``b4._seed_roots``' row: direction in + client id)."""
    from ouroboros import dialogue_store

    dialogue_store.append(Path(root), "chat", {"ts": _ts(minute), "direction": "in", "chat_id": chat_id,
                                               "text": text, "client_message_id": cmid})


def reply_row(root, chat_id, text, minute, second=0):
    """An ordinary answer as ``send_with_budget`` stores it (direction out, narration)."""
    from ouroboros import dialogue_store

    dialogue_store.append(Path(root), "chat", {"ts": _ts(minute, second), "direction": "out", "chat_id": chat_id,
                                               "text": text, "format": "", "narration": True})


def narration_row(root, chat_id, task_id, text, minute, **meta):
    """A task's progress line as ``send_with_budget(is_progress=True)`` stores it (``supervisor/message_bus.py``)."""
    from ouroboros import dialogue_store

    dialogue_store.append(Path(root), "progress", {
        "ts": _ts(minute), "type": "send_message", "task_id": task_id, "is_progress": True, "direction": "out",
        "chat_id": chat_id, "user_id": 0, "text": text, "content": text, "format": "", "role": "assistant",
        "system_type": "", "narration": True, **meta})


def terminal_fields(usage, *, title, metadata=None, task_contract=None):
    """This checkout's ``task_finalization.terminal_result_fields``; the candidate also takes the task's metadata."""
    from ouroboros.task_finalization import terminal_result_fields

    accepted = inspect.signature(terminal_result_fields).parameters
    extra = {key: value for key, value in (("metadata", metadata), ("task_contract", task_contract)) if key in accepted}
    return terminal_result_fields(usage, title=title, **extra)


APPENDED = {}  # task id -> how its terminal row was written ("terminal_projection" or the disclosed fallback)


def append_terminal(root, task_id, minute):
    """The finished root's own chat row through ``terminal_projection.append_terminal_projection`` (its readiness, the
    one chat writer seam, its receipt). Only if that refuses is ``_project_row`` appended directly (disclosed). The
    producer's clock on the row is replaced in the page with ``_ts(minute)`` (``fix_terminal_time``)."""
    from ouroboros import project_dialogue as dialogue
    from ouroboros import terminal_projection as tp
    from ouroboros.task_results import load_task_result

    stored = load_task_result(Path(root), task_id)
    task = {"id": task_id, "type": "task", "chat_id": stored.get("chat_id", 1), "root_task_id": task_id,
            "delegation_role": "root"}
    if tp.append_terminal_projection(Path(root), task_id, task, {"chat_id": stored.get("chat_id", 1)}):
        APPENDED[task_id] = "terminal_projection.append_terminal_projection"
        return minute
    row = tp._project_row(task_id, stored, {}, {"chat_id": stored.get("chat_id", 1)})
    row["ts"] = _ts(minute)
    assert dialogue.append_canonical_task_summary(Path(root), row)
    APPENDED[task_id] = "fallback: _project_row + append_canonical_task_summary (no readiness receipt)"
    return minute


def fix_terminal_time(page, task_id, minute):
    """The run's own clock on a terminal row (``ts``, ``terminal_time.occurred_at``) as one afternoon's time."""
    page = json.loads(json.dumps(page))
    for row in page.get("messages") or []:
        if row.get("task_id") == task_id and row.get("system_type") == "task_summary":
            row["ts"] = _ts(minute)
            if isinstance(row.get("terminal_time"), dict):
                row["terminal_time"]["occurred_at"] = _ts(minute)
    return page


# ------------------------------------------------------------------------------------------------
# The page.

class FrameTransport(rw.Transport):
    """``rw.Transport`` answering history with this checkout's own pages, task details with its own route's answers,
    the dashboard log backfill and the census's ``task_bindings``."""

    def __init__(self, page, *, pages=None, bindings=None, logs=None, cursors=None, **kwargs):
        self.pages = dict(pages or {})
        self.bindings = dict(bindings or {})
        self.logs = dict(logs or {})
        self.cursors = dict(cursors or {})  # cursor -> this checkout's own page for that follow-up read
        self.history_reads = []
        self.cursor_misses = []
        super().__init__(page, **kwargs)
        page.route("**/api/logs/*", self._logs)

    def body(self, *, ready=True):
        return {**super().body(ready=ready), "task_bindings": self.bindings}

    def _history(self, route):
        query = parse_qs(urlparse(route.request.url).query)
        chat_id = int(query.get("chat_id", ["1"])[0])
        cursor = query.get("cursor", [""])[0]
        self.history_reads.append((chat_id, bool(cursor)))
        if cursor and cursor in self.cursors:
            route.fulfill(json=self.cursors[cursor])
            return
        if cursor:
            self.cursor_misses.append(cursor[:40])
        if query.get("around", [""])[0] or chat_id not in self.pages:
            if not query.get("around", [""])[0]:
                rows = self.history.get(chat_id, [])
                end = 512 * max(1, len(rows))
                route.fulfill(json={"messages": rows, "progress": [], "has_more": False, "next_cursor": None,
                                    "page_cursor": f"frames-page:{chat_id}", "window": {"complete": True,
                                                                                      "truncated_by": []},
                                    "coverage": {"v": 1, "view": "frames", "upper": {"chat": end, "progress": 0},
                                                 "spans": {"chat": {"from": 0, "to": end, "chain": "frames",
                                                                    "gaps": []},
                                                           "progress": {"from": 0, "to": 0, "chain": "empty",
                                                                        "gaps": []}}}})
                return
            super()._history(route)
            return
        route.fulfill(json=self.pages[chat_id])

    def _logs(self, route):
        name = urlparse(route.request.url).path.rsplit("/", 1)[-1]
        route.fulfill(json={"entries": self.logs.get(name, [])})

    def refresh(self, deadline=WAIT_S):
        """``rw.Transport.refresh`` (one page-wide census read through the app's own ``projects_changed`` path), with
        a deadline for a machine under other agents' load."""
        import time

        before = self.state_reads
        self.send({"type": "projects_changed"})
        end = time.monotonic() + deadline
        while self.state_reads <= before and time.monotonic() < end:
            self.page.wait_for_timeout(60)
        assert self.state_reads > before, "the census read never happened"
        self.page.wait_for_timeout(250)
        rw.settle_frames(self.page)


def frame_path(name, width):
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / f"{name}-{width}.png"


def shot(page, name, width, facts=None, words=None):
    """The frame, then the words it shows: ``words()`` is read again right AFTER the screenshot (what the PNG holds);
    the reading just before it is kept too, and ``words_changed_during_shot`` says whether the screen moved."""
    rw.settle_frames(page)
    page.wait_for_timeout(300)
    before = words() if words else None
    page.screenshot(path=str(frame_path(name, width)), animations="disabled")
    after = words() if words else {}
    record = {**(facts or {}), **after}
    if words:
        record["words_before_shot"] = before
        record["words_changed_during_shot"] = before != after
    (OUT / f"{name}-{width}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str),
                                              encoding="utf-8")
    return after


def new_page(ui, width):
    """A page as the owner's desktop app shows it here: light, this Mac's locale (ru_RU) and clock (MSK)."""
    w, h = SIZES[width]
    page = ui["new_page"](viewport={"width": w, "height": h}, color_scheme="light", locale="ru-RU",
                          timezone_id="Europe/Moscow")
    page.set_default_timeout(WAIT_MS)
    page.set_default_navigation_timeout(3 * WAIT_MS)
    ui["errors"].watch(page)
    return page


def open_room(ui, transport, width):
    """``rw.open_app`` with this machine's deadlines: the real SPA, light theme, the census answered by the transport."""
    import time

    page = transport.page
    origin = urlparse(ui["url"]).netloc
    page.route("**/*", lambda route: route.fallback() if urlparse(route.request.url).netloc == origin else route.abort())
    page.add_init_script("localStorage.setItem('ouroboros.theme', 'light')")
    page.add_init_script(rw._CLIPBOARD)
    page.goto(ui["url"] + "/")
    page.wait_for_selector("#page-chat .rw-trigger", state="attached")
    end = time.monotonic() + WAIT_S
    while not transport.sockets and time.monotonic() < end:
        page.wait_for_timeout(100)
    assert transport.sockets, "the page never opened its socket"
    transport.refresh()
    return page


def each_width(run):
    """Run one state at both widths in one browser; a failed width is reported after the other one ran."""
    failures = []
    for width in WIDTHS:
        try:
            run(width)
        except Exception as exc:  # noqa: BLE001 — collected and raised below
            failures.append((width, f"{type(exc).__name__}: {exc}"))
    assert not failures, failures


def card_of(page, task_id, feed="#chat-messages"):
    return page.locator(f'{feed} .chat-live-card[data-task-id="{task_id}"]')


def expand(card):
    if card.get_attribute("data-expanded") != "1" and card.locator("[data-live-disclosure]").count():
        card.locator("[data-live-disclosure]").first.click()


def center(locator):
    locator.evaluate("e => e.scrollIntoView({block: 'center'})")


def card_words(card):
    def text(selector):
        node = card.locator(selector)
        return node.first.inner_text().strip() if node.count() else ""
    return {"word": text(":scope > [data-live-summary-button] [data-live-phase]") or text("[data-live-phase]"),
            "line": text("[data-live-phase-secondary]"), "title": text("[data-live-title]"),
            "activity": text("[data-live-activity]"), "continue": text("[data-continue-task]"),
            "continue_title": (card.locator("[data-continue-task]").first.get_attribute("title")
                               if card.locator("[data-continue-task]").count() else ""),
            "card": card.inner_text().strip()}


ROW_TEXT = """([scope, id]) => { const root = scope === 'main' ? document.querySelector('#page-chat')
        : document.querySelector('#project-panel');
    const row = root?.querySelector(`.rw-row[data-id="${id}"]`);
    const text = (sel) => (row?.querySelector(sel)?.textContent || '').replace(/\\s+/g, ' ').trim();
    const shown = (sel) => { const n = row?.querySelector(sel); return Boolean(n) && !n.hidden; };
    return { name: text('.rw-name'), word: text('.rw-word'), secondary: shown('.rw-secondary') ? text('.rw-secondary') : '',
             focus: shown('.rw-focus') ? text('.rw-focus') : '', settled: row?.dataset.settled === '1',
             group: row?.closest('.rw-group-block, section')?.querySelector('.rw-group')?.textContent || '',
             row: (row?.textContent || '').replace(/\\s+/g, ' ').trim() }; }"""


def row_words(page, scope, work_id):
    return page.evaluate(ROW_TEXT, [scope, work_id])


# ------------------------------------------------------------------------------------------------
# 1. A live root its author stopped: finish_task(action="stop", answer, rationale) through the REAL loop and terminal
#    writer (tests/test_continue_author_stop._stored_author_end), shown through this checkout's own history reader.

STOP_ID = "stop-root"
STOP_NAME = "Export endpoint"


def _terminal_row(row):
    return row.get("task_id") == STOP_ID and row.get("system_type") == "task_summary"


def author_stop_world(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result, write_task_result
    from tests.test_acceptance_author_stop import _stop_tool_call
    from tests.test_continue_author_stop import _stored_author_end

    tmp_path.mkdir(parents=True, exist_ok=True)
    data, stored = _stored_author_end(tmp_path, monkeypatch, [_stop_tool_call()])
    before = history_page(data, 7)
    # The name the admission namer stores on a root (``suggested_name``): the helper's root has none.
    write_task_result(data, STOP_ID, stored["status"], suggested_name=STOP_NAME)
    owner_row(data, 7, "Ship the export endpoint.", "m-1", 0)
    narration_row(data, 7, STOP_ID, "Checking the export endpoint against empty input.", 2)
    page = history_page(data, 7)
    appended = None
    if not [row for row in page["messages"] if _terminal_row(row)]:
        appended = append_terminal(data, STOP_ID, 6)
        page = history_page(data, 7)
    stored = load_task_result(data, STOP_ID)
    detail = task_detail(data, STOP_ID)
    late = {row.get("history_id") or row.get("summary_id") or "" for row in page["messages"] if _terminal_row(row)}
    shown = shown_in(page, 1, chat_from=7, times={_terminal_row: _ts(6)})
    for row in shown["messages"]:
        if _terminal_row(row) and isinstance(row.get("terminal_time"), dict):
            row["terminal_time"]["occurred_at"] = _ts(6)
    unchanged = [{key: row.get(key) for key in ("text", "outcome_phase", "continuation_offer")}
                 for row in before["messages"] if _terminal_row(row)] == [
                {key: row.get(key) for key in ("text", "outcome_phase", "continuation_offer")}
                for row in page["messages"] if _terminal_row(row)]
    facts = {"stored": {key: stored.get(key) for key in ("status", "reason_code", "title", "suggested_name",
                                                          "outcome_axes", "canonical_terminal_projection",
                                                          "chat_id")},
             "appended_by_frames": bool(appended), "summary_rows": sorted(late),
             "terminal_row_unchanged_by_name": unchanged,
             "history_terminal": [{key: row.get(key) for key in ("system_type", "text", "outcome_phase", "reason_detail",
                                                                  "continuation_offer", "task_terminal_status", "ts")}
                                  for row in page["messages"] if row.get("task_id") == STOP_ID],
             "detail": {key: detail.get(key) for key in ("status", "reason_code", "continuation_offer")}}
    return shown, detail, facts


def history_after(root, chat_id, cursor):
    """The page's own follow-up read (``cursor=…``, kind ``after``) as this checkout's history reader answers it."""
    from ouroboros.gateway.history import make_chat_history_endpoint

    return json.loads(asyncio.run(make_chat_history_endpoint(Path(root))(
        SimpleNamespace(query_params={"chat_id": str(chat_id), "cursor": cursor}))).body)


TRANSPLANT_DROP = ("task_id", "status", "chat_id", "canonical_terminal_projection", "canonical_terminal_projection_ready",
                   "canonical_terminal_projection_origin", "_schema_version")


def author_stop_room(tmp_path, monkeypatch):
    """One room that lives through the stop: the owner's message, the root's narration and its RUNNING record, then
    the REAL loop's stored stop (``_stored_author_end`` in its own root, its record written into this room's root by
    the real writer, chat 1) and its own terminal row (``append_terminal_projection``). Pages: before the end, the
    page's follow-up read after the end (with the cursor the first page gave), and a fresh read (a reload)."""
    from ouroboros.task_results import load_task_result, write_task_result
    from tests.test_acceptance_author_stop import _stop_tool_call
    from tests.test_continue_author_stop import _stored_author_end

    source = tmp_path / "loop"
    source.mkdir(parents=True)
    _data, stored = _stored_author_end(source, monkeypatch, [_stop_tool_call()])
    room = tmp_path / "room"
    room.mkdir(parents=True)
    owner_row(room, 1, "Ship the export endpoint.", "m-1", 0)
    narration_row(room, 1, STOP_ID, "Checking the export endpoint against empty input.", 2)
    running_root(room, STOP_ID, chat_id=1, title=STOP_NAME)
    live = history_page(room, 1)
    live_detail = task_detail(room, STOP_ID)
    fields = {key: value for key, value in stored.items() if key not in TRANSPLANT_DROP}
    write_task_result(room, STOP_ID, stored["status"], chat_id=1, **{**fields, "suggested_name": STOP_NAME,
                                                                     "title": STOP_NAME})
    append_terminal(room, STOP_ID, 6)
    cursors = follow_ups(room, 1, live, STOP_ID, 6)
    reload = fix_terminal_time(history_page(room, 1), STOP_ID, 6)
    end = load_task_result(room, STOP_ID)
    return {"live": live, "live_detail": live_detail, "cursors": cursors, "reload": reload,
            "detail": task_detail(room, STOP_ID), "stored": end, "appended": APPENDED.get(STOP_ID),
            "source_stored": {key: stored.get(key) for key in ("status", "reason_code", "outcome_axes")}}


def follow_ups(root, chat_id, first, task_id=None, minute=None, depth=3):
    """The page's arrivals reads after ``first`` (its ``after_cursor`` chain) as this checkout's reader answers them
    now, keyed by cursor for ``FrameTransport``."""
    cursors, page = {}, first
    for _ in range(depth):
        cursor = page.get("after_cursor")
        if not cursor or cursor in cursors:
            break
        page = history_after(root, chat_id, cursor)
        if task_id:
            page = fix_terminal_time(page, task_id, minute)
        cursors[cursor] = page
    return cursors


def rows_brief(page):
    return [{key: row.get(key) for key in ("history_id", "system_type", "is_progress", "task_terminal_status",
                                           "outcome_phase", "text") if key in row}
            for row in (page or {}).get("messages") or []]


def test_97_probe_author_stop_room(tmp_path, monkeypatch):
    import base64
    world_ = author_stop_room(tmp_path, monkeypatch)

    def decode(cursor):
        try:
            return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        except Exception:  # noqa: BLE001
            return cursor
    dump("97-author-stop-room", {
        "appended": world_["appended"], "source_stored": world_["source_stored"],
        "cursors": {key: decode(world_["live"].get(key) or "") for key in ("page_cursor", "next_cursor")},
        "live": rows_brief(world_["live"]),
        "after": {cursor[:24]: rows_brief(page) for cursor, page in world_["cursors"].items()},
        "reload": rows_brief(world_["reload"]),
        "detail": {key: world_["detail"].get(key) for key in ("status", "reason_code", "continuation_offer")},
        "stored_phase": {key: world_["stored"].get(key) for key in ("status", "reason_code", "suggested_name")}})


# ------------------------------------------------------------------------------------------------
# 2./3. A finished root with its closing sentence (finish_task(summary=…)) through the real task-result writer
#       (``terminal_result_fields``) and terminal projection: the room's Work list (Main, a Project's Latest task)
#       and Main's portal of a Project's work.

CLOSE_ID, CLOSE_NAME = "fr-close", "Fix the export of empty tables"
CLOSE_FOCUS = "Export · checking the empty-table case"
SENTENCE = "Empty tables now export as a header-only CSV; the regression test passes."
PROJ_ID, PROJ_NAME = "fr-proj-run", "Rebuild the export index"
PROJ_SENTENCE = "The export index is rebuilt and every link resolves."
PORTAL_ID, PORTAL_NAME = "fr-portal", "Migrate the export settings"
PORTAL_SENTENCE = "The export settings moved to v3; nothing needed you."
PORTAL_LINE = "Copying the export presets into the v3 store."


def finished_root(root, task_id, *, chat_id, title, sentence, answer, project_id=""):
    """A clean finish of an ordinary root that wrote a closing sentence, as this checkout's writer stores it."""
    from ouroboros import outcomes
    from ouroboros.task_results import write_task_result

    usage = {"wake_summary": sentence}
    fields = terminal_fields(usage, title=title, metadata={})
    write_task_result(Path(root), task_id, "completed", chat_id=chat_id, root_task_id=task_id, delegation_role="root",
                      title=title, suggested_name=title, result=answer, terminal_origin="model_final",
                      _terminal_observed=True, **({"project_id": project_id} if project_id else {}),
                      **outcomes.derive_loop_outcome(answer, {}, {}), **fields)
    return fields


def running_root(root, task_id, *, chat_id, title, project_id=""):
    """The canonical RUNNING record a dispatched root has while it works (``write_task_result``; the admission namer's
    ``suggested_name`` beside its title)."""
    from ouroboros.task_results import write_task_result

    write_task_result(Path(root), task_id, "running", chat_id=chat_id, root_task_id=task_id, delegation_role="root",
                      title=title, suggested_name=title, result="Task is running.",
                      **({"project_id": project_id} if project_id else {}))


def closing_world(tmp_path):
    """Main's own root (live, then finished) and a Project root (finished), with their closing sentences."""
    from ouroboros.projects_registry import create_project

    data = tmp_path / "data"
    data.mkdir(parents=True)
    project = create_project(data, PROJECT["id"], name=PROJECT["name"])
    chat = int(project["chat_id"])
    # Main: the owner's request and the work's narration (its live card), then its finish.
    owner_row(data, 1, "Please fix the export of empty tables and tell me when it is done.", "fr-ask-close", 0)
    narration_row(data, 1, CLOSE_ID, "Reproducing the empty-table export.", 2, suggested_name=CLOSE_NAME)
    running_root(data, CLOSE_ID, chat_id=1, title=CLOSE_NAME)
    live_main, live_detail = history_page(data, 1), task_detail(data, CLOSE_ID)
    main_fields = finished_root(data, CLOSE_ID, chat_id=1, title=CLOSE_NAME, sentence=SENTENCE,
                                answer="Fixed: empty tables export as a header-only CSV.")
    append_terminal(data, CLOSE_ID, 9)
    done_main = fix_terminal_time(history_page(data, 1), CLOSE_ID, 9)
    main_cursors = follow_ups(data, 1, live_main, CLOSE_ID, 9)
    # The Project's own room: its request, its narration, its finish.
    owner_row(data, chat, "Rebuild the export index for the archive.", "fr-ask-proj", 1)
    narration_row(data, chat, PROJ_ID, "Rebuilding the export index.", 3, suggested_name=PROJ_NAME)
    proj_fields = finished_root(data, PROJ_ID, chat_id=chat, title=PROJ_NAME, sentence=PROJ_SENTENCE,
                                answer="The export index is rebuilt.", project_id=PROJECT["id"])
    append_terminal(data, PROJ_ID, 8)
    done_project = fix_terminal_time(history_page(data, chat), PROJ_ID, 8)
    return {"data": data, "chat": chat, "project": project, "live_main": live_main, "live_detail": live_detail,
            "main_cursors": main_cursors,
            "done_main": done_main, "done_project": done_project, "main_detail": task_detail(data, CLOSE_ID),
            "proj_detail": task_detail(data, PROJ_ID), "fields": {"main": main_fields, "project": proj_fields},
            "appended": {key: APPENDED.get(key) for key in (CLOSE_ID, PROJ_ID)}}


def portal_world(tmp_path, *, failed=False):
    """A Main request whose work runs in a Project room (Main shows its portal), then its finish."""
    from ouroboros import dialogue_store
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    data = tmp_path / "data"
    data.mkdir(parents=True)
    project = create_project(data, PROJECT["id"], name=PROJECT["name"])
    chat = int(project["chat_id"])
    owner_row(data, 1, "Move the export settings to the v3 store; run it as a project.", "fr-ask-portal", 0)
    # The Main row that binds the card to its room (gateway's ``project_started`` row, as the work card test seeds it).
    dialogue_store.append(data, "chat", {"ts": _ts(0, 20), "direction": "system", "chat_id": 1, "type": "project_started",
                                         "task_id": PORTAL_ID, "project_id": PROJECT["id"],
                                         "project_name": PROJECT["name"], "task_name": PORTAL_NAME,
                                         "text": f"{PROJECT['name']} › {PORTAL_NAME} · Started"})
    running_root(data, PORTAL_ID, chat_id=chat, title=PORTAL_NAME, project_id=PROJECT["id"])
    live, live_detail = history_page(data, 1), task_detail(data, PORTAL_ID)
    if failed:
        write_task_result(data, PORTAL_ID, "failed", chat_id=chat, root_task_id=PORTAL_ID, delegation_role="root",
                          title=PORTAL_NAME, project_id=PROJECT["id"],
                          error="The v3 store refused the presets: schema version 2 is required.",
                          reason_code="task_exception", **terminal_fields({"wake_summary": PORTAL_SENTENCE},
                                                                          title=PORTAL_NAME, metadata={}))
    else:
        finished_root(data, PORTAL_ID, chat_id=chat, title=PORTAL_NAME, sentence=PORTAL_SENTENCE,
                      answer="The export settings are in the v3 store.", project_id=PROJECT["id"])
    census_done = history_page(data, 1)
    census_cursors = follow_ups(data, 1, live)
    row, frame, outcome = main_completion_row(data, PORTAL_ID)
    done = history_page(data, 1)
    done_cursors = follow_ups(data, 1, live)
    reload_cursors = follow_ups(data, 1, done)
    return {"data": data, "chat": chat, "project": project, "live": live, "live_detail": live_detail,
            "census_done": census_done, "census_cursors": census_cursors, "done": done, "done_cursors": done_cursors,
            "reload_cursors": reload_cursors,
            "frame": frame, "row": row, "outcome": outcome, "detail": task_detail(data, PORTAL_ID)}


def main_completion_row(root, task_id, minute=9):
    """Main's row for a finished Project root, composed by this checkout's own
    ``project_dialogue.project_completion_delivery_outcome`` (its terminal-delivery outbox replaced by a capture of
    the event), stored the way the delivery's ``send_with_budget`` stores it (``message_ingress.log_chat``), and the
    live frame the bridge broadcasts for it (``LocalChatBridge.send_message``)."""
    import pytest as _pytest
    from ouroboros import project_dialogue as dialogue
    from ouroboros.task_results import load_task_result
    from supervisor.message_ingress import log_chat

    result = load_task_result(Path(root), task_id)
    task = {"id": task_id, "type": "task", "chat_id": result.get("chat_id"), "root_task_id": task_id,
            "delegation_role": "root", "project_id": result.get("project_id", "")}
    done_event = {"type": "task_done", "task_id": task_id, "status": result["status"],
                  "outcome_axes": result.get("outcome_axes") or {}, "reason_code": result.get("reason_code") or "",
                  "chat_id": result.get("chat_id")}
    captured = []
    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(dialogue, "_enqueue_main_completion",
                      lambda drive_root, tid, event, event_queue=None: (captured.append(event) or ("owed", False)))
        outcome = dialogue.project_completion_delivery_outcome(Path(root), done_event, task_id, task, result,
                                                               done_event)
    if not captured:
        return None, None, outcome
    event = captured[0]
    meta = {**(event.get("progress_meta") or {}), "narration": False, "delivery_id": event.get("delivery_id", "")}
    stored = log_chat("system", 1, 0, event["text"], ts=_ts(minute), fmt="", source="", task_id=task_id,
                      record_type=event.get("system_type", ""), message_meta=meta, drive_root=Path(root), feed=True)
    frame = {"type": "chat", "role": "system", "content": event["text"], "markdown": False, "is_progress": False,
             "ts": _ts(minute), "task_id": task_id, "chat_id": 1, "system_type": event.get("system_type", ""), **meta}
    return stored, frame, outcome


# ------------------------------------------------------------------------------------------------
# 4. A merged PR whose review later read FAIL: the real pr_merge (fake gh) bound to a review record that settles FAIL
#    after the merge (tests/test_merged_late_review.py's late_fail case), shown through this checkout's history reader.

MERGE_NAME = "Merge the export pull request #7"


def _record(root, task_id, record_id="late-merge-review", *, head=merge_fixture.HEAD):
    from ouroboros import review_ledger

    return review_ledger.write_record(root, review_ledger.ReviewLedgerRecord(
        record_id=record_id, task_id=task_id, state="pending",
        subject={"kind": "base..head", "head": head, "base": merge_fixture.BASE},
        verdict={"aggregate": "NOT_PERFORMED"}, panel={"seats": [{"seat_id": "critic"}]}))


def _fail(root, record):
    from ouroboros import review_ledger

    source = review_ledger.retain_text_source(root, record["task_id"], record_id=record["record_id"], seat_id="critic",
                                              role="response", part="change",
                                              text="# Review\nFAIL: the rollback loses data.\nREPORT-END")
    return review_ledger.revise_record(root, record["record_id"], lambda now: {
        **now, "state": "settled", "verdict": {"aggregate": "FAIL", "per_row": {"critic": "FAIL"}},
        "rows": [{"seat_id": "critic", "status": "responded", "source_refs": [source]}]})


def late_review_world(world):
    from ouroboros import dialogue_store

    owner_row(world.root, 7, "Merge the export pull request #7 once its review passes.", "fr-merge-0", 0)
    record = _record(world.root, "merge-task")
    world.gh.merge = "ok"
    merge_fixture._merge(world, review_record_id=record["record_id"])
    event = world.ctx.pending_events[0]
    dialogue_store.append(world.root, "progress", {
        "type": "task_progress", "chat_id": 7, "task_id": "merge-task", "text": "Reading the PR.", "ts": _ts(2),
        "suggested_name": MERGE_NAME})
    dialogue_store.append(world.root, "progress", {**event, **event["progress_meta"], "type": "task_progress"})
    before = history_page(world.root, 7)
    _fail(world.root, record)
    after = history_page(world.root, 7)
    main_before, main_after = history_page(world.root, 1), None
    main_after = history_page(world.root, 1)
    receipts = [row for row in after["messages"] if row.get("card_row_id")]
    stamp = {(lambda row: bool(row.get("card_row_id"))): _ts(10), (lambda row: row is not None and row.get("text") == "Reading the PR."): _ts(2)}
    return {"before": before, "after": shown_in(after, 1, chat_from=7, times=stamp),
            "rows": {"before": [row.get("text") for row in before["messages"] if row.get("card_row_id")],
                     "after": [row.get("text") for row in receipts],
                     "late_evidence": [row.get("late_evidence") for row in receipts],
                     "room_rows_before": len(before["messages"]), "room_rows_after": len(after["messages"]),
                     "main_rows_before": len((main_before or {}).get("messages") or []),
                     "main_rows_after": len((main_after or {}).get("messages") or [])}}


# ------------------------------------------------------------------------------------------------
# 5. A model sleep parked by the REAL supervisor (``supervisor.events._handle_budget_pause`` over a sleep row, as
#    tests/test_budget_pause_exact_resume.py's sleep case drives it): the event it publishes and the result it writes.

SLEEP_ID = "pause-task"


def sleep_world(tmp_path, monkeypatch):
    from ouroboros import budget_pause
    from ouroboros.task_results import load_task_result
    from supervisor.events import _handle_budget_pause
    from tests._budget_pause_exact_helpers import _install_queue, _pause, _supervisor_ctx

    tmp_path.mkdir(parents=True, exist_ok=True)
    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    ctx, _limit, pause = _pause(tmp_path, monkeypatch, scope="root")
    pause.update(reason="sleep", rail="model_sleep")
    budget_pause.set_budget_pause(tmp_path, ctx.task_id, pause, expected_pause_id=pause["pause_id"])
    budget_pause.end_dispatch_fence(ctx.task_id)
    task = {"id": ctx.task_id, "type": "task", "chat_id": 1, "root_task_id": ctx.task_id, "_attempt": 1}
    worker = SimpleNamespace(busy_task_id=ctx.task_id)
    workers.RUNNING[ctx.task_id] = {"task": task, "worker_id": 0, "attempt": 1}
    workers.WORKERS[0] = worker
    persisted, pushed = [], []
    sctx = _supervisor_ctx(tmp_path, workers, queue, persisted, pushed)
    event = budget_pause.pause_event(task, pause)
    _handle_budget_pause({**event, "worker_id": 0}, sctx)
    result = load_task_result(tmp_path, ctx.task_id)
    events = []
    path = tmp_path / "logs" / "events.jsonl"
    if path.exists():
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    marker = json.loads(json.dumps((workers.PENDING[0] if workers.PENDING else {}).get("_budget_pause") or {},
                                   default=str))
    return {"pushed": pushed, "events": events, "marker": marker,
            "result": {key: result.get(key) for key in ("status", "reason_code", "result", "cost_final")}}


# ------------------------------------------------------------------------------------------------
# 6. The boot notice of a restart that restored work while two places stay paused: the REAL server._run_supervisor
#    boot path (tests/test_server_shutdown.py's harness), its line stored by the real chat writer (``log_chat``).

def place_rows():
    from ouroboros import places as p

    return {key: p.place_summary(p.Place(key, "custom-bridge", "custom", label=label, state=state))
            for key, label, state in [("a-research", "Research room", "paused"), ("b-support", "Support chat", "paused"),
                                      ("c-team", "Team room", "active"), ("d-news", "News feed", "silent")]}


def boot_world(tmp_path, monkeypatch):
    import server
    from ouroboros import places as p
    from tests.test_server_shutdown import _run, _supervisor_harness

    tmp_path.mkdir(parents=True, exist_ok=True)
    rec = _supervisor_harness(monkeypatch, tmp_path, ["stop"])
    path = p.places_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "revision": 4, "places": place_rows(),
                                "migration": {"presence_bindings_v1": True, "at": _ts(0), "problems": []}}),
                    encoding="utf-8")
    rec.restored_pending = 2
    rec.fenced_running = ["a", "b"]
    _run(rec, server)
    notices = [text for _chat, text in rec.alerts if text.startswith("♻️")]
    return notices, list(rec.alerts)


def boot_page(tmp_path, notice):
    from supervisor.message_ingress import log_chat

    data = tmp_path / "boot-data"
    data.mkdir(parents=True)
    owner_row(data, 1, "Please restart and then continue the release notes.", "fr-boot-0", 0)
    reply_row(data, 1, "Restarting now; the queued release work continues after the restart.", 0, 30)
    stored = log_chat("system", 1, 0, notice, ts=_ts(3), fmt="", source="", task_id="", record_type="startup_notice",
                      message_meta={"narration": False}, drive_root=data, feed=True)
    return history_page(data, 1), stored


# ------------------------------------------------------------------------------------------------
# Probe: every producer's output for this side, no browser (the page tests below re-run the same producers).

def test_00_probe_producers(tmp_path, monkeypatch, request):
    out = {}
    try:
        shown, detail, facts = author_stop_world(tmp_path / "s1", monkeypatch)
        out["s1"] = {"facts": facts, "messages": shown["messages"]}
    except Exception as exc:  # noqa: BLE001 — a probe records what failed and goes on
        out["s1"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        cw = closing_world(tmp_path / "s2")
        out["s2"] = {"fields": cw["fields"], "appended": cw["appended"], "main_detail": {k: cw["main_detail"].get(k) for k in (
            "status", "wake_summary", "suggested_name", "title", "outcome_axes")},
                     "done_main": cw["done_main"]["messages"], "done_project": cw["done_project"]["messages"]}
    except Exception as exc:  # noqa: BLE001
        out["s2"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        pw = portal_world(tmp_path / "s3")
        out["s3"] = {"done": pw["done"]["messages"], "detail": {k: pw["detail"].get(k) for k in (
            "status", "wake_summary", "suggested_name", "title", "project_id", "chat_id")},
                     "frame": pw["frame"], "outcome": pw["outcome"], "row": pw["row"]}
        pwf = portal_world(tmp_path / "s3f", failed=True)
        out["s3f"] = {"frame": pwf["frame"], "outcome": pwf["outcome"],
                      "detail": {k: pwf["detail"].get(k) for k in ("status", "error", "reason_code", "wake_summary")}}
    except Exception as exc:  # noqa: BLE001
        out["s3"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        w = request.getfixturevalue("world")
        lw = late_review_world(w)
        out["s4"] = lw["rows"]
    except Exception as exc:  # noqa: BLE001
        out["s4"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["s5"] = sleep_world(tmp_path / "s5", monkeypatch)
    except Exception as exc:  # noqa: BLE001
        out["s5"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        notices, alerts = boot_world(tmp_path / "s6", monkeypatch)
        page, stored = boot_page(tmp_path / "s6b", notices[0] if notices else "")
        out["s6"] = {"notices": notices, "alerts": alerts, "stored": stored, "page": page["messages"]}
    except Exception as exc:  # noqa: BLE001
        out["s6"] = {"error": f"{type(exc).__name__}: {exc}"}
    dump("probe", out)


# ================================================================================================
# The frames. Every producer runs before the browser starts (the sync Playwright loop owns the thread afterwards);
# each state opens one browser and takes both widths in it.

def _ui(request):
    return request.getfixturevalue("subscription_ui")


def top_into_view(locator, margin=72):
    """Scroll the feed so this node's top sits just under the room's header (a long card shows from its start)."""
    locator.evaluate("""(e, margin) => { let c = e.parentElement;
        while (c && !(c.scrollHeight > c.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(c).overflowY))) c = c.parentElement;
        const box = c ? c.getBoundingClientRect() : { top: 0 };
        (c || document.scrollingElement).scrollTop += e.getBoundingClientRect().top - box.top - margin; }""", margin)


PHASE_SETTLED = """([feed, id]) => { const word = (document.querySelector(
    `${feed} .chat-live-card[data-task-id="${id}"] > [data-live-summary-button] [data-live-phase]`)?.textContent || '').trim();
    return ['Failed', 'Stopped', 'Done', 'Done with warnings'].includes(word) ? word : false; }"""


CHIP_LOG = """(id) => { window.__chipLog = []; const t0 = performance.now();
    const read = () => (document.querySelector(
        `#chat-messages .chat-live-card[data-task-id="${id}"] > [data-live-summary-button] [data-live-phase]`)?.textContent || '').trim();
    let last = read(); window.__chipLog.push({ t: 0, word: last });
    new MutationObserver(() => { const word = read(); if (word !== last) { last = word;
        window.__chipLog.push({ t: Math.round(performance.now() - t0), word }); } })
        .observe(document.querySelector('#chat-messages'), { subtree: true, childList: true, characterData: true,
                                                               attributes: true }); }"""


def wait_settled(page, task_id, feed="#chat-messages"):
    return page.wait_for_function(PHASE_SETTLED, arg=[feed, task_id]).json_value()


def open_list(page, scope, work_id):
    rw.trigger(page, scope).click()
    rw.wait_facts(page, scope, lambda f: rw.row(f, work_id) is not None, f"the list row of {work_id}", timeout=WAIT_S)
    rw.settle_frames(page)
    root = "#page-chat" if scope == "main" else "#project-panel"
    page.locator(f'{root} .rw-row[data-id="{work_id}"]').evaluate("e => e.scrollIntoView({block: 'nearest'})")
    return row_words(page, scope, work_id)


def task_done_frame(stored, task_id, *, chat_id, minute):
    """The settled ``task_done`` the supervisor publishes (``supervisor/events_task_done.py``), from the stored row."""
    from ouroboros.task_finalization import quiet_finish, work_label

    return {"ts": _ts(minute), "type": "task_done", "task_id": task_id, "root_task_id": task_id, "task_type": "task",
            "chat_id": chat_id, "status": stored["status"], "wake_silent": stored.get("wake_silent") is True,
            **{key: stored[key] for key in ("wake_summary", "schedule_name") if stored.get(key)}, **work_label(stored),
            "quiet_effective": quiet_finish(stored), "_is_direct_chat": False,
            "root_phase_checkpoint": stored.get("root_phase_checkpoint") or {},
            "outcome_axes": stored.get("outcome_axes") or {}, "reason_code": stored.get("reason_code") or "",
            "artifact_status": stored.get("artifact_status")}


# 1 ----------------------------------------------------------------------------------------------

def test_01_author_stop_card(request, tmp_path, monkeypatch):
    sr = author_stop_room(tmp_path / "s1", monkeypatch)
    done = task_done_frame(sr["stored"], STOP_ID, chat_id=1, minute=6)
    producer = {"appended": sr["appended"], "source_stored": sr["source_stored"],
                "stored": {key: sr["stored"].get(key) for key in ("status", "reason_code", "suggested_name",
                                                                   "outcome_axes")},
                "reload_rows": rows_brief(sr["reload"]),
                "detail": {key: sr["detail"].get(key) for key in ("status", "reason_code", "continuation_offer")}}
    ui = _ui(request)

    def run(width):
        # (a) The page is open while the stop lands: the supervisor's task_done for the stored stop reaches the card,
        #     the page's own follow-up read gets the reader's answer for its cursor.
        transport = FrameTransport(new_page(ui, width), pages={1: sr["live"]}, details={STOP_ID: sr["live_detail"]},
                                   activities=[rw.activity(STOP_ID, started_at=_epoch(1))])
        transport.raw = {STOP_ID: {"running": True, "suggested_name": STOP_NAME}}
        page = open_room(ui, transport, width)
        card = card_of(page, STOP_ID)
        card.wait_for(state="visible")
        page.evaluate(CHIP_LOG, STOP_ID)
        transport.pages[1] = sr["reload"]
        transport.cursors.update(sr["cursors"])
        transport.details[STOP_ID] = sr["detail"]
        transport.send({"type": "log", "chat_id": 1, "data": done})
        live_word = wait_settled(page, STOP_ID)
        try:
            card.locator("[data-continue-task]").first.wait_for(state="visible", timeout=30_000)
        except Exception:  # noqa: BLE001 — the frame records whether Continue is offered
            pass
        page.wait_for_timeout(1500)
        center(card)
        shot(page, "01-stop-live", width, {"settled_word": live_word, "task_done": done},
             words=lambda: card_words(card))
        # The census stops listing the ended work (as the server's next /api/state does).
        transport.drop(STOP_ID)
        transport.refresh()
        page.wait_for_timeout(2000)
        transport.refresh()
        page.wait_for_timeout(600)
        center(card)
        shot(page, "01-stop-live-after", width, {"chip_log": page.evaluate("window.__chipLog"),
                                                 "cursor_misses": transport.cursor_misses},
             words=lambda: card_words(card))
        dump(f"01live-{width}", {"requests": transport.requests, "console": transport.console,
                                 "chip_log": page.evaluate("window.__chipLog"), "history_reads": transport.history_reads,
                                 "cursor_misses": transport.cursor_misses})
        page.close()
        # (b) The page opened after the stop: the room's history replays it.
        transport = FrameTransport(new_page(ui, width), pages={1: sr["reload"]}, details={STOP_ID: sr["detail"]},
                                   cursors=sr["cursors"], activities=[])
        page = open_room(ui, transport, width)
        card = card_of(page, STOP_ID)
        card.wait_for(state="visible")
        word = wait_settled(page, STOP_ID)
        try:
            card.locator("[data-continue-task]").first.wait_for(state="visible", timeout=30_000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1000)
        center(card)
        shot(page, "01-stop-reload", width, {"settled_word": word, "producer": producer},
             words=lambda: card_words(card))
        expand(card)
        page.wait_for_timeout(600)
        top_into_view(card)
        shot(page, "01-stop-reload-open", width, words=lambda: card_words(card))
        dump(f"01-{width}", {"requests": transport.requests, "console": transport.console,
                             "cursor_misses": transport.cursor_misses})
        page.close()

    each_width(run)


# 2 ----------------------------------------------------------------------------------------------

def test_02_closing_line_in_main_list(request, tmp_path):
    from ouroboros.task_results import load_task_result

    cw = closing_world(tmp_path / "s2")
    done = task_done_frame(load_task_result(cw["data"], CLOSE_ID), CLOSE_ID, chat_id=1, minute=9)
    ui = _ui(request)
    project = {**PROJECT, "chat_id": cw["chat"]}

    def run(width):
        transport = FrameTransport(new_page(ui, width), pages={1: cw["live_main"]}, projects=[project],
                                   details={CLOSE_ID: cw["live_detail"]},
                                   activities=[rw.activity(CLOSE_ID, started_at=_epoch(1))])
        transport.raw = {CLOSE_ID: {"running": True, "suggested_name": CLOSE_NAME,
                                    "focus": {"text": CLOSE_FOCUS, "at": _ts(5)}}}
        page = open_room(ui, transport, width)
        card_of(page, CLOSE_ID).wait_for(state="visible")
        open_list(page, "main", CLOSE_ID)
        shot(page, "02-main-list-live", width, words=lambda: row_words(page, "main", CLOSE_ID))
        # The finish: the room's history now holds the finished root's row, the supervisor's task_done reaches the
        # card, and the next census no longer lists the work (the open list keeps it, settled, until it closes).
        transport.pages[1] = cw["done_main"]
        transport.cursors.update(cw["main_cursors"])
        transport.details[CLOSE_ID] = cw["main_detail"]
        transport.send({"type": "log", "chat_id": 1, "data": done})
        word = wait_settled(page, CLOSE_ID)
        transport.drop(CLOSE_ID)
        transport.refresh()
        rw.wait_facts(page, "main", lambda f: (rw.row(f, CLOSE_ID) or {}).get("settled"), "the settled row",
                      timeout=WAIT_S)
        page.wait_for_timeout(400)
        transport.refresh()
        shot(page, "02-main-list-done", width, {"card_word_settled": word, "task_done": done},
             words=lambda: {**row_words(page, "main", CLOSE_ID), "card": card_words(card_of(page, CLOSE_ID))})
        dump(f"02main-{width}", {"requests": transport.requests, "console": transport.console})
        page.close()

    each_width(run)


def test_02_closing_line_in_project_latest(request, tmp_path):
    cw = closing_world(tmp_path / "s2")
    ui = _ui(request)
    project = {**PROJECT, "chat_id": cw["chat"]}

    def run(width):
        transport = FrameTransport(new_page(ui, width), pages={1: cw["done_main"], cw["chat"]: cw["done_project"]},
                                   projects=[project], details={PROJ_ID: cw["proj_detail"], CLOSE_ID: cw["main_detail"]},
                                   activities=[])
        page = open_room(ui, transport, width)
        rw.open_project(page, project)
        feed = f'[id="pchat-{project["id"]}-messages"]'
        card = card_of(page, PROJ_ID, feed)
        card.wait_for(state="visible")
        word = wait_settled(page, PROJ_ID, feed)
        opened = open_list(page, "project", PROJ_ID)
        listed = rw.wait_facts(page, "project", lambda f: f["groups"] == ["Latest task"] and len(f["rows"]) == 1,
                               "the latest root", timeout=WAIT_S)
        shot(page, "02-project-list-latest", width, {"open": opened, "groups": listed["groups"],
                                                     "card_word_settled": word},
             words=lambda: {**row_words(page, "project", PROJ_ID), "card": card_words(card)})
        page.close()

    each_width(run)


# 3 ----------------------------------------------------------------------------------------------

def _portal(request, tmp_path, *, failed, census_only=False):
    pw = portal_world(tmp_path / ("s3f" if failed else "s3"), failed=failed)
    ui = _ui(request)
    project = {**PROJECT, "chat_id": pw["chat"]}
    tag = "03-portal-census-only" if census_only else "03-portal-failed" if failed else "03-portal-done"
    if not census_only:
        assert pw["frame"], pw["outcome"]

    def run(width):
        row = rw.activity(PORTAL_ID, "working", chat_id=pw["chat"], project_id=PROJECT["id"],
                          project_name=PROJECT["name"], started_at=_epoch(0), last_line=PORTAL_LINE,
                          last_line_at=_ts(6), tool_calls=12, cancelable=True)
        transport = FrameTransport(new_page(ui, width), pages={1: pw["live"]}, projects=[project], activities=[row],
                                   bindings={PORTAL_ID: {"project_id": PROJECT["id"], "chat_id": pw["chat"]}},
                                   details={PORTAL_ID: pw["live_detail"]})
        transport.raw = {PORTAL_ID: {"running": True, "suggested_name": PORTAL_NAME}}
        page = open_room(ui, transport, width)
        card = card_of(page, PORTAL_ID)
        card.wait_for(state="visible")
        page.wait_for_function("""(id) => document.querySelector(`#chat-messages .chat-live-card[data-task-id="${id}"]`)
            ?.dataset.portal === '1'""", arg=PORTAL_ID)
        page.wait_for_function("""([id, line]) => (document.querySelector(`#chat-messages .chat-live-card[data-task-id="${id}"]
            [data-live-activity]`)?.textContent || '').includes(line)""", arg=[PORTAL_ID, PORTAL_LINE])
        center(card)
        if not failed and not census_only:
            shot(page, "03-portal-live", width, words=lambda: card_words(card))
        # The finish. Main's own row for the finished Project root arrives live (unless this path has none), the work
        # leaves the census, and its record (the route Main reads for a portal) says how it ended.
        transport.pages[1] = pw["census_done"] if census_only else pw["done"]
        transport.cursors.update(pw["census_cursors"] if census_only else pw["done_cursors"])
        transport.details[PORTAL_ID] = pw["detail"]
        if not census_only:
            transport.send(pw["frame"])
        transport.drop(PORTAL_ID)
        transport.refresh()
        word = wait_settled(page, PORTAL_ID)
        page.wait_for_timeout(600)
        transport.refresh()
        page.wait_for_timeout(400)
        center(card)
        shot(page, tag, width, {"settled_word": word,
                                "main_row": (pw["row"] or {}).get("text") if not census_only else None,
                                "detail": {key: pw["detail"].get(key) for key in (
                                    "status", "wake_summary", "title", "project_id", "error", "reason_code")}},
             words=lambda: card_words(card))
        dump(f"{tag}-{width}", {"requests": transport.requests, "console": transport.console,
                                "frame": pw["frame"], "outcome": pw["outcome"]})
        page.close()
        if failed or census_only:
            return
        transport = FrameTransport(new_page(ui, width), pages={1: pw["done"]}, projects=[project], activities=[],
                                   bindings={PORTAL_ID: {"project_id": PROJECT["id"], "chat_id": pw["chat"]}},
                                   details={PORTAL_ID: pw["detail"]}, cursors=pw["reload_cursors"])
        page = open_room(ui, transport, width)
        card = card_of(page, PORTAL_ID)
        card.wait_for(state="visible")
        word = wait_settled(page, PORTAL_ID)
        page.wait_for_timeout(600)
        transport.refresh()
        page.wait_for_timeout(400)
        center(card)
        shot(page, "03-portal-reload", width, {"settled_word": word, "main_row": (pw["row"] or {}).get("text")},
             words=lambda: card_words(card))
        page.close()

    each_width(run)


def test_03_portal_after_finish(request, tmp_path):
    _portal(request, tmp_path, failed=False)


def test_03_portal_after_failure(request, tmp_path):
    _portal(request, tmp_path, failed=True)


def test_03_portal_census_only(request, tmp_path):
    _portal(request, tmp_path, failed=False, census_only=True)


# 4 ----------------------------------------------------------------------------------------------

def test_04_late_review_on_merged_card(request, world):
    lw = late_review_world(world)
    ui = _ui(request)

    def run(width):
        transport = FrameTransport(new_page(ui, width), pages={1: lw["after"]},
                                   activities=[rw.activity("merge-task", started_at=_epoch(1))])
        transport.raw = {"merge-task": {"running": True, "suggested_name": MERGE_NAME}}
        page = open_room(ui, transport, width)
        card = card_of(page, "merge-task")
        card.wait_for(state="visible")
        expand(card)
        line = card.locator(".chat-live-line.result")
        line.first.wait_for(state="visible")
        top_into_view(card)
        link = line.first.locator("a")
        href = link.first.get_attribute("href") if link.count() else None
        opened = None
        if href:
            try:
                from tests.test_task_review_sources import client_for
                with client_for(world.root) as client:
                    response = client.get(href)
                opened = {"status": response.status_code, "tail": response.text[-40:]}
            except Exception as exc:  # noqa: BLE001 — recorded, the frame stands
                opened = {"error": f"{type(exc).__name__}: {exc}"}
        shot(page, "04-late-review-card", width, {
            "link": {"label": link.first.inner_text().strip(), "href": href, "opened": opened} if href else None,
            "history": lw["rows"]},
            words=lambda: {"line": line.first.inner_text().strip(), "card": card.inner_text().strip()})
        dump(f"04-{width}", {"requests": transport.requests, "console": transport.console})
        page.close()

    each_width(run)


# 5 ----------------------------------------------------------------------------------------------

SLEEP_NAME = "Wait for the nightly export to finish"


def test_05_sleep_lifecycle_rows(request, tmp_path, monkeypatch):
    sw = sleep_world(tmp_path / "s5", monkeypatch)
    real = {**sw["pushed"][0], "ts": _ts(6)}  # the producer's own event; its clock replaced by one afternoon's time
    # BY HAND: the row shape the candidate's label reads (task_checkpoint · budget_scope_paused · pause_reason=sleep).
    # No producer of either checkout writes it: its writers (loop_budget._handle_budget_exceeded,
    # agent._emit_budget_pause_checkpoint) carry no pause_reason; the one writer of pause_reason=sleep is the supervisor
    # event above. Its other fields are those writers' own (the resource_limit they spread).
    checkpoint = {"ts": _ts(6, 1), "type": "task_checkpoint", "task_id": SLEEP_ID, "chat_id": 1,
                  "checkpoint_kind": "budget_scope_paused", "pause_reason": "sleep",
                  **{key: real[key] for key in ("status", "scope", "root_task_id", "physical_calls", "replay_safe",
                                                "auto_resume", "resume_policy") if key in real}}
    data = tmp_path / "s5-room"
    data.mkdir(parents=True)
    owner_row(data, 1, "Wait for the nightly export, then summarise its report.", "fr-sleep-0", 0)
    narration_row(data, 1, SLEEP_ID, "The nightly export is still running; sleeping until it finishes.", 5,
                  suggested_name=SLEEP_NAME)
    room = history_page(data, 1)
    ui = _ui(request)

    def run(width):
        transport = FrameTransport(new_page(ui, width), pages={1: room}, logs={"events": [real, checkpoint]},
                                   activities=[rw.activity(SLEEP_ID, "budget_paused", pause_cause="sleep",
                                                           started_at=_epoch(1))])
        transport.raw = {SLEEP_ID: {"suggested_name": SLEEP_NAME, "_budget_pause": sw["marker"]}}
        page = open_room(ui, transport, width)
        card = card_of(page, SLEEP_ID)
        card.wait_for(state="visible")
        transport.send({"type": "log", "chat_id": 1, "data": real})
        transport.send({"type": "log", "chat_id": 1, "data": checkpoint})
        page.wait_for_timeout(800)
        expand(card)
        page.wait_for_timeout(600)
        top_into_view(card)
        shot(page, "05-sleep-card", width, {"supervisor_event": real, "checkpoint_by_hand": checkpoint,
                                            "stored_result": sw["result"]},
             words=lambda: {**card_words(card), "lines": card.locator(".chat-live-line").all_inner_texts()})
        # Dashboard -> Logs: the recent-history backfill (events.jsonl) holding both rows.
        page.goto(ui["url"] + "/?frames=logs#dashboard")
        page.wait_for_selector("#page-dashboard.active")
        tab = page.locator('[data-dashboard-tab="logs"]')
        if tab.count() and tab.first.is_visible():
            tab.first.click()
        page.wait_for_function("() => /budget_scope_paused|Task sleeping|Budget scope paused/.test("
                               "document.querySelector('#page-dashboard')?.textContent || '')")
        page.wait_for_timeout(600)
        shot(page, "05-sleep-logs", width, words=lambda: {"dashboard": page.locator("#page-dashboard").inner_text()[-3000:]})
        dump(f"05-{width}", {"requests": transport.requests, "console": transport.console})
        page.close()

    each_width(run)


# 6 ----------------------------------------------------------------------------------------------

def test_06_boot_notice_with_paused_places(request, tmp_path, monkeypatch):
    notices, alerts = boot_world(tmp_path / "s6", monkeypatch)
    assert notices, alerts
    room, stored = boot_page(tmp_path / "s6b", notices[0])
    ui = _ui(request)

    def run(width):
        transport = FrameTransport(new_page(ui, width), pages={1: room}, activities=[])
        page = open_room(ui, transport, width)
        bubble = page.locator('#chat-messages .chat-bubble[data-system-type="startup_notice"]')
        bubble.first.wait_for(state="visible")
        center(bubble.first)
        shot(page, "06-boot-notice", width, {"notice_sent": notices[0], "stored": stored, "places": place_rows()},
             words=lambda: {"row": bubble.first.inner_text().strip()})
        page.close()

    each_width(run)


# 99 ---------------------------------------------------------------------------------------------
# DIAGNOSIS ONLY, never a frame of the table: the reload of state 1 once more, the progress row's
# ``task_terminal_status`` stamp (written by the history reader) removed by hand, to see whether that stamp is what
# settles the card's chip before the terminal row is read.

def test_99_diag_stop_reload_without_progress_stamp(request, tmp_path, monkeypatch):
    shown, detail, _facts = author_stop_world(tmp_path / "s1", monkeypatch)
    unstamped = json.loads(json.dumps(shown))
    for row in unstamped["messages"]:
        if row.get("is_progress") and row.get("task_id") == STOP_ID:
            row.pop("task_terminal_status", None)
    ui = _ui(request)
    result = {}
    for label, page_rows in (("stamped", shown), ("unstamped", unstamped)):
        transport = FrameTransport(new_page(ui, 1280), pages={1: page_rows}, details={STOP_ID: detail}, activities=[])
        page = open_room(ui, transport, 1280)
        card = card_of(page, STOP_ID)
        card.wait_for(state="visible")
        result[label] = {"word": wait_settled(page, STOP_ID), **card_words(card)}
        DEBUG.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG / f"99-diag-{label}-1280.png"), animations="disabled")
        page.close()
    dump("99-diag-stop-reload", result)


# 98 ---------------------------------------------------------------------------------------------
# DIAGNOSIS ONLY, never a frame of the table: the live end of state 1 with a timed log of the chip and of the page's
# requests; (A) the room's history still the live page when the census drops the work, (B) the history the server
# answers after the end (its terminal row, the stamp on the progress row), as in the frames.

def test_98_diag_live_stop_flip(request, tmp_path, monkeypatch):
    import time

    sr = author_stop_room(tmp_path / "s1", monkeypatch)
    done = task_done_frame(sr["stored"], STOP_ID, chat_id=1, minute=6)
    ui = _ui(request)
    result = {}
    for label in ("A_follow_ups_by_the_reader", "B_follow_ups_answered_with_the_whole_page"):
        transport = FrameTransport(new_page(ui, 1280), pages={1: sr["live"]}, details={STOP_ID: sr["live_detail"]},
                                   activities=[rw.activity(STOP_ID, started_at=_epoch(1))])
        transport.raw = {STOP_ID: {"running": True, "suggested_name": STOP_NAME}}
        page = open_room(ui, transport, 1280)
        card_of(page, STOP_ID).wait_for(state="visible")
        t0 = time.monotonic()
        timeline = []
        page.on("request", lambda req: timeline.append((round(time.monotonic() - t0, 2), req.method,
                                                        urlparse(req.url).path, "cursor" in urlparse(req.url).query))
                if "/api/" in req.url else None)
        page.evaluate(CHIP_LOG, STOP_ID)
        transport.pages[1] = sr["reload"]
        transport.details[STOP_ID] = sr["detail"]
        if label.startswith("A"):
            transport.cursors.update(sr["cursors"])
        transport.send({"type": "log", "chat_id": 1, "data": done})
        page.wait_for_timeout(2500)
        mark_drop = round(time.monotonic() - t0, 2)
        transport.drop(STOP_ID)
        transport.refresh()
        page.wait_for_timeout(3000)
        result[label] = {"chip_log": page.evaluate("window.__chipLog"), "census_dropped_at_s": mark_drop,
                         "requests": timeline, "cursor_misses": transport.cursor_misses,
                         "final": card_words(card_of(page, STOP_ID))}
        page.close()
    dump("98-diag-live-stop-flip", result)
