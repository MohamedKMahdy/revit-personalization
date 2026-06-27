"""
Tests for compiled-skill deterministic replay (orchestrator/compiled_skill.py). No LLM, no Revit —
a fake dispatch stands in for the live model.

Run:  pytest tests/test_compiled_skill.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import compiled_skill as cs   # noqa: E402


def _door_run():
    return [
        {"name": "get_active_view", "args": {}, "result": {"success": True}},          # read — not compiled
        {"name": "place_element",
         "args": {"family_name": "M_Door", "location": {"x": 1.0, "y": 2.0}, "host_wall_id": 777},
         "result": {"success": True, "element_id": 900}},
        {"name": "set_parameter", "args": {"element_id": 900, "name": "Mark", "value": "D-05"},
         "result": {"success": True}},
        {"name": "tag_element", "args": {"element_id": 900}, "result": {"success": True, "tag_id": 901}},
    ]


def test_synthesize_parameterizes_a_run():
    skill = cs.synthesize(_door_run(), variable_params={"Mark"})
    steps = skill["steps"]
    assert [s["tool"] for s in steps] == ["place_element", "set_parameter", "tag_element"]   # reads dropped
    assert steps[0]["args"]["family_name"] == "M_Door"            # literal
    assert steps[0]["args"]["location"] == "{location}"          # hole
    assert steps[0]["args"]["host_wall_id"] == "{host_wall}"     # hole
    assert steps[1]["args"]["element_id"] == "{e0}"             # references the placed element
    assert steps[1]["args"]["name"] == "Mark" and steps[1]["args"]["value"] == "{Mark}"   # variable -> hole
    assert steps[2]["args"]["element_id"] == "{e0}"


def test_required_bindings_and_can_replay():
    skill = cs.synthesize(_door_run(), variable_params={"Mark"})
    assert cs.required_bindings(skill) == {"location", "host_wall", "Mark"}    # not {e0}
    assert cs.can_replay(skill, {"location": {"x": 0, "y": 0}, "host_wall": 777, "Mark": "D-06"})
    assert not cs.can_replay(skill, {"location": {"x": 0, "y": 0}, "Mark": "D-06"})   # missing host_wall


def test_run_compiled_replays_deterministically():
    skill = cs.synthesize(_door_run(), variable_params={"Mark"})
    sent = []

    def fake(tool, args):
        sent.append((tool, args))
        if tool == "place_element":
            return {"success": True, "element_id": 1234}        # a NEW id this run
        return {"success": True}

    out = cs.run_compiled(skill, {"location": {"x": 5, "y": 6}, "host_wall": 888, "Mark": "D-06"}, fake)
    assert out["done"] is True and out["compiled"] is True and out["failed_step"] is None
    # holes were bound: new host/location, the freshly-placed id flows into set + tag, Mark from binding
    assert sent[0] == ("place_element", {"family_name": "M_Door", "location": {"x": 5, "y": 6}, "host_wall_id": 888})
    assert sent[1] == ("set_parameter", {"element_id": 1234, "name": "Mark", "value": "D-06"})
    assert sent[2] == ("tag_element", {"element_id": 1234})


def test_run_compiled_bails_on_failed_step():
    skill = cs.synthesize(_door_run(), variable_params={"Mark"})

    def fake(tool, args):
        if tool == "place_element":
            return {"success": True, "element_id": 1}
        return {"success": False, "message": "boom"}            # set_parameter fails

    out = cs.run_compiled(skill, {"location": {"x": 0, "y": 0}, "host_wall": 1, "Mark": "D-1"}, fake)
    assert out["done"] is False and out["failed_step"] == 1     # stopped at set_parameter
    assert len(out["tool_calls"]) == 2                          # place ran, set failed, tag never tried


def test_synthesize_none_without_placement():
    calls = [{"name": "set_parameter", "args": {"element_id": 5, "name": "Mark", "value": "x"},
              "result": {"success": True}}]
    assert cs.synthesize(calls, variable_params={"Mark"}) is None
