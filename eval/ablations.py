"""
Consolidated ablations for the thesis results chapter — the before/after evidence that the
"many steps further" work made the system more useful. Deterministic + $0 (no LLM, no Revit);
re-runs the building blocks shipped in earlier phases and prints one summary + writes
results/ablations.csv.

Comparisons:
  A1  Process acceleration  — manual vs assisted user effort (does one-click replay save work?)
  A2  Compound recovery     — v0.3 vs v0.2: multi-element routines captured (wall->door->tag)
  A3  Proactive prediction  — precision@1 of next-action prediction (reactive -> proactive)
  A4  Richer-goal coverage  — flat vs richer build_goal: structures the goal can express

Run:
    python eval/ablations.py            # synthetic + detected routines
    python eval/ablations.py --csv      # also write results/ablations.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).parent))   # sibling eval modules

from mcp_server.log_reader import list_candidate_routines   # noqa: E402
from shared.schemas import ActionRecord                     # noqa: E402
from detector import make_detector                          # noqa: E402
from orchestrator.executor_agent import build_goal          # noqa: E402
from process_acceleration import acceleration_for           # noqa: E402
from prediction_eval import evaluate as eval_prediction     # noqa: E402

RESULTS_DIR = _ROOT / "results"
CSV_PATH = RESULTS_DIR / "ablations.csv"


def _compound_session(reps: int = 3) -> list[ActionRecord]:
    """`reps` of: Place(wall) -> Place(door hosted on it) -> SetParam(Mark) -> Tag(door)."""
    recs: list[ActionRecord] = []
    for r in range(reps):
        t, w, d = r * 10.0, 100 + r, 200 + r
        recs += [
            ActionRecord(action_type="Place", element_id=w, element_category="Walls",
                         family_name="Basic Wall", timestamp_unix=t),
            ActionRecord(action_type="Place", element_id=d, element_category="Doors",
                         family_name="M_Door", host_category="Walls", timestamp_unix=t + 1),
            ActionRecord(action_type="SetParam", element_id=d, param_name="Mark",
                         param_value_after=f"D-{r+1:02d}", timestamp_unix=t + 2),
            ActionRecord(action_type="Tag", element_id=d, tagged_element_id=d,
                         tag_family_name="M_Door Tag", timestamp_unix=t + 3),
        ]
    return recs


def _compound_count(cands) -> int:
    """How many detected routines capture BOTH a wall and a door (a true compound)."""
    n = 0
    for c in cands:
        cats = {a.element_category for ex in c.examples for a in ex.actions if a.action_type == "Place"}
        if "Walls" in cats and "Doors" in cats:
            n += 1
    return n


def run_ablations(routines=None) -> dict:
    routines = routines if routines is not None else [r for r in list_candidate_routines() if r.examples]

    # A1 — process acceleration (manual vs assisted), repetition-weighted
    accs = [acceleration_for(r) for r in routines]
    man = sum(a.manual_actions * a.repetitions for a in accs)
    asst = sum(a.assisted_actions * a.repetitions for a in accs)
    a1 = {"manual_actions": round(man, 1), "assisted_actions": round(asst, 1),
          "reduction_pct": round((1 - asst / man) * 100, 1) if man else 0.0,
          "routines": len(accs)}

    # A2 — compound recovery: v0.3 vs v0.2 on a multi-element workflow
    recs = _compound_session(reps=3)
    a2 = {"v2_compounds": _compound_count(make_detector("v2").detect(recs, session_id="s")),
          "v3_compounds": _compound_count(make_detector("v3").detect(recs, session_id="s"))}

    # A3 — proactive next-action prediction precision@1
    pred = eval_prediction(routines)
    a3 = {"precision_at_1": pred["precision_at_1"], "prefixes": pred["total_prefixes"]}

    # A4 — richer-goal coverage: structures flat vs richer build_goal can express
    flat = {"name": "x", "steps": [{"action_type": "Place", "family_name": "M_Door"},
                                   {"action_type": "SetParam", "param_name": "Mark", "param_value": "D-1"},
                                   {"action_type": "Tag", "tag_family_name": "M_Door Tag"}]}
    rich = {"name": "x", "elements": [{"role": "wall", "family": "Basic Wall"},
                                      {"role": "door", "family": "M_Door", "host": "wall"}],
            "steps": [{"action_type": "Place", "family_name": "M_Door", "element_role": "door",
                       "host_role": "wall",
                       "repeat": {"over": "wall", "spacing_mm": 2000, "mark_expr": "D-{i:02}"},
                       "condition": "width>1500"}]}
    g_flat, g_rich = build_goal(flat), build_goal(rich)
    a4 = {"flat_expresses_loops": "For EACH" in g_flat,
          "richer_expresses_loops": "For EACH" in g_rich,
          "richer_expresses_conditionals": "ONLY IF" in g_rich,
          "richer_expresses_compounds": "SEVERAL related elements" in g_rich}

    return {"A1_process_acceleration": a1, "A2_compound_recovery": a2,
            "A3_proactive_prediction": a3, "A4_richer_goal_coverage": a4}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Consolidated thesis ablations (deterministic, $0).")
    ap.add_argument("--csv", action="store_true", help="also write results/ablations.csv")
    args = ap.parse_args()

    res = run_ablations()
    a1, a2, a3, a4 = (res["A1_process_acceleration"], res["A2_compound_recovery"],
                      res["A3_proactive_prediction"], res["A4_richer_goal_coverage"])
    print("\n================  THESIS ABLATIONS (deterministic, $0)  ================\n")
    print(f"A1  Process acceleration : {a1['manual_actions']:.0f} manual -> {a1['assisted_actions']:.0f} "
          f"assisted actions = {a1['reduction_pct']}% reduction  ({a1['routines']} routines)")
    print(f"A2  Compound recovery    : v0.2 captured {a2['v2_compounds']} compound routine(s); "
          f"v0.3 captured {a2['v3_compounds']}  (wall->door->tag)")
    print(f"A3  Proactive prediction : precision@1 = {a3['precision_at_1']}  over {a3['prefixes']} prefixes")
    print(f"A4  Richer-goal coverage : flat expresses loops={a4['flat_expresses_loops']}; "
          f"richer loops={a4['richer_expresses_loops']}, conditionals={a4['richer_expresses_conditionals']}, "
          f"compounds={a4['richer_expresses_compounds']}")
    print()

    if args.csv:
        RESULTS_DIR.mkdir(exist_ok=True)
        rows = [{"ablation": k, "metric": mk, "value": mv}
                for k, sub in res.items() for mk, mv in sub.items()]
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ablation", "metric", "value"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
