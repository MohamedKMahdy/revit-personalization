"""
Execution-safety boundary tests.

Verifies the pipeline can only emit/dispatch allowlisted tools and that
send_code_to_revit (arbitrary Roslyn C#) is rejected in code — never forwarded
to the Revit plugin. This is the enforced constraint behind the thesis claim
that the personalization pipeline executes only named, bounded tools.

Run:  pytest tests/test_execution_safety.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.tool_allowlist import (
    PERMITTED_PIPELINE_TOOLS,
    DisallowedToolError,
    assert_tool_allowed,
    is_permitted,
    validate_tool_sequence,
)
import mcp_server.revit_bridge as bridge


# ── the allowlist itself ───────────────────────────────────────────────────────

def test_send_code_to_revit_not_permitted():
    assert "send_code_to_revit" not in PERMITTED_PIPELINE_TOOLS
    assert not is_permitted("send_code_to_revit")


def test_assert_tool_allowed_rejects_arbitrary_code():
    with pytest.raises(DisallowedToolError, match="arbitrary code execution"):
        assert_tool_allowed("send_code_to_revit")


def test_assert_tool_allowed_rejects_unknown_tool():
    with pytest.raises(DisallowedToolError):
        assert_tool_allowed("delete_everything")


def test_permitted_tools_pass():
    for tool in ("place_element", "set_parameter", "create_annotation_tag"):
        assert_tool_allowed(tool)  # must not raise


def test_validate_sequence_rejects_send_code():
    seq = [
        {"tool": "place_element", "arguments": {}},
        {"tool": "send_code_to_revit", "arguments": {"code": "Document.Delete(...);"}},
    ]
    with pytest.raises(DisallowedToolError, match="send_code_to_revit"):
        validate_tool_sequence(seq)


def test_validate_sequence_accepts_clean():
    seq = [
        {"tool": "place_element", "arguments": {}},
        {"tool": "set_parameter", "arguments": {}},
        {"tool": "create_annotation_tag", "arguments": {}},
    ]
    validate_tool_sequence(seq)  # must not raise


# ── end-to-end through the bridge (the real execution chokepoint) ──────────────

def test_bridge_rejects_send_code_without_calling_plugin(monkeypatch):
    """A sequence containing send_code_to_revit must be rejected BEFORE any TCP
    call reaches the Revit plugin."""
    calls: list[tuple[str, dict]] = []

    def _spy(command, params, timeout=bridge.REVIT_PLUGIN_TIMEOUT):
        calls.append((command, params))
        return {"elementId": 123}

    monkeypatch.setattr(bridge, "_call_plugin", _spy)

    malicious = [{"tool": "send_code_to_revit", "arguments": {"code": "anything"}}]
    with pytest.raises(DisallowedToolError):
        bridge.execute_tool_sequence(malicious)

    # The plugin must never have been contacted.
    assert calls == [], f"plugin was called despite rejection: {calls}"
    assert not any(c[0] == "send_code_to_revit" for c in calls)


def test_bridge_dispatch_direct_rejects(monkeypatch):
    """_dispatch_tool itself refuses a forbidden tool (no else-passthrough)."""
    calls: list[str] = []
    monkeypatch.setattr(bridge, "_call_plugin", lambda c, p, timeout=30: calls.append(c) or {})

    with pytest.raises(DisallowedToolError):
        bridge._dispatch_tool("send_code_to_revit", {"code": "x"})
    assert calls == []


def test_bridge_allows_permitted_tool(monkeypatch):
    """Sanity: the guard does not over-block — a permitted tool still dispatches."""
    calls: list[str] = []

    def _spy(command, params, timeout=bridge.REVIT_PLUGIN_TIMEOUT):
        calls.append(command)
        return {"elementId": 123}

    monkeypatch.setattr(bridge, "_call_plugin", _spy)

    results = bridge.execute_tool_sequence(
        [{"tool": "place_element", "arguments": {"family_type": "M_Single-Flush",
                                                 "location": {"x": 0, "y": 0, "z": 0}}}]
    )
    assert results, "permitted tool should have executed"
    assert "create_point_based_element" in calls
    assert "send_code_to_revit" not in calls
