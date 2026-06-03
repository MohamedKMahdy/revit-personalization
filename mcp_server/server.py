"""Local Personalization MCP Server — exposes BIM log resources and tools."""
from __future__ import annotations
import json
import os
import sys
import uuid
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP

from mcp_server.log_reader import list_candidate_routines, get_routine_examples
from mcp_server.revit_bridge import execute_shortcut, model_query
from shared.schemas import ShortcutConfig, Motif, MotifStep

SHORTCUTS_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_SHORTCUTS_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "shortcuts"
))
SHORTCUTS_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("revit-personalization")


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("logs://candidate_routines")
def resource_candidate_routines() -> str:
    """List all detected candidate routines with metadata."""
    routines = list_candidate_routines()
    summary = [
        {
            "id": r.id,
            "label": r.label,
            "action_signature": r.action_signature,
            "count": r.count,
            "confidence": r.confidence,
            "example_count": len(r.examples),
        }
        for r in routines
    ]
    return json.dumps(summary, indent=2)


@mcp.resource("logs://routine/{routine_id}/examples")
def resource_routine_examples(routine_id: str) -> str:
    """Return up to 5 example sequences for a given routine ID."""
    routine = get_routine_examples(routine_id, k=5)
    if routine is None:
        return json.dumps({"error": f"Routine '{routine_id}' not found"})
    return routine.model_dump_json(indent=2)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_pattern(sequences: list[dict], routine_id: str = "") -> dict:
    """
    Store and validate example sequences for a routine.

    The actual motif extraction is performed by the Pattern Agent (orchestrator).
    This tool validates the sequences structure and returns them ready for analysis.

    Args:
        sequences: List of action sequence examples (each a list of ActionRecord dicts).
        routine_id: Optional routine ID to associate with these sequences.

    Returns:
        Validated sequences with metadata, ready to pass to the Pattern Agent.
    """
    if not sequences:
        return {"error": "No sequences provided"}

    validated = []
    for i, seq in enumerate(sequences):
        actions = seq.get("actions", seq) if isinstance(seq, dict) else seq
        if not isinstance(actions, list):
            return {"error": f"Sequence {i} has invalid format"}
        validated.append({
            "example_index": i,
            "action_count": len(actions),
            "action_signature": ",".join(
                (a.get("action_type") or a.get("action") or "?")[0]
                for a in actions
            ),
            "actions": actions,
        })

    return {
        "routine_id": routine_id or "unknown",
        "sequence_count": len(validated),
        "sequences": validated,
        "ready_for_pattern_agent": True,
    }


@mcp.tool()
def generate_command(motif: dict, name: str) -> dict:
    """
    Persist a named shortcut config derived from a motif.

    Converts a motif JSON (produced by the Pattern Agent) into a ShortcutConfig
    with a ready-to-execute MCP tool call sequence, and saves it to disk.

    Args:
        motif: Motif dict with keys: name, description, steps, preconditions,
               parameters_to_prompt.
        name: Human-readable shortcut name (e.g. "Place Fire Door 60min").

    Returns:
        The saved ShortcutConfig as a dict.
    """
    try:
        motif_obj = Motif(**motif)
    except Exception as e:
        return {"error": f"Invalid motif: {e}"}

    tool_sequence = _motif_to_tool_sequence(motif_obj)

    config = ShortcutConfig(
        shortcut_id=str(uuid.uuid4())[:8],
        name=name,
        motif=motif_obj,
        mcp_tool_sequence=tool_sequence,
    )

    out_path = SHORTCUTS_DIR / f"{config.shortcut_id}.json"
    out_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")

    return {
        "shortcut_id": config.shortcut_id,
        "name": config.name,
        "saved_to": str(out_path),
        "tool_sequence": tool_sequence,
        "parameters_to_prompt": motif_obj.parameters_to_prompt,
    }


@mcp.tool()
def execute_revit_command(shortcut_id: str, params: dict | None = None) -> dict:
    """
    Execute a saved shortcut in the live Revit model.

    How it works:
      - Writes a pending_execution.json file to the shared IPC directory.
      - The C# RevitLogger add-in (running inside Revit) detects this file,
        reads the ShortcutConfig, and executes Place / SetParam / Tag actions
        directly via the Revit API inside a valid transaction.
      - Returns the execution result written back by the add-in.

    NOTE: The Autodesk Public MCP Server is read-only (Tech Preview, 2026) and
    cannot be used for model modifications. Execution goes through the C# add-in.

    Args:
        shortcut_id: ID of a saved shortcut (from generate_command).
        params:      Optional runtime parameter overrides, e.g. {"Mark": "D-105"}.

    Returns:
        Execution result from the C# add-in, or an error if the add-in is not running.
    """
    shortcut_path = SHORTCUTS_DIR / f"{shortcut_id}.json"
    if not shortcut_path.exists():
        return {"error": f"Shortcut '{shortcut_id}' not found"}

    return execute_shortcut(shortcut_id=shortcut_id, params=params)


