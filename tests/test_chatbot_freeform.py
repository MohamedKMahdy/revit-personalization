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
    # isolate the executor logs so tests never pollute the real executor_runs/transcripts files
    monkeypatch.setattr(cs, "_EXECUTOR_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(cs, "_EXECUTOR_TRANSCRIPT", tmp_path / "transcripts.jsonl")
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


def test_execute_task_carries_context_and_writes_outcome_back(client, monkeypatch):
    """Cross-task memory: the executor receives recent conversation context, and the task OUTCOME is
    written back into the conversation (merged into the trailing assistant turn to keep roles
    alternating) so the next turn knows what was done and the agent stops re-doing it."""
    rid = "routine_writeback_test"
    cs._patterns[rid] = {"id": rid, "label": "x", "status": "new",
                         "history": [{"role": "user", "content": "tag the doors"},
                                     {"role": "assistant", "content": "On it. ##TASK: tag all doors##"}]}
    captured = {}

    def fake_run(goal, *, on_event=None, confirm_fn=None, memory_block="", **kw):
        captured["goal"] = goal
        return {"done": True, "summary": "Tagged 10 doors.", "attempts": 10, "tool_calls": [], "usage": {}}

    monkeypatch.setattr(cs, "run_executor", fake_run)
    try:
        client.post("/api/execute-task", json={"text": "tag all doors", "pattern_id": rid})
        assert "tag the doors" in captured["goal"]                 # recent context fed into the executor
        hist = cs._patterns[rid]["history"]
        assert hist[-1]["role"] == "assistant" and "Tagged 10 doors." in hist[-1]["content"]
        assert sum(1 for m in hist if m["role"] == "assistant") == 1   # merged, not a 2nd assistant turn
    finally:
        cs._patterns.pop(rid, None)


def test_execute_task_persists_executor_session_across_tasks(client, monkeypatch):
    """Persistent session: the 2nd task continues the SAME executor message history (remembers task 1)
    instead of starting cold — only when the prior run ended cleanly on an assistant turn."""
    cs._exec_sessions.clear()
    rid = "routine_session_test"
    cs._patterns[rid] = {"id": rid, "label": "x", "status": "new", "history": []}
    seen = []

    def fake_run(goal, *, on_event=None, confirm_fn=None, memory_block="", prior_messages=None, **kw):
        seen.append(prior_messages)
        msgs = list(prior_messages or []) + [{"role": "user", "content": goal},
                                             {"role": "assistant", "content": "done"}]
        return {"done": True, "summary": "ok", "attempts": 1, "tool_calls": [], "usage": {}, "messages": msgs}

    monkeypatch.setattr(cs, "run_executor", fake_run)
    try:
        client.post("/api/execute-task", json={"text": "first task", "pattern_id": rid})
        client.post("/api/execute-task", json={"text": "second task", "pattern_id": rid})
        assert seen[0] is None                                     # 1st task starts cold
        assert seen[1] and len(seen[1]) >= 2                       # 2nd task continues the session
        assert any("first task" in str(m.get("content", "")) for m in seen[1])   # remembers task 1
    finally:
        cs._patterns.pop(rid, None)
        cs._exec_sessions.pop(rid, None)


def test_execute_task_drops_session_if_unclean(client, monkeypatch):
    """If a run doesn't end on an assistant turn (e.g. hit the cap), the session is dropped so the
    next task starts fresh rather than 400-ing on a dangling tool_result turn."""
    cs._exec_sessions.clear()
    rid = "routine_session_unclean"
    cs._patterns[rid] = {"id": rid, "label": "x", "status": "new", "history": []}

    def fake_run(goal, *, prior_messages=None, **kw):
        # ends on a USER (tool_result) turn — unclean
        return {"done": False, "summary": "cap", "attempts": 14, "tool_calls": [], "usage": {},
                "messages": [{"role": "user", "content": goal}, {"role": "user", "content": [{"x": 1}]}]}

    monkeypatch.setattr(cs, "run_executor", fake_run)
    try:
        client.post("/api/execute-task", json={"text": "t", "pattern_id": rid})
        assert rid not in cs._exec_sessions       # unclean -> not stored
    finally:
        cs._patterns.pop(rid, None)
        cs._exec_sessions.pop(rid, None)


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
