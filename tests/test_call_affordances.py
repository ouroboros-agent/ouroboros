"""What a live call is told and allowed, after the owner's test of 18.10.

In his test the voice could not send "2 + 2 = 4" to the main chat (his own call was not marked as started
by him), did not know it could ask him to show the screen, hunted the catalog for a way to read the task it
had just started (the start receipt pointed at wait_task and get_task_result, neither in its view), and said
"The call has ended." in English after its goodbye (the hang-up's answer invited one more reply).
"""
from __future__ import annotations

import types

from ouroboros.call_protocol import CallSpec


def _spec(**overrides) -> CallSpec:
    fields = dict(call_id="c1", chat_id=1, engine="google", model="gemini-3.8-live", voice="Kore",
                  presence_binding_id="", max_minutes=0, label="Test call", client_message_id="cm-1")
    fields.update(overrides)
    return CallSpec(**fields)


def test_an_owner_call_speaks_as_the_owners_turn_and_a_presence_call_does_not(tmp_path):
    from ouroboros.dialogue_provenance import run_origin
    from ouroboros.tools.control_runtime import _main_notice_refusal

    def ctx(metadata):
        return types.SimpleNamespace(task_metadata=metadata, task_contract=None, drive_root=tmp_path,
                                     budget_drive_root=tmp_path, task_id="t-call")

    owner = {"live_call": _spec().as_metadata(), "origin_suppressed": True}
    assert run_origin({"metadata": owner})["owner_ingress"] is True
    assert _main_notice_refusal(ctx(owner), 1) == ""
    unmarked = {"live_call": _spec().as_metadata()}  # what every owner call carried before
    assert "started by the owner" in _main_notice_refusal(ctx(unmarked), 1)


def test_the_owner_call_door_stamps_the_owner_and_names_the_room(monkeypatch):
    from supervisor import worker_chat_lane

    seen = {}
    monkeypatch.setattr(worker_chat_lane, "wake_gate_open", lambda: True)
    monkeypatch.setattr("supervisor.state.budget_remaining", lambda *_a, **_k: 10.0)
    monkeypatch.setattr("supervisor.state.load_state", lambda: {})

    def admit(_evt, chat_id, text, _media, *, task_metadata, kind):
        seen.update(chat_id=chat_id, text=text, metadata=task_metadata, kind=kind)
        return None  # stop before a thread starts

    monkeypatch.setattr(worker_chat_lane, "_admit_chat_task", admit)
    worker_chat_lane.handle_call_direct(1, _spec())
    assert seen["metadata"]["origin_suppressed"] is True and seen["kind"] == "call"
    assert "open in the main chat" in seen["text"]
    seen.clear()
    worker_chat_lane.handle_call_direct(1, _spec(presence_binding_id="b" * 32))
    assert "origin_suppressed" not in seen["metadata"]  # a Presence call speaks for its conversation


def test_the_calls_opening_names_the_room_show_and_speaking_before_a_slow_tool():
    from supervisor.worker_chat_lane import call_opening_text

    text = call_opening_text(_spec(), 'the project room "Racer"')
    assert 'open in the project room "Racer"' in text
    assert "(Show)" in text and "you may ask for it" in text
    assert "first say in a few words what you are doing, then call it" in text  # measured: speaks 1.2 s after the owner
    assert "another speaker may speak their own" in text
    assert "open in this room" in call_opening_text(_spec())


def test_the_first_view_can_follow_a_started_task_but_never_waits_on_it():
    from ouroboros.live_call_route import CALL_INITIAL_TOOL_NAMES

    assert {"start_task", "peek_task", "get_task_result", "tell_task"} <= set(CALL_INITIAL_TOOL_NAMES)
    assert not {"wait_task", "wait_tasks", "await_messages"} & set(CALL_INITIAL_TOOL_NAMES)  # one tool lane


def test_start_task_tells_a_call_that_the_result_comes_back_and_a_text_turn_to_wait(tmp_path, monkeypatch):
    from ouroboros.tools.control import _start_task

    monkeypatch.setattr("ouroboros.tools.control_events._wait_for_promotion_admission",
                        lambda *_a, **_k: {"status": "scheduled", "effective_project_id": ""})

    def run(metadata):
        ctx = types.SimpleNamespace(pending_events=[], event_queue=None, current_chat_id=1, drive_root=tmp_path,
                                    task_metadata=metadata)
        return _start_task(ctx, "Count today's transactions", title="Transactions")

    in_call = run({"live_call": _spec().as_metadata()})
    assert "arrives in this call by itself" in in_call and "wait_task" not in in_call
    assert "Use wait_task/get_task_result" in run({})


