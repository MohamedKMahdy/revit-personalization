"""
Tests for the proactive next-action predictor (predictor.py). Deterministic, no LLM.

Run:  pytest tests/test_predictor.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.schemas import ActionRecord, RoutineExample, CandidateRoutine  # noqa: E402
from predictor import NextActionPredictor, current_prefix, predict_live    # noqa: E402


def _door_actions(mark="D-1"):
    return [
        ActionRecord(action_type="Place", element_id=1, element_category="Doors",
                     family_name="M_Door", timestamp_unix=0),
        ActionRecord(action_type="SetParam", element_id=1, param_name="Mark",
                     param_value_after=mark, timestamp_unix=1),
        ActionRecord(action_type="Tag", element_id=1, tagged_element_id=1,
                     tag_family_name="M_Door Tag", timestamp_unix=2),
    ]


def _door_routine(support=5):
    return CandidateRoutine(
        id="routine_door", label="Place(M_Door) → SetParam×1 → Tag(M_Door Tag)",
        action_signature="P,S,T", count=support, support=support, confidence=0.95,
        examples=[RoutineExample(example_id=f"e{i}", session_id="s", recorded_at=float(i),
                                 actions=_door_actions(f"D-{i}")) for i in range(support)])


def test_predict_exact_prefix_returns_next_step():
    pred = NextActionPredictor([_door_routine()])
    p = pred.predict(_door_actions()[:1])              # user just placed the door
    assert p is not None and p.match == "exact"
    assert p.next_actions[0] == {"action_type": "SetParam", "key": "Mark"}
    assert p.next_actions[1]["action_type"] == "Tag"
    assert "Mark" in p.headline


def test_predict_after_setparam_predicts_tag():
    pred = NextActionPredictor([_door_routine()])
    p = pred.predict(_door_actions()[:2])              # placed + set Mark
    assert p is not None and len(p.next_actions) == 1 and p.next_actions[0]["action_type"] == "Tag"


def test_predict_none_when_routine_complete():
    pred = NextActionPredictor([_door_routine()])
    assert pred.predict(_door_actions()) is None       # nothing left to predict


def test_predict_type_fallback_for_unseen_family():
    """A placed family we've never seen still gets a useful type-level prediction (lower confidence)."""
    pred = NextActionPredictor([_door_routine()])
    other = [ActionRecord(action_type="Place", element_id=9, element_category="Doors",
                          family_name="M_Totally_Different", timestamp_unix=0)]
    p = pred.predict(other)
    assert p is not None and p.match == "type" and p.confidence < 0.95
    assert p.next_actions[0]["action_type"] == "SetParam"


def test_predict_none_on_empty():
    assert NextActionPredictor([_door_routine()]).predict([]) is None
    assert NextActionPredictor([]).predict(_door_actions()[:1]) is None


def test_prediction_carries_intent_and_states_the_why():
    """Stage 2: when the routine's intent is known, the prediction surfaces the WHY (goal) + WHEN."""
    pred = NextActionPredictor([_door_routine()])
    intents = {"routine_door": {"goal": "keep the door schedule complete",
                                "trigger": "a door placed with no Mark"}}
    p = pred.predict(_door_actions()[:1], intents=intents)
    assert p.goal == "keep the door schedule complete"
    assert p.trigger == "a door placed with no Mark"
    assert "to keep the door schedule complete" in p.headline
    # without intent, the headline still works (no WHY clause)
    p2 = pred.predict(_door_actions()[:1])
    assert p2.goal == "" and "to keep the door schedule" not in p2.headline


def test_current_prefix_picks_in_progress_episode():
    # two elements placed; the SECOND is the in-progress one
    recs = _door_actions()[:2] + [
        ActionRecord(action_type="Place", element_id=2, element_category="Doors",
                     family_name="M_Door", timestamp_unix=10),
        ActionRecord(action_type="SetParam", element_id=2, param_name="Mark",
                     param_value_after="D-2", timestamp_unix=11),
    ]
    prefix = current_prefix(recs)
    assert [a.element_id for a in prefix] == [2, 2]    # only the current element's actions
    p = predict_live(recs, [_door_routine()])
    assert p is not None and p.next_actions[0]["action_type"] == "Tag"   # predicts the door's tag
