"""
Main orchestrator — coordinates Pattern Agent and Macro/Command Agent.

Usage:
    python orchestrator/agents.py --routine-id door_single_flush_tagged [--k 5] [--auto-confirm]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.log_reader import get_routine_examples
from orchestrator.pattern_agent import extract_motif
from orchestrator.macro_agent import generate_tool_sequence


def run(routine_id: str, k: int = 5, auto_confirm: bool = False) -> dict:
    """
    Full pipeline: fetch examples → extract motif → generate tool sequence → confirm → execute.

    Returns a summary dict with the motif and tool sequence.
    """
    print(f"\n[Orchestrator] Loading routine '{routine_id}' (k={k})...")
    routine = get_routine_examples(routine_id, k=k)
    if routine is None:
        print(f"[Orchestrator] ERROR: Routine '{routine_id}' not found.")
        return {"error": f"Routine '{routine_id}' not found"}

    print(f"[Orchestrator] Found {len(routine.examples)} example(s). Label: {routine.label}")

    # --- Pattern Agent ---
    print("\n[Pattern Agent] Analyzing examples with extended thinking...")
    examples_payload = [ex.model_dump() for ex in routine.examples]
    motif = extract_motif(examples_payload, routine_label=routine.label)

    print("\n[Pattern Agent] Extracted motif:")
    print(json.dumps(motif, indent=2))

    # --- Macro Agent ---
    print("\n[Macro Agent] Generating MCP tool call sequence...")
    tool_sequence = generate_tool_sequence(motif)

    print("\n[Macro Agent] Proposed shortcut execution plan (DRY RUN):")
    for i, step in enumerate(tool_sequence, 1):
        print(f"  Step {i}: {step['tool']}({json.dumps(step['arguments'])})")

    if motif.get("parameters_to_prompt"):
        print(f"\n  Parameters you'll be prompted for at runtime: {motif['parameters_to_prompt']}")

    # --- User confirmation ---
    if not auto_confirm:
        answer = input("\nSave this as a shortcut? [y/N] ").strip().lower()
        if answer != "y":
            print("[Orchestrator] Shortcut discarded.")
            return {"status": "discarded", "motif": motif}

    # --- Save via MCP server tool (direct call for CLI mode) ---
    from mcp_server.server import generate_command
    shortcut_name = motif.get("name", f"Shortcut_{routine_id}")
    result = generate_command(motif=motif, name=shortcut_name)

    print(f"\n[Orchestrator] Shortcut saved: '{shortcut_name}' (id={result.get('shortcut_id')})")
    print(f"  File: {result.get('saved_to')}")

    return {
        "status": "saved",
        "shortcut_id": result.get("shortcut_id"),
        "name": shortcut_name,
        "motif": motif,
        "tool_sequence": tool_sequence,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BIM Personalization Orchestrator")
    parser.add_argument("--routine-id", required=True, help="Candidate routine ID to process")
    parser.add_argument("--k", type=int, default=5, help="Number of examples to use (default: 5)")
    parser.add_argument("--auto-confirm", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    result = run(routine_id=args.routine_id, k=args.k, auto_confirm=args.auto_confirm)
    print("\n[Orchestrator] Done.")
    print(json.dumps({k: v for k, v in result.items() if k != "motif"}, indent=2))