def test_the_hang_up_is_answered_silently_and_other_results_when_idle():
    import asyncio

    from ouroboros.gateways.realtime_engine import SCHEDULE_SILENT, SCHEDULE_WHEN_IDLE, ToolCall, ToolResult
    from ouroboros.live_call_route import END_CALL_TOOL
    from ouroboros.live_tool_batch import ToolBatchWorker

    sent = []

    async def main():
        call = types.SimpleNamespace(spec=_spec(), hub=types.SimpleNamespace(end_by_ouroboros=lambda: None))

        async def send(command):
            sent.append(command)
        call._send = send
        lane = ToolBatchWorker.__new__(ToolBatchWorker)
        lane.call = call
        lane.enqueue(ToolCall(call_id="e1", name=END_CALL_TOOL, arguments="{}"), epoch=1)
        await asyncio.sleep(0)

    asyncio.run(main())
    [result] = sent
    assert isinstance(result, ToolResult) and result.scheduling == SCHEDULE_SILENT
    assert ToolResult("x", "ok").scheduling == SCHEDULE_WHEN_IDLE


def test_the_record_keeps_the_owners_languages_and_each_bindings_apart(tmp_path):
    import pytest

    from ouroboros.call_languages import record_call_languages, recorded_call_languages

    assert recorded_call_languages(tmp_path) == () and recorded_call_languages(None) == ()
    assert record_call_languages(tmp_path, ["ru-RU", "en-us", "ru-RU"], task_id="t1") == ("ru-RU", "en-US")
    assert record_call_languages(tmp_path, ["de-DE"], binding_id="b1") == ("de-DE",)
    assert recorded_call_languages(tmp_path) == ("ru-RU", "en-US")  # the owner's calls, in every room
    assert recorded_call_languages(tmp_path, "b1") == ("de-DE",) and recorded_call_languages(tmp_path, "b2") == ()
    with pytest.raises(ValueError, match="Russian"):
        record_call_languages(tmp_path, ["Russian", "en-US"])
    with pytest.raises(ValueError):
        record_call_languages(tmp_path, "ru-RU")  # one list, never a string to split
    assert recorded_call_languages(tmp_path) == ("ru-RU", "en-US")  # a refusal records nothing
    assert record_call_languages(tmp_path, []) == () and recorded_call_languages(tmp_path) == ()  # guessing again
    (tmp_path / "state" / "call_languages.json").write_text("{not json", encoding="utf-8")
    assert recorded_call_languages(tmp_path) == ()


def test_every_call_can_record_languages_and_only_an_owner_call_hangs_up():
    from ouroboros.live_call_route import call_local_tool_names

    assert call_local_tool_names(_spec()) == {"end_call", "set_call_languages"}
    assert call_local_tool_names(_spec(presence_binding_id="b" * 32)) == {"set_call_languages"}
    assert call_local_tool_names(None) == frozenset()


def _call(tmp_path, **spec_fields):
    from ouroboros.live_call_state import SessionRenewal

    call = types.SimpleNamespace(spec=_spec(**spec_fields), drive_root=tmp_path, task_id="t-call",
                                 messages=[{"role": "system", "content": "core"}])
    call.renewal = SessionRenewal(call)
    return call


def _record_via_tool(call, arguments: str):
    import asyncio

    from ouroboros.gateways.realtime_engine import ToolCall
    from ouroboros.live_tool_batch import ToolBatchWorker

    sent = []

    async def main():
        async def send(command):
            sent.append(command)
        call._send = send
        lane = ToolBatchWorker.__new__(ToolBatchWorker)
        lane.call = call
        lane.enqueue(ToolCall(call_id="l1", name="set_call_languages", arguments=arguments), epoch=1)
        for _ in range(500):
            if sent:
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    return sent


