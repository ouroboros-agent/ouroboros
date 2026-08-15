"""Executable coverage for T4.7b1 native core result producers."""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap
from types import SimpleNamespace

import pytest

from ouroboros.cancel_intents import request_cancel
from ouroboros.loop_tool_execution import (
    _extract_result_metadata,
    _is_tool_execution_failure,
)
from ouroboros.task_results import STATUS_RUNNING, STATUS_SCHEDULED, write_task_result
from ouroboros.tools import core, core_artifacts, core_file_tools
from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.tool_result import (
    TOOL_CODE_SPECS,
    LegacyTextResultAdapter,
    ToolCodeSpec,
    ToolResult,
    _publish_builtin_result,
)


_NATIVE_CORE_BRANCH_FAMILIES = {
    "read_file": {"success", "not_found"},
    "list_files": {"success", "listing_error"},
    "search_code": {"success", "argument_error", "not_found"},
    "send_photo": {
        "success", "argument_warning", "unavailable_warning", "size_warning", "io_warning",
    },
    "send_video": {
        "success", "argument_warning", "not_found_warning", "size_warning", "io_warning",
    },
    "send_file": {
        "success", "argument_warning", "artifact_url_fallback", "size_warning", "io_warning",
    },
    "forward_to_worker": {
        "success",
        "argument_error",
        "not_found",
        "inactive",
        "cancel_pending",
        "forbidden",
    },
}


def test_legacy_compatibility_pair_is_closed_paired_and_strictly_typed() -> None:
    expected = {
        "legacy_status": "resource_policy_blocked",
        "legacy_is_error": True,
    }
    assert ToolResult(
        status="blocked",
        code="RESOURCE_BLOCKED",
        text="blocked",
        meta=expected,
    ).meta == expected

    for meta in (
        {"legacy_status": "ok"},
        {"legacy_is_error": False},
    ):
        with pytest.raises(ValueError, match="provided together"):
            ToolResult(status="ok", code="OK", text="done", meta=meta)
    for status in ("", "future_unregistered_status", 3):
        with pytest.raises(ValueError, match="compatibility status"):
            ToolResult(
                status="ok",
                code="OK",
                text="done",
                meta={"legacy_status": status, "legacy_is_error": False},
            )
    for value in (0, 1, "false", None):
        with pytest.raises(TypeError, match="must be a boolean"):
            ToolResult(
                status="ok",
                code="OK",
                text="done",
                meta={"legacy_status": "ok", "legacy_is_error": value},
            )


def test_legacy_pair_uses_host_reserve_without_reducing_producer_budgets() -> None:
    pair = {"legacy_status": "ok", "legacy_is_error": False}
    all_host = {
        "route_note": True,
        "safety_warning": True,
        "ambiguous_safety_wrapper": True,
        "owner_state_restored": True,
        "light_repo_changed": True,
        "workspace_git_refs_changed": True,
        **pair,
    }
    producer_items = {f"k{index}": index for index in range(32)}
    result = ToolResult(
        status="ok",
        code="OK",
        text="done",
        meta={**producer_items, **all_host},
    )
    assert len(result.meta) == 40
    with pytest.raises(ValueError, match="at most 32 non-host keys"):
        ToolResult(
            status="ok",
            code="OK",
            text="done",
            meta={**producer_items, "overflow": True, **pair},
        )

    exact_bytes = ToolResult(
        status="ok",
        code="OK",
        text="done",
        meta={"x": "a" * 8184, **pair},
    )
    assert exact_bytes.meta["legacy_status"] == "ok"
    with pytest.raises(ValueError, match="exceeds 8192"):
        ToolResult(
            status="ok",
            code="OK",
            text="done",
            meta={"x": "a" * 8185, **pair},
        )


