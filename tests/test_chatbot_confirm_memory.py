"""
Tests for the API-fallback confirmation round-trip and the per-user memory endpoints
added to chatbot/chat_server.py.

Run:  pytest tests/test_chatbot_confirm_memory.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp())

sys.path.insert(0, str(Path(__file__).parent.parent))
import chatbot.chat_server as cs            # noqa: E402
from orchestrator import project_memory as pm   # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "MEM_ROOT", tmp_path / "users")
    monkeypatch.setattr(pm, "MEM_PATH", tmp_path / "users" / "u" / "memory.json")
    monkeypatch.setattr(pm, "LEGACY_PATH", tmp_path / "none.json")
    cs._pending_confirms.clear()
    return TestClient(cs.app)


def test_memory_endpoint_reflects_profile(client):
    m = pm.load()
    pm.set_user(m, name="Mohamed", role="MSc student")
    pm.add_preference(m, "always let me pick the location")
    pm.add_convention(m, "Mark", "D-1xx")
    pm.save(m)

    r = client.get("/api/memory").json()
    assert r["name"] == "Mohamed" and r["role"] == "MSc student"
    assert "always let me pick the location" in r["preferences"]
    assert r["conventions"]["Mark"] == "D-1xx"


def test_memory_forget_removes_a_preference(client):
    m = pm.load()
    pm.add_preference(m, "pref to keep")
    pm.add_preference(m, "pref to forget")
    pm.save(m)

    assert client.post("/api/memory/forget", json={"text": "pref to forget"}).json()["ok"] is True
    prefs = client.get("/api/memory").json()["preferences"]
    assert "pref to keep" in prefs and "pref to forget" not in prefs


def test_execute_confirm_round_trip(client):
    """Simulate the executor's worker thread blocking on a confirm; the endpoint releases it."""
    cid = "cfm-itest"
    ev = threading.Event()
    cs._pending_confirms[cid] = {"event": ev, "approved": None}
    result = {}

    def waiter():
        got = ev.wait(timeout=5)
        rec = cs._pending_confirms.pop(cid, None)
        result["approved"] = bool(got and rec and rec.get("approved"))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    out = client.post("/api/execute-confirm", json={"id": cid, "approved": True}).json()
    t.join(timeout=5)

    assert out["ok"] is True and out["approved"] is True
    assert result["approved"] is True       # the blocked thread unblocked with the approval


def test_execute_confirm_reject(client):
    cid = "cfm-reject"
    cs._pending_confirms[cid] = {"event": threading.Event(), "approved": None}
    out = client.post("/api/execute-confirm", json={"id": cid, "approved": False}).json()
    assert out["ok"] is True and out["approved"] is False


def test_execute_confirm_unknown_id(client):
    out = client.post("/api/execute-confirm", json={"id": "missing", "approved": True}).json()
    assert out["ok"] is False