def test_the_voice_records_languages_silently_and_the_next_pause_reopens_the_session(tmp_path):
    from ouroboros.call_languages import recorded_call_languages
    from ouroboros.gateways.realtime_engine import SCHEDULE_SILENT, SCHEDULE_WHEN_IDLE

    call = _call(tmp_path)
    assert call.renewal.languages == () and call.renewal.due() == ""  # nothing recorded, nothing said
    [result] = _record_via_tool(call, '{"languages": ["ru-RU", "en-US"]}')
    assert result.scheduling == SCHEDULE_SILENT and "expects ru-RU, en-US" in result.output
    assert recorded_call_languages(tmp_path) == ("ru-RU", "en-US")
    assert call.messages[-1]["_context_source"] == "set_call_languages"  # a reopened session replays it
    assert call.renewal.due() == "languages"  # the provider takes them only when a session opens
    call.renewal.reset((0, 0), ("ru-RU", "en-US"))  # the reopened session opened with them
    assert call.renewal.due() == ""
    [refused] = _record_via_tool(call, '{"languages": ["Russian"]}')
    assert refused.scheduling == SCHEDULE_WHEN_IDLE and "Not recorded" in refused.output and "Russian" in refused.output
    assert recorded_call_languages(tmp_path) == ("ru-RU", "en-US") and call.renewal.due() == ""


def test_a_call_starts_with_its_record_and_a_presence_call_with_its_bindings(tmp_path):
    from ouroboros.call_languages import record_call_languages

    record_call_languages(tmp_path, ["ru-RU", "en-US"])
    record_call_languages(tmp_path, ["de-DE"], binding_id="b" * 32)
    owner = _call(tmp_path)
    assert owner.renewal.languages == ("ru-RU", "en-US") and owner.renewal.due() == ""
    assert _call(tmp_path, presence_binding_id="b" * 32).renewal.languages == ("de-DE",)


def test_the_opening_asks_to_judge_the_owners_language_until_one_is_recorded():
    from supervisor.worker_chat_lane import call_opening_text

    bare = call_opening_text(_spec())
    assert "no languages recorded" in bare and "from this room's correspondence and your memory" in bare
    assert "set_call_languages" in bare and "ask when unsure" in bare
    recorded = call_opening_text(_spec(), languages=("ru-RU", "en-US"))
    assert "Speech recognition expects ru-RU, en-US" in recorded and "no languages recorded" not in recorded


def test_a_skill_that_declares_the_call_view_opens_every_owner_call_with_its_tools(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.live_call_route import initial_call_schemas
    from ouroboros.tools.registry import ToolRegistry

    data = tmp_path / "data"
    for skill, extra in (("deskx", "live_call: first_view\n"), ("kee", "")):
        home = data / "skills" / "external" / skill
        home.mkdir(parents=True)
        (home / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: test\nversion: 0.1.0\ntype: extension\n"
                                       f"entry: plugin.py\nruntime: python3\npermissions: [tool]\n{extra}---\n",
                                       encoding="utf-8")
    rows = {}
    for skill in ("deskx", "kee"):
        name = extension_loader.extension_surface_name(skill, "act")
        rows[name] = {"name": name, "handler": lambda ctx: "ok", "description": f"{skill} action", "skill": skill,
                      "schema": {"type": "object", "properties": {}}, "timeout_sec": 5}
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_a, **_k: True)
    with extension_loader._lock:
        saved = dict(extension_loader._tools)
        extension_loader._tools.update(rows)
    try:
        registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=data)
        desk, kee = (extension_loader.extension_surface_name(skill, "act") for skill in ("deskx", "kee"))
        owner = {schema["function"]["name"] for schema in initial_call_schemas(registry, _spec())}
        assert desk in owner and kee not in owner  # declared: at once; undeclared: one enable_tools away
        assert {"start_task", "web_search"} <= owner
        presence = {schema["function"]["name"] for schema in initial_call_schemas(registry, _spec(presence_binding_id="b" * 32))}
        assert {desk, kee} <= presence  # a Presence call keeps its binding's whole permitted set
        monkeypatch.setattr(extension_loader, "is_extension_live", lambda skill, *_a, **_k: skill != "deskx")
        assert desk not in {schema["function"]["name"] for schema in initial_call_schemas(registry, _spec())}  # off: nothing
    finally:
        with extension_loader._lock:
            extension_loader._tools.clear()
            extension_loader._tools.update(saved)


def test_the_bundled_desktop_skills_open_every_owner_call():
    import pathlib

    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    from ouroboros.live_call_route import CALL_VIEW_FIRST, CALL_VIEW_MANIFEST_KEY

    skills = pathlib.Path(__file__).resolve().parents[1] / "skills"
    for name in ("unix_computer_use", "windows_computer_use"):
        manifest = parse_skill_manifest_text((skills / name / "SKILL.md").read_text(encoding="utf-8"))
        assert manifest.raw_extra.get(CALL_VIEW_MANIFEST_KEY) == CALL_VIEW_FIRST, name


