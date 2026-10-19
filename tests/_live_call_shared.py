"""The live voice-call driver's harness: fakes of the lanes it meets, shared by its test modules.

``FakeEngine`` implements ``LiveEngine`` (the engines lane), ``FakeHub`` the ``CallHub`` contract
(the audio lane) and ``FakeLedger`` the usage ledger; ``Harness`` runs one call on its own thread
exactly as a turn does, and the ``harness`` fixture builds calls and stops whatever a test leaves
running. A module registers the fixture with ``harness = base.harness``.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import live_turn
from ouroboros.call_protocol import CallSpec, PeerControl, PeerInfo
from ouroboros.gateways.realtime_engine import ENGINE_GOOGLE, Ready
from ouroboros.usage_accounting import BudgetExceeded

_END = object()
OWNER = PeerInfo(peer_id="owner-1", role="owner", mic=True, speaker=True, label="Anton")


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _spec(**overrides) -> CallSpec:
    fields = dict(call_id="call-1", chat_id=1, engine=ENGINE_GOOGLE, model="gemini-3.8-live", voice="Kore",
                  opened_by="owner", presence_binding_id="", max_minutes=0, label="Test call", client_message_id="cm-1")
    fields.update(overrides)
    return CallSpec(**fields)


class FakeEngine:
    """``LiveEngine``: records the session config and every command; the test pushes events."""

    auto_ready = True

    def __init__(self) -> None:
        self.config = None
        self.sent: list = []
        self.closed: list = []
        self._queue = None
        self._loop = None

    async def open(self, config) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self.config = config  # last: ``Harness.start`` returns on it, and a test may push at once
        if self.auto_ready:
            self._queue.put_nowait(Ready({"input_rate": 16000, "output_rate": 24000}))  # Gemini Live's wire rates

    async def send(self, command) -> None:
        self.sent.append(command)

    async def events(self):
        while True:
            event = await self._queue.get()
            if event is _END:
                return
            yield event

    async def close(self, reason: str) -> None:
        self.closed.append(reason)

    def unsent_context(self):
        held, self.held = tuple(getattr(self, "held", ())), []
        return held

    def push(self, event) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def end_stream(self) -> None:
        self.push(_END)

    def sent_of(self, kind):
        return [command for command in self.sent if isinstance(command, kind)]


class FakeLLM:
    def __init__(self) -> None:
        self.engines: list = []
        self.routes: list = []
        self.fail_opens = 0

    def open_live(self, route):
        if self.fail_opens:
            self.fail_opens -= 1
            raise RuntimeError("engine unreachable")
        self.routes.append(route)
        self.engines.append(FakeEngine())
        return self.engines[-1]


class FakeHub:
    """``CallHub``: the test feeds peer events; the driver's output is recorded."""

    def __init__(self) -> None:
        self.inbox: queue.Queue = queue.Queue()
        self.played: list = []
        self.flushes = 0
        self.states: list = []
        self.captions: list = []
        self.rates = None
        self.present = [OWNER]
        self.closed: list = []
        self.activity: list = []
        self.turns: list = []
        self.words: list = []  # (state, detail, action) of every state that carried words
        self.ended_by_owner = False
        self.ended_by_ouroboros = False

    def end_by_ouroboros(self) -> None:
        self.ended_by_ouroboros = True

    def set_engine_rates(self, input_rate, output_rate) -> None:
        self.rates = (input_rate, output_rate)

    def next_event(self, timeout_s):
        try:
            return self.inbox.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def play(self, pcm, item_id) -> None:
        self.played.append((item_id, pcm))

    def flush(self) -> int:
        self.flushes += 1
        return self.flushes

    def send_state(self, state, reason="", *, phase="", detail="", action="") -> None:
        self.states.append((state, reason))
        if detail or action:
            self.words.append((state, detail, action))

    def send_activity(self, activity, label="") -> None:
        self.activity.append((activity, label))

    def send_turn(self, turn) -> None:
        self.turns.append(turn)

    def send_caption(self, speaker, text, final, item_id="") -> None:
        self.captions.append((speaker, text, final, item_id))

    def peers(self):
        return tuple(self.present)

    def close(self, reason, **words) -> None:
        self.closed.append(reason)

    def feed(self, event) -> None:
        self.inbox.put(event)

    def control(self, kind, **fields) -> None:
        self.feed(PeerControl(peer_id="owner-1", role="owner", message={"type": kind, **fields}))


class FakeLedger:
    def __init__(self) -> None:
        self.reserved: list = []
        self.settled: list = []
        self.abandoned: list = []
        self.refused: list = []
        self.warmed: list = []
        self.refuse = False

    def warm_quote(self, **fields):
        self.warmed.append((fields, threading.current_thread().name))
        return None

    def reserve_live_response(self, **fields):
        if self.refuse:
            raise BudgetExceeded("the call's budget is spent")
        attempt = {"key": fields["response_key"], **fields}
        self.reserved.append(attempt)
        return attempt

    def record_refused_live_response(self, **fields):
        self.refused.append(fields)

    def settle_live_response(self, attempt, *, usage, raw_usage, status) -> None:
        self.settled.append((attempt["key"], status, dict(usage)))

    def abandon_live_response(self, attempt, *, reason) -> None:
        self.abandoned.append((attempt["key"], reason))


