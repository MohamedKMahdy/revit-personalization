"""
Revit integration bridge — two separate channels:

1. model_query(tool, args)
   → READ-ONLY calls to the Autodesk Public MCP Server (localhost:3000)
   → Used for model context queries ONLY: element counts, loaded families,
     view info, precondition checks.
   → The Autodesk Public MCP Server is confirmed read-only (Tech Preview, April 2026).
     It CANNOT place elements, set parameters, or create tags.

2. execute_shortcut(shortcut_id, params)
   → Writes a pending_execution.json file to the shared IPC directory.
   → The C# RevitLogger add-in watches this directory (FileSystemWatcher) and
     executes the shortcut directly via the Revit API in a valid transaction.
   → Returns the result written to execution_result_{shortcut_id}.json by the add-in.

Architecture rationale:
   The Autodesk Public MCP Server is read-only in its current Tech Preview.
   Element creation, parameter setting, and tag creation require Revit API
   transactions, which can only be initiated from a thread that holds the
   Revit API context — i.e., from within our C# add-in.
   File-based IPC (pending_execution.json) is the simplest reliable mechanism
   for Python → C# communication without a custom HTTP server in the add-in.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

# ── Shared directory for Python ↔ C# add-in communication ─────────────────────
IPC_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_IPC_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "ipc",
))
IPC_DIR.mkdir(parents=True, exist_ok=True)

# ── Autodesk Public MCP Server (read-only model queries) ──────────────────────
AUTODESK_MCP_BASE = os.environ.get("AUTODESK_MCP_URL", "http://localhost:3000")


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Model context queries  (read-only → Autodesk Public MCP Server)
# ═══════════════════════════════════════════════════════════════════════════════

def model_query(tool_name: str, arguments: dict) -> dict:
    """
    Send a single READ-ONLY query to the Autodesk Public MCP Server.

    Use this for model context queries before suggesting or executing shortcuts:
      - count_elements(category="Doors", level="L1")
      - get_loaded_families(category="Doors")
      - get_active_view()
      - get_project_info()

    NOTE: The Autodesk Public MCP Server (Revit 2027 Tech Preview) is read-only.
    Any attempt to call a write tool will return an error from the server.
    Use execute_shortcut() for all model modification operations.
    """
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "tools/call",
        "params":  {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = httpx.post(
            f"{AUTODESK_MCP_BASE}/",
            json=payload,
            timeout=15.0,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json().get("result", {})
    except httpx.ConnectError:
        return {
            "error": "Autodesk Public MCP Server not reachable at localhost:3000. "
                     "Is Revit 2027 open with the MCP server enabled?",
            "available": False,
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_active_view() -> dict:
    """Query the currently active Revit view (read-only)."""
    return model_query("get_active_view", {})


def count_elements_by_category(category: str, level: str = "") -> dict:
    """Count elements of a given category, optionally filtered by level (read-only)."""
    args = {"category": category}
    if level:
        args["level"] = level
    return model_query("get_elements_by_category", args)


def get_loaded_families(category: str) -> dict:
    """List all loaded families for a given category (read-only)."""
    return model_query("get_loaded_families", {"category": category})


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Shortcut execution  (write → C# add-in via file-based IPC)
# ═══════════════════════════════════════════════════════════════════════════════

def execute_shortcut(
    shortcut_id: str,
    params: dict | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """
    Execute a saved shortcut by signalling the C# RevitLogger add-in.

    Flow:
      1. Python writes  ipc/pending_execution.json
      2. C# FileSystemWatcher detects the file
      3. C# reads ShortcutConfig, runs Place/SetParam/Tag in a Revit transaction
      4. C# writes ipc/execution_result_{shortcut_id}.json
      5. Python reads result and returns it

    Args:
        shortcut_id: ID of a saved ShortcutConfig (from generate_command).
        params:      Runtime parameter overrides, e.g. {"Mark": "D-101"}.
        timeout_s:   Seconds to wait for C# add-in to respond (default 30).

    Returns:
        Result dict from the C# add-in, or an error dict if timeout/unavailable.
    """
    pending_path = IPC_DIR / "pending_execution.json"
    result_path  = IPC_DIR / f"execution_result_{shortcut_id}.json"

    # Clean up any stale result file from a previous run
    result_path.unlink(missing_ok=True)

    # Write the execution request
    request = {
        "shortcut_id": shortcut_id,
        "params":      params or {},
        "requested_at": time.time(),
    }
    pending_path.write_text(json.dumps(request, indent=2), encoding="utf-8")

    # Poll for the result file (C# add-in writes this when done)
    deadline = time.time() + timeout_s
    poll_interval = 0.25
    while time.time() < deadline:
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                result_path.unlink(missing_ok=True)
                return result
            except Exception as exc:
                return {"error": f"Could not read result file: {exc}"}
        time.sleep(poll_interval)

    # Timeout — clean up and explain
    pending_path.unlink(missing_ok=True)
    return {
        "error": (
            f"Shortcut execution timed out after {timeout_s}s. "
            "Ensure the RevitLogger add-in is loaded in Revit 2027 and "
            "a project document is open."
        ),
        "shortcut_id": shortcut_id,
    }


def execute_mcp_tool_sequence(tool_sequence: list[dict]) -> list[dict]:
    """
    Legacy compatibility shim — called by mcp_server/server.py.

    DEPRECATED behaviour: previously forwarded tool calls to the Autodesk
    Public MCP Server, which is read-only and cannot execute model changes.

    New behaviour: returns a clear explanation instructing callers to use
    execute_shortcut() instead.  The server.py execute_revit_command tool
    already calls execute_shortcut() directly via shortcut_id, so this
    function is only reached by old code paths.
    """
    return [
        {
            "tool":   step.get("tool", "unknown"),
            "status": "skipped",
            "reason": (
                "The Autodesk Public MCP Server is read-only (Tech Preview, 2026). "
                "Model modifications are executed by the C# RevitLogger add-in "
                "via file-based IPC. Use execute_revit_command(shortcut_id=...) "
                "from the MCP server tools instead."
            ),
        }
        for step in tool_sequence
    ]
