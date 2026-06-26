"""
Revit integration bridge — connects directly to the mcp-servers-for-revit C# plugin.

ARCHITECTURE
============
The mcp-servers-for-revit stack has two layers:

  1. TypeScript MCP server (optional) → translates MCP tool calls → JSON-RPC 2.0 over TCP
  2. C# Revit plugin (inside Revit 2025/2026, localhost:8080, TCP)
       → executes commands via Revit API transactions

We bypass the TypeScript layer and talk directly to the C# plugin's TCP socket.

BACKEND CONTRACTS (verified against the command/handler/model sources)
======================================================================
  create_point_based_element  ← {"data":[{"category","typeId","locationPoint":{x,y,z},
                                           "width","height","baseLevel","baseOffset",
                                           "hostWallId","facingFlipped"}]}   (mm)
                               → AIResult<List<int>>  {Success,Message,Response:[ids]}
  set_element_parameter        ← {"elementId":int,"parameters":[{"name","value"}]}
                               → AIResult<List<string>>
  tag_element                  ← {"elementId":int,"useLeader":bool,"offsetX","offsetY","tagTypeId"?}
                               → AIResult<int>   (tag id in Response)
  operate_element              ← {"data":{"elementIds":[int],"action":"Select"|"Isolate"|...}}
                               → AIResult<string>
  get_available_family_types   ← {"categoryList":[str],"familyNameFilter"?:str,"limit"?:int}
                               → bare list [ {FamilyTypeId,FamilyName,TypeName,Category}, ... ]

Responses use the AIResult envelope {Success, Message, Response} (PascalCase),
EXCEPT get_available_family_types which returns the list directly.

EXECUTION SAFETY
================
Only the allowlisted pipeline tools (place_element / set_parameter /
create_annotation_tag) are dispatchable. send_code_to_revit is NEVER reachable
through the pipeline — see shared/tool_allowlist.py and _dispatch_tool().

REVIT VERSION / INSTALL
=======================
  Use Revit 2025 or 2026 with the plugin installed at
  %AppData%\\Autodesk\\Revit\\Addins\\<ver>\\. It listens on TCP localhost:8080.
  Verify with: call say_hello → a greeting dialog shows in Revit.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import time
from pathlib import Path

from shared.tool_allowlist import (
    DisallowedToolError,
    assert_tool_allowed,
    validate_tool_sequence,
)

# ── Connection settings ────────────────────────────────────────────────────────
REVIT_PLUGIN_HOST = os.environ.get("REVIT_PLUGIN_HOST", "localhost")
REVIT_PLUGIN_PORT = int(os.environ.get("REVIT_PLUGIN_PORT", "8080"))
REVIT_PLUGIN_TIMEOUT = float(os.environ.get("REVIT_PLUGIN_TIMEOUT", "30"))


# ═══════════════════════════════════════════════════════════════════════════════
# Internal: TCP JSON-RPC call to the C# Revit plugin
# ═══════════════════════════════════════════════════════════════════════════════

def _call_plugin(command: str, params: dict, timeout: float = REVIT_PLUGIN_TIMEOUT) -> dict:
    """
    Send one JSON-RPC 2.0 command to the mcp-servers-for-revit C# plugin via TCP.

    The plugin listens on localhost:8080 and responds with a JSON-RPC result.
    Each call opens a new connection (the TypeScript layer reconnects per call too).

    Returns the JSON-RPC `result` (usually an AIResult dict {Success, Message,
    Response}; a bare list for get_available_family_types), or
    {"error": "...", "available": False} if unreachable.
    """
    request_id = str(int(time.time() * 1000))
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": command,
        "params": params,
        "id": request_id,
    })

    # The plugin's TCP socket occasionally refuses a connection transiently (Revit busy / the
    # IExternalEvent loop mid-cycle) even while Revit is open — which surfaced as place_element
    # randomly failing with "not reachable" mid-run. Retry the CONNECTION a couple times with a short
    # backoff so a blip doesn't kill a step; a truly-closed Revit still falls through to the error.
    not_reachable = {
        "error": (f"mcp-servers-for-revit C# plugin not reachable at "
                  f"{REVIT_PLUGIN_HOST}:{REVIT_PLUGIN_PORT}. "
                  "Ensure Revit 2025/2026 is open with the plugin installed."),
        "available": False,
    }
    attempts = max(1, int(os.environ.get("REVIT_PLUGIN_RETRIES", "2")) + 1)
    for attempt in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((REVIT_PLUGIN_HOST, REVIT_PLUGIN_PORT))
                s.sendall(payload.encode("utf-8"))

                # Read until we have a complete JSON response
                chunks = []
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk.decode("utf-8"))
                    buf = "".join(chunks)
                    try:
                        response = json.loads(buf)
                        break
                    except json.JSONDecodeError:
                        continue  # keep reading

            if "error" in response:
                err = response["error"]
                return {"error": err.get("message", str(err)) if isinstance(err, dict) else str(err)}
            return response.get("result", {})

        except (ConnectionRefusedError, OSError):
            if attempt < attempts - 1:
                time.sleep(0.4 * (attempt + 1))   # transient blip — back off and retry the connection
                continue
            return not_reachable
        except socket.timeout:
            return {"error": f"Command '{command}' timed out after {timeout}s", "available": False}
        except Exception as exc:
            return {"error": str(exc)}


def pick_point(mode: str = "point", prompt: str | None = None, timeout: float = 190.0) -> dict:
    """Ask the user to pick the placement location interactively in Revit.

    Blocks until the user clicks (or presses Esc), so the timeout is long. Returns the
    plugin's AIResult envelope:
      point mode → {"Success": true, "Response": {"mode":"point","x","y","z"}}
      line  mode → {"Success": true, "Response": {"mode":"line","p0":{x,y,z},"p1":{x,y,z}}}
    Coordinates are in millimetres. On Esc/cancel → {"Success": false, "Message": "Pick cancelled."}.
    """
    params: dict = {"mode": mode}
    if prompt:
        params["prompt"] = prompt
    return _call_plugin("pick_point", params, timeout=timeout)


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Model context queries  (READ — used by Macro Agent for grounding)
# ═══════════════════════════════════════════════════════════════════════════════

def model_query(tool_name: str, arguments: dict) -> dict:
    """
    Send a read tool call to the mcp-servers-for-revit C# plugin.

    Supported read tools:
      get_current_view_info()
      get_current_view_elements()
      get_available_family_types(categoryList, familyNameFilter, limit)
      get_selected_elements()
      analyze_model_statistics()
    """
    return _call_plugin(tool_name, arguments)


def model_query_state(query: str) -> dict:
    """
    High-level model state query — dispatches to the right read tool based on
    natural-language query keywords.

    Used by the Macro Agent to ground motif execution before generating the
    tool-call sequence (resolve family names → typeIds, check active view, etc.).
    """
    q = query.lower()

    if "select" in q:
        return _call_plugin("get_selected_elements", {})
    elif "view" in q and "active" in q:
        return _call_plugin("get_current_view_info", {})
    elif "door" in q and ("type" in q or "famil" in q):
        return _call_plugin("get_available_family_types", {
            "categoryList": ["OST_Doors"], "limit": 50,
        })
    elif "window" in q and ("type" in q or "famil" in q):
        return _call_plugin("get_available_family_types", {
            "categoryList": ["OST_Windows"], "limit": 50,
        })
    elif "wall" in q and ("type" in q or "famil" in q):
        return _call_plugin("get_available_family_types", {
            "categoryList": ["OST_Walls"], "limit": 50,
        })
    elif "famil" in q or "type" in q:
        return _call_plugin("get_available_family_types", {"limit": 100})
    elif "statistic" in q or "count" in q:
        return _call_plugin("analyze_model_statistics", {})
    else:
        return _call_plugin("get_current_view_info", {})


def _resolve_family_type(family_name: str, category: str = "") -> dict | None:
    """
    Resolve a family/type name to its FamilyTypeInfo dict via get_available_family_types.

    family_name may be "Family" or "Family : Type" (the format PatternBridge emits).
    The backend filters by family OR type substring, so we filter on the family part
    and then match the full requested label.

    Returns {FamilyTypeId, FamilyName, TypeName, Category, UniqueId} or None.
    (Response keys are PascalCase — the backend returns a bare List<FamilyTypeInfo>.)
    """
    requested = (family_name or "").strip()
    fam_part = requested.split(":")[0].strip()
    if not fam_part:
        return None

    args: dict = {"familyNameFilter": fam_part, "limit": 50}
    if category:
        args["categoryList"] = [category]

    result = _call_plugin("get_available_family_types", args)
    if isinstance(result, dict) and "error" in result:
        return None

    # Bare list, or (defensively) an AIResult-wrapped list under "Response".
    items = result if isinstance(result, list) else result.get("Response", []) if isinstance(result, dict) else []
    if not items:
        return None

    req_lower = requested.lower()

    def label(it: dict) -> str:
        return f"{it.get('FamilyName', '')} : {it.get('TypeName', '')}".strip().lower()

    # Exact full-label or family-name match, then substring.
    for it in items:
        if req_lower == label(it) or req_lower == (it.get("FamilyName", "") or "").lower():
            return it
    for it in items:
        if req_lower in label(it) or fam_part.lower() in (it.get("FamilyName", "") or "").lower():
            return it
    return items[0]


def get_family_type_id(family_name: str, category: str = "") -> int | None:
    """Resolve a family/type name to a Revit typeId (int ElementId), or None."""
    info = _resolve_family_type(family_name, category)
    if info and info.get("FamilyTypeId") is not None:
        try:
            return int(info["FamilyTypeId"])
        except (ValueError, TypeError):
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Connection test
# ═══════════════════════════════════════════════════════════════════════════════

def say_hello() -> dict:
    """
    Connection test — shows a greeting dialog in Revit.
    Use this first to confirm the plugin is running and reachable.
    """
    return _call_plugin("say_hello", {})


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  Shortcut execution (dispatches motif tool-call sequence step by step)
# ═══════════════════════════════════════════════════════════════════════════════

def execute_shortcut(
    shortcut_id: str,
    params: dict | None = None,
    tool_sequence: list[dict] | None = None,
) -> dict:
    """
    Execute a saved shortcut by dispatching its tool-call sequence to the
    mcp-servers-for-revit C# plugin step by step.

    Under the methodology (§4.1):
      - C# logging add-in is OBSERVER ONLY — it does NOT execute model writes.
      - All writes go through this bridge → mcp-servers-for-revit plugin.
      - Called after explicit user confirmation.

    Tool name mapping (motif → backend command):
      place_element           → create_point_based_element  (typeId resolved by name)
      set_parameter           → set_element_parameter
      create_annotation_tag   → tag_element

    Args:
        shortcut_id:   ID of the saved ShortcutConfig (for logging/tracing).
        params:        Runtime parameter overrides, e.g. {"Mark": "D-101"}.
        tool_sequence: Pre-resolved tool call list. If None, loaded from disk.
    """
    if tool_sequence is None:
        shortcuts_dir = Path(os.environ.get(
            "REVIT_PERSONALIZATION_SHORTCUTS_DIR",
            Path.home() / "AppData" / "Local" / "RevitPersonalization" / "shortcuts",
        ))
        shortcut_path = shortcuts_dir / f"{shortcut_id}.json"
        if not shortcut_path.exists():
            return {"error": f"Shortcut '{shortcut_id}' not found"}

        from shared.schemas import ShortcutConfig
        config = ShortcutConfig.model_validate_json(shortcut_path.read_text(encoding="utf-8"))
        tool_sequence = config.mcp_tool_sequence

    if params:
        tool_sequence = _apply_param_overrides(tool_sequence, params)

    # ENFORCED execution-safety boundary (defense-in-depth): validate the WHOLE
    # sequence before executing any step, so a tampered shortcut file or any
    # non-allowlisted tool (e.g. send_code_to_revit) is rejected up front with no
    # partial execution. Raises DisallowedToolError.
    validate_tool_sequence(tool_sequence)

    results: list[dict] = []
    last_element_id: int | None = None

    for i, step in enumerate(tool_sequence):
        tool = step.get("tool", "")
        arguments = dict(step.get("arguments", {}))

        # Resolve {{last_element_id}} placeholder
        for key, val in list(arguments.items()):
            if val == "{{last_element_id}}":
                if last_element_id is not None:
                    arguments[key] = last_element_id
                else:
                    arguments.pop(key)

        result = _dispatch_tool(tool, arguments)
        results.append({"step": i + 1, "tool": tool, "arguments": arguments, "result": result})

        # Track the last PLACED element id for {{last_element_id}} chaining. Only a
        # placement creates the "subject" element that later Tag/SetParam steps act
        # on — a Tag or SetParam result must NOT become the chain target, otherwise a
        # Place → Tag → SetParam routine would set the parameter on the tag, not the
        # element. (Tag-before-SetParam orderings are common in real logs.)
        if tool == "place_element":
            last_element_id = _extract_element_id(result) or last_element_id

    errors = [r for r in results if _step_failed(r.get("result", {}))]
    return {
        "shortcut_id": shortcut_id,
        "steps_executed": len(results),
        "errors": len(errors),
        "results": results,
        "success": len(errors) == 0,
    }


def execute_tool_sequence(
    tool_sequence: list[dict],
    params: dict | None = None,
) -> list[dict]:
    """Execute a tool sequence directly without a saved shortcut file."""
    result = execute_shortcut("<inline>", params=params, tool_sequence=tool_sequence)
    return result.get("results", [])


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _dispatch_tool(tool: str, arguments: dict) -> dict:
    """
    Map permitted motif tool names to mcp-servers-for-revit backend commands.

    ENFORCED execution-safety backstop: deny-by-default. Only the allowlisted
    pipeline tools are dispatchable; there is NO passthrough of arbitrary tool
    names to the plugin, so send_code_to_revit can never be reached this way.

    Backend command contracts (verified against the C# command/handler/model sources):
      create_point_based_element  — {"data":[PointElement]}        (mm units)
      set_element_parameter       — {"elementId", "parameters":[{"name","value"}]}
      tag_element                 — {"elementId","useLeader","offsetX","offsetY","tagTypeId"?}
    """
    assert_tool_allowed(tool)  # raises DisallowedToolError for anything off-allowlist

    if tool == "place_element":
        family_type = arguments.get("family_type", arguments.get("family_name", ""))
        loc = arguments.get("location") or {}
        if not isinstance(loc, dict):
            loc = {}

        # Resolve family name → typeId (and category). The backend derives the
        # category from a valid typeId, so typeId alone is enough to place.
        type_id = arguments.get("typeId")
        category = arguments.get("category")
        if type_id is None and family_type:
            info = _resolve_family_type(family_type)
            if info:
                type_id = info.get("FamilyTypeId")
                if not category and info.get("Category"):
                    # FamilyTypeInfo.Category is a display name ("Doors"); the
                    # backend wants a BuiltInCategory token ("OST_Doors").
                    category = "OST_" + str(info["Category"]).replace(" ", "")

        element: dict = {
            "locationPoint": {
                "x": float(loc.get("x", 0.0)),
                "y": float(loc.get("y", 0.0)),
                "z": float(loc.get("z", 0.0)),
            },
            "baseLevel": float(arguments.get("baseLevel", 0.0)),
            "baseOffset": float(arguments.get("baseOffset", 0.0)),
            "height": float(arguments.get("height", 2100.0)),
            "width": float(arguments.get("width", 900.0)),
        }
        if type_id is not None:
            element["typeId"] = int(type_id)
        if category:
            element["category"] = category
        if arguments.get("hostWallId"):
            element["hostWallId"] = int(arguments["hostWallId"])

        return _call_plugin("create_point_based_element", {"data": [element]})

    elif tool == "set_parameter":
        elem_id = arguments.get("element_id") or arguments.get("elementId") or 0
        param_name = arguments.get("parameter_name") or arguments.get("parameterName") or ""
        value = arguments.get("value", "")

        return _call_plugin("set_element_parameter", {
            "elementId": int(elem_id),
            "parameters": [{"name": param_name, "value": value}],
        })

    elif tool == "create_annotation_tag":
        elem_id = arguments.get("element_id") or arguments.get("elementId") or 0
        params: dict = {
            "elementId": int(elem_id),
            "useLeader": bool(arguments.get("useLeader", False)),
            "offsetX": float(arguments.get("offsetX", 0.0)),
            "offsetY": float(arguments.get("offsetY", 500.0)),
        }
        # tagTypeId is optional — the backend auto-picks a tag family by category.
        if arguments.get("tagTypeId"):
            params["tagTypeId"] = int(arguments["tagTypeId"])

        return _call_plugin("tag_element", params)

    # Unreachable for permitted tools (all handled above). Hard backstop: a
    # permitted-but-unmapped tool fails loudly rather than being forwarded blindly.
    assert_tool_allowed(tool)
    raise DisallowedToolError(f"Tool '{tool}' is permitted but has no dispatch mapping.")


def _step_failed(result: dict) -> bool:
    """A dispatched step failed if it returned a connection error or AIResult.Success == False."""
    if not isinstance(result, dict):
        return True
    if result.get("error"):
        return True
    if result.get("Success") is False:
        return True
    return False


def _extract_element_id(result: dict) -> int | None:
    """
    Extract a placed element's ID from a create_point_based_element result.

    Backend returns AIResult<List<int>>: {"Success":true,"Response":[123]}.
    Also tolerates flat {"elementId":123} shapes for forward/backward compat.
    """
    if not isinstance(result, dict):
        return None

    resp = result.get("Response")
    if isinstance(resp, list) and resp:
        try:
            return int(resp[0])
        except (ValueError, TypeError):
            pass
    if isinstance(resp, (int, str)):
        try:
            return int(resp)
        except (ValueError, TypeError):
            pass

    for key in ("elementId", "element_id", "ElementId", "id"):
        val = result.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass

    inner = result.get("result")
    if isinstance(inner, dict):
        return _extract_element_id(inner)
    return None


def _apply_param_overrides(tool_sequence: list[dict], params: dict) -> list[dict]:
    """Fill {{ParamName}} placeholders with runtime values."""
    result = copy.deepcopy(tool_sequence)
    for step in result:
        args = step.get("arguments", {})
        for key, val in list(args.items()):
            if isinstance(val, str) and val.startswith("{{") and val.endswith("}}"):
                param_name = val[2:-2]
                if param_name in params:
                    args[key] = params[param_name]
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy shim
# ═══════════════════════════════════════════════════════════════════════════════

def execute_mcp_tool_sequence(tool_sequence: list[dict]) -> list[dict]:
    """Legacy compatibility shim — delegates to execute_tool_sequence."""
    return execute_tool_sequence(tool_sequence)


# ═══════════════════════════════════════════════════════════════════════════════
# Quick connection test  (python mcp_server/revit_bridge.py)
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("mcp-servers-for-revit connection test")
    print(f"Connecting to {REVIT_PLUGIN_HOST}:{REVIT_PLUGIN_PORT} …")
    print()

    # 1. say_hello ─ basic reachability
    print("► say_hello")
    result = say_hello()
    if "error" in result:
        print(f"  FAIL: {result['error']}")
        print()
        print("Make sure:")
        print("  1. Revit 2025 or 2026 is open")
        print("  2. The plugin loaded (check Revit journal for 'TCP server started')")
        print("  3. Nothing else is using port 8080")
        sys.exit(1)
    print(f"  OK  → {result}")
    print()

    # 2. get_current_view_info ─ reads active view
    print("► get_current_view_info")
    result = _call_plugin("get_current_view_info", {})
    if isinstance(result, dict) and "error" in result:
        print(f"  FAIL: {result['error']}")
    else:
        view = result.get("Response", result) if isinstance(result, dict) else result
        if isinstance(view, dict):
            print(f"  OK  → view='{view.get('name', view.get('Name', '?'))}'")
        else:
            print(f"  OK  → {view}")
    print()

    # 3. get_available_family_types ─ lists loaded families (bare list)
    print("► get_available_family_types (first 3)")
    result = _call_plugin("get_available_family_types", {"limit": 25})
    if isinstance(result, dict) and "error" in result:
        print(f"  FAIL: {result['error']}")
    else:
        types = result if isinstance(result, list) else result.get("Response", [])
        for t in (types or [])[:3]:
            print(f"  • id={t.get('FamilyTypeId', '?')}  {t.get('FamilyName', '?')} : {t.get('TypeName', '?')}")
        if len(types or []) > 3:
            print(f"  … and {len(types) - 3} more")
    print()

    print("All checks passed. The mcp-servers-for-revit plugin is reachable.")
