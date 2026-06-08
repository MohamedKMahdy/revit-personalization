"""
Evaluation harness — measures Pattern Agent quality vs. number of examples (k).

Implements two evaluation dimensions from thesis §4.4:

SAMPLE EFFICIENCY (§4.4.1)
  For each candidate routine x each k in K_VALUES x reps repetitions:
    1. Sample k examples
    2. Run the Pattern Agent (claude-opus-4-7 + adaptive thinking)
    3. Score the extracted motif against the ground-truth episode structure:
         step_match_accuracy  — fraction of GT steps correctly predicted
         param_coverage       — fraction of SetParam steps with correct param_name
         spurious_steps       — motif steps not in ground truth
         quality_score        — 0-3 rubric (0=unusable, 1=partial, 2=minor edits, 3=exact)
    4. Record latency and token usage
  Output: results/performance_vs_k.csv, results/performance_vs_k_motifs.jsonl

PROCESS ACCELERATION (§4.4.2)
  Compares manual vs. assisted task completion:
    baseline_action_count    — action count from raw log (manual method)
    assisted_action_count    — actions to confirm + run the shortcut (1 click)
    baseline_time_s          — total time from log session timestamps
    estimated_acceleration   — ratio of manual to assisted action count
  Output: included as extra columns in performance_vs_k.csv

Usage:
    # Evaluate all detected routines with default k values
    python eval/run_experiment.py

    # Evaluate a specific routine with more k values and repetitions
    python eval/run_experiment.py --routine-id door_single_flush_tagged --k-values 1,3,5,10 --reps 3

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

# Default k values per thesis §4.4 (Sample Efficiency experiment)
DEFAULT_K_VALUES = [1, 3, 5, 10]

CSV_FIELDS = [
    # Identity
    "routine_id", "routine_label", "k", "rep",
    # Sample Efficiency metrics
    "step_match_accuracy", "param_coverage", "spurious_steps",
    "quality_score",
    "total_steps_gt", "total_steps_pred",
    # Process Acceleration metrics
    "baseline_action_count", "assisted_action_count", "acceleration_ratio",
    "baseline_time_s",
    # Cost / latency
    "input_tokens", "output_tokens", "latency_s",
    # Error
    "error",
]

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


def _baseline_metrics(routine) -> dict:
    """
    Derive Process Acceleration baseline from the raw log:
      baseline_action_count — mean action count across recorded episodes
      baseline_time_s       — mean duration of recorded episodes (seconds)
    The assisted method replaces these with 1 click + ~2s confirmation.
    """
    if not routine.examples:
        return {"baseline_action_count": 0, "baseline_time_s": 0.0}

    counts = [len(ex.actions) for ex in routine.examples]
    mean_count = sum(counts) / len(counts)

    # Compute duration from first and last action timestamp in each episode
    durations = []
    for ex in routine.examples:
        if len(ex.actions) >= 2:
            ts = [a.timestamp_unix for a in ex.actions]
            durations.append(max(ts) - min(ts))

    mean_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "baseline_action_count": round(mean_count, 1),
        "baseline_time_s": round(mean_duration, 1),
    }


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_motif(motif: dict, ground_truth: list[dict]) -> dict:
    """
    Score a predicted motif against the ground truth episode.

    step_match_accuracy: fraction of GT steps appearing (in order) in the motif
    param_coverage:      fraction of SetParam GT params present in the motif
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

    gt_params  = [pn for (at, pn) in gt_pairs if at == "SetParam" and pn]
    pred_params = {pn for (at, pn) in pred_pairs if at == "SetParam" and pn}
    param_coverage = (
        sum(1 for p in gt_params if p in pred_params) / len(gt_params)
        if gt_params else 1.0
    )
    spurious = max(0, len(pred_steps) - len(used_pred))

    return {
        "step_match_accuracy": round(step_match_accuracy, 3),
        "param_coverage":      round(param_coverage, 3),
        "spurious_steps":      spurious,
        "total_steps_gt":      len(gt_pairs),
        "total_steps_pred":    len(pred_steps),
    }


def quality_score(motif: dict, ground_truth: list[dict]) -> int:
    """
    Assign a 0-3 quality score per thesis §4.4 rubric:
      3 — exact match: step_match_accuracy==1.0, param_coverage==1.0, no spurious steps
      2 — correct with minor edits: accuracy>=0.75, param_coverage>=0.75, some spurious
      1 — partially correct: accuracy>=0.5 or param_coverage>=0.5
      0 — unusable: step_match_accuracy<0.5
    """
    s = score_motif(motif, ground_truth)
    acc  = s["step_match_accuracy"]
    cov  = s["param_coverage"]
    spur = s["spurious_steps"]

    if acc == 1.0 and cov == 1.0 and spur == 0:
        return 3
    elif acc >= 0.75 and cov >= 0.75:
        return 2
    elif acc >= 0.5:
        return 1
    else:
        return 0


# ── One cell: single (routine, k, rep) run ───────────────────────────────────

