"""
CLI shim so the C# add-in (revit_addin/PatternBridge.cs) can hand a detected
pattern to the chatbot without hosting Python in-process.

Usage:
    python chatbot/notify_from_file.py <path-to-pattern.json>

The JSON file must contain {label, count, motif, tool_sequence, examples?}.
Delegates to chatbot.trigger.notify_pattern, which starts the chat server if it
isn't already running, POSTs the pattern, and opens the browser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from chatbot.trigger import notify_pattern


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: notify_from_file.py <pattern.json>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"pattern file not found: {path}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    notify_pattern(
        label=data.get("label", "Repeated Workflow"),
        count=int(data.get("count", 0)),
        motif=data.get("motif", {}),
        tool_sequence=data.get("tool_sequence", []),
        examples=data.get("examples", []),
        open_browser=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
