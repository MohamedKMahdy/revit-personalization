# -*- coding: utf-8 -*-
"""
One-time setup: writes the config the Revit add-in needs into
%LOCALAPPDATA%\RevitPersonalization\.env —

    ANTHROPIC_API_KEY   copied from the repo .env (used by the chatbot)
    REPO_ROOT           this repo's path  (so PatternBridge can find chatbot/)
    PYTHON_EXE          this interpreter  (so PatternBridge can launch Python)

Run once with the SAME Python you use to run the chatbot, then restart Revit:
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
TARGET.write_text(
    f"ANTHROPIC_API_KEY={key}\n"
    f"REPO_ROOT={HERE}\n"
    f"PYTHON_EXE={sys.executable}\n",
    encoding="utf-8",
)
print(f"Written to: {TARGET}")
print(f"  REPO_ROOT  = {HERE}")
print(f"  PYTHON_EXE = {sys.executable}")

# ── Next steps ────────────────────────────────────────────────────────────────
print("\nSetup complete.")
print("Next steps:")
print("  1. Close Revit completely")
print("  2. Reopen Revit and open any project (the logger add-in loads automatically)")
print("  3. Model as usual. When the add-in detects a repeated routine, the BIM")
print("     Assistant opens in your browser at http://localhost:5000")
print()
print("Tip: start the assistant manually any time with")
print("     python chatbot/chat_server.py")
