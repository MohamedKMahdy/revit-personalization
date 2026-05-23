"""
JSONL log reader and routine detector for the RevitLogger C# add-in output.

The C# add-in writes one JSON object per line to
  %LOCALAPPDATA%\\RevitPersonalization\\logs\\session_<date>_<docHash>.jsonl

Each file has three record types:
  • record_type == "session_start"  — session metadata (SessionInfo)
  • (no record_type field)          — ActionRecord (Place / SetParam / Tag)
  • record_type == "session_end"    — closing marker

Routine detection follows the episode-grouping approach implicit in:
  Jang & Lee (2023) arXiv:2305.18032 — enhanced BIM logging for reproducibility
  Jang et al. (2023) AEI 57, 102079 — lexicon-based BIM log analysis

Algorithm:
  1. Parse all ActionRecords from all .jsonl files.
  2. Group records by element_id — each element accumulates a sequence of
     [Place → SetParam* → Tag?] actions, forming one "routine episode."
  3. Compute a structural signature per episode:
       (element_category, family_name, tuple of (action_type, param_name))
     Two episodes with the same signature are instances of the same routine.
  4. Groups with ≥ 2 episodes become CandidateRoutine objects for the agents.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from shared.schemas import ActionRecord, CandidateRoutine, RoutineExample

# Primary log directory written by the C# add-in
LOG_DIR = Path(os.environ.get(
    "REVIT_PERSONALIZATION_LOG_DIR",
    Path.home() / "AppData" / "Local" / "RevitPersonalization" / "logs",
))

# Synthetic .json files (old format) kept for offline testing without Revit
SYNTHETIC_DIR = Path(__file__).parent.parent / "tests" / "synthetic_logs"


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def _iter_records(path: Path) -> Iterator[dict]:
    """Yield raw dicts from a .jsonl file, skipping session_start/end markers."""
    with open(path, encoding="utf-8") as f:
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
            yield obj


def _load_action_records(path: Path) -> list[ActionRecord]:
    records = []
    for obj in _iter_records(path):
        try:
            records.append(ActionRecord(**obj))
        except Exception:
            pass
    return records


# ── Old-format .json reader (synthetic test data) ─────────────────────────────

def _load_synthetic_routine(path: Path) -> CandidateRoutine | None:
    """
    Read a synthetic test file written in the old CandidateRoutine JSON format.
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
    where action_sig is e.g. "Place,SetParam(Fire Rating),SetParam(Mark),Tag"

    Two episodes with the same signature are treated as instances of the same
    routine — consistent with the lexicon-based grouping in Jang et al. (2023).
    """
    parts = []
    for a in actions:
        if a.action == "Place":
            parts.append("Place")
        elif a.action == "SetParam":
            parts.append(f"SetParam({a.paramName or ''})")
        elif a.action == "Tag":
            parts.append("Tag")
    category   = actions[0].elementCategory if actions else ""
    family     = actions[0].familyType.split(":")[0] if actions[0].familyType else ""
    return f"{category}|{family}|{','.join(parts)}"


def _short_signature(actions: list[ActionRecord]) -> str:
    """Compact signature for the CandidateRoutine.action_signature field: P,S,S,T"""
    return ",".join(
        a.action[0] if a.action else "?" for a in actions
    )


def _build_label(actions: list[ActionRecord]) -> str:
    """Human-readable label: 'Place(M_Single-Flush) -> SetParamx4 -> Tag'"""
    parts = []
    set_count = 0
    for a in actions:
        if a.action == "Place":
            fname = a.familyType.split(":")[0] if a.familyType else a.elementCategory
            parts.append(f"Place({fname})")
        elif a.action == "SetParam":
            set_count += 1
        elif a.action == "Tag":
            if set_count:
                parts.append(f"SetParam×{set_count}")
                set_count = 0
            parts.append(f"Tag({a.tagFamily or ''})")
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
    tagged_element_id == E for Tag records), sorted by timestamp.

    Only episodes that start with a Place action are considered (we must have
    witnessed the element being placed to form a valid routine).
    """
    # Map element_id → list of ActionRecords involving that element
    element_actions: dict[int, list[ActionRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x.timestamp):
        if r.action in ("Place", "SetParam"):
            element_actions[r.elementId].append(r)
        elif r.action == "Tag" and r.taggedElementId is not None:
            element_actions[r.taggedElementId].append(r)

    # Build episodes (only elements we saw placed)
    episodes: list[tuple[int, list[ActionRecord]]] = []
    for eid, actions in element_actions.items():
        if not any(a.action == "Place" for a in actions):
            continue
        episodes.append((eid, actions))

    # Group by structural signature
    sig_to_episodes: dict[str, list[tuple[int, list[ActionRecord]]]] = defaultdict(list)
    for eid, actions in episodes:
        sig = _episode_signature(actions)
        sig_to_episodes[sig].append((eid, actions))

    # Convert groups with enough repeats into CandidateRoutine objects
    routines: list[CandidateRoutine] = []
    for sig, group in sig_to_episodes.items():
        if len(group) < min_repeats:
            continue

        examples = [
            RoutineExample(
                example_id=f"ex_{i+1:03d}",
                session_id=session_id,
                recorded_at=actions[0].timestamp,
                actions=actions,
            )
            for i, (_, actions) in enumerate(group)
        ]

        first_actions = group[0][1]
        # Confidence: fraction of episodes that match the most common param values
        confidence = min(1.0, len(group) / 5)

        routine_id = "routine_" + sig.replace("|", "_").replace(",", "_").replace(" ", "")[:40]
        routines.append(CandidateRoutine(
            id=routine_id,
            label=_build_label(first_actions),
            action_signature=_short_signature(first_actions),
            count=len(group),
            confidence=round(confidence, 2),
            examples=examples,
        ))

    return routines


# ── Public API ────────────────────────────────────────────────────────────────

def list_candidate_routines(
    include_synthetic: bool = True,
    min_repeats: int = 2,
) -> list[CandidateRoutine]:
    """
    Return all detected candidate routines from both real and synthetic logs.

    Real logs (.jsonl from C# add-in) are processed with the episode-grouping
    algorithm. Synthetic test files (.json) are loaded directly as CandidateRoutine
    objects and merged in (deduplicated by id).
    """
    routines: dict[str, CandidateRoutine] = {}

    # ── Real JSONL logs ──
    if LOG_DIR.exists():
        for jsonl_file in sorted(LOG_DIR.glob("session_*.jsonl")):
            try:
                records = _load_action_records(jsonl_file)
                session_id = jsonl_file.stem.replace("session_", "")
                for r in _detect_routines_from_records(records, session_id, min_repeats):
                    if r.id not in routines or r.count > routines[r.id].count:
                        routines[r.id] = r
            except Exception:
                pass

    # ── Synthetic test data (.json, old format) ──
    if include_synthetic and SYNTHETIC_DIR.exists():
        for json_file in SYNTHETIC_DIR.glob("*.json"):
            try:
                r = _load_synthetic_routine(json_file)
                if r and (r.id not in routines or r.count > routines[r.id].count):
                    routines[r.id] = r
            except Exception:
                pass

    return list(routines.values())


def get_routine_examples(routine_id: str, k: int = 5) -> CandidateRoutine | None:
    """Return a CandidateRoutine trimmed to at most k examples, or None if not found."""
    for r in list_candidate_routines():
        if r.id == routine_id:
            return r.model_copy(update={"examples": r.examples[:k]})
    return None
