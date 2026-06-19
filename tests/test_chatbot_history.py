"""
Chatbot pattern-history test suite.

Covers the browsable detected-pattern history added to chatbot/chat_server.py:
persistence + CRUD, the active-selection rule, conversation isolation, re-detection
dedup/re-badge, stale-id safety, and the streaming path (_stream) — per-record turn
ordering (must alternate for Anthropic), control-token parsing, visible-message
stripping, and error rollback.

Run:  pytest tests/test_chatbot_history.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# chat_server instantiates an Anthropic client and computes HISTORY_PATH at import,
# so both must be satisfied BEFORE importing it.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-import")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).parent.parent))
import chatbot.chat_server as cs  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh, isolated server state + history file per test."""
    cs._patterns.clear()
    cs._locks.clear()
    monkeypatch.setattr(cs, "_active_id", None)
    monkeypatch.setattr(cs, "HISTORY_PATH", tmp_path / "pattern_history.json")
    return TestClient(cs.app)


A = {"id": "routine_A", "label": "Place Wall + Tag + Mark", "count": 6,
     "motif": {"steps": [{"action": "Place"}]},
     "tool_sequence": [{"tool": "place_element"}, {"tool": "tag_element"}]}
B = {"id": "routine_B", "label": "Place Door + Finish + Tag", "count": 5,
     "tool_sequence": [{"tool": "place_element"}]}
C = {"id": "routine_C", "label": "Place Window + Sill", "count": 4,
     "tool_sequence": [{"tool": "place_element"}]}


# ── CRUD + active-selection rule ────────────────────────────────────────────────

def test_first_detection_becomes_active(client):
    r = client.post("/api/pattern", json=A).json()
    assert r["ok"] and r["is_new"] and r["active_id"] == "routine_A"


def test_second_detection_surfaces_when_first_idle(client):
    client.post("/api/pattern", json=A)
    r = client.post("/api/pattern", json=B).json()
    assert r["active_id"] == "routine_B"            # A wasn't engaged → surface B
    lst = client.get("/api/patterns").json()
    assert [p["id"] for p in lst["patterns"]] == ["routine_B", "routine_A"]  # newest first
    assert next(p for p in lst["patterns"] if p["id"] == "routine_B")["status"] == "new"


def test_engaged_pattern_is_not_yanked_away(client):
    client.post("/api/pattern", json=A)
    client.post("/api/patterns/routine_A/activate")
    cs._patterns["routine_A"]["history"].append({"role": "user", "content": "where?"})
    r = client.post("/api/pattern", json=C).json()
    assert r["active_id"] == "routine_A"            # stays put while engaged
    assert any(p["id"] == "routine_C" for p in client.get("/api/patterns").json()["patterns"])


def test_activate_marks_seen_and_returns_conversation(client):
    client.post("/api/pattern", json=A)
    r = client.post("/api/patterns/routine_A/activate").json()
    assert r["ok"] and r["messages"] == [] and r["status"] == "seen"
    assert client.get("/api/patterns").json()["active_id"] == "routine_A"


def test_activate_unknown_is_404(client):
    assert client.post("/api/patterns/nope/activate").status_code == 404


def test_conversation_isolation(client):
    client.post("/api/pattern", json=A)
    client.post("/api/pattern", json=B)
    cs._patterns["routine_A"]["history"].append({"role": "user", "content": "hi A"})
    assert cs._has_user_turns(cs._patterns["routine_A"])
    assert not cs._has_user_turns(cs._patterns["routine_B"])   # no leak


def test_delete_repoints_active(client):
    client.post("/api/pattern", json=A)
    client.post("/api/pattern", json=B)          # B active
    r = client.delete("/api/patterns/routine_B").json()
    assert r["active_id"] == "routine_A"
    assert len(client.get("/api/patterns").json()["patterns"]) == 1


def test_persistence_round_trips(client):
    client.post("/api/pattern", json=A)
    client.post("/api/pattern", json=B)
    saved = json.loads(cs.HISTORY_PATH.read_text(encoding="utf-8"))
    assert saved["active_id"] == "routine_B"
    assert len(saved["patterns"]) == 2


# ── dedup / re-detection ────────────────────────────────────────────────────────

def test_redetection_updates_in_place(client):
    client.post("/api/pattern", json=A)
    r = client.post("/api/pattern", json={**A, "count": 9}).json()
    assert not r["is_new"]
    ids = [p["id"] for p in client.get("/api/patterns").json()["patterns"]]
    assert ids.count("routine_A") == 1
    assert cs._patterns["routine_A"]["count"] == 9


