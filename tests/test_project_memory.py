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
    # Isolate per-user roots AND neutralize the legacy-import path so tests never read real memory.
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "users" / "test" / "memory.json")
    monkeypatch.setattr(pm, "LEGACY_PATH", tmp_path / "nonexistent.json")
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


def test_memory_is_scoped_per_user(mem_file):
    """Two users on the same machine get independent, persistent memory."""
    alice = pm.load("alice")
    pm.set_user(alice, name="Alice", role="architect")
    pm.add_preference(alice, "always let me pick the location")
    pm.learn_substitution(alice, "r1", "M_Single-Flush", "M_Door-Passage-Single-Flush", "Door")
    pm.save(alice, "alice")

    bob = pm.load("bob")
    pm.add_preference(bob, "never auto-tag")
    pm.save(bob, "bob")

    a2 = pm.load("alice")
    b2 = pm.load("bob")
    assert a2["user"]["name_hint"] == "Alice" and a2["user"]["role_hint"] == "architect"
    assert "always let me pick the location" in a2["user"]["preferences"]
    assert a2["routines"]["r1"]["family_substitutions"]                       # alice's routine
    assert b2["user"]["preferences"] == ["never auto-tag"]                    # bob's are separate
    assert b2["routines"] == {} and "always let me pick the location" not in b2["user"]["preferences"]
    assert (pm.MEM_ROOT / "alice" / "memory.json").exists()
    assert (pm.MEM_ROOT / "bob" / "memory.json").exists()


def test_user_block_renders_profile_and_is_empty_when_unknown(mem_file):
    m = pm.load("u")
    assert pm.user_block(m) == ""                                            # nothing known yet
    pm.set_user(m, name="Mohamed", role="MSc student")
    pm.add_preference(m, "keep answers committee-defensible")
    pm.add_convention(m, "door Mark scheme", "D-1xx")
    block = pm.user_block(m)
    assert "Mohamed" in block and "MSc student" in block
    assert "committee-defensible" in block and "D-1xx" in block
    # the executor's full block embeds the user profile too
    pm.learn_substitution(m, "r1", "A", "B", "Door")
    full = pm.to_prompt(m, "r1")
    assert "Mohamed" in full and "'A' -> 'B'" in full


def test_legacy_global_store_migrates_once(mem_file, tmp_path, monkeypatch):
    """An existing pre-per-user global store is imported on first load."""
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"preferences":["pick location"],"routines":{"r9":{"label":"L","executions":2,'
                      '"family_substitutions":{},"last_host_wall_id":null,"last_values":{}}}}',
                      encoding="utf-8")
    monkeypatch.setattr(pm, "LEGACY_PATH", legacy)
    m = pm.load("fresh")                                       # no per-user file yet → import legacy
    assert "pick location" in m["user"]["preferences"]          # old top-level prefs migrated to user
    assert m["routines"]["r9"]["executions"] == 2


def test_learn_caches_families_discovered_from_model(mem_file):
    """When the executor QUERIES the model for loaded types, memory caches them and renders
    them back next run so the agent doesn't re-discover what's loaded."""
    m = pm.load()
    calls = [
        {"name": "get_available_family_types", "args": {"category": "OST_Doors"},
         "result": {"success": True, "types": [
             {"family": "M_Door-Passage-Single-Flush", "type": "0915x2134mm", "id": 1},
             {"family": "M_Door-Passage-Single-Flush", "type": "0762x2032mm", "id": 2},
             {"family": "Curtain Wall Dbl Glass", "type": "default", "id": 3}]}},
        {"name": "place_element", "args": {"family_name": "M_Door-Passage-Single-Flush"},
         "result": {"success": True, "element_id": 5}},
    ]
    pm.learn_from_run(m, "r1", "Door", calls, done=True)
    fams = m["project"]["loaded_families"]["OST_Doors"]
    assert fams == ["Curtain Wall Dbl Glass", "M_Door-Passage-Single-Flush"]   # deduped + sorted

    block = pm.to_prompt(m, "r1")
    assert "LOADED" in block and "M_Door-Passage-Single-Flush" in block and "Doors:" in block


def test_record_host_wall(mem_file):
    m = pm.load()
    calls = [{"name": "place_element", "args": {"family_name": "M_Door", "host_wall_id": 1663968},
              "result": {"success": True, "element_id": 2}}]
    pm.learn_from_run(m, "r1", "Door", calls, done=True)
    assert m["routines"]["r1"]["last_host_wall_id"] == 1663968
