"""
Unit tests for orchestrator/rule_induction.py — the Stage 1 deterministic rule-induction core
(conditionals + per-context sequences), with the honest evidence/identifiability guards.

Run:  pytest tests/test_rule_induction.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import rule_induction as ri  # noqa: E402


def _ctx(value, **context):
    return {"value": value, "context": context}


def test_threshold_conditional_generalizes_both_branches():
    ex = [_ctx("Standard", width=900), _ctx("Standard", width=1000),
          _ctx("Wide", width=1600), _ctx("Wide", width=2000)]
    rule = ri.induce_rule(ex)
    assert rule and rule["kind"] == "conditional" and rule["mode"] == "threshold"
    assert ri.apply_rule(rule, {"width": 2500}) == "Wide"      # held-out high
    assert ri.apply_rule(rule, {"width": 800}) == "Standard"   # held-out low


def test_categorical_conditional():
    ex = [_ctx("60", rating="fire"), _ctx("60", rating="fire"), _ctx("0", rating="normal")]
    rule = ri.induce_rule(ex)
    assert rule and rule["mode"] == "category"
    assert ri.apply_rule(rule, {"rating": "fire"}) == "60"
    assert ri.apply_rule(rule, {"rating": "smoke"}) is None    # category never seen -> abstain


def test_per_context_sequence_continues_known_group():
    ex = [_ctx(f"D-1{i:02d}", level="L1") for i in range(1, 4)] + \
         [_ctx(f"D-2{i:02d}", level="L2") for i in range(1, 4)]
    rule = ri.induce_rule(ex)
    assert rule and rule["kind"] == "per_context_seq"
    assert ri.apply_rule(rule, {"level": "L2"}) == "D-204"     # continues L2's own sequence
    assert ri.apply_rule(rule, {"level": "L1"}) == "D-104"
    assert ri.apply_rule(rule, {"level": "L3"}) is None        # unseen group -> abstain


def test_one_branch_conditional_is_not_invented():
    ex = [_ctx("Standard", width=900), _ctx("Standard", width=1000), _ctx("Standard", width=1100)]
    assert ri.induce_rule(ex) is None                          # only one value -> no conditional


def test_single_context_group_not_identifiable_as_keyed():
    ex = [_ctx(f"D-1{i:02d}", level="L1") for i in range(1, 4)]
    rule = ri.induce_rule(ex)
    # one group can't establish per-level keying; no conditional either -> None
    assert rule is None


def test_no_context_returns_none():
    assert ri.induce_rule([{"value": "x"}, {"value": "y"}]) is None
    assert ri.induce_rule([]) is None
