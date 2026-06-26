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


def test_proactively_queries_then_places_loaded_family():
    """Agent INVESTIGATES the model first (lists loaded types), then places the family that
    actually exists — grounding itself instead of placing blind and failing."""
    script = [
        Resp([_txt("Let me check which door families are loaded here first."),
              _tu("get_available_family_types", {"category": "OST_Doors"}, "t1")]),
        Resp([_tu("place_element",
              {"family_name": "M_Door-Passage-Single-Flush", "location": {"x": 2500, "y": 0}}, "t2")]),
        Resp([_txt("Placed the loaded family on the first try.")]),
    ]

    def fake_dispatch(name, args):
        if name == "get_available_family_types":
            return {"success": True, "types": [{"family": "M_Door-Passage-Single-Flush",
                                                "type": "0915x2134mm", "id": 1}]}
        if name == "place_element":
            return {"success": True, "element_id": 42}
        return {"success": False, "message": "unknown"}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)
    assert out["done"] is True
    assert [c["name"] for c in out["tool_calls"]] == ["get_available_family_types", "place_element"]
    assert all(c["result"]["success"] for c in out["tool_calls"])     # queried first -> no failed write


def test_uses_selection_as_host():
    """If the user pre-selected a wall, the agent reads the selection and hosts on that wall id."""
    script = [
        Resp([_txt("Checking your selection for a host wall."), _tu("get_selected_elements", {}, "t1")]),
        Resp([_tu("place_element",
              {"family_name": "M_Door", "location": {"x": 0, "y": 0}, "host_wall_id": 777}, "t2")]),
        Resp([_txt("Hosted on the wall you selected.")]),
    ]

    def fake_dispatch(name, args):
        if name == "get_selected_elements":
            return {"success": True, "selected_ids": [777]}
        if name == "place_element":
            return {"success": True, "element_id": 9}
        return {"success": False}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)
    assert out["done"] is True
    assert out["tool_calls"][0]["name"] == "get_selected_elements"
    assert out["tool_calls"][1]["args"]["host_wall_id"] == 777


def test_inspect_model_no_walls_stops_gracefully():
    """Door routine + a model with zero walls: the agent checks, sees Walls:0, and stops with an
    explanation instead of looping place_element forever against an impossible host."""
    script = [
        Resp([_txt("Checking the model before placing a wall-hosted door."), _tu("inspect_model", {}, "t1")]),
        Resp([_txt("Your model has no walls, so there is nothing to host a door on — "
                   "draw or pick a wall and I'll continue.")]),
    ]

    def fake_dispatch(name, args):
        if name == "inspect_model":
            return {"success": True, "counts": {"Walls": 0, "Doors": 0}, "total_categories": 5}
        return {"success": True, "element_id": 1}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch)
    assert out["done"] is True
    assert [c["name"] for c in out["tool_calls"]] == ["inspect_model"]     # never attempted a place
    assert "wall" in out["summary"].lower()


def test_real_dispatch_query_tools(monkeypatch):
    """The new read tools map onto the right plugin calls and normalize their results."""
    from mcp_server import revit_bridge as rb

    def fake_call(method, params=None):
        if method == "get_current_view_info":
            return {"Name": "L1 - Architectural", "ViewType": "FloorPlan", "Scale": 100}
        if method == "analyze_model_statistics":
            return {"categories": [{"categoryName": "Walls", "elementCount": 3},
                                   {"categoryName": "Doors", "elementCount": 1},
                                   {"categoryName": "Cameras", "elementCount": 9}]}
        if method == "get_selected_elements":
            return [{"Id": 123, "Category": "Walls"}, {"ElementId": 456}]
        return None

    monkeypatch.setattr(rb, "_call_plugin", fake_call)

    v = ex.real_dispatch("get_active_view", {})
    assert v["success"] and v["view"]["name"] == "L1 - Architectural" and v["view"]["type"] == "FloorPlan"

    m = ex.real_dispatch("inspect_model", {})
    assert m["success"] and m["counts"]["Walls"] == 3 and m["counts"]["Doors"] == 1
    assert "Cameras" not in m["counts"] and m["total_categories"] == 3      # only placement cats surfaced

    s = ex.real_dispatch("get_selected_elements", {})
    assert s["success"] and s["selected_ids"] == [123, 456]


