"""
Evaluation harness — tests Pattern Agent with varying k and scores output quality.

Usage:
    python eval/run_experiment.py [--routine-id door_single_flush_tagged]
"""
from __future__ import annotations
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.log_reader import get_routine_examples
from orchestrator.pattern_agent import extract_motif

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

GROUND_TRUTH = {
    "door_single_flush_tagged": {
        "expected_steps": ["Place", "SetParam", "SetParam", "SetParam", "SetParam", "Tag"],
        "constant_params": {"Fire Rating": "60", "Width": 915, "OmniClass Number": "23.30.20.11"},
        "variable_params": ["Mark"],
    },
    "window_fixed_classified": {
        "expected_steps": ["Place", "SetParam", "SetParam", "SetParam", "Tag"],
        "constant_params": {"OmniClass Number": "23.30.30.11", "Sill Height": 900},
        "variable_params": ["Mark"],
    },
}


def score_motif(motif: dict, ground_truth: dict) -> dict:
    """Score a motif against ground truth. Returns scores 0.0–1.0."""
    extracted_steps = [s.get("action") for s in motif.get("steps", [])]
    expected_steps = ground_truth["expected_steps"]

    step_match = sum(a == b for a, b in zip(extracted_steps, expected_steps)) / max(len(expected_steps), 1)

    constant_hits = 0
    for step in motif.get("steps", []):
        if step.get("action") == "SetParam" and step.get("paramValueType") == "constant":
            p, v = step.get("paramName"), step.get("paramValue")
            if p in ground_truth["constant_params"]:
                if str(v) == str(ground_truth["constant_params"][p]):
                    constant_hits += 1
    const_score = constant_hits / max(len(ground_truth["constant_params"]), 1)

    extracted_vars = set(motif.get("parameters_to_prompt", []))
    expected_vars = set(ground_truth["variable_params"])
    var_score = len(extracted_vars & expected_vars) / max(len(expected_vars), 1)

    return {
        "step_match": round(step_match, 3),
        "constant_param_score": round(const_score, 3),
        "variable_param_score": round(var_score, 3),
        "overall": round((step_match + const_score + var_score) / 3, 3),
    }


def run_experiment(routine_id: str, k_values: list[int] | None = None) -> None:
    if k_values is None:
        k_values = [1, 3, 5]

    gt = GROUND_TRUTH.get(routine_id)
    if gt is None:
        print(f"No ground truth for routine '{routine_id}'. Available: {list(GROUND_TRUTH)}")
        return

    rows = []
    for k in k_values:
        routine = get_routine_examples(routine_id, k=k)
        if routine is None or len(routine.examples) < k:
            print(f"  k={k}: not enough examples, skipping")
            continue

        examples = [ex.model_dump() for ex in routine.examples[:k]]

        print(f"\n  k={k}: running Pattern Agent...")
        t0 = time.time()
        try:
            motif = extract_motif(examples, routine_label=routine.label)
            elapsed = round(time.time() - t0, 1)
            scores = score_motif(motif, gt)
            print(f"    Elapsed: {elapsed}s | Scores: {scores}")
            rows.append({"routine_id": routine_id, "k": k, "elapsed_s": elapsed, **scores})
        except Exception as e:
            print(f"    FAILED: {e}")
            rows.append({"routine_id": routine_id, "k": k, "error": str(e)})

    if rows:
        out_path = RESULTS_DIR / f"performance_vs_k_{routine_id}.csv"
        fieldnames = ["routine_id", "k", "elapsed_s", "step_match",
                      "constant_param_score", "variable_param_score", "overall", "error"]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--routine-id", default="door_single_flush_tagged")
    parser.add_argument("--k-values", nargs="+", type=int, default=[1, 3, 5])
    args = parser.parse_args()

    print(f"Running evaluation for '{args.routine_id}' with k={args.k_values}")
    run_experiment(args.routine_id, args.k_values)
