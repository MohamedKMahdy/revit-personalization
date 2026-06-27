"""
Prediction evaluation — precision@1 of the proactive next-action predictor (Pillar B).

For every learned routine, replay each recorded example one action at a time; at each prefix the
predictor must name the NEXT action. precision@1 = fraction of prefixes where the top prediction's
next action matches what the user actually did next. This quantifies how well the system anticipates
the routines the user already repeats (the realtime-personalization claim).

Deterministic, $0 (no LLM / Revit). Run:
    python eval/prediction_eval.py --synthetic
    python eval/prediction_eval.py --real --csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.log_reader import list_candidate_routines   # noqa: E402
from detector._common import token                          # noqa: E402
from predictor import NextActionPredictor                   # noqa: E402

RESULTS_DIR = Path(__file__).parent.parent / "results"
CSV_PATH = RESULTS_DIR / "prediction_eval.csv"


def evaluate(routines) -> dict:
    predictor = NextActionPredictor(routines)
    total = hits = exact_hits = 0
    per_routine: list[dict] = []
    for r in routines:
        r_total = r_hits = 0
        for ex in r.examples:
            acts = ex.actions
            for L in range(1, len(acts)):                 # predict the action at index L from prefix[:L]
                pred = predictor.predict(acts[:L])
                r_total += 1
                if pred and pred.next_actions:
                    if pred.next_actions[0] == {"action_type": acts[L].action_type,
                                                "key": _key(acts[L])}:
                        r_hits += 1
                        if pred.match == "exact":
                            exact_hits += 1
        if r_total:
            per_routine.append({"routine_id": r.id, "label": r.label, "support": r.support,
                                "prefixes": r_total, "hits": r_hits,
                                "precision_at_1": round(r_hits / r_total, 3)})
            total += r_total
            hits += r_hits
    return {"routines": per_routine, "total_prefixes": total,
            "precision_at_1": round(hits / total, 3) if total else 0.0,
            "exact_match_share": round(exact_hits / hits, 3) if hits else 0.0}


def _key(a) -> str:
    from detector._common import derive_key
    return derive_key(a)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Next-action prediction precision@1.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--synthetic", action="store_true")
    src.add_argument("--real", action="store_true")
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()

    routines = [r for r in list_candidate_routines(include_synthetic=not args.real) if r.examples]
    if args.real:
        routines = [r for r in routines if not r.id.startswith("synthetic")]

    res = evaluate(routines)
    print(f"\n{'routine':46}  reps  prefixes  hits  prec@1")
    print("-" * 78)
    for r in res["routines"]:
        print(f"{r['label'][:46]:46}  {r['support']:>4}  {r['prefixes']:>8}  {r['hits']:>4}  {r['precision_at_1']:>6}")
    print("-" * 78)
    print(f"OVERALL precision@1 = {res['precision_at_1']}  over {res['total_prefixes']} prefixes "
          f"({res['exact_match_share']*100:.0f}% of hits were exact token matches)\n")

    if args.csv and res["routines"]:
        RESULTS_DIR.mkdir(exist_ok=True)
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(res["routines"][0].keys()))
            w.writeheader()
            w.writerows(res["routines"])
        print(f"wrote {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
