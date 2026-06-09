# -*- coding: utf-8 -*-
"""
One-time setup: copies ANTHROPIC_API_KEY from .env into
%LOCALAPPDATA%\RevitPersonalization\.env so Revit can find it.

Run once, then restart Revit:
    python setup_revit_env.py
"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE   = Path(__file__).parent
DOTENV = HERE / ".env"
TARGET = Path(os.environ["LOCALAPPDATA"]) / "RevitPersonalization" / ".env"

# ── Read key from .env ────────────────────────────────────────────────────────
if not DOTENV.exists():
    print(f"ERROR: {DOTENV} not found.")
    sys.exit(1)

key = None
for line in DOTENV.read_text(encoding="utf-8").splitlines():
    if line.startswith("ANTHROPIC_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'")
        break

if not key:
    print("ERROR: ANTHROPIC_API_KEY not found in .env")
    sys.exit(1)

print(f"Key found: {key[:20]}…")

# ── Write to LOCALAPPDATA location ────────────────────────────────────────────
TARGET.parent.mkdir(parents=True, exist_ok=True)
TARGET.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
print(f"Written to: {TARGET}")

# ── Verify Revit can find it ──────────────────────────────────────────────────
print("\nSetup complete.")
print("Next steps:")
print("  1. Close Revit completely")
print("  2. Reopen Revit 2027 and open any project")
print("  3. The BIM Assistant panel will appear docked on the right")
print("  4. Click '⚡ Test' to try the chat immediately")
print()
print("The panel will also auto-activate when RevitLogger detects a pattern.")