def test_real_dispatch_routes_full_plugin_surface(monkeypatch):
    """A non-curated plugin tool (e.g. create_grid) routes through the generic pass-through."""
    from mcp_server import revit_bridge as rb
    captured = {}

    def fake_call(command, params, timeout=None):
        captured["command"] = command
        return {"Success": True, "Response": [9001], "Message": "grid created"}

    monkeypatch.setattr(rb, "_call_plugin", fake_call)

    out = ex.real_dispatch("create_grid", {"data": {"originX": 0, "count": 3, "spacing": 6000}})
    assert captured["command"] == "create_grid"
    assert out["success"] is True and out["response"] == [9001]


def test_real_dispatch_rejects_blocked_tool():
    """send_code_to_revit is not exposed by name and never dispatches via the generic path."""
    assert "send_code_to_revit" not in ex.ALLOWED_TOOLS
    out = ex.real_dispatch("send_code_to_revit", {"code": "doc.Delete(x)"})
    assert out["success"] is False        # falls through to the unknown/disallowed branch


def test_execute_revit_api_fallback(monkeypatch):
    """The gated fallback compiles+runs C# via send_code_to_revit and normalizes the result."""
    from mcp_server import revit_bridge as rb
    captured = {}

    def fake_call(command, params, timeout=None):
        captured["command"] = command
        captured["params"] = params
        return {"success": True, "result": "[123,124]", "errorMessage": ""}   # ExecutionResultInfo

    monkeypatch.setattr(rb, "_call_plugin", fake_call)
    monkeypatch.setattr(ex, "API_FALLBACK_ENABLED", True)

    out = ex.real_dispatch("execute_revit_api",
                           {"purpose": "count walls", "code": "return new FilteredElementCollector(document).OfClass(typeof(Wall)).ToElementIds();",
                            "transactionMode": "none"})
    assert captured["command"] == "send_code_to_revit"
    assert captured["params"]["transactionMode"] == "none"           # read-only honored
    assert out["success"] is True and out["result"] == "[123,124]"


def test_execute_revit_api_reports_failure(monkeypatch):
    from mcp_server import revit_bridge as rb
    monkeypatch.setattr(rb, "_call_plugin",
                        lambda c, p, timeout=None: {"success": False, "result": None, "errorMessage": "CS0103: undefined"})
    monkeypatch.setattr(ex, "API_FALLBACK_ENABLED", True)
    out = ex.real_dispatch("execute_revit_api", {"purpose": "x", "code": "bad code"})
    assert out["success"] is False and "CS0103" in out["message"]


def test_execute_revit_api_disabled(monkeypatch):
    monkeypatch.setattr(ex, "API_FALLBACK_ENABLED", False)
    out = ex.real_dispatch("execute_revit_api", {"purpose": "x", "code": "return 1;"})
    assert out["success"] is False and "disabled" in out["message"].lower()


def test_needs_confirmation():
    assert ex.needs_confirmation("execute_revit_api", {"transactionMode": "auto"}) is True
    assert ex.needs_confirmation("execute_revit_api", {}) is True                 # defaults to auto
    assert ex.needs_confirmation("execute_revit_api", {"transactionMode": "none"}) is False  # read-only
    assert ex.needs_confirmation("place_element", {}) is False
    assert ex.needs_confirmation("delete_element", {}) is False                   # gate is API-only


def test_confirm_gate_blocks_write_when_declined():
    """A write via execute_revit_api is NOT run when the user declines; the agent is told."""
    script = [
        Resp([_tu("execute_revit_api",
                  {"purpose": "rename view", "code": "document.ActiveView.Name=\"X\";", "transactionMode": "auto"}, "t1")]),
        Resp([_txt("Understood — I won't run that.")]),
    ]
    dispatched, confirms = [], []

    def fake_dispatch(name, args):
        dispatched.append(name)
        return {"success": True, "result": "ok"}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch,
                          guard_api_fallback=False,            # isolate the confirm gate from the nudge
                          confirm_fn=lambda n, a: (confirms.append(n) or False))
    assert confirms == ["execute_revit_api"]                  # gate consulted
    assert "execute_revit_api" not in dispatched              # never dispatched
    assert out["tool_calls"][0]["result"]["success"] is False
    assert "declined" in out["tool_calls"][0]["result"]["message"].lower()


