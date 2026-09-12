"""Tests for process memory infrastructure.

Covers:
- Execution reflection trigger logic (should_generate_reflection)
- Error detail collection and marker detection
- Reflection loading into context
"""

import inspect
import os
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ─────────────────────────────────────────────────────────────────────────────
# should_generate_reflection
# ─────────────────────────────────────────────────────────────────────────────

class TestReflectionTrigger:
    """should_generate_reflection(llm_trace) must detect error conditions."""

    def test_clean_trace_no_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
            {"tool": "run_command", "args": {}, "result": "ok", "is_error": False},
        ]}
        assert should_generate_reflection(trace) is False

    def test_error_tool_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "run_command", "args": {}, "result": "⚠️ TOOL_ERROR: failed", "is_error": True},
        ]}
        assert should_generate_reflection(trace) is True

    def test_review_blocked_marker_triggers_reflection(self):
        """Same contract, typed source (owner item I24): a blocked review opens
        reflection because the call carries the refusal's own status, not because
        the word appears in its body."""
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False,
             "status": "review_blocked", "tool_result_code": "REVIEW_BLOCKED",
             "result": "⚠️ REVIEW_BLOCKED (attempt 1/3): reviewer flagged version sync"},
        ]}
        assert should_generate_reflection(trace) is True

    def test_tests_failed_marker_triggers_reflection(self):
        """The commit that carried a failing-test verdict is typed as blocked too;
        a genuinely successful commit whose output merely quotes a test name is
        not, which is the half the old substring scan could not express."""
        from ouroboros.reflection import should_generate_reflection
        blocked = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": True,
             "status": "review_blocked", "tool_result_code": "REVIEW_BLOCKED",
             "result": "⚠️ REVIEW_BLOCKED: TESTS_FAILED: VERSION not in README"},
        ]}
        assert should_generate_reflection(blocked) is True
        quoted = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False, "status": "ok",
             "result": "OK: committed test_tests_failed_marker_triggers_reflection"},
        ]}
        assert should_generate_reflection(quoted) is False

    def test_a_preserved_commit_with_failing_post_commit_tests_still_reflects(self):
        """The ordinary self-modification commit reports failing post-commit
        tests as a WARNING appended to a success: the commit is preserved, the
        call is ok, and no consumer of that distinction may be told otherwise.
        The failing tests are still the class the register exists for, so the
        producer states them as a typed fact beside the unchanged text and the
        triggers read that, never the word in the body.

        Driven through the real producer seam (tools/git.py) and the real trace
        projection, because a hand-written row cannot prove either of them.
        """
        import types

        from ouroboros.loop_tool_execution import _typed_execution_failure, _typed_result_metadata
        from ouroboros.reflection import (
            POST_COMMIT_TESTS_FAILED, _admits_pattern_register, _detect_markers,
            _trace_call_errored, should_generate_reflection,
        )
        from ouroboros.tools import git as git_tools
        from ouroboros.tools.tool_result import (
            _install_tool_result_sidecar, _published_tool_result, _restore_tool_result_sidecar,
        )

        def _row(test_warning: str) -> dict:
            ctx = types.SimpleNamespace()
            sentinel = object()
            token = _install_tool_result_sidecar(ctx, sentinel)
            try:
                text = git_tools._publish_post_commit_test_fact(
                    ctx, "OK: committed to dev: fix parser[pushed: abc1234]" + test_warning,
                    test_warning,
                )
                published = _published_tool_result(ctx, sentinel)
            finally:
                _restore_tool_result_sidecar(token)
            is_error = _typed_execution_failure(True, published) if published else False
            return {"tool": "commit_reviewed", "result": text, "is_error": is_error,
                    **_typed_result_metadata("commit_reviewed", text, is_error, published)}

        failed = _row("\n\n⚠️ TESTS_FAILED (commit preserved, consecutive failures: 2):\n"
                      "⚠️ TESTS_FAILED: Post-commit verification failed.")
        # The commit succeeded and stays a success on every axis that reads it.
        assert failed["is_error"] is False and failed["status"] == "ok"
        assert _trace_call_errored(failed) is False
        trace = {"tool_calls": [failed]}
        assert should_generate_reflection(trace, rounds=3, cost_usd=0.5) is True
        assert _detect_markers(trace) == [POST_COMMIT_TESTS_FAILED]
        assert _admits_pattern_register(
            {"error_count": 0, "key_markers": _detect_markers(trace),
             "child_failure_classes": []},
        ) is True

        clean = _row("")
        assert "post_commit_tests" not in clean
        assert should_generate_reflection({"tool_calls": [clean]}) is False
        assert _detect_markers({"tool_calls": [clean]}) == []

    def test_empty_trace_no_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        assert should_generate_reflection({"tool_calls": []}) is False
        assert should_generate_reflection({}) is False

    def test_nontrivial_rounds_triggers_reflection(self):
        """rounds >= NONTRIVIAL_ROUNDS_THRESHOLD fires even on a clean trace."""
        from ouroboros.reflection import should_generate_reflection, NONTRIVIAL_ROUNDS_THRESHOLD
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace, rounds=NONTRIVIAL_ROUNDS_THRESHOLD) is True
        assert should_generate_reflection(clean_trace, rounds=NONTRIVIAL_ROUNDS_THRESHOLD - 1) is False

    def test_nontrivial_cost_triggers_reflection(self):
        """cost_usd >= NONTRIVIAL_COST_THRESHOLD fires even on a clean trace."""
        from ouroboros.reflection import should_generate_reflection, NONTRIVIAL_COST_THRESHOLD
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace, rounds=0, cost_usd=NONTRIVIAL_COST_THRESHOLD) is True
        assert should_generate_reflection(clean_trace, rounds=0, cost_usd=NONTRIVIAL_COST_THRESHOLD - 0.01) is False

    def test_unknown_cost_is_not_treated_as_confirmed_zero(self):
        from ouroboros.reflection import should_generate_reflection

        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "ok", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace, rounds=0, cost_usd=None) is False

    def test_default_kwargs_clean_trace_no_reflection(self):
        """Default kwargs (rounds=0, cost unknown) keep clean behavior unchanged."""
        from ouroboros.reflection import should_generate_reflection
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace) is False

    def test_structured_non_zero_exit_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {
                "tool": "run_command",
                "args": {},
                "result": "exit_code=-9",
                "is_error": False,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
            },
        ]}
        assert should_generate_reflection(trace) is True


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestHelperFunctions:
    """_detect_markers and _collect_error_details must extract structured info."""

    def test_detect_markers_finds_all(self):
        """Owner item I24: the markers are the TYPED codes of the calls that went
        wrong, not eight hand-listed words matched against result bodies. The
        contract is unchanged — every failure class in the trace is named once,
        sorted — but a typed failure nobody thought to add to a list is now
        included, and the second call below (a refusal the host classified as a
        policy denial) is named by its own code."""
        from ouroboros.reflection import _detect_markers
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True, "status": "review_blocked",
             "tool_result_code": "REVIEW_BLOCKED", "result": "⚠️ REVIEW_BLOCKED: test"},
            {"tool": "run_command", "is_error": True, "status": "non_zero_exit",
             "tool_result_code": "SHELL_EXIT_ERROR", "result": "⚠️ SHELL_EXIT_ERROR: 2 failed"},
        ]}
        assert _detect_markers(trace) == ["REVIEW_BLOCKED", "SHELL_EXIT_ERROR"]

    def test_detect_markers_falls_back_to_a_legacy_rows_status(self):
        """A row written before the typed code existed still names its class."""
        from ouroboros.reflection import _detect_markers
        trace = {"tool_calls": [
            {"tool": "run_command", "is_error": True, "status": "timeout", "result": "boom"},
        ]}
        assert _detect_markers(trace) == ["timeout"]

    def test_detect_markers_empty_trace(self):
        from ouroboros.reflection import _detect_markers
        assert _detect_markers({}) == []
        assert _detect_markers({"tool_calls": []}) == []

    def test_collect_error_details_includes_tool_name(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True,
             "result": "⚠️ REVIEW_BLOCKED: test"},
        ]}
        details = _collect_error_details(trace)
        assert "commit_reviewed" in details
        assert "REVIEW_BLOCKED" in details

    def test_collect_error_details_respects_cap(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "run_command", "is_error": True,
             "result": "x" * 5000},
        ]}
        details = _collect_error_details(trace, cap=200)
        # v6.70.0: the canonical OMISSION NOTE is appended AFTER the cap
        # (utils.truncate_review_artifact semantics), replacing the legacy
        # in-budget "... [+N chars]" marker.
        assert details.startswith("[run_command]: " + "x" * 20)
        assert "OMISSION NOTE" in details
        assert len(details) <= 200 + 90  # cap + canonical marker overhead

    def test_collect_error_details_skips_clean_results(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "read_file", "is_error": False, "result": "file contents"},
            {"tool": "run_command", "is_error": True, "result": "error happened"},
        ]}
        details = _collect_error_details(trace)
        assert "read_file" not in details
        assert "run_command" in details

    def test_collect_error_details_includes_structured_status(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {
                "tool": "run_command",
                "is_error": True,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
                "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
            },
        ]}
        details = _collect_error_details(trace)
        assert "status=non_zero_exit" in details
        assert "signal=SIGKILL" in details

    def test_marker_mid_body_of_ok_result_is_not_error_evidence(self):
        """The contract this kept with a bounded head+tail view: a read of a doc
        that MENTIONS a marker (evidence-parity widened trace results to 15k-80k+)
        must not classify a clean task as errored. Typed codes hold it by
        construction — the text is never scanned — so the whole class is gone,
        including the mentions that landed inside the old 350+350 window."""
        from ouroboros.reflection import _detect_markers, should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "read_file", "is_error": False, "status": "ok",
             "result": "⚠️ SHELL_EXIT_ERROR appears in prose " + ("doc body " * 100)},
        ]}
        assert _detect_markers(trace) == []
        assert should_generate_reflection(trace) is False

    def test_a_typed_failure_is_detected_wherever_its_text_sits(self):
        """The other half of the same contract: a real failure is named from its
        own typed record, so neither a head marker nor a late tail verdict has to
        land inside a scan window to be seen."""
        from ouroboros.reflection import _detect_markers, should_generate_reflection
        head = {"tool_calls": [
            {"tool": "run_command", "is_error": True, "status": "non_zero_exit",
             "tool_result_code": "SHELL_EXIT_ERROR",
             "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=1." + ("x" * 2000)},
        ]}
        assert _detect_markers(head) == ["SHELL_EXIT_ERROR"]
        assert should_generate_reflection(head) is True

        tail = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True, "status": "review_blocked",
             "tool_result_code": "REVIEW_BLOCKED",
             "result": ("preflight log line\n" * 300) + "⚠️ TESTS_FAILED: 2 failed in suite"},
        ]}
        assert _detect_markers(tail) == ["REVIEW_BLOCKED"]
        assert should_generate_reflection(tail) is True

    def test_collect_error_details_keeps_breadth_across_large_errors(self):
        """v6.71.1 round-2: one oversized first error must not monopolize the 3000
        budget and hide later distinct errors — each snippet is pre-capped."""
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "run_command", "is_error": True, "result": "a" * 5000},
            {"tool": "run_script", "is_error": True, "result": "b" * 5000},
            {"tool": "edit_text", "is_error": True, "result": "c" * 5000},
        ]}
        details = _collect_error_details(trace)
        assert "run_command" in details
        assert "run_script" in details
        assert "edit_text" in details

    def test_run_reflection_pipeline_maps_usage_keys_correctly(self):
        """_run_reflection maps usage['rounds'] and usage['cost'] to the correct kwargs.

        Pins the dict-key contract: if 'cost' were renamed to 'cost_usd' inside
        _run_reflection, the cost threshold trigger would silently return 0.0 and
        this test would catch it.
        """
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import _run_reflection

        class FakeEnv:
            drive_root = __import__("pathlib").Path("/tmp/fake_drive")

        class FakeLlm:
            pass

        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "ok", "is_error": False},
        ]}
        high_cost_usage = {"rounds": 20, "cost": 6.0}

        with mock.patch("ouroboros.reflection.should_generate_reflection",
                        wraps=lambda trace, *, task=None, rounds=0, cost_usd=0.0: True) as mock_sgr, \
             mock.patch("ouroboros.reflection.generate_reflection",
                        return_value={"reflection": "ok", "backlog_candidates": []}) as mock_gen, \
             mock.patch("ouroboros.reflection.append_reflection") as mock_append:

            result = _run_reflection(
                FakeEnv(),
                FakeLlm(),
                {"id": "t1", "type": "task", "text": "goal"},
                high_cost_usage,
                clean_trace,
                {},
            )

        # should_generate_reflection must be called with the correct kwargs from usage dict
        mock_sgr.assert_called_once()
        _, kwargs = mock_sgr.call_args
        assert kwargs.get("rounds") == 20, f"Expected rounds=20, got {kwargs.get('rounds')}"
        assert kwargs.get("cost_usd") == 6.0, f"Expected cost_usd=6.0, got {kwargs.get('cost_usd')}"

        # When should_generate_reflection returns True, generate+append must be called
        mock_gen.assert_called_once()
        mock_append.assert_called_once()
        assert result is not None

    def test_run_reflection_pipeline_propagates_unknown_cost(self):
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import _run_reflection

        class FakeEnv:
            drive_root = __import__("pathlib").Path("/tmp/fake_drive")

        captured = {}

        def should_run(trace, *, task=None, rounds=0, cost_usd=None):
            captured["cost_usd"] = cost_usd
            return False

        with mock.patch("ouroboros.reflection.should_generate_reflection", side_effect=should_run):
            result = _run_reflection(
                FakeEnv(),
                object(),
                {"id": "t-unknown", "type": "task", "text": "goal"},
                {"rounds": 1, "cost": None, "cost_final": False},
                {"tool_calls": []},
                {},
            )

        assert result is None
        assert captured["cost_usd"] is None

    def test_generate_reflection_uses_nontrivial_prompt_for_clean_trace(self):
        """generate_reflection picks the non-error prompt for a clean, high-round trace."""
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens, model_role=""):
                assert model_role == "light"
                captured["prompt"] = messages[0]["content"]
                return {"content": "Friction was in repeated advisory runs."}, {"cost": 0}

        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
            {"tool": "run_command", "args": {}, "result": "ok", "is_error": False},
        ]}
        entry = generate_reflection(
            task={"id": "task-2", "type": "task", "text": "A 20-round clean task"},
            llm_trace=clean_trace,
            trace_summary="20 tool calls, 0 errors",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 20, "cost": 6.0},
        )

        prompt = captured["prompt"]
        # Non-error prompt markers must be present
        assert "high round count or high cost" in prompt, "Expected nontrivial prompt framing"
        assert "Where was the friction?" in prompt, "Expected friction question"
        # Error-only prompt text must NOT appear
        assert "The task had errors" not in prompt, "Error-only prompt must not be used for clean trace"
        assert entry["reflection"] == "Friction was in repeated advisory runs."

    def test_generate_reflection_uses_error_prompt_for_error_trace(self):
        """generate_reflection picks the error prompt when trace contains blocking markers."""
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens, model_role=""):
                assert model_role == "light"
                captured["prompt"] = messages[0]["content"]
                return {"content": "Root cause was missing tests."}, {"cost": 0}

        error_trace = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False,
             "status": "review_blocked", "tool_result_code": "REVIEW_BLOCKED",
             "result": "⚠️ REVIEW_BLOCKED: tests_affected"},
        ]}
        generate_reflection(
            task={"id": "task-3", "type": "task", "text": "Blocked commit"},
            llm_trace=error_trace,
            trace_summary="1 tool call, 0 errors (but REVIEW_BLOCKED)",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 5, "cost": 1.0},
        )
        prompt = captured["prompt"]
        assert "The task had errors or blocking events" in prompt
        assert "high round count or high cost" not in prompt

    def test_generate_reflection_includes_review_evidence(self):
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens, model_role=""):
                assert model_role == "light"
                captured["prompt"] = messages[0]["content"]
                return {"content": "Reflection mentions tests_affected."}, {"cost": 0}

        entry = generate_reflection(
            task={"id": "task-1", "type": "task", "text": "Fix commit flow"},
            llm_trace={"tool_calls": [{
                "tool": "commit_reviewed",
                "is_error": False,
                "result": "⚠️ REVIEW_BLOCKED: blocked by tests_affected",
            }]},
            trace_summary="repo_commit blocked",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 3, "cost": 0.01},
            review_evidence={
                "has_evidence": True,
                "recent_attempts": [{
                    "status": "blocked",
                    "critical_findings": [{
                        "severity": "critical",
                        "item": "tests_affected",
                        "reason": "broken",
                    }],
                }],
            },
        )

        assert "Structured review evidence" in captured["prompt"]
        assert "tests_affected" in captured["prompt"]
        assert entry["review_evidence"]["has_evidence"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Reflection context loading
# ─────────────────────────────────────────────────────────────────────────────

class TestReflectionContextLoading:
    """build_recent_sections must load execution reflections from JSONL."""

    def test_reflections_loaded_when_file_exists(self):
        from ouroboros.context import build_recent_sections
        source = inspect.getsource(build_recent_sections)
        assert "task_reflections.jsonl" in source, (
            "build_recent_sections must load from task_reflections.jsonl"
        )
        assert "Execution reflections" in source, (
            "Section header must contain 'Execution reflections'"
        )

    def test_reflection_entry_format(self):
        """Reflection entries must include required fields."""
        from ouroboros.reflection import _detect_markers, _collect_error_details
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True, "status": "review_blocked",
             "tool_result_code": "REVIEW_BLOCKED", "result": "⚠️ REVIEW_BLOCKED: test"},
        ]}
        markers = _detect_markers(trace)
        assert "REVIEW_BLOCKED" in markers

        details = _collect_error_details(trace)
        assert "commit_reviewed" in details
        assert "REVIEW_BLOCKED" in details


