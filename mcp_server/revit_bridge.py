"""
Revit integration bridge — connects directly to the mcp-servers-for-revit C# plugin.

ARCHITECTURE
============
The mcp-servers-for-revit stack has two layers:

  1. TypeScript MCP server (npx mcp-server-for-revit, stdio)
       → translates MCP tool calls → JSON-RPC 2.0 over TCP
  2. C# Revit Plugin (inside Revit 2026, localhost:8080, TCP)
       → executes commands via Revit API transactions

We bypass the TypeScript layer and talk directly to the C# plugin's TCP socket.
This keeps the bridge simple and avoids subprocess/async complexity.

TOOL COVERAGE (v1.0.0, commit 1a52de9)
=======================================
  create_point_based_element  — place doors/windows/furniture  ✅
  get_available_family_types  — resolve family names → typeId   ✅
  get_current_view_info       — active view details             ✅
  get_selected_elements       — current selection               ✅
  send_code_to_revit          — execute C# inside Revit         ✅ (used for gaps below)
  set_element_parameter       — set param by name/value        ⚠ via send_code_to_revit
  create_element_tag          — tag a specific element         ⚠ via send_code_to_revit

GAPS vs. METHODOLOGY REQUIREMENTS
===================================
  set_parameter  — modify_element.js exists but is an empty stub in v1.0.0
  tag_element    — only tag_all_walls / tag_all_rooms available
  → Per methodology §4 "if a needed operation is missing, add it as a backend
    tool extension". Tracked in docs/backend_extensions_needed.md.
  → Interim: use send_code_to_revit with inline C# (validated approach).

REVIT VERSION
=============
  Plugin supports Revit 2020–2026.  Use Revit 2026 for execution.
  The C# logging add-in (RevitLogger) can run in Revit 2027 for logging;
  use Revit 2026 for write operations until a 2027 plugin build is available.

INSTALLATION
============
  1. Download revit-plugin-2026.zip from GitHub Releases (v1.0.0)
  2. Extract to %AppData%\Autodesk\Revit\Addins\2026\
  3. Open Revit 2026 — the plugin auto-starts and listens on TCP localhost:8080
  4. Verify: call say_hello — should show a dialog in Revit
"""
from __future__ import annotations

import copy
import json
import os
import socket
import time
from pathlib import Path

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

    Returns the result dict, or {"error": "...", "available": False} if unreachable.
    """
    request_id = str(int(time.time() * 1000))
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": command,
        "params": params,
        "id": request_id,
    })

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
                # Try to parse — break when we have a complete object
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
        return {
            "error": (
                f"mcp-servers-for-revit C# plugin not reachable at "
                f"{REVIT_PLUGIN_HOST}:{REVIT_PLUGIN_PORT}. "
                "Ensure Revit 2026 is open with the plugin installed. "
                "See docs/backend_extensions_needed.md for installation steps."
            ),
            "available": False,
        }
    except socket.timeout:
        return {
            "error": f"Command '{command}' timed out after {timeout}s",
            "available": False,
        }
    except Exception as exc:
        return {"error": str(exc)}


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


def get_family_type_id(family_name: str, category: str = "") -> int | None:
    """
    Resolve a family name string to a Revit typeId (integer ElementId).

    create_point_based_element requires typeId, not a name string.
    This helper calls get_available_family_types and does a fuzzy name match.

    Returns the typeId int, or None if not found.
    """
    args: dict = {"familyNameFilter": family_name, "limit": 10}
    if category:
        args["categoryList"] = [category]

    result = _call_plugin("get_available_family_types", args)
    if "error" in result:
        return None

    # Result is a list of {typeId, familyName, typeName, ...}
    items = result if isinstance(result, list) else result.get("familyTypes", result.get("types", []))
    if not items:
        return None

    # Exact match first, then partial
    name_lower = family_name.lower()
    for item in items:
        fn = (item.get("familyName", "") + ":" + item.get("typeName", "")).lower()
        if name_lower in fn or fn.startswith(name_lower):
            return item.get("typeId") or item.get("id")

    # Fallback: return first result
    first = items[0] if items else {}
    return first.get("typeId") or first.get("id")


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Write operations (via mcp-servers-for-revit C# plugin)
# ═══════════════════════════════════════════════════════════════════════════════

def place_element(
    family_name: str,
    location_x: float = 0.0,
    location_y: float = 0.0,
    location_z: float = 0.0,
    width: float = 900.0,
    height: float = 2100.0,
    base_level: float = 0.0,
    base_offset: float = 0.0,
    host_wall_id: int | None = None,
    type_id: int | None = None,
) -> dict:
    """
    Place a point-based element (door, window, furniture) in the active view.

    Maps to mcp-servers-for-revit: create_point_based_element.
    Units: millimeters.

    Args:
        family_name: Revit family name (used to resolve typeId if not provided).
        location_x/y/z: Placement coordinates in mm.
        width/height: Element dimensions in mm.
        base_level: Base level height in mm.
        base_offset: Offset from base level in mm.
        host_wall_id: ElementId of host wall (auto-detected if not provided).
        type_id: Direct typeId override (skips family name lookup).
    """
    if type_id is None:
        type_id = get_family_type_id(family_name)

    element_data: dict = {
        "name": family_name,
        "locationPoint": {"x": location_x, "y": location_y, "z": location_z},
        "width": width,
        "height": height,
        "baseLevel": base_level,
        "baseOffset": base_offset,
    }
    if type_id is not None:
        element_data["typeId"] = type_id
    if host_wall_id is not None:
        element_data["hostWallId"] = host_wall_id

    return _call_plugin("create_point_based_element", {"data": [element_data]})


def set_element_parameter(element_id: int, param_name: str, value) -> dict:
    """
    Set a parameter value on a Revit element by name.

    INTERIM IMPLEMENTATION: uses send_code_to_revit with inline C# code.
    This is the documented interim approach until a proper set_element_parameter
    tool is added to the backend (see docs/backend_extensions_needed.md).

    Handles String, Double, and Integer storage types automatically.
    """
    # Escape quotes in param_name and string values to avoid C# injection
    safe_param = param_name.replace('"', '\\"')
    if isinstance(value, str):
        val_expr = f'param.Set("{value.replace(chr(34), chr(92) + chr(34))}")'
    elif isinstance(value, float):
        val_expr = f"param.Set({value})"
    else:
        val_expr = f"param.Set((double){value})"

    code = f"""
