"""
JSONL log reader and routine detector for the RevitLogger C# add-in output.

The C# add-in writes one JSON object per line to
  %LOCALAPPDATA%\\RevitPersonalization\\logs\\session_<date>_<docHash>.jsonl

Each file has three record types:
  • record_type == "session_start"  — session metadata (SessionInfo)
  • (no record_type field)          — ActionRecord (Place / SetParam / Tag)
  • record_type == "session_end"    — closing marker

Routine detection follows the episode-grouping approach from:
  Jang & Lee (2023) arXiv:2305.18032 — enhanced BIM logging for reproducibility
  Jang et al. (2023) AEI 57, 102079 — lexicon-based BIM log analysis

Algorithm:
  1. Parse all ActionRecords from all .jsonl files.
  2. Group records by element_id — each element accumulates a sequence of
     [Place → SetParam* → Tag?] actions, forming one "routine episode."
  3. Compute a structural signature per episode:
       (element_category, family_name, tuple of action_type/param_name)
     Two episodes with the same signature are instances of the same routine.
  4. Groups with ≥ 2 episodes become CandidateRoutine objects for the agents.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample
from detector import Detector, DetectorConfig, make_detector

# Primary log directory written by the C# add-in
LOG_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_LOG_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "logs",
))

# Synthetic .json files kept for offline testing without Revit
SYNTHETIC_DIR = Path(__file__).parent.parent / "tests" / "synthetic_logs"


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def _iter_records(path: Path) -> Iterator[dict]:
    """Yield raw dicts from a .jsonl file, skipping session_start/end markers."""
    # utf-8-sig handles files written with a UTF-8 BOM (older LogWriter versions)
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("record_type") in ("session_start", "session_end"):
                continue
            # Skip internal error lines written by LogWriter's catch block
            if "_error" in obj:
                continue
            yield obj


def _load_action_records(path: Path) -> list[ActionRecord]:
    """Parse a .jsonl session file into a list of ActionRecord objects."""
    records: list[ActionRecord] = []
    for obj in _iter_records(path):
        try:
            records.append(ActionRecord(**obj))
        except Exception as e:
            # Log bad records to stderr for debugging but keep processing
            import sys
            print(f"  [log_reader] skipping bad record in {path.name}: {e}", file=sys.stderr)
    return records


# ── Old-format .json reader (synthetic test data) ─────────────────────────────

def _load_synthetic_routine(path: Path) -> CandidateRoutine | None:
    """
    Read a synthetic test file written in the CandidateRoutine JSON format.
    These are only used for testing without a live Revit installation.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        examples = [
            RoutineExample(
                example_id=ex["example_id"],
                session_id=ex["session_id"],
                recorded_at=ex["recorded_at"],
                actions=[ActionRecord(**a) for a in ex["actions"]],
            )
            for ex in data.get("examples", [])
        ]
        return CandidateRoutine(
            id=data["id"],
            label=data["label"],
            action_signature=data["action_signature"],
            count=data["count"],
            support=data.get("support", data["count"]),
            confidence=data.get("confidence", 0.0),
            examples=examples,
        )
    except Exception:
        return None


# ── Episode grouping ──────────────────────────────────────────────────────────

def _episode_signature(actions: list[ActionRecord]) -> str:
    """
    Structural signature for a routine episode.

    Format: "<category>|<family>|<action_sig>"
    where action_sig is e.g. "Place,SetParam(Mark),SetParam(Fire Rating),Tag"

    Two episodes with the same signature are treated as instances of the same
    routine — consistent with Jang et al. (2023) lexicon-based grouping.
    """
    parts = []
    for a in actions:
        if a.action_type == "Place":
            parts.append("Place")
        elif a.action_type == "SetParam":
            parts.append(f"SetParam({a.param_name or ''})")
        elif a.action_type == "Tag":
            parts.append("Tag")

    category = actions[0].element_category if actions else ""
    family   = actions[0].family_name.split(":")[0] if actions else ""
    return f"{category}|{family}|{','.join(parts)}"


def _short_signature(actions: list[ActionRecord]) -> str:
    """Compact signature for CandidateRoutine.action_signature: e.g. 'P,S,S,T'"""
    return ",".join(
        (a.action_type[0] if a.action_type else "?") for a in actions
    )


def _build_label(actions: list[ActionRecord]) -> str:
    """Human-readable label: 'Place(Door-Passage-Single) → SetParam×1 → Tag'"""
    parts: list[str] = []
    set_count = 0
    for a in actions:
        if a.action_type == "Place":
            fname = a.family_name.split(":")[0] if a.family_name else a.element_category
            parts.append(f"Place({fname})")
        elif a.action_type == "SetParam":
            set_count += 1
        elif a.action_type == "Tag":
            if set_count:
                parts.append(f"SetParam×{set_count}")
                set_count = 0
            parts.append(f"Tag({a.tag_family_name or ''})")
    if set_count:
        parts.append(f"SetParam×{set_count}")
    return " → ".join(parts)