# ─────────────────────────────────────────────────────────────────────────────
# emit_task_results: reflection stays OFF the reply critical path
# ─────────────────────────────────────────────────────────────────────────────

class TestEmitTaskResultsReflectionNotOnCriticalPath:
    """Regression guard for the v4.39.0 UX regression: reflection and backlog
    persistence must not run on the reply critical path. The synchronous
    variant added a 1–3 s LLM round before `send_message` was dispatched;
    the async daemon-thread variant keeps reply latency low.
    """

    def _make_minimal_env(self, tmp_path):
        import pathlib

        class FakeEnv:
            drive_root = tmp_path
            repo_dir = str(tmp_path)

            def drive_path(self, sub):
                return pathlib.Path(self.drive_root) / sub

        return FakeEnv()

    def test_reflection_not_called_synchronously_in_emit_task_results(self, tmp_path):
        """emit_task_results must NOT invoke `_run_reflection` inline — it
        belongs inside the daemon thread started by
        `_run_post_task_processing_async`."""
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import emit_task_results

        env = self._make_minimal_env(tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        task = {"id": "t1", "type": "task", "text": "goal", "chat_id": 1}
        usage = {"rounds": 5, "cost": 1.0, "prompt_tokens": 0, "completion_tokens": 0}
        llm_trace = {"tool_calls": []}

        with mock.patch("ouroboros.agent_task_pipeline._run_reflection") as mock_refl, \
             mock.patch("ouroboros.agent_task_pipeline._update_improvement_backlog") as mock_bl, \
             mock.patch("ouroboros.agent_task_pipeline._run_post_task_processing_async") as mock_async, \
             mock.patch("ouroboros.agent_task_pipeline._run_chat_consolidation"), \
             mock.patch("ouroboros.agent_task_pipeline._run_scratchpad_consolidation"), \
             mock.patch("ouroboros.agent_task_pipeline._store_task_result"), \
             mock.patch("ouroboros.review_evidence.collect_review_evidence",
                        return_value={}, create=True):
            pending_events: list = []
            emit_task_results(
                env=env, memory=mock.MagicMock(), llm=mock.MagicMock(),
                pending_events=pending_events,
                task=task, text="reply",
                usage=usage, llm_trace=llm_trace,
                start_time=0.0,
                drive_logs=tmp_path / "logs",
            )

        # Reflection and backlog are the responsibility of the async helper —
        # emit_task_results must not call them directly. Otherwise reply
        # latency regresses.
        mock_refl.assert_not_called()
        mock_bl.assert_not_called()
        # The async helper must still be invoked exactly once.
        mock_async.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# F5: durable reflection routing (project drive + canonical pointer)
# ─────────────────────────────────────────────────────────────────────────────

def test_append_reflection_routed_project_task_writes_project_drive_and_pointer(tmp_path, monkeypatch):
    """A project-scoped root's FULL reflection lands on the project drive; the
    canonical log gets a bounded pointer row (never the full text — it feeds
    future global context); the prunable mirror drive gets nothing."""
    import json
    import types

    import ouroboros.project_facts as pf
    from ouroboros.reflection import append_reflection_routed

    monkeypatch.setattr(pf, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
    canonical = tmp_path / "data"
    mirror = tmp_path / "mirror"  # headless mirror drive — prunable, never the home
    env = types.SimpleNamespace(drive_root=mirror)
    task = {"id": "t-proj", "project_id": "slime", "budget_drive_root": str(canonical)}
    entry = {
        "ts": "2026-08-10T00:00:00Z", "task_id": "t-proj",
        "reflection": "full project-local reflection text",
    }

    append_reflection_routed(env, task, entry)

    project_log = tmp_path / "projects" / "slime" / "logs" / "task_reflections.jsonl"
    rows = [json.loads(line) for line in project_log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reflection"] == "full project-local reflection text"

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    pointer_rows = [json.loads(line) for line in canonical_log.read_text(encoding="utf-8").splitlines()]
    assert pointer_rows[0]["type"] == "project_reflection_pointer"
    assert pointer_rows[0]["task_id"] == "t-proj"
    assert pointer_rows[0]["project_id"] == "slime"
    assert pointer_rows[0]["reflection_path"] == str(project_log)
    assert "write_failed" not in pointer_rows[0]  # successful project append
    assert "full project-local reflection text" not in canonical_log.read_text(encoding="utf-8")
    assert not (mirror / "logs" / "task_reflections.jsonl").exists()


def test_append_reflection_routed_stamps_pointer_when_project_write_fails(tmp_path, monkeypatch):
    """Finding 4: a failed project-drive append must not leave a pointer that
    claims a full text exists — the pointer row is stamped write_failed."""
    import json
    import types

    import ouroboros.project_facts as pf
    import ouroboros.reflection as refl

    monkeypatch.setattr(pf, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
    canonical = tmp_path / "data"
    env = types.SimpleNamespace(drive_root=tmp_path / "mirror")
    task = {"id": "t-proj", "project_id": "slime", "budget_drive_root": str(canonical)}
    entry = {"ts": "2026-08-10T00:00:00Z", "task_id": "t-proj",
             "reflection": "full project-local reflection text"}

    real_append = refl.append_jsonl

    def _selective(path, row):
        if str(tmp_path / "projects") in str(path):
            raise OSError("disk full")
        return real_append(path, row)

    monkeypatch.setattr(refl, "append_jsonl", _selective)

    refl.append_reflection_routed(env, task, entry)

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    pointer = json.loads(canonical_log.read_text(encoding="utf-8").splitlines()[0])
    assert pointer["type"] == "project_reflection_pointer"
    assert pointer["write_failed"] is True
    assert "full project-local reflection text" not in canonical_log.read_text(encoding="utf-8")


def test_recent_reflections_renders_pointer_row_as_single_line():
    """Finding 3: a canonical pointer row (no reflection text) renders one
    informative line instead of an empty block burning a context slot."""
    from ouroboros.context import _format_recent_reflections

    text = _format_recent_reflections([{
        "ts": "2026-08-10T00:00:00Z",
        "task_id": "t-proj",
        "type": "project_reflection_pointer",
        "project_id": "slime",
        "reflection_path": "/proj/slime/logs/task_reflections.jsonl",
    }])

    assert ("Full reflection lives on project drive: slime — "
            "/proj/slime/logs/task_reflections.jsonl") in text
    assert len(text.strip().splitlines()) == 2  # header + one pointer line


def test_append_reflection_routed_non_project_task_uses_canonical_drive(tmp_path):
    """A non-project root reflects on the canonical budget drive in full — never
    on the prunable mirror the split root executes on."""
    import json
    import types

    from ouroboros.reflection import append_reflection_routed

    canonical = tmp_path / "data"
    mirror = tmp_path / "mirror"
    env = types.SimpleNamespace(drive_root=mirror)
    task = {"id": "t-plain", "budget_drive_root": str(canonical)}
    entry = {"ts": "2026-08-10T00:00:00Z", "task_id": "t-plain", "reflection": "plain reflection"}

    append_reflection_routed(env, task, entry)

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    rows = [json.loads(line) for line in canonical_log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reflection"] == "plain reflection"
    assert not (mirror / "logs" / "task_reflections.jsonl").exists()


def test_pattern_register_admission_is_typed_not_a_marker_scan(tmp_path, monkeypatch):
    """Owner item I24: the Pattern Register opens on typed error evidence.

    The gate used to be ``key_markers`` alone, and while that field was a
    substring scan the register was blind twice over: a typed failure whose word
    nobody had listed never opened it, and a root whose own calls all succeeded
    while its CHILDREN failed never did either. Children do not reflect
    (ARCHITECTURE, Post-task reflection), so the root's collected child classes
    are the only way their failures reach the register at all.

    A clean non-trivial task (reflected on for rounds/cost, no failure anywhere)
    still must NOT open it, which is why the gate is not "reason_code is set":
    that opens on every terminal."""
    import ouroboros.reflection as reflection

    seen = []
    monkeypatch.setattr(reflection, "_update_patterns", lambda root, entry: seen.append(entry))

    base = {"ts": "2026-09-11T00:00:00Z", "task_id": "t", "reflection": "text"}

    reflection.append_reflection(tmp_path, {**base, "key_markers": [], "error_count": 2})
    assert len(seen) == 1, "an errored call with no typed code still admits"

    reflection.append_reflection(tmp_path, {
        **base, "key_markers": [], "error_count": 0, "child_failure_classes": ["failed"],
    })
    assert len(seen) == 2, "a root whose only failures are its children admits"

    reflection.append_reflection(tmp_path, {
        **base, "key_markers": ["SHELL_EXIT_ERROR"], "error_count": 1,
    })
    assert len(seen) == 3, "typed codes still admit"

    reflection.append_reflection(tmp_path, {
        **base, "key_markers": [], "error_count": 0, "child_failure_classes": [],
    })
    assert len(seen) == 3, "a clean non-trivial task does not open the register"

    # loop_evidence_unavailable stores error_count=None; that is not evidence.
    reflection.append_reflection(tmp_path, {**base, "key_markers": [], "error_count": None})
    assert len(seen) == 3


def test_child_failure_classes_reach_the_root_reflection(tmp_path):
    """The typed child classes come from the walk the evidence collector already
    does: one collector, one walk, no second register writer, and children still
    do not reflect on their own."""
    from ouroboros.post_task_synthesis import _child_failure_classes

    rows = [
        {"task_id": "c1", "outcome_axes": {"execution": {"status": "ok"}}},
        {"task_id": "c2", "outcome_axes": {"execution": {"status": "failed"}}},
        {"task_id": "c3", "outcome_axes": {"execution": {"status": "infra_failed"}}},
        {"task_id": "c4", "outcome_axes": {"execution": {"status": "failed"}}},
        {"task_id": "c5"},
    ]
    assert _child_failure_classes(rows) == ["failed", "infra_failed"]
    assert _child_failure_classes([]) == []
    assert _child_failure_classes(None) == []


def test_only_a_genuinely_failed_child_admits_the_register(tmp_path):
    """"Not ok" is not "failed".

    A child the parent cancelled in an ordinary cascade, one that soft-landed
    best_effort on a rail, and a degraded one all end non-ok with nothing having
    gone wrong. Admitting them opened the Pattern Register - a paid light-model
    rewrite - on a clean root, with an empty markers line and nothing to learn.
    Built through the real normalizer, because the raw rows the walk reads carry
    only a status.
    """
    from ouroboros.outcomes import normalize_outcome_axes
    from ouroboros.post_task_synthesis import _child_failure_classes
    from ouroboros.reflection import _admits_pattern_register

    def _classes(item):
        return _child_failure_classes([{"outcome_axes": normalize_outcome_axes(item)}])

    for benign in (
        {"task_id": "c", "status": "cancelled"},
        {"task_id": "c", "status": "completed",
         "outcome_axes": {"execution": {"status": "best_effort"}}},
        {"task_id": "c", "status": "completed",
         "outcome_axes": {"execution": {"status": "degraded"}}},
        {"task_id": "c", "status": "completed"},
    ):
        classes = _classes(benign)
        assert classes == [], benign
        assert _admits_pattern_register(
            {"error_count": 0, "key_markers": [], "child_failure_classes": classes},
        ) is False, benign

    for failure in (
        {"task_id": "c", "status": "failed", "outcome_axes": {"execution": {"status": "failed"}}},
        {"task_id": "c", "status": "failed",
         "outcome_axes": {"execution": {"status": "infra_failed"}}},
    ):
        classes = _classes(failure)
        assert classes and _admits_pattern_register(
            {"error_count": 0, "key_markers": [], "child_failure_classes": classes},
        ) is True, failure