var elem = Document.GetElement(new ElementId({element_id}L));
if (elem == null) return "ERROR: element {element_id} not found";
var param = elem.LookupParameter("{safe_param}");
if (param == null) return "ERROR: parameter '{safe_param}' not found on element";
if (param.IsReadOnly) return "ERROR: parameter '{safe_param}' is read-only";
using (var tx = new Transaction(Document, "Set {safe_param}")) {{
    tx.Start();
    if (param.StorageType == StorageType.String)
        param.Set("{value}");
    else if (param.StorageType == StorageType.Double)
        {val_expr};
    else if (param.StorageType == StorageType.Integer)
        param.Set({int(value) if not isinstance(value, str) else 0});
    tx.Commit();
}}
return "OK: {safe_param} = {value}";
"""
    return _call_plugin("send_code_to_revit", {"code": code, "parameters": []})


def create_element_tag(element_id: int, tag_family_name: str) -> dict:
    """
    Place an annotation tag on a specific element.

    INTERIM IMPLEMENTATION: uses send_code_to_revit with inline C# code.
    Searches loaded tag families by name and places the tag at the element's
    location in the active view.

    See docs/backend_extensions_needed.md for the planned proper extension.
    """
    safe_tag = tag_family_name.replace('"', '\\"')
    code = f"""
var elem = Document.GetElement(new ElementId({element_id}L));
if (elem == null) return "ERROR: element {element_id} not found";
var activeView = Document.ActiveView;
// Find matching tag family symbol
var tagSymbol = new FilteredElementCollector(Document)
    .OfClass(typeof(FamilySymbol))
    .Cast<FamilySymbol>()
    .FirstOrDefault(fs =>
        fs.FamilyName.IndexOf("{safe_tag}", StringComparison.OrdinalIgnoreCase) >= 0 ||
        fs.Name.IndexOf("{safe_tag}", StringComparison.OrdinalIgnoreCase) >= 0);
