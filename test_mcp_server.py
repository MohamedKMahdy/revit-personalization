"""
Quick smoke-test for the MCP server tools and resources.
Run from project root: python test_mcp_server.py
"""
import sys, json, io
sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mcp_server.server import (
    resource_candidate_routines,
    resource_routine_examples,
    analyze_pattern,
    generate_command,
    list_shortcuts,
    query_model,
)

PASS = "[PASS]"
FAIL = "[FAIL]"

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  {status}  {label}" + (f"  -- {detail}" if detail else ""))
    return condition

all_ok = True

# ── Resource: logs://candidate_routines ──────────────────────────────────────
print("\n=== Resource: logs://candidate_routines ===")
result = resource_candidate_routines()
routines = json.loads(result)
all_ok &= check("Returns a list", isinstance(routines, list))
all_ok &= check("At least 1 routine detected", len(routines) > 0, f"found {len(routines)}")
for r in routines:
    print(f"      ID={r['id']}  label={r['label']}  count={r['count']}  sig={r['action_signature']}")

# ── Resource: logs://routine/{id}/examples ───────────────────────────────────
print("\n=== Resource: logs://routine/{id}/examples ===")
if routines:
    top_id = routines[0]["id"]
    result2 = resource_routine_examples(top_id)
    data = json.loads(result2)
    all_ok &= check("No error key", "error" not in data)
    n_ex = len(data.get("examples", []))
    all_ok &= check("Has examples", n_ex > 0, f"{n_ex} examples")
    not_found = resource_routine_examples("nonexistent_id_xyz")
    nf = json.loads(not_found)
    all_ok &= check("Missing routine returns error", "error" in nf)

# ── Tool: analyze_pattern ─────────────────────────────────────────────────────
print("\n=== Tool: analyze_pattern ===")
fake_seq = [{
    "actions": [
        {"action_type": "Place",    "family_name": "Door-Test"},
        {"action_type": "SetParam", "param_name": "Mark", "param_value_after": "D1"},
        {"action_type": "Tag",      "tag_family_name": "Door Tag"},
    ]
}]
ap = analyze_pattern(fake_seq, routine_id="test")
all_ok &= check("Returns dict",            isinstance(ap, dict))
all_ok &= check("sequence_count == 1",     ap.get("sequence_count") == 1)
all_ok &= check("action_signature = P,S,T", ap["sequences"][0]["action_signature"] == "P,S,T")
all_ok &= check("ready_for_pattern_agent", ap.get("ready_for_pattern_agent") is True)
all_ok &= check("Empty input returns error", "error" in analyze_pattern([]))

# ── Tool: generate_command ────────────────────────────────────────────────────
print("\n=== Tool: generate_command ===")
test_motif = {
    "name": "Test Door Shortcut",
    "description": "Place door, set Mark, tag",
    "steps": [
        {"action_type": "Place",    "family_name": "Door-Test",  "param_name": "",     "param_value": None,  "param_value_type": "",         "tag_family_name": ""},
        {"action_type": "SetParam", "family_name": "",           "param_name": "Mark", "param_value": None,  "param_value_type": "variable", "tag_family_name": ""},
        {"action_type": "Tag",      "family_name": "",           "param_name": "",     "param_value": None,  "param_value_type": "",         "tag_family_name": "Door Tag"},
    ],
    "preconditions": ["Active view must be a floor plan"],
    "parameters_to_prompt": ["Mark"],
}
gc = generate_command(motif=test_motif, name="Test Door Shortcut")
all_ok &= check("Returns dict",               isinstance(gc, dict))
all_ok &= check("Has shortcut_id",            "shortcut_id" in gc)
all_ok &= check("Has tool_sequence",          "tool_sequence" in gc)
all_ok &= check("3 steps in sequence",        len(gc.get("tool_sequence", [])) == 3)
all_ok &= check("First step is place_element",gc["tool_sequence"][0]["tool"] == "place_element")
all_ok &= check("Mark is placeholder",        gc["tool_sequence"][1]["arguments"]["value"] == "{{Mark}}")
all_ok &= check("Last step is tag",           gc["tool_sequence"][2]["tool"] == "create_annotation_tag")
all_ok &= check("Saved to disk",              "saved_to" in gc)
all_ok &= check("parameters_to_prompt has Mark", "Mark" in gc.get("parameters_to_prompt", []))
print(f"      shortcut_id={gc.get('shortcut_id')}  saved_to={gc.get('saved_to')}")

# ── Tool: list_shortcuts ──────────────────────────────────────────────────────
print("\n=== Tool: list_shortcuts ===")
shortcuts = list_shortcuts()
all_ok &= check("Returns a list",            isinstance(shortcuts, list))
all_ok &= check("Test shortcut is listed",   any(s.get("name") == "Test Door Shortcut" for s in shortcuts))
print(f"      {len(shortcuts)} shortcut(s) on disk")
for s in shortcuts:
    print(f"      id={s['shortcut_id']}  name={s['name']}  steps={s['steps']}")

# ── Tool: query_model (Revit not running — expect graceful error) ──────────────
print("\n=== Tool: query_model (Autodesk Public MCP Server — Revit not open) ===")
qr = query_model("get_active_view", {})
all_ok &= check("Returns dict (not exception)", isinstance(qr, dict))
all_ok &= check("Graceful error when unreachable", "error" in qr or "available" in qr)
print(f"      response: {qr}")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "="*55)
if all_ok:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED — see [FAIL] lines above")
print("="*55)
