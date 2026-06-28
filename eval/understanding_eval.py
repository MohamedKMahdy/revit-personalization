"""
Held-out generalization harness — does the system UNDERSTAND the user's conventions, or only DETECT
and replay what it literally saw?

Understanding (vs detection) is the ability to act correctly for a situation ABSENT from the
demonstrations. We operationalize it with the questions a pure pattern-replayer cannot answer:
  GENERALIZE     -- the right value for a held-out instance the demos never showed
  COUNTERFACTUAL -- when the convention does NOT apply (or isn't determined by the evidence) -> refuse
                    rather than fabricate a value

Two strategies are graded on identical held-out probes, all $0 + deterministic (no LLM, no Revit):
  DETECTION (baseline) -- literal replay: re-emit the last demonstrated value (what a compiled skill
                          bound to last_values does).
  UNDERSTANDING (rule) -- induce the generating rule and apply it to the held-out case:
                          * flat numeric sequence  (executor_agent.induce_sequence_rule)
                          * conditional / per-context sequence  (rule_induction.induce_rule)

Probes are pre-labelled so the regression test catches BOTH regressions (a supported probe breaking)
and dishonest over-generalization (an honesty probe fabricating a value it can't justify):
  supported  -> UNDERSTANDING predicts the oracle, DETECTION does not
  honesty    -> UNDERSTANDING abstains (returns None) because the data doesn't determine the answer
                (an undemonstrated conditional branch, an unseen context group, free-text)

Run:  python eval/understanding_eval.py
"""
from __future__ import annotations

import os
import pathlib
import sys
from dataclasses import dataclass, field

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator.executor_agent import (  # noqa: E402
    induce_sequence_rule, next_from_rule, next_in_sequence,
)
from orchestrator.rule_induction import induce_rule, apply_rule  # noqa: E402


@dataclass
class Probe:
    """One held-out test of a hidden user convention. The system must predict the value for an instance
    NOT among the demonstrations. Sequence probes use `observed` (+`held_out_oracle`); context probes use
    `examples` ([{value, context}]) + `query_context`."""
    name: str
    convention: str
    kind: str                                       # "supported" | "honesty"
    observed: list | None = None
    held_out_oracle: str | None = None
    examples: list | None = None
    query_context: dict = field(default_factory=dict)
    note: str = ""


# A synthetic user ("an architect") with hidden conventions. Demos exercise each convention but never
# cover the held-out case the probe asks about.
def _seq(level, n0, n1):
    return [{"value": f"D-{level}{i:02d}", "context": {"level": f"L{level}"}} for i in range(n0, n1 + 1)]


PROBES = [
    # --- sequence understanding (Stage 0) ---
    Probe("door_mark_next", "Doors numbered D-NN sequentially (step 1)", "supported",
          observed=["D-01", "D-02", "D-03"], held_out_oracle="D-04"),
    Probe("window_mark_step5", "Windows numbered in steps of 5: W-100, W-105, ...", "supported",
          observed=["W-100", "W-105", "W-110"], held_out_oracle="W-115",
          note="literal replay gives W-110; a naive +1 gives W-111; the STEP must be learned"),
    Probe("freetext_comment", "Free-text Comments vary per room (not predictable)", "honesty",
          observed=["lobby entry", "corridor", "stair core"], held_out_oracle=None,
          note="no inducible rule -> must REFUSE, not fabricate"),

    # --- conditional & context-keyed understanding (Stage 1) ---
    Probe("frame_by_width", "Conditional: wider doors get Frame=Wide, narrow get Standard", "supported",
          examples=[{"value": "Wide", "context": {"width": 1600}},
                    {"value": "Wide", "context": {"width": 2000}},
                    {"value": "Standard", "context": {"width": 900}},
                    {"value": "Standard", "context": {"width": 1000}}],
          query_context={"width": 2500}, held_out_oracle="Wide",
          note="last demo was Standard; held-out width 2500 -> the learned threshold yields Wide"),
    Probe("mark_per_level", "Mark numbered per level: L1 -> D-1NN, L2 -> D-2NN", "supported",
          examples=_seq(1, 1, 3) + _seq(2, 1, 3),
          query_context={"level": "L2"}, held_out_oracle="D-204",
          note="next Mark on a KNOWN level continues that level's own sequence, not the global last"),

    # --- honesty: the evidence doesn't determine the answer ---
    Probe("frame_one_branch_only", "Only narrow doors demonstrated -> Wide branch is unproven", "honesty",
          examples=[{"value": "Standard", "context": {"width": 900}},
                    {"value": "Standard", "context": {"width": 1000}},
                    {"value": "Standard", "context": {"width": 1100}}],
          query_context={"width": 1600}, held_out_oracle=None,
          note="a branch never demonstrated must not be invented"),
    Probe("mark_unseen_level", "First Mark on a never-seen level L3 is under-determined", "honesty",
          examples=_seq(1, 1, 3) + _seq(2, 1, 3),
          query_context={"level": "L3"}, held_out_oracle=None,
          note="an unseen context group can't be predicted from the data -> abstain (identifiability)"),
]


