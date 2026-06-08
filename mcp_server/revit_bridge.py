"""
Revit integration bridge — connects to mcp-servers-for-revit.

mcp-servers-for-revit is an open-source TypeScript MCP server + in-Revit plugin
(https://github.com/simonmoreau/mcp-servers-for-revit) that provides the write
execution surface for this thesis system.  The C# add-in is now OBSERVER ONLY —
all model writes are delegated here.

Two channels share the same backend:

1. model_query / model_query_state  (READ tools)
   → get_active_view, get_loaded_families, get_elements_by_category, etc.
   → used by the Macro Agent to ground motif execution in current model state.

2. execute_shortcut                  (WRITE tools)
   → place_element, set_parameter, create_annotation_tag
   → dispatches the Macro Agent's resolved tool-call sequence step by step.

Transport: JSON-RPC 2.0 over HTTP to the mcp-servers-for-revit local server.
Default URL: http://localhost:3001  (configurable via MCP_REVIT_BACKEND_URL env var).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import httpx

# ── Backend URL (mcp-servers-for-revit SSE/HTTP endpoint) ─────────────────────
MCP_REVIT_URL = os.environ.get("MCP_REVIT_BACKEND_URL", "http://localhost:3001")


# ═══════════════════════════════════════════════════════════════════════════════
# Internal: single JSON-RPC call to the backend
# ═══════════════════════════════════════════════════════════════════════════════

def _call_backend(tool_name: str, arguments: dict, timeout: float = 30.0) -> dict:
    """
    Dispatch one MCP tool call to mcp-servers-for-revit via JSON-RPC 2.0.

    Returns the tool result dict, or an error dict if the backend is unreachable.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = httpx.post(
            f"{MCP_REVIT_URL}/",
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        # JSON-RPC error object
        if "error" in data:
            return {"error": data["error"].get("message", str(data["error"]))}
        return data.get("result", {})
    except httpx.ConnectError:
        return {
            "error": (
                f"mcp-servers-for-revit not reachable at {MCP_REVIT_URL}. "
                "Ensure Revit is open and the mcp-servers-for-revit plugin is active."
            ),
            "available": False,
        }
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Model context queries  (READ — used by Macro Agent for grounding)
# ═══════════════════════════════════════════════════════════════════════════════

def model_query(tool_name: str, arguments: dict) -> dict:
    """
    Send a read tool call to mcp-servers-for-revit.

    Common tools:
      get_active_view()
      get_loaded_families(category="Doors")
      get_elements_by_category(category="Doors", level="L1")
      get_levels()
      get_project_info()
      get_selected_elements()
    """
    return _call_backend(tool_name, arguments)


def model_query_state(query: str) -> dict:
    """
    High-level model state query — resolves a natural-language request about
    current Revit model context by dispatching the appropriate read tool.

    Examples:
      "current selection"           -> get_selected_elements
      "available door types"        -> get_loaded_families(category=Doors)
      "active view"                 -> get_active_view
      "levels"                      -> get_levels
      "elements on level L1"        -> get_elements_by_category
      "project info"                -> get_project_info

    Used by the Macro Agent to ground motif execution before generating
    the tool call sequence.
    """
    q = query.lower()

    if "select" in q:
        return _call_backend("get_selected_elements", {})
    elif "door" in q and ("type" in q or "famil" in q):
        return _call_backend("get_loaded_families", {"category": "Doors"})
    elif "window" in q and ("type" in q or "famil" in q):
        return _call_backend("get_loaded_families", {"category": "Windows"})
    elif "wall" in q and ("type" in q or "famil" in q):
        return _call_backend("get_loaded_families", {"category": "Walls"})
    elif "level" in q:
        return _call_backend("get_levels", {})
    elif "view" in q:
        return _call_backend("get_active_view", {})
    elif "project" in q or "info" in q:
        return _call_backend("get_project_info", {})
    else:
        # Treat the query as a category filter
        return _call_backend("get_elements_by_category", {"category": query})


def get_active_view() -> dict:
    """Return the currently active Revit view."""
    return _call_backend("get_active_view", {})


def get_loaded_families(category: str) -> dict:
    """List all loaded families for a given Revit category."""
    return _call_backend("get_loaded_families", {"category": category})


def get_elements_by_category(category: str, level: str = "") -> dict:
    """Get elements of a category, optionally filtered by level."""
    args: dict = {"category": category}
    if level:
        args["level"] = level
    return _call_backend("get_elements_by_category", args)


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Shortcut execution  (WRITE — dispatches to mcp-servers-for-revit)
# ═══════════════════════════════════════════════════════════════════════════════

def execute_shortcut(
    shortcut_id: str,
    params: dict | None = None,
    tool_sequence: list[dict] | None = None,
) -> dict:
    """
    Execute a saved shortcut by dispatching its tool-call sequence to
    mcp-servers-for-revit step by step.

    Under the updated architecture (section 4.1-4.2 of the methodology):
      - The C# add-in is OBSERVER ONLY and does NOT execute model writes.
      - All writes go through mcp-servers-for-revit (place_element,
        set_parameter, create_annotation_tag).
      - The Macro Agent resolves placeholders and calls this after confirmation.

    Args:
        shortcut_id:    ID of the saved ShortcutConfig (for logging).
        params:         Runtime parameter overrides, e.g. {"Mark": "D-101"}.
        tool_sequence:  Pre-resolved tool call list.  If None, loaded from disk.

    Returns:
        Summary dict: steps_executed, errors, per-step results, success flag.
    """
    # Load tool sequence from disk if not provided
    if tool_sequence is None:
        shortcuts_dir = Path(os.environ.get(
            "REVIT_PERSONALIZATION_SHORTCUTS_DIR",
            Path.home() / "AppData" / "Local" / "RevitPersonalization" / "shortcuts",
        ))
        shortcut_path = shortcuts_dir / f"{shortcut_id}.json"
        if not shortcut_path.exists():
            return {"error": f"Shortcut '{shortcut_id}' not found at {shortcut_path}"}

        from shared.schemas import ShortcutConfig
        config = ShortcutConfig.model_validate_json(shortcut_path.read_text(encoding="utf-8"))
        tool_sequence = config.mcp_tool_sequence

    # Apply runtime parameter overrides (fills {{ParamName}} placeholders)
    if params:
        tool_sequence = _apply_param_overrides(tool_sequence, params)

    results: list[dict] = []
    last_element_id: int | None = None

    for i, step in enumerate(tool_sequence):
        tool = step.get("tool", "")
        arguments = dict(step.get("arguments", {}))

        # Resolve the {{last_element_id}} placeholder set by a preceding Place step
        for key, val in list(arguments.items()):
            if val == "{{last_element_id}}":
                if last_element_id is not None:
                    arguments[key] = last_element_id
                else:
                    arguments.pop(key)  # skip unresolvable placeholder

        result = _call_backend(tool, arguments)
        results.append({
            "step": i + 1,
            "tool": tool,
            "arguments": arguments,
            "result": result,
        })

        # Track the last created/placed element for chaining subsequent steps
        if "element_id" in result:
            last_element_id = result["element_id"]
        elif "id" in result:
            last_element_id = result["id"]
        elif "elementId" in result:
            last_element_id = result["elementId"]

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
    """
    Execute a tool sequence directly (without a saved shortcut file).
    Used by the Macro Agent dry-run -> confirm -> execute flow.
    """
    result = execute_shortcut(
        shortcut_id="<inline>",
        params=params,
        tool_sequence=tool_sequence,
    )
    return result.get("results", [])


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_param_overrides(tool_sequence: list[dict], params: dict) -> list[dict]:
    """Fill {{ParamName}} placeholders with runtime values from the params dict."""
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
# Legacy shim  (called by old orchestrator/agents.py code paths)
# ═══════════════════════════════════════════════════════════════════════════════

def execute_mcp_tool_sequence(tool_sequence: list[dict]) -> list[dict]:
    """
    Legacy compatibility shim — now delegates directly to mcp-servers-for-revit.

    Previously this wrote a file to the IPC directory for the C# add-in to pick
    up.  Under the new architecture (section 4.1) the C# add-in is observer only;
    all model writes go through mcp-servers-for-revit.
    """
    return execute_tool_sequence(tool_sequence)
