"""
Tests for the self-healing execution agent (orchestrator/executor_agent.py).

Deterministic: a FAKE Anthropic client scripts the tool-use turns and a FAKE Revit
dispatch scripts the tool results, so the loop is verified end-to-end with NO API and
NO live Revit. Asserts the loop:
  • feeds tool errors back and keeps going (self-heals: family-not-loaded -> recovers),
  • stops at the iteration cap,
  • never dispatches a disallowed tool.

Run:  pytest tests/test_executor_agent.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import executor_agent as ex  # noqa: E402


# ── fake Anthropic client ────────────────────────────────────────────────────────
class Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class Resp:
    def __init__(self, blocks):
        self.content = blocks


def _tu(name, inp, tid):
    return Block("tool_use", name=name, input=inp, id=tid)


def _txt(t):
    return Block("text", text=t)


class FakeClient:
    """Returns scripted responses in order, ignoring the messages."""
    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = 0

    class _M:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kw):
            self.outer.calls += 1
            r = self.outer._script[self.outer._i]
            self.outer._i += 1
            return r

    @property
    def messages(self):
        return FakeClient._M(self)


def test_self_heals_family_not_loaded():
    """place(M_Single-Flush) fails -> agent lists types -> re-places loaded family -> set -> tag -> done."""
    script = [
        Resp([_txt("Placing the door."), _tu("place_element",
              {"family_name": "M_Single-Flush", "location": {"x": 2500, "y": 0}}, "t1")]),
        Resp([_txt("Not loaded — checking what's available."),
              _tu("get_available_family_types", {"category": "OST_Doors"}, "t2")]),
        Resp([_tu("place_element",
              {"family_name": "M_Door-Passage-Single-Flush", "location": {"x": 2500, "y": 0}}, "t3")]),
        Resp([_tu("set_parameter", {"element_id": 999, "name": "Mark", "value": "D-101"}, "t4")]),
        Resp([_tu("tag_element", {"element_id": 999}, "t5")]),
        Resp([_txt("Done — placed M_Door-Passage-Single-Flush, set Mark, and tagged it.")]),
    ]

    def fake_dispatch(name, args):
        if name == "place_element":
            if args["family_name"] == "M_Single-Flush":
                return {"success": False, "message": "Successfully created 0 element(s) — family not loaded"}
            return {"success": True, "message": "placed", "element_id": 999}
        if name == "get_available_family_types":
            return {"success": True, "types": [{"family": "M_Door-Passage-Single-Flush", "type": "900 x 2100mm", "id": 1}]}
        if name == "set_parameter":
            return {"success": True, "message": "set"}
        if name == "tag_element":
            return {"success": True, "message": "tagged", "tag_id": 1000}
        return {"success": False, "message": "unknown"}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)

    assert out["done"] is True
    names = [c["name"] for c in out["tool_calls"]]
    assert names == ["place_element", "get_available_family_types", "place_element", "set_parameter", "tag_element"]
    # the FIRST place failed, the SECOND (recovered family) succeeded
    assert out["tool_calls"][0]["result"]["success"] is False
    assert out["tool_calls"][0]["args"]["family_name"] == "M_Single-Flush"
    assert out["tool_calls"][2]["result"]["success"] is True
    assert out["tool_calls"][2]["args"]["family_name"] == "M_Door-Passage-Single-Flush"


def test_no_host_then_pick_recovers():
    """place fails 'no host' -> agent calls pick_point -> re-places at picked point -> done."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("No wall there — please click on a wall."), _tu("pick_point", {"prompt": "Click on a wall"}, "t2")]),
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 3000, "y": 0}}, "t3")]),
        Resp([_txt("Placed on the wall you picked.")]),
    ]
    state = {"picked": False}

    def fake_dispatch(name, args):
        if name == "place_element":
            if not state["picked"]:
                return {"success": False, "message": "no valid host found"}
            return {"success": True, "element_id": 555}
        if name == "pick_point":
            state["picked"] = True
            return {"success": True, "location": {"x": 3000, "y": 0, "z": 0}}
        return {"success": False, "message": "unknown"}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)
    assert out["done"] is True
    assert [c["name"] for c in out["tool_calls"]] == ["place_element", "pick_point", "place_element"]
    assert out["tool_calls"][-1]["result"]["success"] is True


def test_iteration_cap():
    """A model that never stops calling tools must terminate at the cap, not hang."""
    looping = [Resp([_tu("get_available_family_types", {"category": "OST_Doors"}, f"t{i}")]) for i in range(50)]

    def fake_dispatch(name, args):
        return {"success": True, "types": []}

    out = ex.run_executor("goal", client=FakeClient(looping), dispatch_fn=fake_dispatch, max_iters=5)
    assert out["done"] is False
    assert out["attempts"] == 5


def test_disallowed_tool_never_dispatched():
    """A tool outside the allowlist is rejected without ever touching dispatch."""
    script = [
        Resp([_tu("send_code_to_revit", {"code": "doc.Delete(everything)"}, "t1")]),
        Resp([_txt("Understood, stopping.")]),
    ]
    dispatched = []

    def fake_dispatch(name, args):
        dispatched.append(name)
        return {"success": True}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)
    assert "send_code_to_revit" not in dispatched          # never executed
    assert out["tool_calls"][0]["result"]["success"] is False
    assert "not allowed" in out["tool_calls"][0]["result"]["message"]
    assert out["done"] is True


def test_events_streamed():
    """on_event must emit reasoning / tool / result / done so the chat can stream it."""
    script = [
        Resp([_txt("Placing."), _tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("All set.")]),
    ]
    events = []
    ex.run_executor("goal", client=FakeClient(script),
                    dispatch_fn=lambda n, a: {"success": True, "element_id": 1},
                    on_event=lambda k, p: events.append(k))
    kinds = set(events)
    assert {"reasoning", "tool", "result", "done"} <= kinds