def test_confirm_gate_allows_when_approved():
    script = [
        Resp([_tu("execute_revit_api", {"purpose": "p", "code": "return 1;", "transactionMode": "auto"}, "t1")]),
        Resp([_txt("done")]),
    ]
    dispatched = []
    out = ex.run_executor("goal", client=FakeClient(script), guard_api_fallback=False, preflight=False,
                          dispatch_fn=lambda n, a: (dispatched.append(n) or {"success": True, "result": "1"}),
                          confirm_fn=lambda n, a: True)
    assert dispatched == ["execute_revit_api"] and out["tool_calls"][0]["result"]["success"] is True


def test_confirm_gate_skips_readonly_api_calls():
    """A read-only API query (transactionMode none) runs without prompting."""
    script = [
        Resp([_tu("execute_revit_api", {"purpose": "count", "code": "return 1;", "transactionMode": "none"}, "t1")]),
        Resp([_txt("done")]),
    ]
    confirms, dispatched = [], []
    out = ex.run_executor("goal", client=FakeClient(script), guard_api_fallback=False, preflight=False,
                          dispatch_fn=lambda n, a: (dispatched.append(n) or {"success": True}),
                          confirm_fn=lambda n, a: (confirms.append(n) or False))
    assert confirms == []                       # read-only not gated
    assert dispatched == ["execute_revit_api"]  # ran despite confirm_fn=False


def test_api_fallback_nudged_then_allowed_on_reaffirm():
    """The first escalation to raw API is redirected (nudged); the agent must reaffirm to run it."""
    script = [
        Resp([_tu("execute_revit_api", {"purpose": "p1", "code": "return 1;", "transactionMode": "none"}, "t1")]),
        Resp([_tu("execute_revit_api", {"purpose": "reaffirm: no tool renames a view",
                                        "code": "return 1;", "transactionMode": "none"}, "t2")]),
        Resp([_txt("done")]),
    ]
    dispatched = []
    out = ex.run_executor("goal", client=FakeClient(script), preflight=False,   # guard on by default
                          dispatch_fn=lambda n, a: (dispatched.append(n) or {"success": True, "result": "1"}))
    assert [c["name"] for c in out["tool_calls"]] == ["execute_revit_api", "execute_revit_api"]
    assert dispatched == ["execute_revit_api"]                   # only the SECOND (reaffirmed) ran
    assert out["tool_calls"][0]["result"]["success"] is False    # first was nudged, not run
    assert "ONLY for operations" in out["tool_calls"][0]["result"]["message"]
    assert out["tool_calls"][1]["result"]["success"] is True


def test_api_nudge_rearms_after_a_structured_tool():
    """A structured tool between API attempts re-arms the nudge — each fresh escalation is challenged."""
    script = [
        Resp([_tu("execute_revit_api", {"purpose": "p", "code": "return 1;", "transactionMode": "none"}, "t1")]),
        Resp([_tu("get_active_view", {}, "t2")]),
        Resp([_tu("execute_revit_api", {"purpose": "p", "code": "return 1;", "transactionMode": "none"}, "t3")]),
        Resp([_txt("ok")]),
    ]
    dispatched = []
    out = ex.run_executor("goal", client=FakeClient(script), preflight=False,
                          dispatch_fn=lambda n, a: (dispatched.append(n) or {"success": True}))
    assert dispatched == ["get_active_view"]                     # both API calls nudged, not run
    assert out["tool_calls"][0]["result"]["success"] is False
    assert out["tool_calls"][2]["result"]["success"] is False    # nudged again after the re-arm


def test_hit_hosted_placement_gap():
    """The 'created 0 / no element' signature on a placement marks the hosted-placement gap."""
    assert ex._hit_hosted_placement_gap(
        [{"name": "place_element", "args": {}, "result": {"success": False, "message": "Successfully created 0 element(s)."}}]) is True
    assert ex._hit_hosted_placement_gap(
        [{"name": "place_element", "args": {}, "result": {"success": False, "message": "no valid host found"}}]) is False
    assert ex._hit_hosted_placement_gap(
        [{"name": "place_element", "args": {}, "result": {"success": True, "element_id": 1}}]) is False
    assert ex._hit_hosted_placement_gap(
        [{"name": "get_active_view", "args": {}, "result": {"success": True}}]) is False


