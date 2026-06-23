"""
Full Revit capability surface for the executor.

The mcp-servers-for-revit plugin exposes ~30 JSON-RPC commands. The executor used to
see only a small curated subset (place/set/tag/query). This module exposes the REST of
the plugin's public commands as executor tools so the agent has the FULL capabilities of
the backend — create walls/floors/grids/levels/rooms, dimensions, color overrides,
material/room/statistics queries, duplicate, delete, atomic transaction groups, image
export, and so on.

How it works
------------
The exact JSON-RPC parameter contract of every command was extracted from the plugin's
C# source (Commands/*.cs Execute() + the [JsonProperty] Model classes + EventHandlers)
and stored in revit_tools.json. Each tool's `input_schema` mirrors EXACTLY the JSON the
plugin expects at top level, so dispatch is a thin pass-through:
    _call_plugin(plugin_command, args)  →  normalize the AIResult envelope.
All lengths are MILLIMETRES at the JSON boundary (the plugin converts to feet).

Safety
------
`send_code_to_revit` (arbitrary C# execution) is NEVER included here — it is the one
standing execution-safety boundary (mirrors shared/tool_allowlist.py). Destructive
commands (delete_element, operate_element, execute_transaction_group) ARE exposed because
the user asked for full capability, but they are flagged `destructive` and the executor
runs only after the user has confirmed the routine in the chatbot.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Commands that must never be reachable through the executor (arbitrary code execution).
BLOCKED_COMMANDS = {"send_code_to_revit"}

# The socket must stay open at least as long as the plugin's own internal timeout for a
# command, or the read times out before Revit answers. These commands are known-slow.
_SLOW_TOOLS = {
    "get_current_view_elements": 90.0,     # plugin waits 60s
    "get_material_quantities": 120.0,
    "export_room_data": 120.0,
    "analyze_model_statistics": 90.0,
    "create_structural_framing_system": 180.0,
    "place_and_configure": 180.0,
    "execute_transaction_group": 240.0,
    "export_view_image": 180.0,
    "color_splash": 120.0,
}
_DEFAULT_TIMEOUT = 60.0

_DATA: list[dict] = json.loads((Path(__file__).parent / "revit_tools.json").read_text(encoding="utf-8"))

# Public registry (blocked commands filtered out defensively even if present in the file).
PLUGIN_TOOLS: list[dict] = [s for s in _DATA if s.get("plugin_command") not in BLOCKED_COMMANDS]

_COMMAND_BY_TOOL: dict[str, str] = {s["tool_name"]: s["plugin_command"] for s in PLUGIN_TOOLS}
DESTRUCTIVE_TOOLS: set[str] = {s["tool_name"] for s in PLUGIN_TOOLS if s.get("destructive")}


def _description(spec: dict) -> str:
    """Build the model-facing tool description from the extracted contract."""
    parts = [spec.get("summary", "").strip()]
    if spec.get("when_to_use"):
        parts.append("WHEN: " + spec["when_to_use"].strip())
    if spec.get("gotchas"):
        parts.append("NOTES: " + spec["gotchas"].strip())
    if spec.get("destructive"):
        parts.insert(0, "⚠ DESTRUCTIVE — only use when the routine/goal explicitly calls for it.")
    return "\n".join(p for p in parts if p)


def tool_schemas() -> list[dict]:
    """Anthropic tool schemas for every exposed plugin command."""
    return [
        {"name": s["tool_name"], "description": _description(s), "input_schema": s["input_schema"]}
        for s in PLUGIN_TOOLS
    ]


TOOL_SCHEMAS: list[dict] = tool_schemas()
TOOL_NAMES: set[str] = set(_COMMAND_BY_TOOL)


def normalize(res: Any) -> dict:
    """Normalize a plugin reply to {success, message, response} for the executor loop."""
    if isinstance(res, dict):
        if "error" in res:
            return {"success": False, "message": str(res["error"])}
        if "Success" in res:                       # standard AIResult envelope
            out = {"success": bool(res.get("Success")), "message": res.get("Message") or ""}
            if res.get("Response") is not None:
                out["response"] = res["Response"]
            return out
        # commands that return an ad-hoc object (e.g. delete_element → {deleted,count})
        ok = res.get("deleted", res.get("success", True))
        return {"success": bool(ok), "message": res.get("message") or "", "response": res}
    if isinstance(res, list):
        return {"success": True, "response": res, "message": f"{len(res)} item(s)"}
    return {"success": True, "response": res}


def dispatch(tool_name: str, args: dict) -> dict:
    """Execute one exposed plugin tool by passing its args straight through to the plugin."""
    from mcp_server import revit_bridge as rb

    command = _COMMAND_BY_TOOL.get(tool_name)
    if command is None:
        return {"success": False, "message": f"unknown plugin tool '{tool_name}'"}
    timeout = _SLOW_TOOLS.get(tool_name, _DEFAULT_TIMEOUT)
    res = rb._call_plugin(command, args or {}, timeout=timeout)
    return normalize(res)