def _run_one(routine, k: int, rep: int, gt_steps: list[dict]) -> dict:
    """Run Pattern Agent once and return a result row dict."""
    import anthropic

    examples = routine.examples[:k] if rep == 1 else random.sample(routine.examples, k)

    slim = []
    for i, ex in enumerate(examples):
        slim.append({"example": i + 1, "actions": [
            {k2: v for k2, v in a.model_dump().items()
             if k2 in _KEEP_FIELDS and v not in (None, "", 0)}
            for a in ex.actions
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
    # Fixed: use adaptive thinking (budget_tokens removed in Opus 4.7+)
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    elapsed = time.time() - t0

    # Extract text block (skip thinking blocks)
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
    qs     = quality_score(motif, gt_steps)
    usage  = response.usage

    return {
        "routine_id":    routine.id,
        "routine_label": routine.label,
        "k":             k,
        "rep":           rep,
        **scores,
        "quality_score": qs,
        "input_tokens":  usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_s":     round(elapsed, 2),
        "error":         "",
        "_motif":        motif,  # stripped before writing to CSV
    }


# ── Main experiment loop ──────────────────────────────────────────────────────

def run_experiment(
    routine_ids: Optional[list[str]] = None,
    k_values: list[int] = DEFAULT_K_VALUES,
    reps: int = 1,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    all_routines = list_candidate_routines()
    if not all_routines:
        print("No candidate routines found.")
        print("  * Run Revit with the add-in and repeat an action sequence >=2 times, OR")
        print("  * Use synthetic logs in tests/synthetic_logs/")
        sys.exit(1)

    routines = (
        [r for r in all_routines if r.id in routine_ids]
        if routine_ids else all_routines
    )
    if not routines:
        print(f"Routines not found: {routine_ids}")
        sys.exit(1)

    print(f"\nEvaluation harness")
    print(f"  Routines : {len(routines)}")
    print(f"  k values : {list(k_values)}  (thesis §4.4 default: {DEFAULT_K_VALUES})")
    print(f"  Reps     : {reps}")
    print(f"  Model    : {MODEL}\n")

    rows: list[dict] = []

    for routine in routines:
        gt_steps = _ground_truth_steps(routine)
        baseline = _baseline_metrics(routine)
        available = len(routine.examples)

        # Assisted action count: 1 (click confirm in NotificationUI) + execute
        assisted_action_count = 2

        print(f"{'─'*55}")
        print(f"Routine : {routine.label}")
        print(f"  Examples  : {available}")
        print(f"  GT steps  : {[s['action_type'] for s in gt_steps]}")
        print(f"  Baseline  : {baseline['baseline_action_count']:.0f} actions, "
              f"{baseline['baseline_time_s']:.0f}s/episode")

        for k in k_values:
            if k > available:
                print(f"  k={k}: only {available} examples — skipping")
                continue
            for rep in range(1, reps + 1):
                print(f"  k={k} rep={rep}:  ", end="", flush=True)
                try:
                    row = _run_one(routine, k, rep, gt_steps)

                    accel = (
                        round(baseline["baseline_action_count"] / assisted_action_count, 1)
                        if baseline["baseline_action_count"] > 0 else 0
                    )

                    print(
                        f"acc={row['step_match_accuracy']:.0%}  "
                        f"cov={row['param_coverage']:.0%}  "
                        f"qs={row['quality_score']}/3  "
                        f"lat={row['latency_s']:.1f}s  "
                        f"tok={row['input_tokens']}in+{row['output_tokens']}out"
                    )

                    # Save full motif JSON for inspection
                    with open(JSONL_PATH, "a", encoding="utf-8") as jf:
                        jf.write(json.dumps({
                            "routine_id": routine.id, "k": k, "rep": rep,
                            "motif": row.pop("_motif"),
                            "scores": {
                                s: row[s] for s in [
                                    "step_match_accuracy", "param_coverage",
                                    "spurious_steps", "quality_score",
                                ]
                            },
                        }) + "\n")

                    row.update({
                        "baseline_action_count": baseline["baseline_action_count"],
                        "assisted_action_count": assisted_action_count,
                        "acceleration_ratio":   accel,
                        "baseline_time_s":      baseline["baseline_time_s"],
                    })

                except Exception as exc:
                    print(f"ERROR — {exc}")
                    row = {
                        "routine_id": routine.id, "routine_label": routine.label,
                        "k": k, "rep": rep, "error": str(exc),
                        **{f: 0 for f in CSV_FIELDS
                           if f not in ("routine_id", "routine_label", "k", "rep", "error")},
                    }
                rows.append({f: row.get(f, 0) for f in CSV_FIELDS})

    # Write CSV
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'─'*55}")
    print(f"Sample Efficiency  -> {CSV_PATH}")
    print(f"Motif archive      -> {JSONL_PATH}")

    ok_rows = [r for r in rows if not r["error"]]
    if ok_rows:
        avg_acc  = sum(r["step_match_accuracy"] for r in ok_rows) / len(ok_rows)
        avg_cov  = sum(r["param_coverage"]      for r in ok_rows) / len(ok_rows)
        avg_qs   = sum(r["quality_score"]        for r in ok_rows) / len(ok_rows)
        avg_acc_ratio = sum(r["acceleration_ratio"] for r in ok_rows) / len(ok_rows)
        print(f"\nOverall (n={len(ok_rows)}):")
        print(f"  Step accuracy    : {avg_acc:.0%}")
        print(f"  Param coverage   : {avg_cov:.0%}")
        print(f"  Quality score    : {avg_qs:.2f} / 3")
        print(f"  Accel. ratio     : {avg_acc_ratio:.1f}x  (manual actions / assisted actions)")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="BIM Personalization evaluation harness (§4.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--routine-id", nargs="*",
        help="Routine IDs to evaluate (default: all detected routines)",
    )
    parser.add_argument(
        "--k-values", default=",".join(str(k) for k in DEFAULT_K_VALUES),
        help=f"Comma-separated k values (default: {','.join(str(k) for k in DEFAULT_K_VALUES)})",
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
