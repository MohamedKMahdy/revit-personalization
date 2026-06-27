"""
Held-out generalization harness — does the system UNDERSTAND the user's conventions, or only DETECT
and replay what it literally saw?

Understanding (vs detection) is the ability to act correctly for a situation ABSENT from the
demonstrations. We operationalize it with the questions a pure pattern-replayer cannot answer:
  GENERALIZE     -- the right value for a held-out instance the demos never showed
  COUNTERFACTUAL -- when the convention does NOT apply -> refuse rather than fabricate a wrong value

Two strategies are graded on identical held-out probes, all $0 + deterministic (no LLM, no Revit):
  DETECTION (baseline) -- literal replay: re-emit the last demonstrated value (what a compiled skill
                          bound to last_values does).
  UNDERSTANDING (rule) -- induce the generating rule from the demos and apply it to the held-out case
                          (orchestrator.executor_agent.induce_sequence_rule / next_from_rule).

Each probe is pre-labelled by what the CURRENT deterministic layer SHOULD do, so the regression test
catches BOTH regressions (a supported probe breaking) and silent over-generalization (a gap probe
spuriously "passing"):
  supported  -> UNDERSTANDING correct, DETECTION wrong
  gap        -> UNDERSTANDING also wrong  (documents what Stage 1b LLM rule-induction must add)
  honesty    -> UNDERSTANDING refuses (returns None) rather than emit a confident wrong value

This file is the Stage 0 measurement: it establishes the baseline GAP (detection collapses on
held-out instances) so every later "understanding" change is a measured improvement, not a claim.

Run:  python eval/understanding_eval.py
"""
from __future__ import annotations

import os
import pathlib
import sys
from dataclasses import dataclass

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.executor_agent import (  # noqa: E402
    induce_sequence_rule, next_from_rule, next_in_sequence,
)


@dataclass
class Probe:
    """One held-out test of a hidden user convention. `observed` are the demonstrated values; the
    system must predict the value for an instance NOT among them (`held_out_oracle`)."""
    name: str
    convention: str               # human description of the hidden rule
    observed: list                # the demonstrated values the user produced
    held_out_oracle: str | None   # correct value for the held-out case (None for honesty probes)
    kind: str                     # "supported" | "gap" | "honesty"
    note: str = ""


# A synthetic user ("an architect") with hidden conventions. The demos exercise each convention but
# never cover the held-out case the probe asks about.
PROBES = [
    Probe("door_mark_next", "Doors numbered D-NN sequentially (step 1)",
          ["D-01", "D-02", "D-03"], "D-04", "supported"),
    Probe("window_mark_step5", "Windows numbered in steps of 5: W-100, W-105, ...",
          ["W-100", "W-105", "W-110"], "W-115", "supported",
          "literal replay gives W-110; a naive +1 gives W-111; the STEP must be learned"),
    Probe("mark_per_level_restart", "Mark restarts per level: D-{level}{nn} (first on L2 -> D-201)",
          ["D-101", "D-102", "D-103"], "D-201", "gap",
          "a flat sequence rule can't see the level context -> needs Stage 1b rule-induction"),
    Probe("frame_by_width", "Conditional: width>1500 -> Frame=Wide, else Standard",
          ["Standard", "Standard", "Standard"], "Wide", "gap",
          "value depends on a CONDITION, not a sequence -> needs Stage 1b rule-induction"),
    Probe("freetext_comment", "Free-text Comments vary per room (not predictable)",
          ["lobby entry", "corridor", "stair core"], None, "honesty",
          "no inducible rule -> the system must REFUSE, not fabricate a value"),
]


def detection_predict(observed: list):
    """DETECTION baseline: literal replay -- re-emit the last demonstrated value (no generalization)."""
    return observed[-1] if observed else None


def understanding_predict(observed: list):
    """UNDERSTANDING: induce the sequence rule and apply it; fall back to +1; refuse (None) when
    there's no inducible structure and nothing advanceable (honest abstention)."""
    rule = induce_sequence_rule(observed)
    if rule:
        return next_from_rule(rule)
    return next_in_sequence(observed[-1]) if observed else None


def grade(probe: Probe) -> dict:
    det = detection_predict(probe.observed)
    und = understanding_predict(probe.observed)
    if probe.kind == "honesty":
        und_ok, det_ok = und is None, det is None       # correct = abstain
    else:
        und_ok, det_ok = und == probe.held_out_oracle, det == probe.held_out_oracle
    return {"name": probe.name, "kind": probe.kind, "convention": probe.convention,
            "oracle": probe.held_out_oracle, "detection": det, "understanding": und,
            "det_ok": det_ok, "und_ok": und_ok, "note": probe.note}


def run(probes: list | None = None) -> list:
    return [grade(p) for p in (probes or PROBES)]


def summary(rows: list) -> dict:
    transfer = [r for r in rows if r["kind"] in ("supported", "gap")]
    supported = [r for r in rows if r["kind"] == "supported"]
    honesty = [r for r in rows if r["kind"] == "honesty"]
    return {
        "understanding_transfer": (sum(r["und_ok"] for r in transfer), len(transfer)),
        "detection_transfer": (sum(r["det_ok"] for r in transfer), len(transfer)),
        "supported_understood": (sum(r["und_ok"] for r in supported), len(supported)),
        "honesty_refused": (sum(r["und_ok"] for r in honesty), len(honesty)),
        "open_gaps": [r["name"] for r in rows if r["kind"] == "gap" and not r["und_ok"]],
    }


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def main() -> None:
    rows = run()
    print("=" * 92)
    print("UNDERSTANDING vs DETECTION  —  held-out generalization (deterministic, $0)")
    print("=" * 92)
    for r in rows:
        tag = "OK " if r["und_ok"] else ("GAP" if r["kind"] == "gap" else "!! ")
        print(f"[{tag}] {r['name']:22} {r['kind']:9} oracle={_fmt(r['oracle']):9} "
              f"detection={_fmt(r['detection']):11} understanding={_fmt(r['understanding']):11}")
        print(f"       {r['convention']}")
        if r["note"]:
            print(f"       ↳ {r['note']}")
    s = summary(rows)
    ut, utn = s["understanding_transfer"]
    dt, dtn = s["detection_transfer"]
    print("-" * 92)
    print(f"held-out transfer:  UNDERSTANDING {ut}/{utn}   vs   DETECTION {dt}/{dtn}")
    print(f"supported conventions understood: {s['supported_understood'][0]}/{s['supported_understood'][1]}")
    print(f"honesty (refused to fabricate):   {s['honesty_refused'][0]}/{s['honesty_refused'][1]}")
    if s["open_gaps"]:
        print(f"open gaps (Stage 1b LLM rule-induction targets): {', '.join(s['open_gaps'])}")
    print("=" * 92)


if __name__ == "__main__":
    main()