@mcp.tool()
def query_model(tool_name: str, arguments: dict | None = None) -> dict:
    """
    Query the live Revit model via the Autodesk Public MCP Server (read-only).

    Use this to fetch model context before suggesting or executing shortcuts:
      - tool_name="get_elements_by_category", arguments={"category":"Doors","level":"L1"}
      - tool_name="get_active_view"
      - tool_name="get_loaded_families", arguments={"category":"Doors"}

    NOTE: The Autodesk Public MCP Server only supports read-only operations.
    Requires Revit 2027 to be running with the MCP server enabled (localhost:3000).

    Args:
        tool_name:  Autodesk Public MCP Server tool name.
        arguments:  Tool arguments dict (optional).

    Returns:
        Query result from the Autodesk server, or error if not reachable.
    """
    return model_query(tool_name, arguments or {})


@mcp.tool()
def list_shortcuts() -> list[dict]:
    """List all saved shortcuts."""
    shortcuts = []
    for f in SHORTCUTS_DIR.glob("*.json"):
        try:
            c = ShortcutConfig.model_validate_json(f.read_text(encoding="utf-8"))
            shortcuts.append({
                "shortcut_id": c.shortcut_id,
                "name": c.name,
                "steps": len(c.motif.steps),
                "parameters_to_prompt": c.motif.parameters_to_prompt,
            })
        except Exception:
            pass
    return shortcuts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _motif_to_tool_sequence(motif: Motif) -> list[dict]:
    """Convert a Motif into a list of Revit Public MCP Server tool calls."""
    sequence = []
    for step in motif.steps:
        if step.action_type == "Place":
            sequence.append({
                "tool": "place_element",
                "arguments": {
                    "family_type": step.family_name,
                    "location": "{{location}}",  # filled at runtime
                },
            })
        elif step.action_type == "SetParam":
            value = (
                step.param_value
                if step.param_value_type == "constant"
                else f"{{{{{step.param_name}}}}}"
            )
            sequence.append({
                "tool": "set_parameter",
                "arguments": {
                    "element_id": "{{last_element_id}}",
                    "parameter_name": step.param_name,
                    "value": value,
                },
            })
        elif step.action_type == "Tag":
            sequence.append({
                "tool": "create_annotation_tag",
                "arguments": {
                    "element_id": "{{last_element_id}}",
                    "tag_family": step.tag_family_name,
                },
            })
    return sequence


def _apply_param_overrides(tool_sequence: list[dict], params: dict) -> list[dict]:
    import copy
    result = copy.deepcopy(tool_sequence)
    for step in result:
        args = step.get("arguments", {})
        for key, val in args.items():
            if isinstance(val, str) and val.startswith("{{") and val.endswith("}}"):
                param_name = val[2:-2]
                if param_name in params:
                    args[key] = params[param_name]
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Revit Personalization MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="sse",
        help="MCP transport (default: sse — required for Autodesk Assistant and MCP Inspector)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3100,
        help="HTTP port for sse/streamable-http transport (default: 3100)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        print("[RevitPersonalization MCP] Starting with stdio transport", flush=True)
        mcp.run(transport="stdio")
    else:
        host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.settings.host = host
        print(f"[RevitPersonalization MCP] Starting with {args.transport} transport")
        print(f"  Listening : http://{host}:{args.port}")
        print(f"  Resources : logs://candidate_routines")
        print(f"              logs://routine/{{id}}/examples")
        print(f"  Tools     : analyze_pattern, generate_command, execute_revit_command,")
        print(f"              query_model, list_shortcuts")
        print(f"  MCP Insp. : npx @modelcontextprotocol/inspector  -> {args.transport}://http://127.0.0.1:{args.port}/sse")
        print(f"  Autodesk  : Add MCP Server -> http://{host}:{args.port}/sse", flush=True)
        mcp.run(transport=args.transport)