def detection_predict(probe: Probe):
    """DETECTION baseline: literal replay -- re-emit the last demonstrated value."""
    if probe.examples is not None:
        return probe.examples[-1]["value"] if probe.examples else None
    return probe.observed[-1] if probe.observed else None


def understanding_predict(probe: Probe):
    """UNDERSTANDING: induce a rule and apply it to the held-out case; abstain (None) when the data
    doesn't determine a value."""
    if probe.examples is not None:
        rule = induce_rule(probe.examples)
        return apply_rule(rule, probe.query_context) if rule else None
    rule = induce_sequence_rule(probe.observed or [])
    if rule:
        return next_from_rule(rule)
    return next_in_sequence(probe.observed[-1]) if probe.observed else None


def grade(probe: Probe) -> dict:
    det = detection_predict(probe)
    und = understanding_predict(probe)
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
    sup = [r for r in rows if r["kind"] == "supported"]
    hon = [r for r in rows if r["kind"] == "honesty"]
    return {
        "understanding_supported": (sum(r["und_ok"] for r in sup), len(sup)),
        "detection_supported": (sum(r["det_ok"] for r in sup), len(sup)),
        "understanding_honesty": (sum(r["und_ok"] for r in hon), len(hon)),
        "detection_honesty": (sum(r["det_ok"] for r in hon), len(hon)),
        "failures": [r["name"] for r in rows if not r["und_ok"]],
    }


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def main() -> None:
    try:                                   # the table uses '↳' which the Windows cp1252 console rejects
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rows = run()
    print("=" * 96)
    print("UNDERSTANDING vs DETECTION  —  held-out generalization (deterministic, $0)")
    print("=" * 96)
    for r in rows:
        tag = "OK " if r["und_ok"] else "!! "
        print(f"[{tag}] {r['name']:22} {r['kind']:9} oracle={_fmt(r['oracle']):10} "
              f"detection={_fmt(r['detection']):12} understanding={_fmt(r['understanding']):12}")
        print(f"       {r['convention']}")
        if r["note"]:
            print(f"       ↳ {r['note']}")
    s = summary(rows)
    print("-" * 96)
    print(f"supported (must generalize): UNDERSTANDING {s['understanding_supported'][0]}/{s['understanding_supported'][1]}"
          f"   vs   DETECTION {s['detection_supported'][0]}/{s['detection_supported'][1]}")
    print(f"honesty   (must abstain):    UNDERSTANDING {s['understanding_honesty'][0]}/{s['understanding_honesty'][1]}"
          f"   vs   DETECTION {s['detection_honesty'][0]}/{s['detection_honesty'][1]}")
    if s["failures"]:
        print(f"FAILURES: {', '.join(s['failures'])}")
    print("=" * 96)


if __name__ == "__main__":
    main()