def _schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


class Harness:
    """One call on its own thread, wired to the fakes; ``tool_hook`` is what a tool does."""

    def __init__(self, monkeypatch, tmp_path, *, spec=None, task=None) -> None:
        from ouroboros import loop, loop_budget
        from supervisor import message_bus

        self.root = tmp_path
        self.task = task
        (tmp_path / "logs").mkdir(exist_ok=True)
        self.spec = spec or _spec()
        self.hub, self.llm, self.ledger = FakeHub(), FakeLLM(), FakeLedger()
        self.schemas = [_schema("knowledge_read"), _schema("web_search"), _schema("enable_tools")]
        self.progress: list = []
        self.broadcasts: list = []
        self.batches: list = []
        self.tool_hook = None
        self.result = None
        self.error = None
        monkeypatch.setattr(live_turn, "_hub_for", lambda call_id: self.hub if call_id == self.spec.call_id else None)
        monkeypatch.setattr(live_turn, "_ledger", lambda: self.ledger)
        monkeypatch.setattr(live_turn, "initial_call_schemas", lambda tools, spec=None: list(self.schemas))
        monkeypatch.setattr(loop, "_setup_dynamic_tools",
                            lambda tools, schemas, messages, context_mode="max", call=False: (list(schemas), None))
        monkeypatch.setattr(loop, "handle_tool_calls", self._handle_tool_calls)
        monkeypatch.setattr(loop, "_measure_tool_result_batch", lambda ctx, messages, schemas, *, round_id: None)
        monkeypatch.setattr(loop_budget, "_cleanup_loop_resources", lambda executor, exit_ctx: None)
        monkeypatch.setattr(message_bus, "get_bridge", lambda: SimpleNamespace(
            broadcast=self.broadcasts.append, send_chat_action=lambda *a, **k: None,
            send_message=lambda *a, **k: (True, "")))
        self.ctx = SimpleNamespace(task_attempt=1, active_context_mode="nano", drive_root=tmp_path, task_id="t-call")
        self.tools = SimpleNamespace(_ctx=self.ctx)
        self.messages = [{"role": "system", "content": "You are Ouroboros, on a call."},
                         {"role": "user", "content": "[Live call] A live voice call is open in this room."}]
        self.incoming: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()
        from ouroboros.utils import utc_now_iso

        self.context_captured_at = utc_now_iso()  # as the agent records it before the context reads the room

    def emit_progress(self, text, **kwargs) -> None:
        self.progress.append((text, kwargs))

    def _handle_tool_calls(self, tool_calls, tools, drive_logs, task_id, executor, messages, llm_trace,
                           emit_progress, *, fit_candidate, tool_schemas) -> None:
        self.batches.append([call["id"] for call in tool_calls])
        for call in tool_calls:
            if self.tool_hook is not None:
                self.tool_hook(call, messages, tool_schemas)
            else:
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"],
                                 "content": f"result of {call['function']['name']}"})

    def start(self) -> FakeEngine:
        def _run():
            try:
                self.result = live_turn.run_live_turn(
                    messages=self.messages, tools=self.tools, llm=self.llm, drive_logs=self.root / "logs",
                    emit_progress=self.emit_progress, incoming_messages=self.incoming, task_id="t-call",
                    task_type="task", budget_remaining_usd=10.0, event_queue=self.events, drive_root=self.root,
                    spec=self.spec, task=self.task, context_captured_at=self.context_captured_at)
            except BaseException as exc:  # surfaced to the test thread
                self.error = exc

        self.thread = threading.Thread(target=_run, name="call-under-test", daemon=True)
        self.thread.start()
        assert _wait_for(lambda: bool(self.llm.engines) and self.llm.engines[-1].config is not None)
        return self.llm.engines[-1]

    def engine(self) -> FakeEngine:
        return self.llm.engines[-1]

    def stop(self) -> None:
        from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, write_owner_message

        assert write_owner_message(self.root, "stop", "t-call", msg_id="owner-stop", kind=KIND_FINALIZE_NOW)

    def finish(self, timeout=15.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the call did not end"
        if self.error is not None:
            raise self.error
        return self.result

    def counts_heard(self) -> int:
        return sum(str(m.get("content")).startswith("[Heard in the call]") for m in self.messages)

    def chat_rows(self) -> list:
        from tests._dialogue import chat_rows

        return chat_rows(self.root)

    def task_row(self) -> dict:
        from ouroboros.task_results import load_task_result

        return load_task_result(self.root, "t-call") or {}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Build calls; whatever the test leaves running is stopped and joined here."""
    created: list = []

    def make(**kwargs) -> Harness:
        created.append(Harness(monkeypatch, tmp_path, **kwargs))
        return created[-1]

    yield make
    for h in created:
        thread = getattr(h, "thread", None)
        if thread is not None and thread.is_alive():
            h.stop()
            thread.join(15)
    assert _wait_for(lambda: not any(t.name.startswith("call-") for t in threading.enumerate()), 5)
