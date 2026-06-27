"""
Tests for the Pattern Agent's deterministic downgrade guard (orchestrator/pattern_agent.py
::_validate_and_downgrade). This is the safety net that keeps a richer-motif claim only when the
recorded examples actually support it — preventing over-generalization (a loop/compound/condition
hallucinated from flat single-element examples). No LLM / network needed.

Run:  pytest tests/test_pattern_agent_guard.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator.pattern_agent import _validate_and_downgrade, _normalize_intent  # noqa: E402


def test_normalize_intent_keeps_wellformed_hypothesis():
    m = {"intent": {"goal": " schedule-ready door ", "trigger": "door with no Mark"}}
    _normalize_intent(m)
    assert m["intent"] == {"goal": "schedule-ready door", "trigger": "door with no Mark", "downstream": ""}


def test_normalize_intent_drops_empty_or_malformed():
    for bad in ({"intent": {"goal": "", "trigger": ""}}, {"intent": "a string"}, {"intent": None}):
        _normalize_intent(bad)
        assert "intent" not in bad


def _ex(actions):
    return {"actions": actions}


def _place(fam):
    return {"action_type": "Place", "family_name": fam}


def _setp(name, val):
    return {"action_type": "SetParam", "param_name": name, "param_value_after": val}


def _base_motif(**over):
    m = {"name": "r", "description": "d", "steps": [], "preconditions": [], "parameters_to_prompt": []}
    m.update(over)
    return m


def test_loop_downgraded_when_examples_show_single_placement():
    motif = _base_motif(workflow_type="loop", steps=[
        {"action_type": "Place", "family_name": "M_Door",
         "repeat": {"over": "wall", "spacing_mm": 2000}}])
    examples = [_ex([_place("M_Door"), _setp("Mark", "D-1")]),
                _ex([_place("M_Door"), _setp("Mark", "D-2")])]
    out = _validate_and_downgrade(motif, examples)
    assert out["workflow_type"] == "linear"
    assert "repeat" not in out["steps"][0]
    assert out["_downgrade_notes"]


def test_loop_kept_when_examples_repeat_the_family():
    motif = _base_motif(workflow_type="loop", steps=[
        {"action_type": "Place", "family_name": "M_Door",
         "repeat": {"over": "wall", "spacing_mm": 2000}}])
    examples = [_ex([_place("M_Door"), _place("M_Door"), _place("M_Door")])]   # placed 3x in one rep
    out = _validate_and_downgrade(motif, examples)
    assert out["workflow_type"] == "loop"
    assert out["steps"][0]["repeat"]["spacing_mm"] == 2000
    assert "_downgrade_notes" not in out


def test_compound_downgraded_when_single_element():
    motif = _base_motif(workflow_type="compound",
                        elements=[{"role": "wall", "family": "Basic Wall"},
                                  {"role": "door", "family": "M_Door", "host": "wall"}],
                        steps=[{"action_type": "Place", "family_name": "M_Door", "element_role": "door",
                                "host_role": "wall"}])
    examples = [_ex([_place("M_Door")]), _ex([_place("M_Door")])]
    out = _validate_and_downgrade(motif, examples)
    assert out["workflow_type"] == "linear" and out["elements"] == []
    assert "element_role" not in out["steps"][0] and "host_role" not in out["steps"][0]


def test_compound_kept_when_two_distinct_elements():
    motif = _base_motif(workflow_type="compound",
                        elements=[{"role": "wall", "family": "Basic Wall"},
                                  {"role": "door", "family": "M_Door", "host": "wall"}],
                        steps=[{"action_type": "Place", "family_name": "Basic Wall", "element_role": "wall"},
                               {"action_type": "Place", "family_name": "M_Door", "element_role": "door",
                                "host_role": "wall"}])
    examples = [_ex([_place("Basic Wall"), _place("M_Door")])]
    out = _validate_and_downgrade(motif, examples)
    assert out["workflow_type"] == "compound" and len(out["elements"]) == 2
    assert out["steps"][1]["element_role"] == "door" and out["steps"][1]["host_role"] == "wall"


def test_condition_stripped_on_constant_param():
    motif = _base_motif(steps=[
        {"action_type": "SetParam", "param_name": "Frame", "condition": "width>1500",
         "value_expr": "'Wide' if width>1500 else 'Standard'"}])
    examples = [_ex([_setp("Frame", "Standard")]), _ex([_setp("Frame", "Standard")])]  # constant
    out = _validate_and_downgrade(motif, examples)
    assert "condition" not in out["steps"][0] and "value_expr" not in out["steps"][0]


def test_condition_kept_on_varying_param():
    motif = _base_motif(steps=[
        {"action_type": "SetParam", "param_name": "Frame", "condition": "width>1500",
         "value_expr": "'Wide' if width>1500 else 'Standard'"}])
    examples = [_ex([_setp("Frame", "Wide")]), _ex([_setp("Frame", "Standard")])]      # varies
    out = _validate_and_downgrade(motif, examples)
    assert out["steps"][0]["condition"] == "width>1500"
    assert out["steps"][0]["value_expr"].startswith("'Wide'")


def test_flat_motif_untouched():
    motif = _base_motif(steps=[{"action_type": "Place", "family_name": "M_Door"},
                               {"action_type": "SetParam", "param_name": "Mark", "param_value_type": "variable"}])
    examples = [_ex([_place("M_Door"), _setp("Mark", "D-1")]),
                _ex([_place("M_Door"), _setp("Mark", "D-2")])]
    out = _validate_and_downgrade(motif, examples)
    assert "_downgrade_notes" not in out and out["workflow_type"] == "linear"
