"""
Evaluation harness — measures Pattern Agent quality vs. number of examples (k).

For each candidate routine × each k in K_VALUES × reps repetitions:
  1. Sample k examples
  2. Run the Pattern Agent (claude-opus-4-7 + extended thinking)
  3. Score the extracted motif against the ground-truth episode structure:
       step_match_accuracy  — fraction of GT steps correctly predicted
       param_coverage       — fraction of SetParam steps with correct param_name
       spurious_steps       — motif steps not in ground truth
  4. Record latency and token usage

Output:
  results/performance_vs_k.csv           — one row per (routine, k, rep)
  results/performance_vs_k_motifs.jsonl  — full motif JSON for inspection

Usage:
    # Evaluate all detected routines with default k values
    python eval/run_experiment.py

    # Evaluate a specific routine with more k values and repetitions
    python eval/run_experiment.py --routine-id door_single_flush_tagged --k-values 1,2,3,5 --reps 3

ANTHROPIC_API_KEY must be set.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.log_reader import list_candidate_routines, get_routine_examples
from orchestrator.pattern_agent import extract_motif, SYSTEM_PROMPT, MODEL

RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

CSV_PATH   = RESULTS_DIR / "performance_vs_k.csv"
JSONL_PATH = RESULTS_DIR / "performance_vs_k_motifs.jsonl"

CSV_FIELDS = [
    "routine_id", "routine_label", "k", "rep",
    "step_match_accuracy", "param_coverage", "spurious_steps",
    "total_steps_gt", "total_steps_pred",
    "input_tokens", "output_tokens",
    "latency_s", "error",
]

# Fields to keep when slimming action dicts for the agent
_KEEP_FIELDS = {
    "action_type", "element_category", "family_name", "type_name",
    "param_name", "param_value_before", "param_value_after",
    "tag_family_name", "tagged_element_id",
    "level_name", "view_type", "transaction_name",
}


# ── Ground-truth derivation ───────────────────────────────────────────────────

def _ground_truth_steps(routine) -> list[dict]:
    """
    Use the example with the most actions as the ground-truth step sequence.
    Only action_type and param_name are used for scoring.
    """
    if not routine.examples:
        return []
    best = max(routine.examples, key=lambda e: len(e.actions))
    return [
        {"action_type": a.action_type, "param_name": a.param_name or ""}
        for a in best.actions
    ]


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_motif(motif: dict, ground_truth: list[dict]) -> dict:
    """
    Score a predicted motif against the ground truth episode.

    step_match_accuracy: what fraction of GT steps appear (in order) in the motif
    param_coverage:      what fraction of SetParam GT params are in the motif
    spurious_steps:      motif steps with no matching GT step
    """
    pred_steps = motif.get("steps", [])

    gt_pairs   = [(s["action_type"], s["param_name"]) for s in ground_truth]
    pred_pairs = [
        (s.get("action_type", s.get("action", "")), s.get("param_name", ""))
        for s in pred_steps
    ]

    # Greedy forward match
    used_pred = set()
    matched   = 0
    for gt_at, gt_pn in gt_pairs:
        for j, (p_at, p_pn) in enumerate(pred_pairs):
            if j in used_pred:
                continue
            if p_at == gt_at and (gt_pn == "" or p_pn == gt_pn):
                matched += 1
                used_pred.add(j)
                break

    step_match_accuracy = matched / len(gt_pairs) if gt_pairs else 1.0

    gt_params   = [pn for (at, pn) in gt_pairs if at == "SetParam" and pn]
    pred_params  = {pn for (at, pn) in pred_pairs if at == "SetParam" and pn}
    param_coverage = (
        sum(1 for p in gt_params if p in pred_params) / len(gt_params)
        if gt_params else 1.0
    )

    return {
        "step_match_accuracy": round(step_match_accuracy, 3),
        "param_coverage":      round(param_coverage, 3),
        "spurious_steps":      max(0, len(pred_steps) - len(used_pred)),
        "total_steps_gt":      len(gt_pairs),
        "total_steps_pred":    len(pred_steps),
    }


# ── One cell: single (routine, k, rep) run ────────────────────────────────────

def _run_one(routine, k: int, rep: int, gt_steps: list[dict]) -> dict:
    """Run Pattern Agent once and return a result row dict."""
    import anthropic

    examples = routine.examples[:k] if rep == 1 else random.sample(routine.examples, k)

    # Build slim payload (same slimming as pattern_agent.extract_motif)
    slim = []
    for i, ex in enumerate(examples):
        acts = ex.actions
        slim.append({"example": i + 1, "actions": [
            {k2: v for k2, v in a.model_dump().items()
             if k2 in _KEEP_FIELDS and v not in (None, "", 0)}
            for a in acts
        ]})

    user_msg = (
        f"Here are {k} recorded examples of the Revit routine "
        f'"{routine.label}". '
        "Extract a generalised motif that captures the invariant pattern.\n\n"
        f"Examples:\n{json.dumps(slim, indent=2)}\n\n"
        "Return ONLY the JSON motif object."
    )

    client = anthropic.Anthropic()
    t0 = time.time()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 8000},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    elapsed = time.time() - t0

    # Extract text block
    text = next(
        (b.text.strip() for b in response.content if b.type == "text"), ""
    )
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    motif  = json.loads(text)
    scores = score_motif(motif, gt_steps)
    usage  = response.usage

    return {
        "routine_id":    routine.id,
        "routine_label": routine.label,
        "k":             k,
        "rep":           rep,
        **scores,
        "input_tokens":  usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_s":     round(elapsed, 2),
        "error":         "",
        "_motif":        motif,  # stripped before writing to CSV
    }


# ── Main experiment loop ──────────────────────────────────────────────────────

def run_experiment(
    routine_ids: Optional[list[str]] = None,
    k_values: list[int] = (1, 2, 3, 5),
    reps: int = 1,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    all_routines = list_candidate_routines()
    if not all_routines:
        print("No candidate routines found.")
        print("  • Run Revit 2027 with the add-in and repeat an action sequence ≥2 times, OR")
        print("  • Use synthetic logs in tests/synthetic_logs/")
        sys.exit(1)

    routines = (
        [r for r in all_routines if r.id in routine_ids]
        if routine_ids else all_routines
    )
    if not routines:
        print(f"Routines not found: {routine_ids}")
        sys.exit(1)

    print(f"\nEvaluation harness — {len(routines)} routine(s), "
          f"k={list(k_values)}, reps={reps}\n")

    rows: list[dict] = []

    for routine in routines:
        gt_steps = _ground_truth_steps(routine)
        available = len(routine.examples)

        print(f"{'─'*55}")
        print(f"Routine: {routine.label}")
        print(f"  Examples available: {available}")
        print(f"  Ground truth:       {[s['action_type'] for s in gt_steps]}")

        for k in k_values:
            if k > available:
                print(f"  k={k}: only {available} examples available — skipping")
                continue
            for rep in range(1, reps + 1):
                print(f"  k={k} rep={rep}:  ", end="", flush=True)
                try:
                    row = _run_one(routine, k, rep, gt_steps)
                    print(
                        f"step_acc={row['step_match_accuracy']:.0%}  "
                        f"param_cov={row['param_coverage']:.0%}  "
                        f"latency={row['latency_s']:.1f}s  "
                        f"tokens={row['input_tokens']}in+{row['output_tokens']}out"
                    )
                    # Save full motif for inspection
                    with open(JSONL_PATH, "a", encoding="utf-8") as jf:
                        jf.write(json.dumps({
                            "routine_id": routine.id, "k": k, "rep": rep,
                            "motif": row.pop("_motif"), "scores": {
                                s: row[s] for s in
                                ["step_match_accuracy", "param_coverage", "spurious_steps"]
                            },
                        }) + "\n")
                except Exception as exc:
                    print(f"ERROR — {exc}")
                    row = {
                        "routine_id": routine.id, "routine_label": routine.label,
                        "k": k, "rep": rep, "error": str(exc),
                        **{f: 0 for f in CSV_FIELDS if f not in
                           ("routine_id","routine_label","k","rep","error")},
                    }
                rows.append({f: row.get(f, 0) for f in CSV_FIELDS})

    # Write CSV
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'─'*55}")
    print(f"Results → {CSV_PATH}")
    print(f"Motifs  → {JSONL_PATH}")

    ok_rows = [r for r in rows if not r["error"]]
    if ok_rows:
        avg_acc = sum(r["step_match_accuracy"] for r in ok_rows) / len(ok_rows)
        avg_cov = sum(r["param_coverage"]      for r in ok_rows) / len(ok_rows)
        print(f"\nOverall (n={len(ok_rows)}):  "
              f"avg step accuracy = {avg_acc:.0%}   "
              f"avg param coverage = {avg_cov:.0%}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pattern Agent evaluation harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--routine-id", nargs="*",
        help="Routine IDs to evaluate (default: all detected routines)",
    )
    parser.add_argument(
        "--k-values", default="1,2,3,5",
        help="Comma-separated k values, e.g. '1,2,3,5' (default: 1,2,3,5)",
    )
    parser.add_argument(
        "--reps", type=int, default=1,
        help="Repetitions per (routine, k) cell (default: 1)",
    )
    args = parser.parse_args()

    k_values = [int(x) for x in args.k_values.split(",")]
    run_experiment(
        routine_ids=args.routine_id,
        k_values=k_values,
        reps=args.reps,
    )
