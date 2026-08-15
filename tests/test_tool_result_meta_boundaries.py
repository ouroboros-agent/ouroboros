"""Boundary tests for producer and host-owned ToolResult metadata."""

from __future__ import annotations

import json

import pytest

from ouroboros.tools.tool_result import ToolResult, _compose_execute_result_result


def test_composition_reserves_host_keys_beyond_32_producer_items() -> None:
    ordinary = {f"k{index}": index for index in range(32)}
    base = ToolResult(
        status="error",
        code="TOOL_ERROR",
        text="failed",
        meta=ordinary,
    )

    composed = _compose_execute_result_result("fixture", base, "route", "warning")

    assert composed.meta == {
        **ordinary,
        "route_note": True,
        "safety_warning": True,
    }
    with pytest.raises(ValueError, match="at most 32"):
        ToolResult(
            status="ok",
            code="OK",
            text="done",
            meta={f"k{index}": index for index in range(33)},
        )


def test_composition_reserves_host_bytes_beyond_exact_producer_limit() -> None:
    empty_payload_size = len(json.dumps({"payload": ""}, separators=(",", ":")))
    exact_meta = {"payload": "x" * (8192 - empty_payload_size)}
    assert len(json.dumps(exact_meta, separators=(",", ":")).encode()) == 8192
    base = ToolResult(
        status="ok",
        code="GIT_ERROR",
        text="failed",
        meta=exact_meta,
    )

    composed = _compose_execute_result_result("fixture", base, "route", "warning")

    assert composed.meta == {
        **exact_meta,
        "route_note": True,
        "safety_warning": True,
    }
    empty_host_size = len(json.dumps({"route_note": ""}, separators=(",", ":")))
    existing_host_meta = {"route_note": "x" * (8192 - empty_host_size)}
    existing = ToolResult(status="ok", code="OK", text="done", meta=existing_host_meta)
    assert _compose_execute_result_result(
        "fixture",
        existing,
        "route",
        "",
    ).meta == {"route_note": True}
    with pytest.raises(ValueError, match="8192"):
        ToolResult(
            status="ok",
            code="OK",
            text="done",
            meta={"payload": exact_meta["payload"] + "x"},
        )
    with pytest.raises(ValueError, match="reserved overhead"):
        ToolResult(
            status="ok",
            code="OK",
            text="done",
            meta={"route_note": "x" * 9000},
        )
