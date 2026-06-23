"""
Tests for project memory (orchestrator/project_memory.py) — the persistent
project-understanding the executor reads and writes back to.

Run:  pytest tests/test_project_memory.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import project_memory as pm  # noqa: E402


@pytest.fixture
def mem_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "project_memory.json")
    return pm.MEM_PATH


def test_round_trip(mem_file):
    m = pm.load()
    assert m["routines"] == {}
    pm.learn_substitution(m, "r1", "M_Single-Flush", "M_Door-Passage-Single-Flush", "Door")
    pm.save(m)
    m2 = pm.load()
    assert m2["routines"]["r1"]["family_substitutions"]["M_Single-Flush"] == "M_Door-Passage-Single-Flush"


def test_learn_from_run_detects_substitution_and_values(mem_file):
    m = pm.load()
    calls = [
        {"name": "place_element", "args": {"family_name": "M_Single-Flush"},
         "result": {"success": False, "message": "family not loaded"}},
        {"name": "get_available_family_types", "args": {"category": "OST_Doors"},
         "result": {"success": True}},
        {"name": "place_element", "args": {"family_name": "M_Door-Passage-Single-Flush"},
         "result": {"success": True, "element_id": 7}},
        {"name": "set_parameter", "args": {"name": "Mark", "value": "D-101"},
         "result": {"success": True}},
        {"name": "tag_element", "args": {"element_id": 7}, "result": {"success": True}},
    ]
    pm.learn_from_run(m, "r1", "Door", calls, done=True)
    r = m["routines"]["r1"]
    assert r["family_substitutions"] == {"M_Single-Flush": "M_Door-Passage-Single-Flush"}
    assert r["executions"] == 1
    assert r["last_values"]["Mark"] == "D-101"


def test_to_prompt_renders_and_is_empty_when_unknown(mem_file):
    m = pm.load()
    pm.learn_substitution(m, "r1", "M_Single-Flush", "M_Door-Passage-Single-Flush", "Door")
    pm.add_preference(m, "always let me pick the location")
    block = pm.to_prompt(m, "r1")
    assert "M_Single-Flush" in block and "M_Door-Passage-Single-Flush" in block
    assert "always let me pick the location" in block
    # nothing known about another routine and no prefs in a fresh memory → empty block
    assert pm.to_prompt(pm.load(), "unknown") == ""


def test_no_substitution_when_family_unchanged(mem_file):
    m = pm.load()
    calls = [{"name": "place_element", "args": {"family_name": "M_Door"},
              "result": {"success": True, "element_id": 1}}]
    pm.learn_from_run(m, "r1", "Door", calls, done=True)
    assert m["routines"]["r1"]["family_substitutions"] == {}   # wanted == used → nothing learned
    assert m["routines"]["r1"]["executions"] == 1


def test_record_host_wall(mem_file):
    m = pm.load()
    calls = [{"name": "place_element", "args": {"family_name": "M_Door", "host_wall_id": 1663968},
              "result": {"success": True, "element_id": 2}}]
    pm.learn_from_run(m, "r1", "Door", calls, done=True)
    assert m["routines"]["r1"]["last_host_wall_id"] == 1663968
