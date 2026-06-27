"""
Tests for the free-form conversational agent (/api/execute-task) and the proactive prediction
endpoint (/api/predict) added to chatbot/chat_server.py. The executor + live log readers are mocked,
so no Anthropic API and no Revit are needed.

Run:  pytest tests/test_chatbot_freeform.py -v
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
from predictor import Prediction                 # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "users" / "u" / "memory.json")
    monkeypatch.setattr(pm, "LEGACY_PATH", tmp_path / "none.json")
    return TestClient(cs.app)


def test_execute_task_streams_and_wraps_request_as_goal(client, monkeypatch):
    """A free-form task runs through the executor with a goal built from the user's NL request, and
    the reasoning/result/summary stream back over SSE."""
    captured = {}

    def fake_run(goal, *, on_event=None, confirm_fn=None, memory_block="", **kw):
        captured["goal"] = goal
        on_event("reasoning", "Checking the model")
        on_event("tool", {"name": "inspect_model", "args": {}})
        on_event("result", {"name": "inspect_model", "result": {"success": True, "message": "ok"}})
        return {"done": True, "summary": "There are 3 fire doors on level 2.",
                "attempts": 1, "tool_calls": [], "usage": {}, "model": "claude-sonnet-4-6"}

    monkeypatch.setattr(cs, "run_executor", fake_run)
    body = client.post("/api/execute-task", json={"text": "how many fire doors on level 2?"}).text

    assert "how many fire doors on level 2?" in captured["goal"]   # build_freeform_goal wrapped it
    assert "Checking the model" in body                            # reasoning streamed
    assert "There are 3 fire doors on level 2." in body           # final summary streamed
    assert "fire doors" in body and '"done"' in body


def test_execute_task_empty_is_rejected(client):
    assert "No task given" in client.post("/api/execute-task", json={"text": "   "}).text


def test_execute_task_passes_confirm_fn_for_writes(client, monkeypatch):
    """The free-form path still hands the executor a confirm_fn (writes stay gated)."""
    seen = {}

    def fake_run(goal, *, on_event=None, confirm_fn=None, memory_block="", **kw):
        seen["has_confirm_fn"] = callable(confirm_fn)
        return {"done": True, "summary": "done", "attempts": 0, "tool_calls": [], "usage": {}}

    monkeypatch.setattr(cs, "run_executor", fake_run)
    client.post("/api/execute-task", json={"text": "rename the active view"})
    assert seen["has_confirm_fn"] is True


def test_predict_endpoint_returns_prediction(client, monkeypatch):
    monkeypatch.setattr("mcp_server.log_reader.load_real_action_records", lambda: [], raising=False)
    monkeypatch.setattr("mcp_server.log_reader.list_candidate_routines", lambda *a, **k: [], raising=False)
    monkeypatch.setattr("predictor.predict_live", lambda recs, routines: Prediction(
        routine_id="r1", routine_label="Place(M_Door) → SetParam×1 → Tag", support=5,
        confidence=0.9, match="exact", next_actions=[{"action_type": "SetParam", "key": "Mark"}]))
    r = client.get("/api/predict").json()
    assert r["prediction"]["routine_id"] == "r1"
    assert "Mark" in r["prediction"]["headline"] and r["prediction"]["match"] == "exact"


def test_predict_endpoint_null_when_nothing_in_progress(client, monkeypatch):
    monkeypatch.setattr("mcp_server.log_reader.load_real_action_records", lambda: [], raising=False)
    monkeypatch.setattr("mcp_server.log_reader.list_candidate_routines", lambda *a, **k: [], raising=False)
    assert client.get("/api/predict").json()["prediction"] is None
