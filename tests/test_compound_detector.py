"""
Tests for the v0.3 compound (multi-element) detector (detector/v3_compound.py).

Proves v0.3 groups a host-linked "place wall -> place door on it -> set Mark -> tag" compound into
ONE routine — the case v0.2 splits (it drops the bare wall Place and mines the door alone). No LLM.

Run:  pytest tests/test_compound_detector.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.schemas import ActionRecord            # noqa: E402
from detector import make_detector                 # noqa: E402


def _compound_session(reps: int = 3) -> list[ActionRecord]:
    """`reps` repetitions of: Place(Basic Wall) -> Place(M_Door hosted on the wall) ->
    SetParam(Mark) on the door -> Tag the door. One session (reps 10s apart, < idle gap)."""
    recs: list[ActionRecord] = []
    for r in range(reps):
        t = r * 10.0
        wall_id, door_id = 100 + r, 200 + r
        recs.append(ActionRecord(action_type="Place", element_id=wall_id, element_category="Walls",
                                 family_name="Basic Wall", timestamp_unix=t))
        recs.append(ActionRecord(action_type="Place", element_id=door_id, element_category="Doors",
                                 family_name="M_Door-Passage", host_category="Walls", timestamp_unix=t + 1))
        recs.append(ActionRecord(action_type="SetParam", element_id=door_id, param_name="Mark",
                                 param_value_after=f"D-{r+1:02d}", timestamp_unix=t + 2))
        recs.append(ActionRecord(action_type="Tag", element_id=door_id, tagged_element_id=door_id,
                                 tag_family_name="M_Door Tag", timestamp_unix=t + 3))
    return recs


def _categories(cand) -> set[str]:
    return {a.element_category for ex in cand.examples for a in ex.actions if a.action_type == "Place"}


def test_v3_groups_wall_door_tag_compound():
    recs = _compound_session(reps=3)
    cands = make_detector("v3").detect(recs, session_id="s")
    assert cands, "v0.3 should surface the compound routine"
    top = max(cands, key=lambda c: c.support)
    assert top.support == 3                                   # 3 repetitions clustered
    cats = _categories(top)
    assert "Walls" in cats and "Doors" in cats               # BOTH elements captured in one routine


def test_v2_splits_and_drops_the_wall():
    """Contrast: v0.2 mines the door alone and never captures the wall (the gap v0.3 fixes)."""
    recs = _compound_session(reps=3)
    cands = make_detector("v2").detect(recs, session_id="s")
    # v0.2 may surface the door routine, but no candidate contains the wall Place
    assert all("Walls" not in _categories(c) for c in cands)


def test_v3_aliases_resolve():
    for name in ("v3", "compound", "v0.3", "multi-element"):
        assert make_detector(name).name == "v3-compound"


def test_v3_flat_routine_still_detected():
    """A flat single-element routine (no host link) detects the same under v0.3 as v0.2."""
    recs: list[ActionRecord] = []
    for r in range(3):
        t = r * 10.0
        did = 300 + r
        recs.append(ActionRecord(action_type="Place", element_id=did, element_category="Doors",
                                 family_name="M_Door", timestamp_unix=t))
        recs.append(ActionRecord(action_type="SetParam", element_id=did, param_name="Mark",
                                 param_value_after=f"D-{r}", timestamp_unix=t + 1))
        recs.append(ActionRecord(action_type="Tag", element_id=did, tagged_element_id=did,
                                 tag_family_name="M_Door Tag", timestamp_unix=t + 2))
    cands = make_detector("v3").detect(recs, session_id="s")
    assert cands and max(cands, key=lambda c: c.support).support == 3
