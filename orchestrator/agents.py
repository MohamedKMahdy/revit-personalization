"""
BIM Personalization Orchestrator — main CLI entry point.

Pipeline (updated architecture §4.1–4.2):
  1. Fetch k examples of a candidate routine from real/synthetic logs
  2. Pattern Agent  (claude-opus-4-8 + adaptive thinking)
       → analyses k examples → extracts a generalised Motif JSON
  3. Macro Agent (claude-sonnet-4-6)
       → queries live model state via model:query_state (grounding)
       → maps Motif → ordered mcp-servers-for-revit tool call sequence
       → checks preconditions
  4. Dry-run preview shown to the user
  5. User confirms → shortcut saved to disk
  6. Optional: execute via mcp-servers-for-revit (requires Revit + plugin running)

Usage:
    # List available routines from real logs
    python orchestrator/agents.py --list

    # Run the full pipeline on a specific routine
    python orchestrator/agents.py --routine-id <id> [--k 5] [--execute] [--auto-confirm]

    # Quick demo using synthetic test data
    python orchestrator/agents.py --routine-id door_single_flush_tagged --k 5

Environment variables:
    ANTHROPIC_API_KEY         — required; your Anthropic API key
    PATTERN_AGENT_MODEL       — override Pattern Agent model (default: claude-opus-4-8)
    MACRO_AGENT_MODEL         — override Macro Agent model (default: claude-sonnet-4-6)
    MCP_REVIT_BACKEND_URL     — mcp-servers-for-revit URL (default: http://localhost:3001)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Force UTF-8 console output on Windows so Unicode labels print correctly.
# Guard on __main__: reconfiguring stdout at import time detaches the capture
# stream's buffer and crashes pytest's output capture at teardown when this
# module is imported by a test.
if __name__ == "__main__" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Allow imports from project root when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.log_reader import list_candidate_routines, get_routine_examples
from mcp_server.revit_bridge import execute_shortcut, execute_tool_sequence
from orchestrator.pattern_agent import extract_motif
from orchestrator.macro_agent import generate_tool_sequence, get_context_summary
from shared.schemas import Motif, ShortcutConfig

SHORTCUTS_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_SHORTCUTS_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "shortcuts",
))


# ── Pretty printing ───────────────────────────────────────────────────────────

SEP = "-" * 60


def _header(text: str) -> None:
    print(f"\n{SEP}")
    print(f"  {text}")
    print(SEP)


def _step(label: str, text: str = "") -> None:
    print(f"\n[{label}] {text}")


def _ok(text: str) -> None:
    print(f"  OK  {text}")


def _warn(text: str) -> None:
    print(f"  !! {text}", file=sys.stderr)


# ── Core pipeline ─────────────────────────────────────────────────────────────

def run(
    routine_id: str,
    k: int = 5,
    auto_confirm: bool = False,
    execute: bool = False,
    execute_params: Optional[dict] = None,
    fetch_context: bool = True,
) -> dict:
    """
    Full pipeline: fetch → Pattern Agent → Macro Agent (with grounding) → confirm → save → (execute).

    Args:
        routine_id:     Candidate routine ID from the log reader.
        k:              Number of example episodes to pass to the Pattern Agent.
        auto_confirm:   Skip the interactive confirmation prompt.
        execute:        Execute the shortcut in Revit after saving.
        execute_params: Runtime parameter overrides for execution.
        fetch_context:  Query live Revit model for grounding (requires Revit + plugin).

    Returns:
        Summary dict describing what happened (status, shortcut_id, motif, etc.).
    """
    # ── 0. API key check ──────────────────────────────────────────────────────
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "\nERROR: ANTHROPIC_API_KEY environment variable is not set.\n"
            "  Set it with:  set ANTHROPIC_API_KEY=sk-ant-...\n"
            "  Get a key at: https://console.anthropic.com/",
            file=sys.stderr,
        )
        return {"error": "ANTHROPIC_API_KEY not set"}

    # ── 1. Fetch examples ─────────────────────────────────────────────────────
    _header("BIM Personalization Orchestrator")
    _step("1/5", f"Loading routine '{routine_id}' (k={k})…")

    routine = get_routine_examples(routine_id, k=k)
    if routine is None:
        _warn(f"Routine '{routine_id}' not found. Run with --list to see available routines.")
        return {"error": f"Routine '{routine_id}' not found"}

    _ok(f"Found {len(routine.examples)} example(s) — label: {routine.label}")
    _ok(f"Signature: {routine.action_signature}  confidence: {routine.confidence:.0%}")

    # ── 2. Pattern Agent ──────────────────────────────────────────────────────
    _step("2/5", "Pattern Agent analysing examples (adaptive thinking)…")
    t0 = time.time()

    examples_payload = [ex.model_dump() for ex in routine.examples]
    try:
        motif = extract_motif(examples_payload, routine_label=routine.label)
    except Exception as exc:
        _warn(f"Pattern Agent failed: {exc}")
        return {"error": str(exc)}

    elapsed = time.time() - t0
    _ok(f"Motif extracted in {elapsed:.1f}s — '{motif['name']}'")
    _ok(f"Steps: {len(motif['steps'])}  |  "
        f"Variable params: {motif.get('parameters_to_prompt', [])}")

    print("\n  Motif detail:")
    for i, step in enumerate(motif["steps"], 1):
        at = step.get("action_type", step.get("action", "?"))
        if at == "Place":
            print(f"    {i}. Place  family_name={step.get('family_name','?')!r}")
        elif at == "SetParam":
            vtype = step.get("param_value_type", "?")
            val   = step.get("param_value")
            display_val = repr(val) if vtype == "constant" else "(user input)"
            print(f"    {i}. SetParam  {step.get('param_name','?')!r}  -> {display_val}")
        elif at == "Tag":
            print(f"    {i}. Tag  tag_family={step.get('tag_family_name','?')!r}")

    if motif.get("preconditions"):
        print(f"  Preconditions: {motif['preconditions']}")

    # ── 3. Macro Agent ────────────────────────────────────────────────────────
    _step("3/5", "Macro Agent querying model context + generating tool sequence…")
    t0 = time.time()

    try:
        tool_sequence = generate_tool_sequence(motif, fetch_context=fetch_context)
    except Exception as exc:
        _warn(f"Macro Agent failed: {exc}")
        return {"error": str(exc), "motif": motif}

    elapsed = time.time() - t0
    _ok(f"Tool sequence generated in {elapsed:.1f}s — {len(tool_sequence)} step(s)")

    # ── 4. Dry-run preview ────────────────────────────────────────────────────
    _step("4/5", "DRY RUN — shortcut execution plan:")
    for i, step in enumerate(tool_sequence, 1):
        args_str = json.dumps(step.get("arguments", {}))
        print(f"    {i}. {step['tool']}({args_str})")

    if motif.get("parameters_to_prompt"):
        print(f"\n  You will be prompted for: {motif['parameters_to_prompt']}")

    # ── 5. Confirm & save ─────────────────────────────────────────────────────
    _step("5/5", "Save shortcut?")

    if not auto_confirm:
        try:
            answer = input(f"  Save '{motif['name']}' as a one-click shortcut? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer != "y":
            print("  Shortcut discarded.")
            return {"status": "discarded", "motif": motif, "tool_sequence": tool_sequence}

    SHORTCUTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        motif_obj = Motif(**motif)
    except Exception as exc:
        _warn(f"Motif validation failed: {exc}")
        return {"error": str(exc)}

    import uuid
    shortcut_id = uuid.uuid4().hex[:8]
    config = ShortcutConfig(
        shortcut_id=shortcut_id,
        name=motif["name"],
        motif=motif_obj,
        mcp_tool_sequence=tool_sequence,
    )
    out_path = SHORTCUTS_DIR / f"{shortcut_id}.json"
    out_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    _ok(f"Saved: {out_path}")

    result = {
        "status": "saved",
        "shortcut_id": shortcut_id,
        "name": motif["name"],
        "saved_to": str(out_path),
        "motif": motif,
        "tool_sequence": tool_sequence,
    }

    # ── 6. Optional live execution ────────────────────────────────────────────
    if execute:
        _step("BONUS", "Executing via mcp-servers-for-revit…")
        exec_result = execute_shortcut(
            shortcut_id=shortcut_id,
            params=execute_params,
        )
        result["execution_result"] = exec_result

        if exec_result.get("success"):
            _ok(f"All {exec_result['steps_executed']} step(s) executed successfully.")
        else:
            n_err = exec_result.get("errors", 0)
            _warn(f"Execution had {n_err} error(s):")
            for step_r in exec_result.get("results", []):
                if "error" in step_r.get("result", {}):
                    print(f"    step {step_r['step']} {step_r['tool']}: "
                          f"{step_r['result']['error']}")

    return result


# ── List mode ─────────────────────────────────────────────────────────────────

def list_routines() -> None:
    routines = list_candidate_routines()
    if not routines:
        print("No candidate routines found.")
        print(f"Run Revit with the add-in and repeat an action sequence ≥2 times.")
        print(f"Logs are read from: %LOCALAPPDATA%\\RevitPersonalization\\logs\\")
        return

    print(f"\nCandidate routines ({len(routines)} found):\n")
    for r in routines:
        src = "real" if r.id.startswith("routine_") else "synthetic"
        print(f"  {'-'*55}")
        print(f"  ID:         {r.id}")
        print(f"  Label:      {r.label}")
        print(f"  Signature:  {r.action_signature}")
        print(f"  Count:      {r.count}x  Confidence: {r.confidence:.0%}  Source: {src}")
    print(f"  {'─'*55}")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BIM Personalization Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true",
                        help="List all detected candidate routines and exit")
    parser.add_argument("--routine-id",
                        help="Candidate routine ID to process")
    parser.add_argument("--k", type=int, default=5,
                        help="Number of examples to use (default: 5)")
    parser.add_argument("--auto-confirm", action="store_true",
                        help="Skip the save-shortcut confirmation prompt")
    parser.add_argument("--execute", action="store_true",
                        help="Execute the shortcut in Revit via mcp-servers-for-revit")
    parser.add_argument("--no-context", action="store_true",
                        help="Skip live model context query (useful when Revit is not running)")
    parser.add_argument("--params", type=str, default="{}",
                        help='JSON dict of runtime parameter overrides, e.g. \'{"Mark":"D-101"}\'')

    args = parser.parse_args()

    if args.list:
        list_routines()
        sys.exit(0)

    if not args.routine_id:
        parser.error("--routine-id is required unless --list is used")

    try:
        execute_params = json.loads(args.params)
    except json.JSONDecodeError:
        parser.error(f"--params must be valid JSON: {args.params!r}")

    result = run(
        routine_id=args.routine_id,
        k=args.k,
        auto_confirm=args.auto_confirm,
        execute=args.execute,
        execute_params=execute_params,
        fetch_context=not args.no_context,
    )

    print("\n" + "─" * 60)
    print("Done.")
    # Print summary without the full motif to keep output readable
    summary = {k: v for k, v in result.items() if k not in ("motif", "tool_sequence")}
    print(json.dumps(summary, indent=2, default=str))
