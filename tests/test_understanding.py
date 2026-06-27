"""
Tests for Stage 3 active confirmation of understanding:
- orchestrator/understanding.describe_understanding (induced rules + intent -> plain hypotheses)
- orchestrator/project_memory record/confirm/understanding_block + auto-demote
- orchestrator/understanding.log_understanding (the ledger)
- chat_server /api/understanding [GET] + /api/understanding/confirm [POST]

Deterministic, no LLM / no Revit.  Run:  pytest tests/test_understanding.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatbot.chat_server as cs                       # noqa: E402
from orchestrator import understanding as und          # noqa: E402
from orchestrator import project_memory as pm          # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402


def _seq_motif():
    return {"steps": [{"action_type": "SetParam", "param_name": "Mark",
                       "param_value": None, "param_value_type": "variable"}]}


def _seq_examples():
    return [{"actions": [{"param_name": "Mark", "param_value_after": f"D-10{i}"}]} for i in range(1, 4)]


def _cond_motif():
    return {"steps": [{"action_type": "SetParam", "param_name": "Frame",
                       "param_value": None, "param_value_type": "variable"}]}


def _cond_examples():
    return [{"actions": [{"param_name": "Frame", "param_value_after": v},
                         {"param_name": "Width", "param_value_after": w}]}
            for v, w in [("Wide", "1600"), ("Wide", "2000"), ("Standard", "900"), ("Standard", "1000")]]


# ── describe_understanding ──────────────────────────────────────────────────────────
def test_describe_sequence_rule():
    hyps = und.describe_understanding(_seq_motif(), _seq_examples())
    h = next(x for x in hyps if x["key"] == "rule:Mark")
    assert "number Mark sequentially" in h["statement"] and "D-104" in h["statement"]


def test_describe_conditional_rule():
    hyps = und.describe_understanding(_cond_motif(), _cond_examples())
    h = next(x for x in hyps if x["key"] == "rule:Frame")
    assert "set Frame by Width" in h["statement"] and "Wide" in h["statement"]


def test_describe_intent():
    m = _seq_motif(); m["intent"] = {"goal": "a tagged door", "trigger": "a door with no Mark"}
    hyps = und.describe_understanding(m, _seq_examples())
    keys = {h["key"] for h in hyps}
    assert {"rule:Mark", "intent:goal", "intent:trigger"} <= keys


def test_describe_empty_when_nothing_inducible():
    assert und.describe_understanding({"steps": [{"action_type": "Place", "family_name": "M_Door"}]}, []) == []


# ── memory: confirm / correct / auto-demote / render ────────────────────────────────
def test_confirm_renders_into_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "m.json")
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    mem = pm.load()
    pm.record_understanding(mem, "r1", [{"key": "rule:Mark", "statement": "You number Mark sequentially.", "kind": "rule"}])
    assert pm.understanding_block(mem, "r1") == ""              # proposed -> not yet applied
    assert pm.confirm_understanding(mem, "r1", "rule:Mark", True) == "confirmed"
    blk = pm.understanding_block(mem, "r1")
    assert "CONFIRMED" in blk and "number Mark sequentially" in blk


def test_correction_overrides_then_auto_demotes(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "m.json")
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    mem = pm.load()
    pm.record_understanding(mem, "r1", [{"key": "rule:Mark", "statement": "guess", "kind": "rule"}])
    assert pm.confirm_understanding(mem, "r1", "rule:Mark", False, "Mark restarts per floor") == "corrected"
    assert "follow this instead: Mark restarts per floor" in pm.understanding_block(mem, "r1")
    # corrected again -> auto-demoted -> no longer applied
    assert pm.confirm_understanding(mem, "r1", "rule:Mark", False, "actually per room") == "demoted"
    assert pm.understanding_block(mem, "r1") == ""


def test_to_prompt_includes_confirmed_understanding(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "m.json")
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    mem = pm.load()
    pm.record_understanding(mem, "r1", [{"key": "intent:goal", "statement": "Goal: a tagged door.", "kind": "intent"}])
    pm.confirm_understanding(mem, "r1", "intent:goal", True)
    assert "a tagged door" in pm.to_prompt(mem, "r1")


# ── ledger ──────────────────────────────────────────────────────────────────────────
def test_log_understanding_appends(tmp_path):
    p = tmp_path / "ledger.jsonl"
    und.log_understanding("r1", [{"key": "rule:Mark", "status": "confirmed"}], path=p)
    und.log_understanding("r1", [{"key": "intent:goal", "status": "corrected"}], path=p)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[0]["routine_id"] == "r1" and rows[1]["status"] == "corrected"


# ── endpoints ───────────────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "users" / "u" / "memory.json")
    monkeypatch.setattr(pm, "LEGACY_PATH", tmp_path / "none.json")
    monkeypatch.setattr(und, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cs, "_EXECUTOR_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(cs, "_EXECUTOR_TRANSCRIPT", tmp_path / "transcripts.jsonl")
    return TestClient(cs.app)


def test_understanding_endpoints_round_trip(client):
    rid = "routine_understand"
    m = _seq_motif(); m["intent"] = {"goal": "a tagged door", "trigger": ""}
    cs._patterns[rid] = {"id": rid, "label": "Door", "status": "new",
                         "motif": m, "examples": _seq_examples(), "history": []}
    try:
        got = client.get(f"/api/understanding?pattern_id={rid}").json()["understanding"]
        keys = {h["key"]: h for h in got}
        assert "rule:Mark" in keys and "intent:goal" in keys
        assert all(h["status"] == "proposed" for h in got)

        s = client.post("/api/understanding/confirm",
                        json={"pattern_id": rid, "key": "rule:Mark", "accepted": True}).json()
        assert s["status"] == "confirmed"
        assert "CONFIRMED" in pm.understanding_block(pm.load(), rid)
    finally:
        cs._patterns.pop(rid, None)