def _detect_routines_from_records(
    records: list[ActionRecord],
    session_id: str,
    min_repeats: int = 2,
) -> list[CandidateRoutine]:
    """
    Group action records into per-element episodes, then find repeated signatures.

    An episode for element E is: all records with element_id == E (or
    tagged_element_id == E for Tag records), sorted by timestamp_unix.

    Only episodes that start with a Place action are considered.
    """
    # Map element_id → list of ActionRecords involving that element
    element_actions: dict[int, list[ActionRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x.timestamp_unix):
        if r.action_type in ("Place", "SetParam"):
            element_actions[r.element_id].append(r)
        elif r.action_type == "Tag" and r.tagged_element_id is not None:
            # Attach tag to the element it labels
            element_actions[r.tagged_element_id].append(r)

    # Build episodes (only elements we witnessed being placed)
    episodes: list[tuple[int, list[ActionRecord]]] = [
        (eid, acts)
        for eid, acts in element_actions.items()
        if any(a.action_type == "Place" for a in acts)
    ]

    # Group by structural signature
    sig_to_episodes: dict[str, list[tuple[int, list[ActionRecord]]]] = defaultdict(list)
    for eid, actions in episodes:
        sig = _episode_signature(actions)
        sig_to_episodes[sig].append((eid, actions))

    # Convert groups with enough repeats into CandidateRoutine objects
    routines: list[CandidateRoutine] = []
    for _sig, group in sig_to_episodes.items():
        if len(group) < min_repeats:
            continue

        examples = [
            RoutineExample(
                example_id=f"ex_{i+1:03d}",
                session_id=session_id,
                recorded_at=actions[0].timestamp_unix,
                actions=actions,
            )
            for i, (_, actions) in enumerate(group)
        ]

        first_actions = group[0][1]
        confidence    = round(min(1.0, len(group) / 5), 2)
        routine_id    = (
            "routine_"
            + _sig.replace("|", "_").replace(",", "_").replace("(", "").replace(")", "").replace(" ", "")[:40]
        )

        routines.append(CandidateRoutine(
            id=routine_id,
            label=_build_label(first_actions),
            action_signature=_short_signature(first_actions),
            count=len(group),
            confidence=confidence,
            examples=examples,
        ))

    return routines


# ── Detector selection ────────────────────────────────────────────────────────

def _resolve_detector(
    detector: Detector | str | None,
    config: DetectorConfig | None,
    min_repeats: int | None,
) -> Detector:
    """
    Resolve the detector to use. v0.2 ('v2') is the default; v0.1 ('v1') is
    reachable explicitly via the `detector` argument or the REVIT_DETECTOR_VERSION
    environment variable — never a buried constant.
    """
    if detector is not None and hasattr(detector, "detect"):
        return detector  # already an instance
    name = detector if isinstance(detector, str) else os.environ.get("REVIT_DETECTOR_VERSION")
    cfg = config or DetectorConfig()
    if min_repeats is not None:
        cfg = replace(cfg, min_cluster_size=min_repeats)
    return make_detector(name, cfg)


# ── Public API ────────────────────────────────────────────────────────────────

def list_candidate_routines(
    include_synthetic: bool = True,
    min_repeats: int | None = None,
    *,
    detector: Detector | str | None = None,
    config: DetectorConfig | None = None,
) -> list[CandidateRoutine]:
    """
    Return all detected candidate routines from both real and synthetic logs.

    Real logs (.jsonl from the C# add-in) are run through the selected detector.
    The default is v0.2 (similarity clustering); pass detector="v1" (or set
    REVIT_DETECTOR_VERSION=v1) to use the v0.1 substring baseline for comparison.

    Synthetic .json test files are pre-grouped CandidateRoutines and are loaded
    directly (merged in, deduplicated by id).

    `min_repeats`, when given, overrides the detector's min_cluster_size.
    Results are sorted by support (cluster size / frequency) descending.
    """
    det = _resolve_detector(detector, config, min_repeats)
    routines: dict[str, CandidateRoutine] = {}

    # ── Real JSONL logs: gather all records, detect once ──
    if LOG_DIR.exists():
        all_records: list[ActionRecord] = []
        for jsonl_file in sorted(LOG_DIR.glob("session_*.jsonl")):
            try:
                all_records.extend(_load_action_records(jsonl_file))
            except Exception as e:
                import sys
                print(f"  [log_reader] error reading {jsonl_file.name}: {e}", file=sys.stderr)
        if all_records:
            try:
                for r in det.detect(all_records, session_id="logs"):
                    if r.id not in routines or r.support > routines[r.id].support:
                        routines[r.id] = r
            except Exception as e:
                import sys
                print(f"  [log_reader] detector error: {e}", file=sys.stderr)

    # ── Synthetic test data (.json) ──
    if include_synthetic and SYNTHETIC_DIR.exists():
        for json_file in SYNTHETIC_DIR.glob("*.json"):
            try:
                r = _load_synthetic_routine(json_file)
                if r and (r.id not in routines or r.support > routines[r.id].support):
                    routines[r.id] = r
            except Exception:
                pass

    return sorted(routines.values(), key=lambda r: r.support, reverse=True)


def get_routine_examples(routine_id: str, k: int = 5) -> CandidateRoutine | None:
    """Return a CandidateRoutine trimmed to at most k examples, or None if not found."""
    for r in list_candidate_routines():
        if r.id == routine_id:
            return r.model_copy(update={"examples": r.examples[:k]})
    return None
