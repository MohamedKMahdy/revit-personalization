"""Bridges execute calls to the Revit Public MCP Server (localhost:3000)."""
from __future__ import annotations
import httpx
import json
import os

REVIT_MCP_BASE = os.environ.get("REVIT_MCP_URL", "http://localhost:3000")


def _call_revit_tool(tool_name: str, arguments: dict) -> dict:
    """Send a single tool call to the Revit Public MCP Server."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = httpx.post(
            f"{REVIT_MCP_BASE}/",
            json=payload,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("result", result)
    except httpx.ConnectError:
        return {"error": "Revit Public MCP Server not reachable. Is Revit 2027 running?"}
    except Exception as e:
        return {"error": str(e)}


def execute_mcp_tool_sequence(tool_sequence: list[dict]) -> list[dict]:
    """Execute a list of MCP tool calls in order, returning each result."""
    results = []
    for step in tool_sequence:
        tool_name = step.get("tool")
        arguments = step.get("arguments", {})
        result = _call_revit_tool(tool_name, arguments)
        results.append({"tool": tool_name, "arguments": arguments, "result": result})
        if "error" in result:
            break
    return results