def test_api_fallback_allowed_after_created_zero():
    """A wall-hosted family that returns 'created 0' is a REAL capability gap (the structured tool
    can't host it) → the API fallback must NOT be nudged; it runs (subject to the confirm gate)."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door-Vision", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_tu("execute_revit_api", {"purpose": "host the door via NewFamilyInstance",
                                        "code": "return document.Create.NewFamilyInstance(...);",
                                        "transactionMode": "auto"}, "t2")]),
        Resp([_txt("Door placed via the API.")]),
    ]

    def fake_dispatch(name, args):
        if name == "place_element":
            return {"success": False, "message": "Successfully created 0 element(s)."}
        if name == "execute_revit_api":
            return {"success": True, "result": "555"}
        return {"success": True}

    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch, preflight=False)
    api = next(c for c in out["tool_calls"] if c["name"] == "execute_revit_api")
    assert api["result"]["success"] is True                       # ran, not nudged
    assert "ONLY for operations" not in str(api["result"].get("message", ""))


def test_required_steps_from_motif():
    motif = {"steps": [
        {"action": "Place", "family_type": "M_Single-Flush"},
        {"action": "SetParam", "param_name": "Mark", "param_value": "D-101"},
        {"action": "Tag"},
    ]}
    assert ex.required_steps_from_motif(motif) == [
        {"type": "place"},
        {"type": "set_parameter", "name": "Mark", "value": "D-101"},
        {"type": "tag"},
    ]


def test_next_in_sequence():
    assert ex.next_in_sequence("D-105") == "D-106"
    assert ex.next_in_sequence("1002") == "1003"
    assert ex.next_in_sequence("W-09") == "W-10"        # preserve zero-padding width
    assert ex.next_in_sequence("D-099") == "D-100"
    assert ex.next_in_sequence("Room 5") == "Room 6"
    assert ex.next_in_sequence("ABC") is None           # no number to increment
    assert ex.next_in_sequence(None) is None


def test_resolve_routine_values_constants_and_sequences():
    motif = {"steps": [
        {"action_type": "Place", "family_name": "M_Door"},
        {"action_type": "SetParam", "param_name": "Mark", "param_value": None, "param_value_type": "variable"},
        {"action_type": "SetParam", "param_name": "Width", "param_value": "900", "param_value_type": "constant"},
    ]}
    # variable Mark resolves from the value we set last time (memory); constant Width stays
    vals = ex.resolve_routine_values(motif, examples=[], last_values={"Mark": "D-105"})
    assert vals == {"Mark": "D-106", "Width": "900"}
    # no memory yet → take the highest Mark from the recorded examples and go next
    examples = [{"actions": [{"param_name": "Mark", "param_value_after": "D-101"}]},
                {"actions": [{"param_name": "Mark", "param_value_after": "D-103"}]}]
    vals2 = ex.resolve_routine_values(motif, examples=examples, last_values={})
    assert vals2["Mark"] == "D-104" and vals2["Width"] == "900"


def test_goal_and_required_use_resolved_values():
    motif = {"name": "Door", "steps": [
        {"action_type": "Place", "family_name": "M_Door"},
        {"action_type": "SetParam", "param_name": "Mark", "param_value": None, "param_value_type": "variable"},
    ]}
    pv = {"Mark": "D-106"}
    assert "Set parameter 'Mark' = 'D-106'" in ex.build_goal(motif, None, pv)
    assert {"type": "set_parameter", "name": "Mark", "value": "D-106"} in ex.required_steps_from_motif(motif, pv)


def test_required_steps_and_goal_read_real_motif_fields():
    """The Pattern Agent emits action_type / family_name / tag_family_name — not action / family_type."""
    motif = {"name": "Place Window + Marks", "steps": [
        {"action_type": "Place", "family_name": "M_Window-Fixed"},
        {"action_type": "Tag", "tag_family_name": "M_Window Tag"},
        {"action_type": "SetParam", "param_name": "Mark", "param_value": "1002"},
        {"action_type": "SetParam", "param_name": "Comments", "param_value": "dds"},
    ]}
    assert ex.required_steps_from_motif(motif) == [
        {"type": "place"}, {"type": "tag"},
        {"type": "set_parameter", "name": "Mark", "value": "1002"},
        {"type": "set_parameter", "name": "Comments", "value": "dds"},
    ]
    goal = ex.build_goal(motif)
    assert "Place the family 'M_Window-Fixed'" in goal      # not the old "1. ?"
    assert "M_Window Tag" in goal and "Mark" in goal and "1002" in goal


def test_placed_element_id_from_any_placement_tool():
    assert ex.placed_element_id(
        [{"name": "place_and_configure", "args": {}, "result": {"success": True, "response": [888]}}]) == 888
    assert ex.placed_element_id(
        [{"name": "place_element", "args": {}, "result": {"success": True, "element_id": 12}}]) == 12
    assert ex.placed_element_id([{"name": "inspect_model", "args": {}, "result": {"success": True}}]) is None


def test_place_and_configure_counts_as_place_plus_params():
    """The atomic place_and_configure places AND sets parameters — it satisfies both, so a routine
    done that way is complete (not falsely reported unfinished)."""
    script = [
        Resp([_tu("place_and_configure", {"placements": [{"familyName": "M_Window-Fixed"}]}, "t1")]),
        Resp([_tu("tag_element", {"element_id": 555}, "t2")]),
        Resp([_txt("Done.")]),
    ]

    def fake_dispatch(name, args):
        if name == "place_and_configure":
            return {"success": True, "response": [555]}
        return {"success": True}

    required = [{"type": "place"}, {"type": "set_parameter", "name": "Mark", "value": "1002"}, {"type": "tag"}]
    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch, required=required)
    assert out["done"] is True                              # place+params via the atomic tool, then tagged
    assert ex.placed_element_id(out["tool_calls"]) == 555   # id pulled from the generic 'response'


def test_completion_reprompt_when_model_stops_after_place():
    """Model places then declares done; enforcement re-prompts and the model finishes the routine."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("Placed the door.")]),                                   # stops early → nudged
        Resp([_tu("set_parameter", {"element_id": 999, "name": "Mark", "value": "D-101"}, "t2")]),
        Resp([_tu("tag_element", {"element_id": 999}, "t3")]),
        Resp([_txt("All done.")]),
    ]

    def fake_dispatch(name, args):
        if name == "place_element":
            return {"success": True, "element_id": 999}
        return {"success": True}

    required = [{"type": "place"}, {"type": "set_parameter", "name": "Mark", "value": "D-101"}, {"type": "tag"}]
    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch, required=required)
    assert out["done"] is True
    names = [c["name"] for c in out["tool_calls"]]
    assert names == ["place_element", "set_parameter", "tag_element"]


