"""
Quick diagnostic: shows what the RevitLogger add-in has written to disk.
Run from the project root:  python check_logs.py
"""
import json, os, sys
from pathlib import Path

# Force UTF-8 output so Unicode box-drawing chars print on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = Path.home() / "AppData" / "Local" / "RevitPersonalization" / "logs"

if not LOG_DIR.exists():
    print(f"Log folder not yet created: {LOG_DIR}")
    print("Make sure Revit 2027 is running with the add-in loaded.")
else:
    files = sorted(LOG_DIR.glob("*.jsonl"))
    print(f"Log folder: {LOG_DIR}")
    print(f"Session files found: {len(files)}\n")

    for f in files[-3:]:           # show last 3 sessions
        lines = f.read_text(encoding="utf-8-sig").strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        actions = [r for r in records if "action_type" in r]

        print(f"── {f.name} ({len(actions)} action records) ──")
        for r in actions[:15]:     # first 15 actions
            tx = r.get("transaction_name", "")
            at = r.get("action_type", "")
            el = r.get("element_category", "")
            fn = r.get("family_name", "")
            pn = r.get("param_name", "")
            pv = r.get("param_value_after", "")
            if at == "Place":
                print(f"  PLACE    {el} | {fn}")
            elif at == "SetParam":
                print(f"  SETPARAM {pn} = {pv!r}  [{el}]")
            elif at == "Tag":
                tagged = r.get("tagged_element_id", "")
                print(f"  TAG      element {tagged}")
        if len(actions) > 15:
            print(f"  ... and {len(actions)-15} more")
        print()