def test_dismissed_routine_rebadges_on_recurrence(client):
    client.post("/api/pattern", json=A)
    cs._patterns["routine_A"]["status"] = "dismissed"
    cs._patterns["routine_A"]["count"] = 9
    client.post("/api/pattern", json={**A, "count": 12})
    assert cs._patterns["routine_A"]["status"] == "new"


def test_derived_id_without_explicit_id(client):
    payload = {"label": "No-Id Routine", "count": 3,
               "tool_sequence": [{"tool": "place_element"}]}
    r = client.post("/api/pattern", json=payload).json()
    assert r["id"].startswith("pat_") and r["is_new"]


# ── stale-id safety ─────────────────────────────────────────────────────────────

def test_stale_id_execute_does_not_retarget(client):
    client.post("/api/pattern", json=A)
    res = client.post("/api/execute", json={"pattern_id": "ghost"}).json()
    assert res.get("success") is False and "error" in res     # no silent retarget, no bridge call


def test_lock_identity_is_stable(client):
    assert cs._lock_for("routine_A") is cs._lock_for("routine_A")


# ── streaming path (_stream) with a mocked Anthropic client ─────────────────────

class _FakeStream:
    def __init__(self, text, raise_exc):
        self._text, self._raise = text, raise_exc
    async def __aenter__(self):
        if self._raise:
            raise RuntimeError("simulated upstream failure")
        return self
    async def __aexit__(self, *a):
        return False
    @property
    def text_stream(self):
        text = self._text
        async def gen():
            for i in range(0, len(text), 4):     # tiny chunks to exercise SSE buffering
                yield text[i:i + 4]
        return gen()


class _FakeMessages:
    reply = "Hello!"
    raise_exc = False
    def stream(self, **kw):
        return _FakeStream(self.reply, self.raise_exc)


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _parse_sse(text):
    chunks, action, done = "", None, False
    for line in text.split("\n"):
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        chunks += ev.get("t", "")
        if ev.get("done"):
            done, action = True, ev.get("action")
    return chunks, action, done


@pytest.fixture
def fake_client(client, monkeypatch):
    fc = _FakeClient()
    monkeypatch.setattr(cs, "_client", fc)
    client.post("/api/pattern", json={"id": "routine_X", "label": "X", "count": 4,
                                      "tool_sequence": [{"tool": "place_element"}]})
    client.post("/api/patterns/routine_X/activate")
    return client, fc


def test_start_stores_alternating_and_parses_location(fake_client):
    client, fc = fake_client
    fc.messages.reply = "Hi! Where to place it? ##LOCATION:1,2,0##"
    r = client.post("/api/start", json={"pattern_id": "routine_X"})
    chunks, action, done = _parse_sse(r.text)
    hist = cs._patterns["routine_X"]["history"]
    assert done and "Where to place it" in chunks
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "__INIT__"
    assert cs._patterns["routine_X"]["pending_location"] == {"x": 1.0, "y": 2.0, "z": 0.0}


def test_chat_alternates_and_surfaces_action(fake_client):
    client, fc = fake_client
    fc.messages.reply = "Hi!"
    client.post("/api/start", json={"pattern_id": "routine_X"})
    fc.messages.reply = "Placing it now. ##EXECUTE##"
    r = client.post("/api/chat", json={"pattern_id": "routine_X", "text": "at 1,2"})
    _chunks, action, _done = _parse_sse(r.text)
    hist = cs._patterns["routine_X"]["history"]
    assert action == "execute"
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
    assert hist[2]["content"] == "at 1,2"


def test_visible_messages_strip_tokens_and_hide_init(fake_client):
    client, fc = fake_client
    fc.messages.reply = "Hi! ##LOCATION:0,0,0##"
    client.post("/api/start", json={"pattern_id": "routine_X"})
    vis = cs._visible_messages(cs._patterns["routine_X"])
    assert all(not (m["role"] == "user" and m["content"] == "__INIT__") for m in vis)
    assert all("##" not in m["content"] for m in vis)


def test_stream_error_rolls_back_user_turn(fake_client):
    client, fc = fake_client
    fc.messages.reply = "Hi!"
    client.post("/api/start", json={"pattern_id": "routine_X"})
    before = len(cs._patterns["routine_X"]["history"])
    fc.messages.raise_exc = True
    r = client.post("/api/chat", json={"pattern_id": "routine_X", "text": "boom"})
    chunks, _action, done = _parse_sse(r.text)
    hist = cs._patterns["routine_X"]["history"]
    assert done and "Error" in chunks
    assert len(hist) == before                              # dangling user rolled back
    assert [m["role"] for m in hist] == ["user", "assistant"]  # still alternating