def test_completion_deterministic_finish_when_model_wont():
    """Model places then keeps stopping (e.g. Gemini Flash). After the nudge cap, the executor
    completes the known set_parameter + tag itself so the routine doesn't end half-done."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Window", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("Window placed.")]),     # nudge 1
        Resp([_txt("It is placed.")]),      # nudge 2
        Resp([_txt("Placed it.")]),         # cap reached → deterministic finish
    ]
    dispatched = []

    def fake_dispatch(name, args):
        dispatched.append((name, args))
        if name == "place_element":
            return {"success": True, "element_id": 777}
        return {"success": True}

    required = [{"type": "place"}, {"type": "set_parameter", "name": "Mark", "value": "W-1"}, {"type": "tag"}]
    out = ex.run_executor("goal", client=FakeClient(script), dispatch_fn=fake_dispatch, required=required)

    assert out["done"] is True                                  # routine completed despite the model
    names = [c["name"] for c in out["tool_calls"]]
    assert names == ["place_element", "set_parameter", "tag_element"]
    setp = next(c for c in out["tool_calls"] if c["name"] == "set_parameter")
    assert setp["args"] == {"element_id": 777, "name": "Mark", "value": "W-1"}   # known step, deterministic
    assert ("tag_element", {"element_id": 777}) in dispatched


def test_no_required_means_legacy_done():
    """Without a required spec, stopping after one tool is 'done' (unchanged behavior)."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("Placed.")]),
    ]
    out = ex.run_executor("goal", client=FakeClient(script),
                          dispatch_fn=lambda n, a: {"success": True, "element_id": 1})
    assert out["done"] is True and [c["name"] for c in out["tool_calls"]] == ["place_element"]


def test_preflight_facts_from_selection():
    """Pre-flight reads the live selection and turns it into a host-wall instruction; empty/erroring
    selections yield no facts (best-effort, never raises)."""
    facts = ex._preflight_facts(lambda n, a: {"success": True, "selected_ids": [777, 778]}, lambda *_: None)
    assert "777" in facts and "host_wall_id" in facts
    assert ex._preflight_facts(lambda n, a: {"success": True, "selected_ids": []}, lambda *_: None) == ""
    def _raise(n, a):
        raise RuntimeError("no Revit")
    assert ex._preflight_facts(_raise, lambda *_: None) == ""


