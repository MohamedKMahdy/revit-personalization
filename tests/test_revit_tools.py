"""
Tests for orchestrator/revit_tools.py — the full plugin-capability surface exposed to
the executor. Verifies the safety boundary (no send_code_to_revit), schema integrity,
result normalization, and generic pass-through dispatch.

Run:  pytest tests/test_revit_tools.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import revit_tools as rt  # noqa: E402


def test_send_code_to_revit_is_never_exposed():
    assert "send_code_to_revit" not in rt.TOOL_NAMES
    assert all(s["plugin_command"] != "send_code_to_revit" for s in rt.PLUGIN_TOOLS)
    assert "send_code_to_revit" not in {t["name"] for t in rt.TOOL_SCHEMAS}


def test_schema_integrity():
    names = [t["name"] for t in rt.TOOL_SCHEMAS]
    assert len(names) == len(set(names)), "duplicate tool names"
    for t in rt.TOOL_SCHEMAS:
        assert t["input_schema"].get("type") == "object", t["name"]
        assert isinstance(t["description"], str) and t["description"].strip(), t["name"]
    # the contracts we care about are present
    for expected in ("create_line_based_element", "create_surface_based_element", "create_grid",
                     "create_level", "create_dimensions", "delete_element", "operate_element",
                     "get_material_quantities", "export_view_image"):
        assert expected in rt.TOOL_NAMES, expected


def test_destructive_flagged_and_warned():
    assert {"delete_element", "operate_element", "execute_transaction_group"} <= rt.DESTRUCTIVE_TOOLS
    desc = {t["name"]: t["description"] for t in rt.TOOL_SCHEMAS}
    assert "DESTRUCTIVE" in desc["delete_element"]


def test_normalize_envelope_and_variants():
    assert rt.normalize({"Success": True, "Response": [7], "Message": "ok"}) == {
        "success": True, "message": "ok", "response": [7]}
    assert rt.normalize({"Success": False, "Message": "boom"})["success"] is False
    assert rt.normalize({"error": "unreachable"})["success"] is False
    # bare list (e.g. get_available_family_types-style)
    n = rt.normalize([{"a": 1}, {"b": 2}])
    assert n["success"] is True and len(n["response"]) == 2
    # ad-hoc delete reply
    d = rt.normalize({"deleted": True, "count": 3})
    assert d["success"] is True and d["response"]["count"] == 3
    assert rt.normalize({"deleted": False})["success"] is False


def test_dispatch_passthrough(monkeypatch):
    from mcp_server import revit_bridge as rb
    captured = {}

    def fake_call(command, params, timeout=None):
        captured["command"] = command
        captured["params"] = params
        captured["timeout"] = timeout
        return {"Success": True, "Response": [12345], "Message": "created"}

    monkeypatch.setattr(rb, "_call_plugin", fake_call)

    args = {"data": [{"category": "OST_Walls",
                      "locationLine": {"p0": {"x": 0, "y": 0, "z": 0}, "p1": {"x": 5000, "y": 0, "z": 0}}}]}
    out = rt.dispatch("create_line_based_element", args)

    assert captured["command"] == "create_line_based_element"    # mapped to the real plugin method
    assert captured["params"] is args                            # passed straight through
    assert captured["timeout"] == 60.0                           # default timeout
    assert out == {"success": True, "message": "created", "response": [12345]}


def test_dispatch_slow_tool_gets_longer_timeout(monkeypatch):
    from mcp_server import revit_bridge as rb
    seen = {}
    monkeypatch.setattr(rb, "_call_plugin",
                        lambda c, p, timeout=None: seen.update(t=timeout) or {"Success": True})
    rt.dispatch("execute_transaction_group", {"calls": []})
    assert seen["t"] == 240.0     # known-slow override


def test_dispatch_unknown_tool():
    out = rt.dispatch("not_a_real_tool", {})
    assert out["success"] is False and "unknown" in out["message"]