def test_the_page_offers_talk_only_where_the_google_key_is_set(monkeypatch):
    from ouroboros import llm_live
    from ouroboros.provider_models import PROVIDER_ENV_KEYS

    key = PROVIDER_ENV_KEYS["google"]
    monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {})
    assert llm_live.live_key_configured() is False
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {key: " k "})
    assert llm_live.live_key_configured() is True  # saved in Settings, not yet in the environment
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {})
    monkeypatch.setenv(key, "k")
    assert llm_live.live_key_configured() is True


def _boundary_call(**fields):
    from ouroboros.live_call_state import Openings, SessionRenewal

    lane = types.SimpleNamespace(running=False, pending=[], claimed={})
    call = types.SimpleNamespace(spec=_spec(**fields), messages=[], engine=object(), closing=False, reconnecting=False,
                                 response_open=False, spoken={}, tools_lane=lane, drive_root=None)
    call.renewal = SessionRenewal(call)
    return call, Openings(call.renewal)


def test_what_is_addressed_to_the_call_waits_for_a_completed_turn_and_opens_one_response():
    from ouroboros.gateways.realtime_engine import ContextItem

    call, openings = _boundary_call()
    live = openings.live(presence=False)
    assert live.opens_response and "has just opened this call" in live.text and live.source == "call_live"
    assert openings.live(presence=False) is None  # once per call: a renewal or resumption continues the talk
    call.response_open = True
    openings.hold(live)
    openings.hold(ContextItem("The owner typed: look at this", "room_row:in", opens_response=True))
    assert openings.take() == []  # an answer is open: completing the turn would cut it
    call.response_open, call.renewal.owner_speaking = False, True
    assert openings.take() == []  # the owner is speaking
    call.renewal.owner_speaking = False
    from ouroboros.live_call_state import Acks

    openings.hold(Acks(()))  # acknowledgements of held mail wait behind it
    sent = openings.take()
    assert [getattr(item, "source", "acks") for item in sent] == ["call_live", "room_row:in", "", "acks"]
    assert [getattr(item, "opens_response", None) for item in sent] == [False, False, True, None]
    assert sent[2].text == ""  # one bare completion opens one response for both
    assert openings.take() == []
    _call2, presence = _boundary_call(presence_binding_id="b" * 32)
    assert "the other side can hear you" in presence.live(presence=True).text


def test_the_room_feed_marks_the_owners_words_and_the_reports_of_tasks_the_call_started(tmp_path):
    from ouroboros.live_awareness import RoomRows

    started = {"t-weather"}
    rooms = RoomRows(tmp_path, 1, "t-call", "c1", "", started=lambda: started)
    rooms._member = None
    rows = [{"chat_id": 1, "direction": "in", "text": "look at this link", "ts": "a", "msg_id": "m1"},
            {"chat_id": 1, "direction": "out", "task_id": "t-weather", "text": "+6 tonight", "ts": "b", "msg_id": "m2"},
            {"chat_id": 1, "direction": "out", "task_id": "t-other", "text": "another turn", "ts": "c", "msg_id": "m3"},
            {"chat_id": 1, "direction": "system", "type": "notice", "text": "budget", "ts": "d", "msg_id": "m4"}]
    for row in rows:
        rooms._on_row(row)
    items = rooms.drain()
    assert [item.opens_response for item in items] == [True, True, False, False]


def test_start_task_in_a_call_remembers_the_task_so_its_report_reaches_the_call(tmp_path, monkeypatch):
    from ouroboros.tools.control import _start_task

    monkeypatch.setattr("ouroboros.tools.control_events._wait_for_promotion_admission",
                        lambda *_a, **_k: {"status": "scheduled", "effective_project_id": ""})
    ctx = types.SimpleNamespace(pending_events=[], event_queue=None, current_chat_id=1, drive_root=tmp_path,
                                task_metadata={"live_call": _spec().as_metadata()})
    _start_task(ctx, "Find tonight's weather", title="Weather")
    assert len(ctx._call_started_task_ids) == 1
    text = types.SimpleNamespace(pending_events=[], event_queue=None, current_chat_id=1, drive_root=tmp_path,
                                 task_metadata={})
    _start_task(text, "Find tonight's weather", title="Weather")
    assert not hasattr(text, "_call_started_task_ids")  # a text turn waits on its own tasks instead
