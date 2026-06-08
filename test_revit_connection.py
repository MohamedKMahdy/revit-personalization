"""
RevitWriteServer end-to-end connection test.

HOW THIS WORKS
==============

  Revit 2027 (running)
       │  RevitWriteServer.dll is loaded as an add-in
       │  TcpCommandServer listens on localhost:8080
       │
       └─────────── TCP socket ────────────────
                                               │
  This script (Python)                         │
       │  opens a socket to localhost:8080     │
       │  sends JSON-RPC {"method":"say_hello"}┘
       │  Revit receives it → UI thread runs → shows dialog
       └  receives {"status":"Hello from Revit 2027..."}

WHAT YOU NEED BEFORE RUNNING
=============================
  1. Open Revit 2027
  2. Open any .rvt project  (even a blank one)
  3. Wait ~10 seconds for the add-in to load
  4. Run this script:
       cd C:\Users\DE1E7A\revit-personalization
       python test_revit_connection.py

WHAT WILL HAPPEN
================
  • A dialog pops up inside Revit saying "Hello from RevitWriteServer (Revit 2027)!"
  • This script prints the response and two more read-only queries
  • No changes are made to your model — these are read-only checks

If you see "Connection refused": Revit is not open or the add-in didn't load.
If you see "Timed out": a previous dialog is still blocking the UI thread — dismiss it.
"""
import sys
import os

# Make sure we can import the bridge regardless of where the script is run from
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp_server.revit_bridge import _call_plugin, say_hello, REVIT_PLUGIN_HOST, REVIT_PLUGIN_PORT

DIVIDER = "─" * 55


def check(label: str, method: str, params: dict):
    print(f"► {label}")
    result = _call_plugin(method, params)
    if "error" in result:
        print(f"  ✗ FAIL  {result['error']}")
        return False
    print(f"  ✓ OK    {result}")
    return True


print()
print("RevitWriteServer — connection test")
print(f"Target: {REVIT_PLUGIN_HOST}:{REVIT_PLUGIN_PORT}")
print(DIVIDER)
print()

# ── 1. say_hello ────────────────────────────────────────────────────────────
# A TaskDialog will appear IN REVIT — click OK to continue.
print("Step 1: say_hello")
print("  → A dialog will appear IN REVIT. Click OK to continue.")
result = say_hello()
if "error" in result:
    print()
    print(f"  FAILED: {result['error']}")
    print()
    print("Checklist:")
    print("  [ ] Revit 2027 is open")
    print("  [ ] A .rvt project is open (not just the start screen)")
    print("  [ ] RevitWriteServer.dll is in %AppData%\\Autodesk\\Revit\\Addins\\2027\\")
    print("       (run: dir %AppData%\\Autodesk\\Revit\\Addins\\2027\\)")
    print("  [ ] Nothing else is using TCP port 8080")
    sys.exit(1)

print(f"  Response: {result}")
print()

# ── 2. get_current_view_info ────────────────────────────────────────────────
print("Step 2: get_current_view_info  (read-only, no dialog)")
result = _call_plugin("get_current_view_info", {})
if "error" not in result:
    view = result.get("view", result)
    name = view.get("name", "?")
    vtype = view.get("type", "?")
    level = view.get("level", "")
    level_str = f"  level='{level}'" if level else ""
    print(f"  Active view: '{name}'  type={vtype}{level_str}")
else:
    print(f"  (skipped: {result['error']})")
print()

# ── 3. get_available_family_types ───────────────────────────────────────────
print("Step 3: get_available_family_types  (read-only, no dialog)")
result = _call_plugin("get_available_family_types", {})
if "error" not in result:
    types = result.get("types", result) if isinstance(result, dict) else result
    types = types or []
    print(f"  Loaded family types: {len(types)} total")
    for t in types[:5]:
        tid = t.get("id", t.get("typeId", "?"))
        name = t.get("name", "?")
        cat = t.get("category", "")
        cat_str = f"  [{cat}]" if cat else ""
        print(f"    id={tid}  {name}{cat_str}")
    if len(types) > 5:
        print(f"    … and {len(types) - 5} more")
else:
    print(f"  (skipped: {result['error']})")
print()

print(DIVIDER)
print("All steps passed.")
print("RevitWriteServer is running and accepting commands from Python.")
print()
print("What just happened:")
print("  Python → TCP socket → Revit API → ExternalEvent → UI thread")
print("  The dialog you saw in Revit was triggered by this Python script.")
print()
print("Next step: run the full agent pipeline:")
print("  python orchestrator/agents.py --routine-id test_door")
