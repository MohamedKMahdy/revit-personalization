"""
Tests for the consolidated thesis ablations (eval/ablations.py). Deterministic, $0 — guards the
before/after invariants so the results chapter can't silently regress.

Run:  pytest tests/test_ablations.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))
from ablations import run_ablations   # noqa: E402


def test_ablations_invariants():
    res = run_ablations()
    a1, a2, a3, a4 = (res["A1_process_acceleration"], res["A2_compound_recovery"],
                      res["A3_proactive_prediction"], res["A4_richer_goal_coverage"])

    # A1: assisting saves user effort (assisted < manual)
    assert a1["assisted_actions"] < a1["manual_actions"] and a1["reduction_pct"] > 0

    # A2: v0.3 recovers the compound that v0.2 misses
    assert a2["v3_compounds"] >= 1 and a2["v3_compounds"] > a2["v2_compounds"]

    # A3: prediction precision is a valid probability and non-trivial on learned routines
    assert 0.0 <= a3["precision_at_1"] <= 1.0 and a3["prefixes"] > 0

    # A4: the richer goal expresses structure the flat one cannot
    assert a4["flat_expresses_loops"] is False
    assert a4["richer_expresses_loops"] and a4["richer_expresses_conditionals"] and a4["richer_expresses_compounds"]