def test_builtin_publisher_preserves_direct_string_abi_without_active_sidecar() -> None:
    ctx = SimpleNamespace()
    text = "exact bytes \u2603\n"
    assert _publish_builtin_result(
        ctx,
        "RESOURCE_NOT_FOUND",
        text,
        legacy_status="ok",
        legacy_is_error=False,
        meta={"source": "fixture"},
    ) == text
    assert not hasattr(ctx, "_active_builtin_tool_result")


def test_resource_codes_are_centrally_triaged() -> None:
    assert TOOL_CODE_SPECS["RESOURCE_NOT_FOUND"] == ToolCodeSpec(
        status="unavailable",
        outcome_bucket="resource_not_found",
        ui_severity="warning",
        recovery="select an existing resource or create it through its owning workflow",
    )
    assert TOOL_CODE_SPECS["RESOURCE_UNAVAILABLE"] == ToolCodeSpec(
        status="unavailable",
        outcome_bucket="resource_unavailable",
        ui_severity="warning",
        recovery="restore the resource or retry when it becomes available",
    )


def _registry(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ToolRegistry, ToolContext, pathlib.Path, pathlib.Path]:
    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setattr(
        "ouroboros.safety.check_safety",
        lambda *_args, **_kwargs: (True, ""),
    )
    registry = ToolRegistry(repo_dir=repo, drive_root=drive)
    ctx = ToolContext(
        repo_dir=repo,
        drive_root=drive,
        current_chat_id=17,
        task_id="parent",
    )
    registry.set_context(ctx)
    return registry, ctx, repo, drive


def _assert_legacy_pair(tool_name: str, result: ToolResult) -> None:
    actual_error = _is_tool_execution_failure(True, result.text, result)
    actual_status = _extract_result_metadata(
        tool_name,
        result.text,
        actual_error,
        result,
    )["status"]
    assert result.meta["legacy_is_error"] is actual_error
    assert result.meta["legacy_status"] == actual_status


def _disable_legacy_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LegacyTextResultAdapter,
        "from_text",
        classmethod(
            lambda _cls, *_args, **_kwargs: pytest.fail(
                "native core producer used LegacyTextResultAdapter"
            )
        ),
    )


