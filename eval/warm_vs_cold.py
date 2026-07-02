"""Warm-vs-cold: does the assistant get BETTER at a routine with use? (thesis meta-learning evidence)

Derives the learning delta from REAL executor telemetry (executor_runs.jsonl — every live run the
system ever made), the same conservative log-derived style as process_acceleration.py:

    cold    = a routine's 1st run   (no per-user prior: no last_values, no known host, no compiled skill)
    warming = its 2nd run           (prior forming)
    warm    = its 3rd+ runs         (accumulated prior; may replay as a compiled skill at $0, no LLM)

Per phase we report: mean attempts (tool-loop iterations), mean $ cost, success (done) rate, failed-step
rate, escalation rate, and the compiled-replay share. A declining attempts/cost curve with rising
compiled share IS the in-context learning-to-learn claim measured on real usage.

HONEST FRAMING (state in the write-up): this is OBSERVATIONAL telemetry, not a controlled A/B — it
includes development-era runs (bugs later fixed inflate early attempts, which *overstates* cold cost but
also reflects real early-life behavior). The controlled version is the leave-one-user-out study
(eval/meta_learning_eval.py) once colleague data lands.

    python eval/warm_vs_cold.py [--csv]           # prints the table; --csv writes eval/results/warm_vs_cold.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

RUNS_PATH = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
             / "RevitPersonalization" / "logs" / "executor_runs.jsonl")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
CSV_PATH = RESULTS_DIR / "warm_vs_cold.csv"

PHASES = ("cold", "warming", "warm")


def _phase(run_index: int) -> str:
    return "cold" if run_index == 1 else ("warming" if run_index == 2 else "warm")


def load_runs(path: Path = RUNS_PATH) -> list[dict]:
    if not path.exists():
        print(f"no telemetry at {path}", file=sys.stderr)
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    # routine replays only (freeform chat tasks have no learning-curve identity)
    return [r for r in rows if r.get("routine_id") and r.get("routine_id") != "freeform"]


def analyze(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (per_run_rows, per_phase_summary)."""
    by_routine: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_routine[r["routine_id"]].append(r)

    per_run: list[dict] = []
    for rid, runs in by_routine.items():
        runs.sort(key=lambda r: r.get("ts", ""))
        for i, r in enumerate(runs, start=1):
            steps = r.get("steps") or []
            failed = sum(1 for s in steps if isinstance(s, dict) and s.get("ok") is False)
            per_run.append({
                "routine_id": rid,
                "run_index": i,
                "phase": _phase(i),
                "ts": r.get("ts", ""),
                "model": r.get("model", ""),
                "compiled": 1 if r.get("model") == "compiled" else 0,
                "attempts": int(r.get("attempts") or 0),
                "failed_steps": failed,
                "escalated": 1 if r.get("escalated") else 0,
                "done": 1 if r.get("done") else 0,
                "est_cost_usd": float(r.get("est_cost_usd") or 0.0),
            })

    summary: list[dict] = []
    for phase in PHASES:
        sel = [p for p in per_run if p["phase"] == phase]
        if not sel:
            continue
        n = len(sel)
        summary.append({
            "phase": phase,
            "runs": n,
            "mean_attempts": round(sum(p["attempts"] for p in sel) / n, 2),
            "mean_cost_usd": round(sum(p["est_cost_usd"] for p in sel) / n, 4),
            "done_rate": round(sum(p["done"] for p in sel) / n, 2),
            "mean_failed_steps": round(sum(p["failed_steps"] for p in sel) / n, 2),
            "escalation_rate": round(sum(p["escalated"] for p in sel) / n, 2),
            "compiled_share": round(sum(p["compiled"] for p in sel) / n, 2),
        })
    return per_run, summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true", help="also write eval/results/warm_vs_cold.csv")
    ap.add_argument("--since", default="", help="only runs with ts >= this ISO date (segment out the "
                    "development era, when high attempt counts reflected since-fixed system bugs)")
    a = ap.parse_args()

    rows = load_runs()
    if a.since:
        rows = [r for r in rows if str(r.get("ts", "")) >= a.since]
    if not rows:
        print("no routine runs in telemetry — nothing to analyze")
        return 0
    per_run, summary = analyze(rows)

    print("\n=========  WARM vs COLD — learning delta from live telemetry  =========\n")
    print(f"telemetry: {RUNS_PATH}")
    print(f"routine runs analyzed: {len(per_run)} across {len({p['routine_id'] for p in per_run})} routines\n")
    hdr = f"{'phase':8} {'runs':>5} {'attempts':>9} {'cost$':>8} {'done':>6} {'failed':>7} {'escal':>6} {'compiled':>9}"
    print(hdr); print("-" * len(hdr))
    for s in summary:
        print(f"{s['phase']:8} {s['runs']:>5} {s['mean_attempts']:>9} {s['mean_cost_usd']:>8} "
              f"{s['done_rate']:>6} {s['mean_failed_steps']:>7} {s['escalation_rate']:>6} {s['compiled_share']:>9}")

    cold = next((s for s in summary if s["phase"] == "cold"), None)
    warm = next((s for s in summary if s["phase"] == "warm"), None)
    if cold and warm and cold["mean_attempts"]:
        att = (1 - warm["mean_attempts"] / cold["mean_attempts"]) * 100
        cost = (1 - warm["mean_cost_usd"] / cold["mean_cost_usd"]) * 100 if cold["mean_cost_usd"] else 0
        print(f"\nHEADLINE: warm runs use {att:.0f}% fewer attempts and cost {cost:.0f}% less than cold runs"
              f" (compiled share warm: {warm['compiled_share']:.0%}).")
        print("Caveat: observational telemetry incl. development-era runs — see module docstring.")

    if a.csv:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(per_run[0].keys()))
            w.writeheader()
            w.writerows(per_run)
        sum_path = RESULTS_DIR / "warm_vs_cold_summary.csv"
        with open(sum_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader()
            w.writerows(summary)
        print(f"\nwrote {CSV_PATH}\nwrote {sum_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
