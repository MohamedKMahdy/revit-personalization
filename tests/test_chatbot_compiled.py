"""
Integration test: /api/execute-smart distills a compiled skill from a successful agent run, then on
the next run REPLAYS it deterministically via real_dispatch — without invoking the LLM agent. Executor
+ live model are mocked, so no API / no Revit.

Run:  pytest tests/test_chatbot_compiled.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp())

sys.path.insert(0, str(Path(__file__).parent.parent))
import chatbot.chat_server as cs                 # noqa: E402
from orchestrator import project_memory as pm    # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "users" / "u" / "memory.json")
    monkeypatch.setattr(pm, "LEGACY_PATH", tmp_path / "none.json")
    # isolate the executor logs so tests never pollute the real executor_runs/transcripts files
    monkeypatch.setattr(cs, "_EXECUTOR_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(cs, "_EXECUTOR_TRANSCRIPT", tmp_path / "transcripts.jsonl")
    monkeypatch.setattr(cs, "HISTORY_PATH", tmp_path / "pattern_history.json")
    return TestClient(cs.app)


def test_execute_smart_compiles_then_replays_without_the_agent(client, monkeypatch):
    rid = "routine_compiletest"
    motif = {"steps": [
        {"action_type": "Place", "family_name": "M_Door"},
        {"action_type": "SetParam", "param_name": "Mark", "param_value": None, "param_value_type": "variable"},
        {"action_type": "Tag", "tag_family_name": "M_Door Tag"}]}
    cs._patterns[rid] = {"id": rid, "label": "Door", "status": "new", "motif": motif,
                         "examples": [], "history": [], "pending_location": {"x": 1, "y": 2, "z": 0},
                         "param_overrides": {"Mark": "D-99"}}   # a chat-typed value: must be ONE-SHOT
    # memory: a known host wall + a last Mark, so the replay's holes (location/host_wall/Mark) all bind
    m = pm.load(); r = pm.routine_mem(m, rid, "Door")
    r["last_host_wall_id"] = 777; r["last_values"] = {"Mark": "D-00"}; pm.save(m)

    monkeypatch.setattr(cs, "_existing_param_values", lambda *a, **k: {})
    agent_calls = [
        {"name": "place_element", "args": {"family_name": "M_Door", "location": {"x": 1, "y": 2}, "host_wall_id": 777},
         "result": {"success": True, "element_id": 900}},
        {"name": "set_parameter", "args": {"element_id": 900, "name": "Mark", "value": "D-01"},
         "result": {"success": True}},
        {"name": "tag_element", "args": {"element_id": 900}, "result": {"success": True}}]
    calls = {"agent": 0}

    def fake_exec(goal, *, on_event=None, **kw):
        calls["agent"] += 1
        return {"done": True, "summary": "placed", "attempts": 3, "tool_calls": agent_calls,
                "usage": {}, "model": "claude-sonnet-4-6"}

    dispatched = []
    monkeypatch.setattr(cs, "run_executor", fake_exec)
    monkeypatch.setattr(cs, "real_dispatch", lambda tool, args:
                        (dispatched.append((tool, args)) or
                         ({"success": True, "element_id": 1234} if "place" in tool else {"success": True})))

    try:
        # Run 1: no skill yet -> agent path -> distills + stores a compiled skill
        client.post("/api/execute-smart", json={"pattern_id": rid})
        assert calls["agent"] == 1
        skill = pm.get_compiled_skill(pm.load(), rid)
        assert skill and any(s["tool"] == "place_element" for s in skill["steps"])

        # Run 2: skill exists + holes bindable -> DETERMINISTIC replay via real_dispatch, NOT the agent
        client.post("/api/execute-smart", json={"pattern_id": rid})
        assert calls["agent"] == 1                               # agent did NOT run again
        assert any(t == "place_element" for t, _ in dispatched)  # replayed through real_dispatch
        assert any(t == "tag_element" for t, _ in dispatched)

        # one-shot override: the chat-typed Mark must be CLEARED after a done run so the next
        # placement increments instead of being pinned forever (the "Mark stuck at 101" bug)
        assert cs._patterns[rid].get("param_overrides") == {}
    finally:
        cs._patterns.pop(rid, None)