def test_read_list_and_search_registry_paths_are_native(
    tmp_path,
    monkeypatch,
) -> None:
    registry, _ctx, repo, _drive = _registry(tmp_path, monkeypatch)
    (repo / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    _disable_legacy_adapter(monkeypatch)

    cases = (
        ("read_file", {"path": "sample.txt"}, "OK"),
        ("read_file", {"path": "missing.txt"}, "RESOURCE_NOT_FOUND"),
        ("list_files", {"path": "."}, "OK"),
        ("list_files", {"path": "missing"}, "TOOL_ERROR"),
        ("search_code", {"query": "alpha", "path": "."}, "OK"),
        ("search_code", {"query": "", "path": "."}, "TOOL_ARG_ERROR"),
        (
            "search_code",
            {"query": "alpha", "path": "missing"},
            "RESOURCE_NOT_FOUND",
        ),
    )
    for tool_name, args, code in cases:
        result = registry.execute_result(tool_name, args)
        assert result.code == code
        registry._ctx._read_file_seen = {}
        assert registry.execute(tool_name, args) == result.text
        _assert_legacy_pair(tool_name, result)


def test_media_registry_paths_are_native_and_preserve_warning_compatibility(
    tmp_path,
    monkeypatch,
) -> None:
    registry, ctx, repo, _drive = _registry(tmp_path, monkeypatch)
    image = repo / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    video = repo / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    document = repo / "report.txt"
    document.write_text("report", encoding="utf-8")
    _disable_legacy_adapter(monkeypatch)

    cases = (
        ("send_photo", {}, "TOOL_ARG_ERROR"),
        ("send_photo", {"file_path": str(image)}, "OK"),
        ("send_video", {"file_path": "missing.mp4"}, "RESOURCE_NOT_FOUND"),
        ("send_video", {"file_path": str(video)}, "OK"),
        ("send_file", {}, "TOOL_ARG_ERROR"),
        ("send_file", {"file_path": str(document)}, "OK"),
    )
    for tool_name, args, code in cases:
        result = registry.execute_result(tool_name, args)
        assert result.code == code
        _assert_legacy_pair(tool_name, result)

    ctx.current_chat_id = None
    unavailable = registry.execute_result("send_photo", {"file_path": str(image)})
    assert unavailable.code == "RESOURCE_UNAVAILABLE"
    assert unavailable.meta["legacy_status"] == "ok"
    assert unavailable.meta["legacy_is_error"] is False


def test_send_file_keeps_artifact_fact_when_download_url_generation_fails(
    tmp_path,
    monkeypatch,
) -> None:
    registry, ctx, repo, drive = _registry(tmp_path, monkeypatch)
    document = repo / "report.txt"
    document.write_text("report", encoding="utf-8")
    ctx.drive_root = drive
    monkeypatch.setattr(
        "ouroboros.gateway.files.download_url_for_local_file",
        lambda _path: (_ for _ in ()).throw(RuntimeError("URL unavailable")),
    )
    _disable_legacy_adapter(monkeypatch)

    result = registry.execute_result("send_file", {"file_path": str(document)})

    assert result.code == "OK"
    assert result.meta["artifact_registered"] is True
    assert ctx.pending_events[-1]["download_url"] == ""
    _assert_legacy_pair("send_file", result)


def test_media_size_and_io_warning_families_remain_native_ok_compatibility(
    tmp_path,
    monkeypatch,
) -> None:
    from ouroboros.tools import core_artifacts

    registry, _ctx, repo, _drive = _registry(tmp_path, monkeypatch)
    paths = {
        "send_photo": repo / "image.png",
        "send_video": repo / "video.mp4",
        "send_file": repo / "report.txt",
    }
    for path in paths.values():
        path.write_bytes(b"fixture bytes")
    _disable_legacy_adapter(monkeypatch)

    for constant in (
        "_MAX_PHOTO_FILE_BYTES",
        "_MAX_VIDEO_FILE_BYTES",
        "_MAX_DOCUMENT_FILE_BYTES",
    ):
        monkeypatch.setattr(core_artifacts, constant, 1)
    for tool_name, path in paths.items():
        result = registry.execute_result(tool_name, {"file_path": str(path)})
        assert result.code == "TOOL_ARG_ERROR"
        _assert_legacy_pair(tool_name, result)

    monkeypatch.setattr(core_artifacts, "_MAX_PHOTO_FILE_BYTES", 1000)
    monkeypatch.setattr(core_artifacts, "_MAX_VIDEO_FILE_BYTES", 1000)
    monkeypatch.setattr(core_artifacts, "_MAX_DOCUMENT_FILE_BYTES", 1000)
    original_read_bytes = pathlib.Path.read_bytes

    def fail_fixture_read(path: pathlib.Path) -> bytes:
        if path in paths.values():
            raise OSError("fixture read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fail_fixture_read)
    for tool_name, path in paths.items():
        result = registry.execute_result(tool_name, {"file_path": str(path)})
        assert result.code == "TOOL_ERROR"
        _assert_legacy_pair(tool_name, result)


def test_forward_registry_terminal_families_are_native(
    tmp_path,
    monkeypatch,
) -> None:
    registry, ctx, _repo, drive = _registry(tmp_path, monkeypatch)
    child_drive = drive / "child"
    child_drive.mkdir()
    write_task_result(
        drive,
        "child",
        STATUS_RUNNING,
        child_drive_root=str(child_drive),
        parent_task_id="parent",
        root_task_id="parent",
        result="running",
    )
    write_task_result(
        drive,
        "queued",
        STATUS_SCHEDULED,
        parent_task_id="parent",
        root_task_id="parent",
        result="queued",
    )
    write_task_result(
        drive,
        "foreign",
        STATUS_RUNNING,
        parent_task_id="other",
        root_task_id="other",
        result="running",
    )
    write_task_result(
        drive,
        "cancelling",
        STATUS_RUNNING,
        parent_task_id="parent",
        root_task_id="parent",
        result="running",
    )
    request_cancel(drive, "cancelling", reason="fixture teardown")
    _disable_legacy_adapter(monkeypatch)

    cases = (
        ({"task_id": "../bad", "message": "x"}, "TOOL_ARG_ERROR"),
        ({"task_id": "missing", "message": "x"}, "RESOURCE_NOT_FOUND"),
        ({"task_id": "queued", "message": "x"}, "RESOURCE_UNAVAILABLE"),
        ({"task_id": "cancelling", "message": "x"}, "RESOURCE_UNAVAILABLE"),
        ({"task_id": "foreign", "message": "x"}, "ACCESS_BLOCKED"),
        ({"task_id": "child", "message": "continue"}, "OK"),
    )
    for args, code in cases:
        result = registry.execute_result("forward_to_worker", args)
        assert result.code == code
        _assert_legacy_pair("forward_to_worker", result)

    assert (child_drive / "memory" / "owner_mailbox" / "child.jsonl").is_file()
    assert ctx.task_id == "parent"


def test_native_branch_inventory_names_every_migrated_entry() -> None:
    assert set(_NATIVE_CORE_BRANCH_FAMILIES) == {
        "read_file",
        "list_files",
        "search_code",
        "send_photo",
        "send_video",
        "send_file",
        "forward_to_worker",
    }
    assert all(families for families in _NATIVE_CORE_BRANCH_FAMILIES.values())


def test_native_handler_return_inventory_has_no_untyped_string_terminal() -> None:
    def direct_returns(function) -> list[ast.expr | None]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        root = tree.body[0]
        assert isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef))
        returns: list[ast.expr | None] = []

        class ReturnVisitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if node is root:
                    self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Return(self, node: ast.Return) -> None:
                returns.append(node.value)

        ReturnVisitor().visit(root)
        return returns

    strict_publishers = (
        core_artifacts._send_photo,
        core_artifacts._send_video,
        core_artifacts._send_file,
        core._forward_to_worker,
    )
    for function in strict_publishers:
        for value in direct_returns(function):
            assert isinstance(value, ast.Call)
            assert isinstance(value.func, ast.Name)
            assert value.func.id == "_publish_builtin_result"

    allowed_calls = {
        "_publish_builtin_result",
        "_annotate_reread",
        "_repo_list",
        "_data_list",
    }
    for function in (
        core_file_tools._read_file,
        core_file_tools._list_files,
        core._code_search,
    ):
        for value in direct_returns(function):
            if isinstance(value, ast.Name):
                assert value.id in {"block", "block_msg"}
                continue
            assert isinstance(value, ast.Call)
            if isinstance(value.func, ast.Name):
                call_name = value.func.id
            else:
                assert isinstance(value.func, ast.Attribute)
                call_name = value.func.attr
            assert call_name in allowed_calls

    for function in (
        core_file_tools._read_file,
        core_file_tools._list_files,
        core._code_search,
    ):
        source = inspect.getsource(function)
        assert "publish_result=True" in source


def test_loop_ignores_forged_compatibility_carrier_until_consumer_cutover() -> None:
    forged = ToolResult(
        status="blocked",
        code="ACCESS_BLOCKED",
        text="plain legacy success",
        meta={"legacy_status": "error", "legacy_is_error": True},
    )

    actual_error = _is_tool_execution_failure(True, forged.text, forged)
    actual = _extract_result_metadata(
        "read_file",
        forged.text,
        actual_error,
        forged,
    )

    assert actual_error is False
    assert actual["status"] == "ok"
    assert (forged.status, forged.code) == ("blocked", "ACCESS_BLOCKED")
