"""
Pattern watcher — recreates the retired revit_addin/PatternBridge on the Python
side for the generalBIMlog architecture.

The old flow (C#): RoutineDetector fired live → PatternBridge → notify the chatbot.
That lived in revit_addin, which is retired; generalBIMlog only LOGS now, so nothing
pushed detected routines to the BIM Assistant anymore. This daemon restores it:

  loop every --interval seconds:
    1. run the detector over the real generalBIMlog logs (list_candidate_routines)
    2. for each routine with support >= --min-support not already notified:
         - generate its motif (Pattern agent) + tool sequence (Macro agent)
         - notify_pattern() it to the chatbot → the BIM Assistant pane lights up
    3. remember it so it isn't re-announced

Usage:
    python pattern_watcher.py                 # watch forever (default 15s / support 3)
    python pattern_watcher.py --once          # one scan, then exit
    python pattern_watcher.py --once --dry-run # detect + generate, but don't notify
    python pattern_watcher.py --reset          # forget what was already announced

Needs ANTHROPIC_API_KEY (read from the project .env if not already in the env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_server.log_reader import list_candidate_routines, get_routine_examples
from orchestrator.pattern_agent import extract_motif
from orchestrator.macro_agent import generate_tool_sequence
from chatbot.trigger import notify_pattern

STATE_PATH = (Path.home() / "AppData" / "Local" / "RevitPersonalization"
              / "pattern_watcher_state.json")


def _load_api_key() -> None:
    """Pull ANTHROPIC_API_KEY from the project .env if it isn't already set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
                return


def _load_notified() -> set[str]:
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_notified(ids: set[str]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except Exception as exc:
        print(f"[watcher] could not save state: {exc}", file=sys.stderr)


def scan_once(min_support: int, dry_run: bool) -> int:
    """One detect → generate → notify pass. Returns the number newly announced."""
    notified = _load_notified()
    routines = [r for r in list_candidate_routines(include_synthetic=False)
                if r.support >= min_support]
    fresh = [r for r in routines if r.id not in notified]
    print(f"[watcher] {len(routines)} routine(s) >= support {min_support}; {len(fresh)} new")

    announced = 0
    for r in fresh:
        print(f"[watcher] new routine: {r.label}  (support {r.support})")
        try:
            full = get_routine_examples(r.id, k=5)
            examples = [ex.model_dump() for ex in full.examples]
            motif = extract_motif(examples, routine_label=r.label)
            # fetch_context=False: don't require the :8080 backend just to announce
            seq = generate_tool_sequence(motif, fetch_context=False)
        except Exception as exc:
            print(f"[watcher]   generate failed, skipping: {exc}", file=sys.stderr)
            continue

        if dry_run:
            print(f"[watcher]   DRY-RUN: would announce '{motif['name']}' "
                  f"({len(seq)} steps, vars={motif.get('parameters_to_prompt', [])})")
            announced += 1
            continue  # dry-run has no side effects — don't mark it announced

        try:
            notify_pattern(label=r.label, count=r.support, motif=motif,
                           tool_sequence=seq, examples=examples[:3], open_browser=False)
            print(f"[watcher]   -> announced to the assistant: {motif['name']}")
        except Exception as exc:
            print(f"[watcher]   notify failed, will retry next scan: {exc}", file=sys.stderr)
            continue

        notified.add(r.id)
        _save_notified(notified)
        announced += 1
    return announced


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect routines from generalBIMlog logs and push them to the BIM Assistant.")
    ap.add_argument("--interval", type=int, default=15, help="seconds between scans (default 15)")
    ap.add_argument("--min-support", type=int, default=3, help="min cluster size to announce (default 3)")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--dry-run", action="store_true", help="detect + generate but do not notify")
    ap.add_argument("--reset", action="store_true", help="forget previously-announced routines")
    args = ap.parse_args()

    _load_api_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (and not found in .env).", file=sys.stderr)
        return 1

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("[watcher] state reset — all routines eligible to re-announce.")

    if args.once:
        scan_once(args.min_support, args.dry_run)
        return 0

    print(f"[watcher] watching generalBIMlog logs every {args.interval}s "
          f"(min support {args.min_support}). Ctrl+C to stop.")
    while True:
        try:
            scan_once(args.min_support, args.dry_run)
        except Exception as exc:
            print(f"[watcher] scan error: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