def test_preflight_runs_before_loop_and_emits():
    """run_executor reads the selection ONCE up front and emits a pre-flight reasoning event."""
    script = [Resp([_txt("Nothing to do.")])]
    events = []
    ex.run_executor("goal", client=FakeClient(script),
                    dispatch_fn=lambda n, a: {"success": True, "selected_ids": [42]},
                    on_event=lambda k, p: events.append((k, p)))
    assert any(k == "reasoning" and "Pre-flight" in str(p) for k, p in events)


def test_preflight_can_be_disabled():
    """preflight=False makes no selection query (so a routine that doesn't need it isn't slowed)."""
    calls = []
    ex.run_executor("goal", client=FakeClient([Resp([_txt("done")])]),
                    dispatch_fn=lambda n, a: (calls.append(n) or {"success": True}), preflight=False)
    assert "get_selected_elements" not in calls


def test_choose_start_model_warm_simple_starts_cheap(monkeypatch):
    """A memory-WARM, simple routine on a paid ceiling starts on the cheap model and escalates up."""
    monkeypatch.setattr(ex, "EXECUTOR_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr(ex, "ADAPTIVE_START", True)
    monkeypatch.setattr(ex, "CHEAP_MODEL", "claude-haiku-4-5")
    simple = {"steps": [{"action_type": "Place", "family_name": "M_Door"},
                        {"action_type": "SetParam", "param_name": "Mark"}, {"action_type": "Tag"}]}
    start, esc = ex.choose_start_model(simple, {"executions": 3})
    assert start == "claude-haiku-4-5" and esc == "claude-sonnet-4-6"


def test_choose_start_model_cold_or_complex_starts_on_ceiling(monkeypatch):
    monkeypatch.setattr(ex, "EXECUTOR_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr(ex, "ADAPTIVE_START", True)
    monkeypatch.setattr(ex, "CHEAP_MODEL", "claude-haiku-4-5")
    simple = {"steps": [{"action_type": "Place", "family_name": "M_Door"}]}
    assert ex.choose_start_model(simple, {}) == ("claude-sonnet-4-6", None)              # cold (no executions)
    assert ex.choose_start_model(simple, None) == ("claude-sonnet-4-6", None)
    # a non-place/set/tag step makes it "not simple" → start on the ceiling even if warm
    complex_ = {"steps": [{"action_type": "DeleteElement"}]}
    assert ex.choose_start_model(complex_, {"executions": 9}) == ("claude-sonnet-4-6", None)


def test_choose_start_model_noop_on_gemini(monkeypatch):
    """When the ceiling is the free Gemini tier, never start on paid Haiku (that would cost MORE)."""
    monkeypatch.setattr(ex, "EXECUTOR_MODEL", "gemini-flash")
    monkeypatch.setattr(ex, "ADAPTIVE_START", True)
    simple = {"steps": [{"action_type": "Place", "family_name": "M_Door"}]}
    assert ex.choose_start_model(simple, {"executions": 3}) == ("gemini-flash", None)


def test_adaptive_escalation_steps_up_after_failures():
    """Starting cheap, after `escalate_after_failures` failures the run steps up to the ceiling."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 1, "y": 0}}, "t2")]),
        Resp([_txt("stuck")]),
    ]
    events = []
    out = ex.run_executor("goal", client=FakeClient(script),
                          dispatch_fn=lambda n, a: {"success": False, "message": "Successfully created 0 element(s)."},
                          model="haiku", escalate_to="sonnet", escalate_after_failures=2,
                          on_event=lambda k, p: events.append((k, p)), preflight=False)
    assert out["escalated"] is True
    assert out["model"] == "claude-sonnet-4-6"                       # ended on the ceiling
    assert any(k == "reasoning" and "Escalating" in str(p) for k, p in events)


def test_no_escalation_when_cheap_model_succeeds():
    """If the cheap model finishes without enough failures, it never escalates (stays cheap)."""
    script = [
        Resp([_tu("place_element", {"family_name": "M_Door", "location": {"x": 0, "y": 0}}, "t1")]),
        Resp([_txt("done")]),
    ]
    out = ex.run_executor("goal", client=FakeClient(script),
                          dispatch_fn=lambda n, a: {"success": True, "element_id": 1},
                          model="haiku", escalate_to="sonnet", escalate_after_failures=2, preflight=False)
    assert out["escalated"] is False and out["model"] == "claude-haiku-4-5"


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