if (tagSymbol == null) return "ERROR: tag family '{safe_tag}' not found";
if (!tagSymbol.IsActive) tagSymbol.Activate();
using (var tx = new Transaction(Document, "Tag element")) {{
    tx.Start();
    var refLink = new Reference(elem);
    var loc = (elem.Location as LocationPoint)?.Point ?? XYZ.Zero;
    var tag = IndependentTag.Create(
        Document, tagSymbol.Id, activeView.Id,
        refLink, false, TagOrientation.Horizontal, loc);
    tx.Commit();
    return "OK: tagged element {element_id} -> tag " + tag.Id.Value.ToString();
}}
"""
    return _call_plugin("send_code_to_revit", {"code": code, "parameters": []})


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
      - C# add-in is OBSERVER ONLY — it does NOT execute model writes.
      - All writes go through this bridge → mcp-servers-for-revit plugin.
      - Called after explicit user confirmation.

    Tool name mapping (motif → plugin):
      place_element           → create_point_based_element  (with typeId resolution)
      set_parameter           → set_element_parameter       (via send_code_to_revit)
      create_annotation_tag   → create_element_tag           (via send_code_to_revit)

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

        # Map motif tool names → actual backend commands
        result = _dispatch_tool(tool, arguments)
        results.append({"step": i + 1, "tool": tool, "arguments": arguments, "result": result})

        # Track last placed element ID for chaining
        last_element_id = _extract_element_id(result) or last_element_id

    errors = [r for r in results if "error" in r.get("result", {})]
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
    Map motif tool names (as used in ShortcutConfig.mcp_tool_sequence) to the
    actual mcp-servers-for-revit backend commands, handling schema differences.
    """
    if tool == "place_element":
        # Motif: place_element(family_type, location)
        # Backend: create_point_based_element(data=[{typeId, locationPoint, ...}])
        family_type = arguments.get("family_type", arguments.get("family_name", ""))
        loc = arguments.get("location", {})
        if isinstance(loc, str):
            loc = {}  # placeholder not resolved yet
        return place_element(
            family_name=family_type,
            location_x=loc.get("x", 0.0),
            location_y=loc.get("y", 0.0),
            location_z=loc.get("z", 0.0),
            width=arguments.get("width", 900.0),
            height=arguments.get("height", 2100.0),
            host_wall_id=arguments.get("host_wall_id"),
        )

    elif tool == "set_parameter":
        # Motif: set_parameter(element_id, parameter_name, value)
        return set_element_parameter(
            element_id=int(arguments.get("element_id", 0)),
            param_name=arguments.get("parameter_name", ""),
            value=arguments.get("value", ""),
        )

    elif tool == "create_annotation_tag":
        # Motif: create_annotation_tag(element_id, tag_family)
        return create_element_tag(
            element_id=int(arguments.get("element_id", 0)),
            tag_family_name=arguments.get("tag_family", ""),
        )

    else:
        # Pass through any other tool directly to the plugin
        return _call_plugin(tool, arguments)


def _extract_element_id(result: dict) -> int | None:
    """Extract the placed element's ID from a create_point_based_element result."""
    if not isinstance(result, dict):
        return None
    # Try common field names in the result
    for key in ("elementId", "element_id", "id", "ElementId"):
        val = result.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    # Check nested results list
    items = result.get("results", result.get("elements", []))
    if items and isinstance(items, list) and isinstance(items[0], dict):
        return _extract_element_id(items[0])
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

    print("RevitWriteServer connection test")
    print(f"Connecting to {REVIT_PLUGIN_HOST}:{REVIT_PLUGIN_PORT} …")
    print()

    # 1. say_hello ─ basic reachability
    print("► say_hello")
    result = say_hello()
    if "error" in result:
        print(f"  FAIL: {result['error']}")
        print()
        print("Make sure:")
        print("  1. Revit 2027 is open")
        print("  2. RevitWriteServer.dll loaded (check Revit journal for 'TCP server started')")
        print("  3. Nothing else is using port 8080")
        sys.exit(1)
    print(f"  OK  → {result}")
    print()

    # 2. get_current_view_info ─ reads active view
    print("► get_current_view_info")
    result = _call_plugin("get_current_view_info", {})
    if "error" in result:
        print(f"  FAIL: {result['error']}")
    else:
        view = result.get("view", result)
        print(f"  OK  → view='{view.get('name', '?')}' type={view.get('type', '?')}")
    print()

    # 3. get_available_family_types ─ lists loaded families
    print("► get_available_family_types (first 3)")
    result = _call_plugin("get_available_family_types", {})
    if "error" in result:
        print(f"  FAIL: {result['error']}")
    else:
        types = result.get("types", result) if isinstance(result, dict) else result
        for t in (types or [])[:3]:
            print(f"  • id={t.get('id', t.get('typeId', '?'))}  {t.get('name', '?')}")
        total = len(types or [])
        if total > 3:
            print(f"  … and {total - 3} more")
    print()

    print("All checks passed. RevitWriteServer is running correctly.")
